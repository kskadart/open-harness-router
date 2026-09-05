"""Generate the Claude Code settings file that lists the router's fleet models.

Run from the repository root: ``.env`` and ``ROUTER_CONFIG_PATH`` resolve
relative to the working directory, as under the launchd service
(``settings.RoutingSettings``)::

    PYTHONPATH=src .venv/bin/python -m cli.sync_client_config \\
        [--settings-path FILE] [--check]

    make sync-client-config

See README, "Model picker and client settings".

Exit codes:

* 0 -- the settings file was written, or already matched;
* 1 -- the routing configuration or the settings file cannot be used;
  nothing is written and the previous version stays in place;
* 3 -- ``--check`` only: the file is out of sync; the difference is printed
  and nothing is written.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from errors import ConfigError
from routing.config_loader import load_routing_config
from routing.schema import RouteLimits, RoutingConfig, advertised_model_ids
from settings import Settings

EXIT_OK = 0
EXIT_CONFIG_ERROR = 1
EXIT_OUT_OF_SYNC = 3

# A dedicated file, never ``~/.claude/settings.json``: the CLI rewrites that
# one during a session, and a regenerated copy would drop what it wrote.
DEFAULT_SETTINGS_PATH = Path.home() / ".claude" / "open-harness-router.settings.json"

ENFORCEMENT_ENV_VAR = "CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT"
ENFORCEMENT_ENV_VALUE = "1"

_ENV_KEY = "env"
_MODEL_PICKER_KEY = "modelPicker"
_OPTIONS_KEY = "options"


def collect_model_options(
    config: RoutingConfig,
) -> tuple[list[dict[str, str]], int, list[str]]:
    """Build the picker rows, counting why each skipped rule contributed none.

    Rows follow rule order, which is also resolution order, and carry the
    EFFECTIVE window and output cap of their own rule.

    Args:
        config: the validated routing configuration.

    Returns:
        A triple of (options, passthrough_count, skipped): the picker rows,
        the number of rules served by a passthrough provider, and the match
        descriptions of pattern rules skipped for naming no ids.

    Raises:
        ConfigError: every rule that could have contributed rows was
            skipped, so the picker would be empty; or an offered model has
            no effective ``context_window``.
    """
    options: list[dict[str, str]] = []
    skipped: list[str] = []
    passthrough_count = 0
    for rule in config.rules:
        provider = config.providers[rule.provider]
        if provider.type != "openai-translate":
            passthrough_count += 1
            continue
        model_ids = advertised_model_ids(rule)
        if not model_ids:
            skipped.append(f"{rule.match.type} '{rule.match.value}'")
            print(
                f"open-harness-router: skipping rule {rule.match.type} "
                f"'{rule.match.value}' -> provider '{rule.provider}': a "
                f"{rule.match.type} value is a pattern, not a model id. Add "
                "'client_models' with the exact ids clients may send for "
                "this rule to offer it in the picker",
                file=sys.stderr,
            )
            continue
        limits = RouteLimits.resolve(provider, rule)
        if limits.context_window is None:
            raise ConfigError(
                f"provider '{rule.provider}' serves {model_ids} without a "
                "context_window; the picker disables client-side compaction, "
                "so set it on the rule or the provider"
            )
        description = (
            f"{rule.provider} -- window {limits.context_window}, "
            f"max output {limits.max_tokens_limit}"
        )
        options.extend(
            {"model": model, "label": model, "description": description}
            for model in model_ids
        )
    if skipped and not options:
        raise ConfigError(
            f"no model can be listed: every offerable rule was skipped ({skipped}). "
            "Add 'client_models' with the exact ids clients may send for at "
            "least one of them"
        )
    return options, passthrough_count, skipped


def read_settings(path: Path) -> tuple[str, dict[str, Any]]:
    """Read the settings file as raw text and as a parsed object.

    Args:
        path: the settings file.

    Returns:
        A pair of (raw text, parsed object); ``("", {})`` when the file does
        not exist yet.

    Raises:
        ConfigError: the target is a directory, or its content is not a JSON
            object -- overwriting either would destroy a file this command
            does not own.
    """
    if path.is_dir():
        raise ConfigError(
            f"settings target {path} is a directory; point --settings-path "
            "at a file"
        )
    if not path.exists():
        return "", {}
    text = path.read_text(encoding="utf-8")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{path} is not valid JSON ({exc}); fix it or "
            f"'mv {path} {path}.bak' and re-run"
        ) from exc
    if not isinstance(parsed, dict):
        raise ConfigError(f"settings file {path} must hold a JSON object")
    return text, parsed


def build_settings_document(
    existing: dict[str, Any], options: list[dict[str, str]]
) -> dict[str, Any]:
    """Merge the picker rows and the enforcement variable into existing settings.

    Every key this command does not own is preserved, at the top level and
    inside ``env``/``modelPicker`` -- a hand-set
    ``modelPicker.replaceBuiltInOptions`` survives a regeneration.

    Args:
        existing: the parsed settings file (empty for a new file).
        options: the picker rows from :func:`collect_model_options`.

    Returns:
        The document to write.

    Raises:
        ConfigError: ``env`` or ``modelPicker`` exists but is not an object.
    """
    document = dict(existing)
    env_section = _object_section(document, _ENV_KEY)
    env_section[ENFORCEMENT_ENV_VAR] = ENFORCEMENT_ENV_VALUE
    document[_ENV_KEY] = env_section
    picker_section = _object_section(document, _MODEL_PICKER_KEY)
    picker_section[_OPTIONS_KEY] = options
    document[_MODEL_PICKER_KEY] = picker_section
    return document


def render_settings(document: dict[str, Any]) -> str:
    """Serialize the settings document exactly as it is written to disk.

    Args:
        document: the merged settings document.

    Returns:
        Pretty-printed JSON with a trailing newline.
    """
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def write_settings(path: Path, text: str) -> None:
    """Write the settings file atomically; a failed write keeps the previous version.

    Raises:
        OSError: the temporary file cannot be written or moved into place.
    """
    # os.replace on a symlink would replace the link
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    # tempfile creates 0600; keep the operator's mode
    existing_mode = target.stat().st_mode & 0o777 if target.exists() else None
    # SIM115: the handle is closed by the ``with`` below; it is created
    # outside it so a failure after creation can still remove the file.
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115
        "w",
        encoding="utf-8",
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(text)
        if existing_mode is not None:
            temp_path.chmod(existing_mode)
        os.replace(temp_path, target)
    except OSError:
        temp_path.unlink(missing_ok=True)
        raise


def _object_section(document: dict[str, Any], key: str) -> dict[str, Any]:
    """Return a mutable copy of a top-level object section, creating it when absent."""
    section = document.get(key, {})
    if not isinstance(section, dict):
        raise ConfigError(
            f"settings key '{key}' holds {type(section).__name__}, expected "
            "an object; fix it before syncing"
        )
    return dict(section)


def _diff(current: str, wanted: str, path: Path) -> str:
    """Render the difference between the file on disk and the wanted content."""
    return "".join(
        difflib.unified_diff(
            current.splitlines(keepends=True),
            wanted.splitlines(keepends=True),
            fromfile=f"{path} (on disk)",
            tofile=f"{path} (routing.yaml)",
        )
    ).rstrip("\n")


def _build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="cli.sync_client_config",
        description=(
            "Write the Claude Code settings file that lists the router's "
            "fleet models in the /model picker and disables the client's "
            "window enforcement for unknown model ids."
        ),
    )
    parser.add_argument(
        "--settings-path",
        type=Path,
        default=DEFAULT_SETTINGS_PATH,
        metavar="FILE",
        help=f"settings file to generate (default: {DEFAULT_SETTINGS_PATH})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report whether the file is up to date without writing it",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Synchronise the client settings file with ``routing.yaml``.

    Args:
        argv: command-line arguments without the program name; ``None`` --
            ``sys.argv[1:]``.

    Returns:
        The process exit code (see the module docstring).
    """
    args = _build_parser().parse_args(argv)
    settings_path: Path = args.settings_path
    try:
        config = load_routing_config(Settings().routing.config_path)
        options, passthrough_count, skipped = collect_model_options(config)
        current, existing = read_settings(settings_path)
        wanted = render_settings(build_settings_document(existing, options))
        cause = (
            f"{passthrough_count} rules serve a passthrough provider and "
            f"{len(skipped)} pattern rules were skipped for naming no 'client_models'"
            if skipped
            else "every rule serves a passthrough provider whose native model "
            "ids the client already lists"
        )
        zero_model = "" if options else f" -- {cause}; there is nothing to add to the picker"

        if current == wanted:
            print(
                f"OK: {settings_path} is in sync ({len(options)} models)"
                f"{zero_model}"
            )
            return EXIT_OK
        if args.check:
            print(_diff(current, wanted, settings_path), file=sys.stderr)
            print(
                f"OUT OF SYNC: {settings_path} does not match routing.yaml; "
                "run 'make sync-client-config'",
                file=sys.stderr,
            )
            return EXIT_OUT_OF_SYNC
        write_settings(settings_path, wanted)
    except (ConfigError, ValidationError, OSError) as exc:
        # OSError covers an unreadable target and a target that turned into
        # something unwritable between the read and the write: the operator
        # gets the refusal, not a traceback.
        print(f"open-harness-router: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    print(f"WROTE {settings_path} ({len(options)} models){zero_model}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
