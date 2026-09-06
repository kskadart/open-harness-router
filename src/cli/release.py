"""Release helpers: the unreleased pull requests, changelog sections and version bumps.

Every subcommand runs from the repository root::

    PYTHONPATH=src .venv/bin/python -m cli.release version [--bump {major,minor,micro}]
    PYTHONPATH=src .venv/bin/python -m cli.release collect [--repo OWNER/NAME] [--changelog PATH]
    PYTHONPATH=src .venv/bin/python -m cli.release changelog --version X.Y.Z [--date YYYY-MM-DD] \\
        [--input FILE] [--write] [--changelog PATH]
    PYTHONPATH=src .venv/bin/python -m cli.release bump --to X.Y.Z
    PYTHONPATH=src .venv/bin/python -m cli.release notes --version X.Y.Z [--changelog PATH]

``collect`` asks GitHub for the pull requests merged into ``main`` (``gh pr
list --limit 500``, far above this repository's size) and subtracts both the
numbers CHANGELOG.md references as ``#N`` in a released section and the
``chore(release):`` pull requests: "unreleased" is that set difference, not a
date, because a rebase merge leaves no pull request number in the commit
subject. Its JSON is what ``changelog --input`` renders, and the
``category``/``entry`` fields are used verbatim, so edits made to the JSON
after ``collect`` reach the file.

The top section is a DRAFT while its version carries no tag (``draft_version``
in the JSON): its pull requests are collected again, so ``changelog --write``,
which replaces an untagged top section, regenerates it in full. Every section
below the top one counts as released, tagged or not.

The ``/release`` skill drives these subcommands; see
``.claude/skills/release/SKILL.md``.

Exit codes:

* 0 -- ok;
* 2 -- usage, an invalid version string, or a target lower than the current
  version;
* 3 -- nothing to do: no unreleased pull requests, a section that is frozen
  by its tag or is not the top one, or no section for that version;
* 4 -- a ``gh`` or ``git`` command failed; its message goes to stderr.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOTHING_TO_DO = 3
EXIT_SUBPROCESS = 4

PROJECT_NAME = "open-harness-router"
PYPROJECT_PATH = Path("pyproject.toml")
LOCK_PATH = Path("uv.lock")
DEFAULT_CHANGELOG_PATH = Path("CHANGELOG.md")
BASE_BRANCH = "main"
# One page, far above this repository's pull request count: no pagination.
PR_LIMIT = 500
SUBPROCESS_TIMEOUT_S = 60.0

CATEGORY_BREAKING = "Breaking changes"
CATEGORY_ADDED = "Added"
CATEGORY_CHANGED = "Changed"
CATEGORY_FIXED = "Fixed"
CATEGORY_DOCUMENTATION = "Documentation"
# Rendering order of the groups inside one section; empty groups are omitted.
SECTION_ORDER = (
    CATEGORY_BREAKING,
    CATEGORY_ADDED,
    CATEGORY_CHANGED,
    CATEGORY_FIXED,
    CATEGORY_DOCUMENTATION,
)
# Conventional-commit types with a group of their own; every other type and
# every title without a prefix lands in Changed.
TYPE_CATEGORIES = {
    "feat": CATEGORY_ADDED,
    "fix": CATEGORY_FIXED,
    "docs": CATEGORY_DOCUMENTATION,
}
BUMP_PARTS = ("major", "minor", "micro")
RELEASE_TITLE_PREFIX = "chore(release):"
BREAKING_TITLE_MARKER = "BREAKING"

CHANGELOG_HEADER = """# Changelog

All notable changes to this project are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are `major.minor.micro`
and every release is tagged `vX.Y.Z`.
"""

_EXIT_CODE_EPILOG = """exit codes:
  0  ok
  2  usage, an invalid version string, or a target lower than the current version
  3  nothing to do: no unreleased pull requests, a section frozen by its tag or
     below the top one, or no section for that version
  4  a 'gh' or 'git' command failed; its message goes to stderr
