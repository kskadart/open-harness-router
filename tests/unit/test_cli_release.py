"""Unit tests for ``cli.release``: unreleased pull requests, sections and bumps.

The pure functions run against the fixtures in ``tests/fixtures/release/``.
The ``main`` tests work in a temporary directory and replace the ``gh`` and
``git`` edges of the module with in-memory answers, so no test reaches the
network, the real repository or a real ``gh``. The single exception is the
``uv.lock`` test, which reads this repository's own lock file.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from cli import release
from cli.release import (
    CATEGORY_ADDED,
    CATEGORY_BREAKING,
    CATEGORY_CHANGED,
    CATEGORY_DOCUMENTATION,
    CATEGORY_FIXED,
    CHANGELOG_HEADER,
    EXIT_NOTHING_TO_DO,
    EXIT_OK,
    EXIT_SUBPROCESS,
    EXIT_USAGE,
    PROJECT_NAME,
    ChangelogConflictError,
    CommandError,
    MergedPullRequest,
    ReleaseEntry,
    bump_version,
    categorize_title,
    extract_section,
    insert_section,
    load_entries,
    lock_mentions_project_version,
    main,
    parse_merged_pull_requests,
    parse_version,
    read_project_version,
    referenced_pr_numbers,
    released_pr_numbers,
    render_section,
    rewrite_pyproject_version,
    select_unreleased,
    suggest_bump,
    top_section_version,
)

_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "release"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_PRS_JSON = (_FIXTURES_DIR / "prs.json").read_text(encoding="utf-8")
_SAMPLE_PYPROJECT = (_FIXTURES_DIR / "sample_pyproject.toml").read_text(encoding="utf-8")
_SAMPLE_CHANGELOG = (_FIXTURES_DIR / "changelog.md").read_text(encoding="utf-8")

_PR_URL = "https://github.com/kskadart/open-harness-router/pull"
# tests/fixtures/release/changelog.md: the top section lists #3 and #5, the
# one below it lists #9; prs.json also holds #7, #11, #13 and the release
# pull request #15.
_TOP_SECTION_VERSION = "0.2.0"
_LOWER_SECTION_VERSION = "0.1.0"
_SAMPLE_PROJECT_VERSION = "0.1.0"
_RELEASE_DATE = "2026-09-06"


def _fixture_pull_requests() -> list[MergedPullRequest]:
    """The merged pull requests of ``prs.json``, in file order."""
    return parse_merged_pull_requests(_PRS_JSON)


def _stub_edges(
    monkeypatch: pytest.MonkeyPatch,
    pull_requests: list[MergedPullRequest],
    *,
    tag_exists: bool = False,
    newest_tag: str | None = None,
) -> None:
    """Replace the ``gh`` and ``git`` edges with fixed answers."""
    monkeypatch.setattr(
        release, "fetch_merged_pull_requests", lambda repo=None: list(pull_requests)
    )
    monkeypatch.setattr(release, "newest_version_tag", lambda: newest_tag)
    monkeypatch.setattr(release, "version_tag_exists", lambda tag: tag_exists)


@pytest.fixture
def project_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A working directory holding the sample ``pyproject.toml``, entered by the process."""
    (tmp_path / "pyproject.toml").write_text(_SAMPLE_PYPROJECT, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_parse_version_three_components_returns_integers() -> None:
    assert parse_version("10.2.30") == (10, 2, 30)


@pytest.mark.parametrize(
    "text", ["1.2", "1.2.3.4", "v1.2.3", "1.2.3-rc1", "1.2.3+local", "", "one.2.3"]
)
def test_parse_version_malformed_string_raises_value_error(text: str) -> None:
    with pytest.raises(ValueError, match="major.minor.micro"):
        parse_version(text)


def test_parse_version_tuples_compare_as_versions() -> None:
    assert parse_version("0.9.0") < parse_version("0.10.0") < parse_version("1.0.0")


@pytest.mark.parametrize(
    ("part", "expected"),
    [("major", "2.0.0"), ("minor", "1.3.0"), ("micro", "1.2.4")],
)
def test_bump_version_part_returns_next_version(part: str, expected: str) -> None:
    assert bump_version("1.2.3", part) == expected


def test_bump_version_unknown_part_raises_value_error() -> None:
    with pytest.raises(ValueError, match="unknown bump"):
        bump_version("1.2.3", "patch")


@pytest.mark.parametrize(
    ("title", "expected_category", "expected_text"),
    [
        (
            "feat(routing): enforce model context windows",
            CATEGORY_ADDED,
            "enforce model context windows",
        ),
        (
            "fix: stop leaking credentials to third parties",
            CATEGORY_FIXED,
            "stop leaking credentials to third parties",
        ),
        (
            "docs(readme): describe the forward-proxy mode",
            CATEGORY_DOCUMENTATION,
            "describe the forward-proxy mode",
        ),
        ("chore(deps): pin httpx to 0.28.1", CATEGORY_CHANGED, "pin httpx to 0.28.1"),
        ("ci: cache the uv download", CATEGORY_CHANGED, "cache the uv download"),
        ("perf: reuse the upstream client", CATEGORY_CHANGED, "reuse the upstream client"),
        ("wibble: an unknown type", CATEGORY_CHANGED, "an unknown type"),
        (
            "Ship Qwen and Grok provider templates",
            CATEGORY_CHANGED,
            "Ship Qwen and Grok provider templates",
        ),
        ("FIX(proxy): Keep The Casing", CATEGORY_FIXED, "Keep The Casing"),
        (
            "refactor(providers)!: drop the legacy provider block",
            CATEGORY_BREAKING,
            "drop the legacy provider block",
        ),
        (
            "feat: BREAKING rename the routing keys",
            CATEGORY_BREAKING,
            "BREAKING rename the routing keys",
        ),
    ],
)
def test_categorize_title_prefix_maps_to_category_and_is_stripped(
    title: str, expected_category: str, expected_text: str
) -> None:
    assert categorize_title(title) == (expected_category, expected_text)


def test_categorize_title_breaking_marker_without_prefix_returns_breaking() -> None:
    assert categorize_title("BREAKING drop the v1 endpoints") == (
        CATEGORY_BREAKING,
        "BREAKING drop the v1 endpoints",
    )


def test_categorize_title_bang_after_scope_returns_breaking_and_strips_prefix() -> None:
    assert categorize_title("refactor(providers)!: drop the legacy block") == (
        CATEGORY_BREAKING,
        "drop the legacy block",
    )


@pytest.mark.parametrize(
    ("categories", "expected"),
    [
        ((CATEGORY_BREAKING, CATEGORY_ADDED), "major"),
        ((CATEGORY_ADDED, CATEGORY_FIXED), "minor"),
        ((CATEGORY_FIXED, CATEGORY_CHANGED, CATEGORY_DOCUMENTATION), "micro"),
        ((), "micro"),
    ],
)
def test_suggest_bump_categories_return_expected_part(
    categories: tuple[str, ...], expected: str
) -> None:
    entries = [
        ReleaseEntry(
            number=index,
            title="t",
            url=f"{_PR_URL}/{index}",
            merged_at="2026-08-01T00:00:00Z",
            category=category,
            entry="t",
        )
        for index, category in enumerate(categories, start=1)
    ]

    assert suggest_bump(entries) == expected


def test_referenced_pr_numbers_two_sections_returns_every_reference() -> None:
    assert referenced_pr_numbers(_SAMPLE_CHANGELOG) == frozenset({3, 5, 9})


def test_referenced_pr_numbers_missing_changelog_returns_empty_set() -> None:
    assert referenced_pr_numbers("") == frozenset()


def test_top_section_version_two_sections_returns_the_newest() -> None:
    assert top_section_version(_SAMPLE_CHANGELOG) == _TOP_SECTION_VERSION


def test_top_section_version_header_only_returns_none() -> None:
    assert top_section_version(CHANGELOG_HEADER) is None


def test_released_pr_numbers_draft_top_section_keeps_only_the_lower_sections() -> None:
    assert released_pr_numbers(_SAMPLE_CHANGELOG, _TOP_SECTION_VERSION) == frozenset({9})


def test_released_pr_numbers_no_draft_returns_every_reference() -> None:
    assert released_pr_numbers(_SAMPLE_CHANGELOG, None) == frozenset({3, 5, 9})


def test_select_unreleased_referenced_and_release_prs_dropped_and_sorted_ascending() -> None:
    entries = select_unreleased(_fixture_pull_requests(), frozenset({3, 5, 9}))

    assert [entry.number for entry in entries] == [7, 11, 13]
    assert entries[0].category == CATEGORY_DOCUMENTATION
    assert entries[1].category == CATEGORY_BREAKING
    assert entries[2].entry == (
        f"Ship Qwen and Grok provider templates ([#13]({_PR_URL}/13))"
    )


def test_select_unreleased_empty_changelog_keeps_every_pull_request_but_the_release_one() -> None:
    entries = select_unreleased(_fixture_pull_requests(), frozenset())

    assert [entry.number for entry in entries] == [3, 5, 7, 9, 11, 13]


def test_render_section_groups_follow_the_pinned_order() -> None:
    entries = select_unreleased(_fixture_pull_requests(), frozenset())

    expected = (
        "\n".join(
            [
                f"## [0.3.0] - {_RELEASE_DATE}",
                "",
                "### Breaking changes",
                "",
                f"- drop the legacy provider block ([#11]({_PR_URL}/11))",
                "",
                "### Added",
                "",
                f"- enforce model context windows ([#3]({_PR_URL}/3))",
                "",
                "### Changed",
                "",
                f"- pin httpx to 0.28.1 ([#9]({_PR_URL}/9))",
                f"- Ship Qwen and Grok provider templates ([#13]({_PR_URL}/13))",
                "",
                "### Fixed",
                "",
                f"- stop leaking credentials to third parties ([#5]({_PR_URL}/5))",
                "",
                "### Documentation",
                "",
                f"- describe the forward-proxy mode ([#7]({_PR_URL}/7))",
            ]
        )
        + "\n"
    )

    assert render_section("0.3.0", _RELEASE_DATE, entries) == expected


def test_insert_section_missing_file_creates_the_header_and_the_section() -> None:
    section = render_section("0.1.0", _RELEASE_DATE, [])

    written = insert_section("", "0.1.0", section, tag_exists=False)

    assert written == f"{CHANGELOG_HEADER}\n{section}"
    assert written.startswith("# Changelog\n")


def test_insert_section_existing_sections_inserts_before_the_first_heading() -> None:
    section = render_section("0.3.0", _RELEASE_DATE, [])

    written = insert_section(_SAMPLE_CHANGELOG, "0.3.0", section, tag_exists=False)

    headings = [line for line in written.splitlines() if line.startswith("## [")]
    assert headings[0] == f"## [0.3.0] - {_RELEASE_DATE}"
    assert headings[1:] == [f"## [{_TOP_SECTION_VERSION}] - 2026-08-20", "## [0.1.0] - 2026-08-01"]
    assert written.endswith(_SAMPLE_CHANGELOG[-60:])


def test_insert_section_header_only_file_appends_the_section() -> None:
    section = render_section("0.1.0", _RELEASE_DATE, [])

    written = insert_section(CHANGELOG_HEADER, "0.1.0", section, tag_exists=False)

    assert written == f"{CHANGELOG_HEADER}\n{section}"


def test_insert_section_untagged_top_section_is_replaced_in_place() -> None:
    entries = select_unreleased(_fixture_pull_requests(), frozenset({9}))
    section = render_section(_TOP_SECTION_VERSION, _RELEASE_DATE, entries)

    written = insert_section(_SAMPLE_CHANGELOG, _TOP_SECTION_VERSION, section, tag_exists=False)
    again = insert_section(written, _TOP_SECTION_VERSION, section, tag_exists=False)

    assert written.count(f"## [{_TOP_SECTION_VERSION}]") == 1
    assert f"## [{_TOP_SECTION_VERSION}] - {_RELEASE_DATE}\n" in written
    assert f"([#13]({_PR_URL}/13))" in written
    assert "## [0.1.0] - 2026-08-01" in written
    assert again == written


def test_insert_section_tagged_top_section_raises_conflict() -> None:
    section = render_section(_TOP_SECTION_VERSION, _RELEASE_DATE, [])

    with pytest.raises(ChangelogConflictError, match=f"tag v{_TOP_SECTION_VERSION} exists"):
        insert_section(_SAMPLE_CHANGELOG, _TOP_SECTION_VERSION, section, tag_exists=True)


def test_insert_section_section_below_the_top_one_raises_conflict() -> None:
    section = render_section(_LOWER_SECTION_VERSION, _RELEASE_DATE, [])

    with pytest.raises(ChangelogConflictError, match="not the top section"):
        insert_section(_SAMPLE_CHANGELOG, _LOWER_SECTION_VERSION, section, tag_exists=False)


def test_extract_section_present_returns_the_body_without_the_heading() -> None:
    body = extract_section(_SAMPLE_CHANGELOG, _TOP_SECTION_VERSION)

    assert body is not None
    assert body.startswith("### Added\n")
    assert body.endswith(f"([#5]({_PR_URL}/5))")
    assert "## [" not in body


def test_extract_section_absent_version_returns_none() -> None:
    assert extract_section(_SAMPLE_CHANGELOG, "9.9.9") is None


def test_read_project_version_sample_returns_the_project_table_version() -> None:
    assert read_project_version(_SAMPLE_PYPROJECT) == _SAMPLE_PROJECT_VERSION


def test_read_project_version_without_project_table_raises_value_error() -> None:
    with pytest.raises(ValueError, match="no \\[project\\] version"):
        read_project_version('[tool.demo]\nversion = "1.0.0"\n')


def test_rewrite_pyproject_version_changes_only_the_project_version_line() -> None:
    rewritten = rewrite_pyproject_version(_SAMPLE_PYPROJECT, "0.2.0")

    before = _SAMPLE_PYPROJECT.splitlines(keepends=True)
    after = rewritten.splitlines(keepends=True)
    changed = [index for index, line in enumerate(after) if line != before[index]]
    assert len(after) == len(before)
    assert changed == [2]
    assert after[2] == 'version = "0.2.0"\n'
    assert 'version = "9.9.9"\n' in after


def test_rewrite_pyproject_version_without_a_version_line_raises_value_error() -> None:
    with pytest.raises(ValueError, match="no version line under \\[project\\]"):
        rewrite_pyproject_version('[project]\nname = "demo"\n', "0.2.0")


def test_lock_mentions_project_version_repository_lock_returns_true() -> None:
    lock_text = (_REPOSITORY_ROOT / "uv.lock").read_text(encoding="utf-8")

    assert lock_mentions_project_version(lock_text, PROJECT_NAME) is True


def test_lock_mentions_project_version_lock_without_the_project_returns_false() -> None:
    lock_text = '[[package]]\nname = "httpx"\nversion = "0.28.1"\n'

    assert lock_mentions_project_version(lock_text, PROJECT_NAME) is False


def test_parse_merged_pull_requests_fixture_returns_typed_rows() -> None:
    pull_requests = _fixture_pull_requests()

    assert [row.number for row in pull_requests] == [13, 3, 15, 9, 11, 5, 7]
    assert pull_requests[0].url == f"{_PR_URL}/13"
    assert pull_requests[0].merged_at == "2026-08-28T09:14:00Z"


def test_parse_merged_pull_requests_not_an_array_raises_value_error() -> None:
    with pytest.raises(ValueError, match="expected a JSON array"):
        parse_merged_pull_requests('{"number": 1}')


def test_load_entries_collect_document_returns_the_categories_verbatim() -> None:
    entries = select_unreleased(_fixture_pull_requests(), frozenset())
    document = json.dumps(
        {"pull_requests": [dataclasses.asdict(entry) for entry in entries]}
    )

    assert load_entries(document) == entries


def test_load_entries_unknown_category_raises_value_error() -> None:
    document = json.dumps([{"number": 1, "category": "Whatever", "entry": "text"}])

    with pytest.raises(ValueError, match="names category 'Whatever'"):
        load_entries(document)


def test_main_collect_every_pull_request_released_exits_nothing_to_do_without_stdout(
    project_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (project_dir / "CHANGELOG.md").write_text(_SAMPLE_CHANGELOG, encoding="utf-8")
    released = [row for row in _fixture_pull_requests() if row.number in {3, 5, 9, 15}]
    _stub_edges(monkeypatch, released, tag_exists=True, newest_tag="v0.2.0")

    exit_code = main(["collect"])

    captured = capsys.readouterr()
    assert exit_code == EXIT_NOTHING_TO_DO
    assert captured.out == ""
    assert "nothing to release" in captured.err


def test_main_collect_untagged_top_section_returns_its_pull_requests_as_a_draft(
    project_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (project_dir / "CHANGELOG.md").write_text(_SAMPLE_CHANGELOG, encoding="utf-8")
    _stub_edges(monkeypatch, _fixture_pull_requests(), tag_exists=False, newest_tag=None)

    exit_code = main(["collect"])

    document = json.loads(capsys.readouterr().out)
    assert exit_code == EXIT_OK
    assert document["draft_version"] == _TOP_SECTION_VERSION
    assert document["previous_tag"] is None
    assert document["current_version"] == _SAMPLE_PROJECT_VERSION
    assert document["suggested_bump"] == "major"
    # #3 and #5 come back from the draft section; #9 stays released, #15 is
    # the release pull request itself.
    assert [row["number"] for row in document["pull_requests"]] == [3, 5, 7, 11, 13]


def test_main_collect_tagged_top_section_excludes_its_pull_requests(
    project_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (project_dir / "CHANGELOG.md").write_text(_SAMPLE_CHANGELOG, encoding="utf-8")
    _stub_edges(monkeypatch, _fixture_pull_requests(), tag_exists=True, newest_tag="v0.2.0")

    exit_code = main(["collect"])

    document = json.loads(capsys.readouterr().out)
    assert exit_code == EXIT_OK
    assert document["draft_version"] is None
    assert document["previous_tag"] == "v0.2.0"
    assert [row["number"] for row in document["pull_requests"]] == [7, 11, 13]


def test_main_collect_gh_failure_exits_with_the_subprocess_code(
    project_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _fail(repo: str | None = None) -> list[MergedPullRequest]:
        raise CommandError("'gh pr list' exited 4: gh auth login required")

    monkeypatch.setattr(release, "fetch_merged_pull_requests", _fail)
    monkeypatch.setattr(release, "version_tag_exists", lambda tag: False)
    monkeypatch.setattr(release, "newest_version_tag", lambda: None)

    exit_code = main(["collect"])

    captured = capsys.readouterr()
    assert exit_code == EXIT_SUBPROCESS
    assert captured.out == ""
    assert "gh auth login required" in captured.err


def test_main_version_without_bump_prints_the_project_version(
    project_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["version"])

    assert exit_code == EXIT_OK
    assert capsys.readouterr().out == f"{_SAMPLE_PROJECT_VERSION}\n"


def test_main_version_bump_minor_prints_the_next_version_without_writing(
    project_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["version", "--bump", "minor"])

    assert exit_code == EXIT_OK
    assert capsys.readouterr().out == "0.2.0\n"
    assert (project_dir / "pyproject.toml").read_text(encoding="utf-8") == _SAMPLE_PYPROJECT


def test_main_bump_to_the_current_version_is_a_no_op_with_exit_ok(
    project_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["bump", "--to", _SAMPLE_PROJECT_VERSION])

    assert exit_code == EXIT_OK
    assert f"already {_SAMPLE_PROJECT_VERSION}" in capsys.readouterr().out
    assert (project_dir / "pyproject.toml").read_text(encoding="utf-8") == _SAMPLE_PYPROJECT


def test_main_bump_to_a_lower_version_exits_usage_without_writing(
    project_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["bump", "--to", "0.0.9"])

    captured = capsys.readouterr()
    assert exit_code == EXIT_USAGE
    assert "lower than the current version" in captured.err
    assert (project_dir / "pyproject.toml").read_text(encoding="utf-8") == _SAMPLE_PYPROJECT


def test_main_bump_to_an_invalid_version_exits_usage(
    project_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["bump", "--to", "0.2"])

    captured = capsys.readouterr()
    assert exit_code == EXIT_USAGE
    assert "major.minor.micro" in captured.err
    assert (project_dir / "pyproject.toml").read_text(encoding="utf-8") == _SAMPLE_PYPROJECT


def test_main_bump_to_a_higher_version_rewrites_the_line_and_reports_the_lock(
    project_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project_dir / "uv.lock").write_text(
        f'[[package]]\nname = "{PROJECT_NAME}"\nversion = "0.1.0"\n', encoding="utf-8"
    )

    exit_code = main(["bump", "--to", "0.2.0"])

    captured = capsys.readouterr()
    assert exit_code == EXIT_OK
    assert "pyproject.toml: 0.1.0 -> 0.2.0" in captured.out
    assert "uv.lock: run 'uv lock'" in captured.out
    assert (project_dir / "pyproject.toml").read_text(encoding="utf-8") == (
        _SAMPLE_PYPROJECT.replace('version = "0.1.0"', 'version = "0.2.0"', 1)
    )


def test_main_bump_without_a_lock_file_reports_it_unchanged(
    project_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["bump", "--to", "0.2.0"])

    assert exit_code == EXIT_OK
    assert "uv.lock: unchanged" in capsys.readouterr().out


def test_main_changelog_write_missing_file_creates_it_with_the_header(
    project_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    entries = select_unreleased(_fixture_pull_requests(), frozenset())
    input_path = project_dir / "prs.json"
    input_path.write_text(
        json.dumps({"pull_requests": [dataclasses.asdict(entry) for entry in entries]}),
        encoding="utf-8",
    )
    _stub_edges(monkeypatch, [])

    exit_code = main(
        [
            "changelog",
            "--version",
            "0.1.0",
            "--date",
            _RELEASE_DATE,
            "--input",
            str(input_path),
            "--write",
        ]
    )

    written = (project_dir / "CHANGELOG.md").read_text(encoding="utf-8")
    assert exit_code == EXIT_OK
    assert written.startswith(CHANGELOG_HEADER)
    assert f"## [0.1.0] - {_RELEASE_DATE}" in written
    assert capsys.readouterr().out == render_section("0.1.0", _RELEASE_DATE, entries)


def test_main_changelog_write_tagged_top_section_exits_nothing_to_do(
    project_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    changelog_path = project_dir / "CHANGELOG.md"
    changelog_path.write_text(_SAMPLE_CHANGELOG, encoding="utf-8")
    _stub_edges(monkeypatch, _fixture_pull_requests(), tag_exists=True)

    exit_code = main(["changelog", "--version", _TOP_SECTION_VERSION, "--write"])

    assert exit_code == EXIT_NOTHING_TO_DO
    assert f"tag v{_TOP_SECTION_VERSION} exists" in capsys.readouterr().err
    assert changelog_path.read_text(encoding="utf-8") == _SAMPLE_CHANGELOG


def test_main_changelog_write_section_below_the_top_one_exits_nothing_to_do(
    project_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    changelog_path = project_dir / "CHANGELOG.md"
    changelog_path.write_text(_SAMPLE_CHANGELOG, encoding="utf-8")
    _stub_edges(monkeypatch, _fixture_pull_requests(), tag_exists=False)

    exit_code = main(["changelog", "--version", _LOWER_SECTION_VERSION, "--write"])

    assert exit_code == EXIT_NOTHING_TO_DO
    assert "not the top section" in capsys.readouterr().err
    assert changelog_path.read_text(encoding="utf-8") == _SAMPLE_CHANGELOG


def test_main_changelog_invalid_version_exits_usage(
    project_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["changelog", "--version", "0.2"])

    assert exit_code == EXIT_USAGE
    assert "major.minor.micro" in capsys.readouterr().err


def test_main_notes_present_section_prints_its_body(
    project_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project_dir / "CHANGELOG.md").write_text(_SAMPLE_CHANGELOG, encoding="utf-8")

    exit_code = main(["notes", "--version", _TOP_SECTION_VERSION])

    captured = capsys.readouterr()
    assert exit_code == EXIT_OK
    assert captured.out == f"{extract_section(_SAMPLE_CHANGELOG, _TOP_SECTION_VERSION)}\n"


def test_main_notes_absent_section_exits_nothing_to_do(
    project_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project_dir / "CHANGELOG.md").write_text(_SAMPLE_CHANGELOG, encoding="utf-8")

    exit_code = main(["notes", "--version", "9.9.9"])

    captured = capsys.readouterr()
    assert exit_code == EXIT_NOTHING_TO_DO
    assert captured.out == ""
    assert "has no section [9.9.9]" in captured.err