"""

_VERSION_RE = re.compile(r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<micro>\d+)$")
_CONVENTIONAL_PREFIX_RE = re.compile(r"^(?P<type>[A-Za-z]+)(?:\([^)]*\))?(?P<breaking>!)?:[ \t]*")
_PR_REFERENCE_RE = re.compile(r"#(\d+)")
_SECTION_HEADING_RE = re.compile(r"^## \[", re.MULTILINE)
_TOP_SECTION_RE = re.compile(r"^## \[(?P<version>[^\]]+)\]", re.MULTILINE)
_PYPROJECT_VERSION_RE = re.compile(r'^version\s*=\s*"[^"]*"')


class CommandError(RuntimeError):
    """A ``gh`` or ``git`` command failed; the message carries what it printed."""


class ChangelogConflictError(Exception):
    """The section cannot be written: its tag exists, or it is not the top section."""


@dataclass(frozen=True, slots=True)
class MergedPullRequest:
    """One pull request merged into the base branch, as ``gh pr list`` reports it.

    Attributes:
        number: the pull request number.
        title: the pull request title.
        url: the pull request URL, used in the changelog link.
        merged_at: merge timestamp (ISO 8601, as ``gh`` prints ``mergedAt``).
    """

    number: int
    title: str
    url: str
    merged_at: str


@dataclass(frozen=True, slots=True)
class ReleaseEntry:
    """A merged pull request classified for the changelog.

    The field order is the key order of the ``pull_requests`` rows that
    ``collect`` prints.

    Attributes:
        number: the pull request number; also the order inside a group.
        title: the original pull request title.
        url: the pull request URL.
        merged_at: merge timestamp, used by the skill's staleness check.
        category: one of :data:`SECTION_ORDER`.
        entry: the bullet text, link included; rendered verbatim.
    """

    number: int
    title: str
    url: str
    merged_at: str
    category: str
    entry: str


def parse_version(text: str) -> tuple[int, int, int]:
    """Parse ``major.minor.micro`` into its three integers.

    Args:
        text: the version string; exactly three dot-separated non-negative
            integers, no pre-release and no local segment.

    Returns:
        The (major, minor, micro) triple; tuples compare as versions do.

    Raises:
        ValueError: the string is not ``major.minor.micro``.
    """
    match = _VERSION_RE.match(text)
    if match is None:
        raise ValueError(f"invalid version '{text}': write it as major.minor.micro, e.g. 1.2.3")
    return int(match.group("major")), int(match.group("minor")), int(match.group("micro"))


def bump_version(current: str, part: str) -> str:
    """Return the version that bumping ``part`` produces, without writing anything.

    Args:
        current: the current version.
        part: ``major``, ``minor`` or ``micro``.

    Returns:
        The next version string.

    Raises:
        ValueError: ``current`` is not a version, or ``part`` is not a bump.
    """
    major, minor, micro = parse_version(current)
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "micro":
        return f"{major}.{minor}.{micro + 1}"
    raise ValueError(f"unknown bump '{part}': use one of {', '.join(BUMP_PARTS)}")


def categorize_title(title: str) -> tuple[str, str]:
    """Classify a pull request title and strip its conventional-commit prefix.

    ``feat`` maps to Added, ``fix`` to Fixed, ``docs`` to Documentation; every
    other type, and a title with no ``type(scope):`` prefix at all, maps to
    Changed. A ``!`` after the type, or the word ``BREAKING`` anywhere in the
    title, wins over the type. The prefix is stripped whenever the title has
    the conventional shape, known type or not; the rest keeps its casing.

    Args:
        title: the pull request title.

    Returns:
        A pair of (category, title without the ``type(scope):`` prefix).
    """
    match = _CONVENTIONAL_PREFIX_RE.match(title)
    if match is None:
        category = CATEGORY_BREAKING if BREAKING_TITLE_MARKER in title else CATEGORY_CHANGED
        return category, title
    text = title[match.end() :]
    if match.group("breaking") is not None or BREAKING_TITLE_MARKER in title:
        return CATEGORY_BREAKING, text
    return TYPE_CATEGORIES.get(str(match.group("type")).lower(), CATEGORY_CHANGED), text


def suggest_bump(entries: Sequence[ReleaseEntry]) -> str:
    """Suggest the bump for these entries: breaking -> major, a feature -> minor, else micro."""
    categories = {entry.category for entry in entries}
    if CATEGORY_BREAKING in categories:
        return "major"
    if CATEGORY_ADDED in categories:
        return "minor"
    return "micro"


def referenced_pr_numbers(changelog_text: str) -> frozenset[int]:
    """Collect every ``#N`` pull request reference in a changelog.

    Args:
        changelog_text: the whole file; ``""`` when it does not exist yet.

    Returns:
        The referenced pull request numbers; these count as released.
    """
    return frozenset(int(number) for number in _PR_REFERENCE_RE.findall(changelog_text))


def top_section_version(changelog_text: str) -> str | None:
    """Return the version of the newest section, or ``None`` when there is none."""
    match = _TOP_SECTION_RE.search(changelog_text)
    return match.group("version") if match is not None else None


def released_pr_numbers(changelog_text: str, draft_version: str | None) -> frozenset[int]:
    """Collect the pull request numbers the changelog counts as released.

    A draft section is skipped: its pull requests are collected again so that
    ``changelog --write`` can regenerate it in full. Only the top section can
    be a draft, and only while its version carries no tag.

    Args:
        changelog_text: the whole file; ``""`` when it does not exist yet.
        draft_version: the version of the draft top section, or ``None``.

    Returns:
        The referenced numbers of every other section.
    """
    span = None if draft_version is None else _section_span(changelog_text, draft_version)
    if span is None:
        return referenced_pr_numbers(changelog_text)
    start, end = span
    return referenced_pr_numbers(changelog_text[:start] + changelog_text[end:])


def select_unreleased(
    pull_requests: Sequence[MergedPullRequest], referenced: Collection[int]
) -> list[ReleaseEntry]:
    """Classify the merged pull requests no changelog section lists yet.

    Dropped: numbers the changelog already references, and the
    ``chore(release):`` pull requests, which carry the sections themselves.

    Args:
        pull_requests: merged pull requests, in any order.
        referenced: pull request numbers from :func:`referenced_pr_numbers`.

    Returns:
        The remaining pull requests as entries, by number ascending.
    """
    entries: list[ReleaseEntry] = []
    for pull_request in sorted(pull_requests, key=lambda row: row.number):
        if pull_request.number in referenced:
            continue
        if pull_request.title.lower().startswith(RELEASE_TITLE_PREFIX):
            continue
        category, text = categorize_title(pull_request.title)
        entries.append(
            ReleaseEntry(
                number=pull_request.number,
                title=pull_request.title,
                url=pull_request.url,
                merged_at=pull_request.merged_at,
                category=category,
                entry=f"{text} ([#{pull_request.number}]({pull_request.url}))",
            )
        )
    return entries


def render_section(version: str, date: str, entries: Sequence[ReleaseEntry]) -> str:
    """Render one changelog section.

    Args:
        version: the version being released.
        date: the release date, ``YYYY-MM-DD``.
        entries: the entries; ``category`` and ``entry`` are used verbatim.

    Returns:
        The section text, ending with a single newline: a ``## [version] -
        date`` heading, then the non-empty groups in :data:`SECTION_ORDER`,
        each with its entries by pull request number ascending.
    """
    lines = [f"## [{version}] - {date}", ""]
    for category in SECTION_ORDER:
        group = sorted(
            (entry for entry in entries if entry.category == category),
            key=lambda entry: entry.number,
        )
        if not group:
            continue
        lines += [f"### {category}", ""]
        lines += [f"- {entry.entry}" for entry in group]
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def insert_section(
    changelog_text: str, version: str, section: str, *, tag_exists: bool
) -> str:
    """Place a rendered section into the changelog text.

    A section for ``version`` that is already the top one and carries no tag
    is replaced in place, so re-running the release before its pull request is
    merged regenerates it. Otherwise the section is inserted before the first
    ``## [`` heading, or appended when the file has none; an empty text
    produces a new file that starts with :data:`CHANGELOG_HEADER`.

    Args:
        changelog_text: the current content; ``""`` when the file is missing.
        version: the version being released.
        section: the output of :func:`render_section`.
        tag_exists: whether ``vX.Y.Z`` exists locally or on origin.

    Returns:
        The full new file content.

    Raises:
        ChangelogConflictError: the version is tagged, or its section is not
            the top one -- released notes are never rewritten.
    """
    if not changelog_text.strip():
        return f"{CHANGELOG_HEADER}\n{section}"
    first_heading = _SECTION_HEADING_RE.search(changelog_text)
    span = _section_span(changelog_text, version)
    if span is None:
        if first_heading is None:
            body = changelog_text.rstrip("\n")
            return f"{body}\n\n{section}"
        cut = first_heading.start()
        return f"{changelog_text[:cut]}{section}\n{changelog_text[cut:]}"
    start, end = span
    if tag_exists:
        raise ChangelogConflictError(
            f"tag v{version} exists: its section is released; release a new version instead"
        )
    if first_heading is None or first_heading.start() != start:
        raise ChangelogConflictError(
            f"section [{version}] is not the top section: only the newest section may be "
            "rewritten; release a new version instead"
        )
    separator = "\n" if end < len(changelog_text) else ""
    return f"{changelog_text[:start]}{section}{separator}{changelog_text[end:]}"


def extract_section(changelog_text: str, version: str) -> str | None:
    """Return the body of the ``## [version]`` section, heading excluded.

    Args:
        changelog_text: the whole file.
        version: the version whose notes are wanted.

    Returns:
        The body without surrounding blank lines, or ``None`` when the file
        has no section for this version.
    """
    span = _section_span(changelog_text, version)
    if span is None:
        return None
    start, end = span
    parts = changelog_text[start:end].split("\n", 1)
    return parts[1].strip("\n") if len(parts) > 1 else ""


def read_project_version(pyproject_text: str) -> str:
    """Read ``version`` from the ``[project]`` table of pyproject.toml.

    Args:
        pyproject_text: the file content.

    Returns:
        The declared version string, unvalidated.

    Raises:
        ValueError: the text is not valid TOML, or declares no project version.
    """
    try:
        document = tomllib.loads(pyproject_text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{PYPROJECT_PATH} is not valid TOML: {exc}") from exc
    project = document.get("project")
    version = project.get("version") if isinstance(project, dict) else None
    if not isinstance(version, str):
        raise ValueError(
            f'{PYPROJECT_PATH} declares no [project] version; add version = "X.Y.Z"'
        )
    return version


def rewrite_pyproject_version(pyproject_text: str, version: str) -> str:
    """Rewrite the ``version`` line of ``[project]``, leaving every other byte alone.

    A ``version`` key of another table (a tool section, a dependency group)
    keeps its value; only the first one inside ``[project]`` is replaced, and
    its line ending and trailing comment survive.

    Args:
        pyproject_text: the file content.
        version: the version to write.

    Returns:
        The new file content.

    Raises:
        ValueError: the ``[project]`` table has no ``version`` line.
    """
    lines = pyproject_text.splitlines(keepends=True)
    inside_project = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            inside_project = stripped == "[project]"
            continue
        if inside_project and _PYPROJECT_VERSION_RE.match(line):
            lines[index] = _PYPROJECT_VERSION_RE.sub(f'version = "{version}"', line, count=1)
            return "".join(lines)
    raise ValueError(
        f'{PYPROJECT_PATH} has no version line under [project]; add version = "{version}"'
    )


def lock_mentions_project_version(lock_text: str, project_name: str = PROJECT_NAME) -> bool:
    """Whether uv.lock pins a version for the project itself.

    ``uv.lock`` carries the project as a ``[[package]]`` of its own (virtual
    source), so a version bump leaves the lock stale until ``uv lock`` runs.

    Args:
        lock_text: the content of uv.lock.
        project_name: the ``[project] name`` to look for.

    Returns:
        True when the lock has to be regenerated after a bump.

    Raises:
        ValueError: the text is not valid TOML.
    """
    try:
        packages = tomllib.loads(lock_text).get("package", [])
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{LOCK_PATH} is not valid TOML: {exc}") from exc
    return any(
        isinstance(package, dict)
        and package.get("name") == project_name
        and isinstance(package.get("version"), str)
        for package in packages
    )


def parse_merged_pull_requests(text: str) -> list[MergedPullRequest]:
    """Convert the JSON of ``gh pr list --json number,title,url,mergedAt`` into rows.

    Args:
        text: the JSON array ``gh`` printed.

    Returns:
        One :class:`MergedPullRequest` per array element, in the given order.

    Raises:
        ValueError: the text is not a JSON array of objects carrying those
            four fields.
    """
    try:
        rows = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not JSON ({exc})") from exc
    if not isinstance(rows, list):
        raise ValueError(f"expected a JSON array, got {type(rows).__name__}")
    pull_requests: list[MergedPullRequest] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"expected an array of objects, got {type(row).__name__}")
        try:
            pull_requests.append(
                MergedPullRequest(
                    number=int(row["number"]),
                    title=str(row["title"]),
                    url=str(row["url"]),
                    merged_at=str(row["mergedAt"]),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"pull request row {row} is unusable ({exc})") from exc
    return pull_requests


def load_entries(text: str) -> list[ReleaseEntry]:
    """Read changelog entries from the JSON ``collect`` prints.

    The whole ``collect`` document and a bare array of its ``pull_requests``
    rows are both accepted; ``category`` and ``entry`` are taken verbatim, so
    entries reworded in the file reach the changelog unchanged.

    Args:
        text: the JSON content of the ``--input`` file.

    Returns:
        The entries, in file order.

    Raises:
        ValueError: the JSON has another shape, a row misses ``number``,
            ``category`` or ``entry``, or names a category outside
            :data:`SECTION_ORDER`.
    """
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"the --input file is not JSON ({exc})") from exc
    rows = document.get("pull_requests") if isinstance(document, dict) else document
    if not isinstance(rows, list):
        raise ValueError(
            "the --input file must hold the JSON that 'cli.release collect' prints "
            "(an object with 'pull_requests', or that array itself)"
        )
    entries: list[ReleaseEntry] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"expected pull request objects, got {type(row).__name__}")
        try:
            entry = ReleaseEntry(
                number=int(row["number"]),
                title=str(row.get("title", "")),
                url=str(row.get("url", "")),
                merged_at=str(row.get("merged_at", "")),
                category=str(row["category"]),
                entry=str(row["entry"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"entry {row} misses number, category or entry ({exc})") from exc
        if entry.category not in SECTION_ORDER:
            raise ValueError(
                f"entry #{entry.number} names category '{entry.category}'; use one of "
                f"{', '.join(SECTION_ORDER)}"
            )
        entries.append(entry)
    return entries


def fetch_merged_pull_requests(repo: str | None = None) -> list[MergedPullRequest]:
    """Ask GitHub for the pull requests merged into the base branch.

    Args:
        repo: ``OWNER/NAME``; ``None`` -- the repository of the working directory.

    Returns:
        The merged pull requests, in ``gh``'s order.

    Raises:
        CommandError: ``gh`` is missing, is not authenticated, or printed
            something other than the requested JSON array.
    """
    command = [
        "gh",
        "pr",
        "list",
        "--state",
        "merged",
        "--base",
        BASE_BRANCH,
        "--limit",
        str(PR_LIMIT),
        "--json",
        "number,title,url,mergedAt",
    ]
    if repo is not None:
        command += ["--repo", repo]
    try:
        return parse_merged_pull_requests(_run(command))
    except ValueError as exc:
        raise CommandError(f"cannot read the output of 'gh pr list': {exc}") from exc


def newest_version_tag() -> str | None:
    """Return the newest local ``vX.Y.Z`` tag, or ``None`` when nothing is tagged.

    Raises:
        CommandError: ``git tag`` failed.
    """
    for line in _run(["git", "tag", "--sort=-v:refname"]).splitlines():
        tag = line.strip()
        if tag.startswith("v") and _VERSION_RE.match(tag[1:]):
            return tag
    return None


def version_tag_exists(tag: str) -> bool:
    """Whether ``tag`` exists in this clone or on ``origin``.

    Raises:
        CommandError: ``git tag`` or ``git ls-remote`` failed.
    """
    if _run(["git", "tag", "--list", tag]).strip():
        return True
    return bool(_run(["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}"]).strip())


def run_version(part: str | None) -> int:
    """Print the project version, or the version a bump would produce."""
    try:
        current = read_project_version(_read_pyproject())
        print(current if part is None else bump_version(current, part))
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE
    return EXIT_OK


def run_collect(repo: str | None, changelog_path: Path) -> int:
    """Print the unreleased pull requests as JSON (shape: the module docstring)."""
    try:
        current_version = read_project_version(_read_pyproject())
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE
    try:
        entries, draft_version = _unreleased_entries(repo, changelog_path)
        previous_tag = newest_version_tag()
    except CommandError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_SUBPROCESS
    if not entries:
        print(
            f"nothing to release: every pull request merged into {BASE_BRANCH} is already "
            f"listed in a released section of {changelog_path}; merge a change first",
            file=sys.stderr,
        )
        return EXIT_NOTHING_TO_DO
    print(
        json.dumps(
            {
                "previous_tag": previous_tag,
                "draft_version": draft_version,
                "current_version": current_version,
                "suggested_bump": suggest_bump(entries),
                "pull_requests": [dataclasses.asdict(entry) for entry in entries],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return EXIT_OK


def run_changelog(
    version: str, date: str | None, input_path: Path | None, write: bool, changelog_path: Path
) -> int:
    """Render one section to stdout and, with ``--write``, put it in the changelog."""
    try:
        parse_version(version)
        release_date = _release_date(date)
        entries = _section_entries(input_path, changelog_path)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except CommandError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_SUBPROCESS
    if not entries:
        print(
            f"nothing to release: no unreleased pull request is left for {version}",
            file=sys.stderr,
        )
        return EXIT_NOTHING_TO_DO
    section = render_section(version, release_date, entries)
    print(section, end="")
    return _write_section(version, section, changelog_path) if write else EXIT_OK


def run_bump(target: str) -> int:
    """Rewrite the project version in pyproject.toml and report the uv.lock follow-up."""
    try:
        wanted = parse_version(target)
        pyproject_text = _read_pyproject()
        current_text = read_project_version(pyproject_text)
        current = parse_version(current_text)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if wanted == current:
        print(f"{PYPROJECT_PATH}: already {target}")
        return EXIT_OK
    if wanted < current:
        print(
            f"ERROR: target {target} is lower than the current version {current_text}; "
            "releases only move forward",
            file=sys.stderr,
        )
        return EXIT_USAGE
    try:
        PYPROJECT_PATH.write_text(
            rewrite_pyproject_version(pyproject_text, target), encoding="utf-8"
        )
    except (OSError, ValueError) as exc:
        print(f"ERROR: cannot write {PYPROJECT_PATH}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    print(f"{PYPROJECT_PATH}: {current_text} -> {target}")
    print(_lock_hint())
    return EXIT_OK


def run_notes(version: str, changelog_path: Path) -> int:
    """Print the body of one section, for ``gh release create --notes-file``."""
    try:
        parse_version(version)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE
    body = extract_section(_read_text(changelog_path), version)
    if body is None:
        print(
            f"ERROR: {changelog_path} has no section [{version}]; write it first with "
            f"'cli.release changelog --version {version} --write'",
            file=sys.stderr,
        )
        return EXIT_NOTHING_TO_DO
    print(body)
    return EXIT_OK


def _write_section(version: str, section: str, changelog_path: Path) -> int:
    """Put a rendered section into the changelog file and report the outcome.

    Args:
        version: the version being released.
        section: the output of :func:`render_section`.
        changelog_path: the file to update; it is created when missing.

    Returns:
        The exit code: 0 written, 3 the section is frozen or misplaced, 4 the
        tag lookup failed, 2 the file cannot be written.
    """
    current = _read_text(changelog_path)
    try:
        # The tag is only queried when a section for this version is already
        # there: that is the one case where its existence decides.
        frozen = extract_section(current, version) is not None and version_tag_exists(f"v{version}")
        changelog_path.write_text(
            insert_section(current, version, section, tag_exists=frozen), encoding="utf-8"
        )
    except CommandError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_SUBPROCESS
    except ChangelogConflictError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_NOTHING_TO_DO
    except OSError as exc:
        print(f"ERROR: cannot write {changelog_path}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    print(f"WROTE {changelog_path} ([{version}])", file=sys.stderr)
    return EXIT_OK


def _section_span(changelog_text: str, version: str) -> tuple[int, int] | None:
    """Locate one section: (start of its heading, start of the next one or EOF)."""
    heading = re.compile(rf"^## \[{re.escape(version)}\]", re.MULTILINE)
    match = heading.search(changelog_text)
    if match is None:
        return None
    following = _SECTION_HEADING_RE.search(changelog_text, match.end())
    return match.start(), following.start() if following is not None else len(changelog_text)


def _read_text(path: Path) -> str:
    """Read a UTF-8 file, or ``""`` when it does not exist yet."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _read_pyproject() -> str:
    """Read pyproject.toml from the working directory.

    Raises:
        ValueError: it cannot be read; the message names the remedy.
    """
    try:
        return PYPROJECT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(
            f"cannot read {PYPROJECT_PATH}: {exc}; run from the repository root"
        ) from exc


def _release_date(date: str | None) -> str:
    """Validate ``--date``, or fall back to today's local date.

    Raises:
        ValueError: the argument is not an ISO ``YYYY-MM-DD`` date.
    """
    if date is None:
        return datetime.date.today().isoformat()
    try:
        return datetime.date.fromisoformat(date).isoformat()
    except ValueError as exc:
        raise ValueError(f"invalid --date '{date}': write it as YYYY-MM-DD ({exc})") from exc


def _section_entries(input_path: Path | None, changelog_path: Path) -> list[ReleaseEntry]:
    """Load the entries of a section from ``--input``, or collect them now.

    Raises:
        ValueError: the input file cannot be read or has the wrong shape.
        CommandError: ``gh`` or ``git`` failed while collecting.
    """
    if input_path is None:
        return _unreleased_entries(None, changelog_path)[0]
    try:
        return load_entries(input_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read {input_path}: {exc}") from exc


def _unreleased_entries(
    repo: str | None, changelog_path: Path
) -> tuple[list[ReleaseEntry], str | None]:
    """Collect the entries no released section lists, plus the draft version.

    Args:
        repo: ``OWNER/NAME`` for ``gh``, or ``None`` for this repository.
        changelog_path: the changelog to subtract.

    Returns:
        A pair of (entries by number ascending, the version of the untagged
        top section or ``None``).

    Raises:
        CommandError: ``gh`` or ``git`` failed.
    """
    changelog_text = _read_text(changelog_path)
    top_version = top_section_version(changelog_text)
    draft_version = (
        top_version
        if top_version is not None and not version_tag_exists(f"v{top_version}")
        else None
    )
    referenced = released_pr_numbers(changelog_text, draft_version)
    return select_unreleased(fetch_merged_pull_requests(repo), referenced), draft_version


def _lock_hint() -> str:
    """One line saying whether uv.lock still pins the previous version."""
    try:
        stale = lock_mentions_project_version(LOCK_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return f"{LOCK_PATH}: unchanged"
    except (OSError, ValueError) as exc:
        return f"{LOCK_PATH}: cannot read it ({exc}); run 'uv lock' and check the result"
    return f"{LOCK_PATH}: run 'uv lock'" if stale else f"{LOCK_PATH}: unchanged"


def _run(command: Sequence[str]) -> str:
    """Run a command and return its stdout.

    Raises:
        CommandError: the program is missing, timed out, or exited non-zero.
    """
    printable = " ".join(command)
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_S, check=False
        )
    except OSError as exc:
        raise CommandError(f"cannot run '{printable}': {exc}; install it and retry") from exc
    except subprocess.TimeoutExpired as exc:
        raise CommandError(
            f"'{printable}' timed out after {SUBPROCESS_TIMEOUT_S:.0f}s; check the network"
        ) from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no output"
        raise CommandError(f"'{printable}' exited {completed.returncode}: {detail}")
    return completed.stdout


def _build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser with one subcommand per release step."""
    parser = argparse.ArgumentParser(
        prog="cli.release",
        description=(
            "Release helpers: the pull requests not yet in CHANGELOG.md, the "
            "changelog section for a version, and the pyproject.toml version bump."
        ),
        epilog=_EXIT_CODE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    version_parser = subparsers.add_parser(
        "version", help="print the project version, or the version a bump would produce"
    )
    version_parser.add_argument(
        "--bump",
        choices=BUMP_PARTS,
        default=None,
        help="print the next version instead of the current one; writes nothing",
    )

    collect_parser = subparsers.add_parser(
        "collect", help="JSON of the merged pull requests no released section lists yet"
    )
    collect_parser.add_argument(
        "--repo", default=None, metavar="OWNER/NAME", help="repository to query (default: this one)"
    )
    collect_parser.add_argument(
        "--changelog",
        type=Path,
        default=DEFAULT_CHANGELOG_PATH,
        metavar="PATH",
        help=f"changelog to subtract (default: {DEFAULT_CHANGELOG_PATH}; "
        "missing = nothing released yet)",
    )

    changelog_parser = subparsers.add_parser(
        "changelog", help="render one changelog section and optionally write it"
    )
    changelog_parser.add_argument(
        "--version", required=True, metavar="X.Y.Z", help="the version being released"
    )
    changelog_parser.add_argument(
        "--date", default=None, metavar="YYYY-MM-DD", help="release date (default: today)"
    )
    changelog_parser.add_argument(
        "--input",
        type=Path,
        default=None,
        metavar="FILE",
        help="JSON from 'collect', edits included (default: collect now)",
    )
    changelog_parser.add_argument(
        "--write",
        action="store_true",
        help="also write the section: replace the untagged top section, else insert it",
    )
    changelog_parser.add_argument(
        "--changelog",
        type=Path,
        default=DEFAULT_CHANGELOG_PATH,
        metavar="PATH",
        help=f"changelog to write (default: {DEFAULT_CHANGELOG_PATH})",
    )

    bump_parser = subparsers.add_parser(
        "bump", help="rewrite the [project] version line of pyproject.toml"
    )
    bump_parser.add_argument("--to", required=True, metavar="X.Y.Z", help="the version to write")

    notes_parser = subparsers.add_parser(
        "notes", help="print one section body, for 'gh release create --notes-file'"
    )
    notes_parser.add_argument(
        "--version", required=True, metavar="X.Y.Z", help="the version whose notes are wanted"
    )
    notes_parser.add_argument(
        "--changelog",
        type=Path,
        default=DEFAULT_CHANGELOG_PATH,
        metavar="PATH",
        help=f"changelog to read (default: {DEFAULT_CHANGELOG_PATH})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the requested subcommand.

    Args:
        argv: command-line arguments without the program name; ``None`` --
            ``sys.argv[1:]``.

    Returns:
        The process exit code (see the module docstring).
    """
    args = _build_parser().parse_args(argv)
    if args.command == "version":
        return run_version(args.bump)
    if args.command == "collect":
        return run_collect(args.repo, args.changelog)
    if args.command == "changelog":
        return run_changelog(args.version, args.date, args.input, args.write, args.changelog)
    if args.command == "bump":
        return run_bump(args.to)
    return run_notes(args.version, args.changelog)


if __name__ == "__main__":
    sys.exit(main())
