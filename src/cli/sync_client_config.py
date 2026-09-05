"""Generate the Claude Code settings file that lists the router's fleet models.

Run from the repository root -- ``.env`` and ``ROUTER_CONFIG_PATH`` are
resolved relative to the working directory, exactly as under the launchd
service (``settings.RoutingSettings``)::

    PYTHONPATH=src .venv/bin/python -m cli.sync_client_config \\
        [--settings-path FILE] [--check]

    make sync-client-config

Why this file exists: Claude Code's ``/model`` picker only discovers ids
containing ``claude``/``anthropic``, so every fleet model the router serves
is invisible to it. ``modelPicker.options`` (CLI v2.1.242+) lists arbitrary
ids, but is read only from managed settings, ``--settings`` and user
settings -- and ``~/.claude/settings.json`` is rewritten by the CLI itself
during a session (``/model``, ``/effort``), so it is the wrong target. The
generated file is a dedicated one, wired into the session with
``claude --settings <file>``.

The same file carries ``env.CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT
= "1"``: Claude Code's proactive compaction assumes its own built-in window
for an unrecognised model id, which has nothing to do with the real window
of the deployment behind the router. Disabling it makes the router's
``context_window`` pre-flight the only guard, so a model is offered only
when its rule or its provider declares one. Recognised ``claude-*`` ids are
unaffected -- the variable applies to unknown ids alone.

Rows come from ``RoutingRule.client_models``, or from the match value of an
``exact`` rule (``routing.schema.advertised_model_ids``); a non-exact rule
with neither is skipped with a warning, because its match value is a
pattern and not a model id -- one such rule must not cost every other model
its picker row. Rules serving a ``passthrough`` provider are skipped too:
the body is forwarded byte-for-byte, there is no window to publish and
native ``claude-*`` ids are already in the picker.
Each row's window and output cap are the EFFECTIVE ones for that rule --
``RoutingRule.max_tokens_limit``/``context_window`` where set, the
provider's values otherwise -- so two models sharing one gateway advertise
their own limits.

Only the routing configuration is loaded (``load_routing_config``), never
``main.build_runtime``: listing models needs no provider keys, no CA
bundles and no HTTP clients.

Exit codes:

* 0 -- the settings file was written, or already matched;
* 1 -- the routing configuration or the settings file cannot be used
  (unparsable JSON, a directory or unreadable target, a model with no
  effective ``context_window``, or every rule skipped so that no model can
  be listed at all); nothing is written;
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


def build_model_options(config: RoutingConfig) -> list[dict[str, str]]:
    """Build the ``modelPicker.options`` rows for every routable fleet model.

    Rows follow rule order, which is also resolution order, so the picker
    reads like the routing table. A ``prefix``/``contains``/``regex`` rule
    that names no ``client_models`` cannot be turned into rows -- its match
    value is a pattern, not a model id -- so it is reported on stderr and
    skipped; the other models keep their rows.

    Args:
        config: the validated routing configuration.

    Returns:
        One row per offered model: ``model``, ``label`` and a
        ``description`` naming the provider and the EFFECTIVE window and
        output cap of that model's own rule (the rule's overrides folded
        onto the provider's values), so two models on one gateway show
        their own numbers rather than a shared default.

    Raises:
        ConfigError: every rule that could have contributed rows was
            skipped, so the picker would be empty; or an offered model has
            no effective ``context_window`` (the client's own compaction is
            disabled by this same settings file, so the router's pre-flight
            would be the only guard -- and there would be none).
    """
    options: list[dict[str, str]] = []
    skipped: list[str] = []
    for rule in config.rules:
        provider = config.providers[rule.provider]
        if provider.type != "openai-translate":
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
                f"provider '{rule.provider}' serves {model_ids} but neither "
                "the rule nor the provider declares a 'context_window'; the "
                "generated settings file disables the client's compaction "
                "for unknown model ids, so the router pre-flight is the only "
                "guard left. Measure the deployment's window and set it on "
                "the rule (per model) or on the provider (as its default), "
                "or drop these models from 'client_models'"
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
    return options


def read_settings(path: Path) -> tuple[str, dict[str, Any]]:
    """Read the settings file as raw text and as a parsed object.

    Args:
        path: the settings file.

    Returns:
        A pair of (raw text, parsed object); ``("", {})`` when the file does
        not exist yet.

    Raises:
        ConfigError: the target is a directory, the file is not parsable
            JSON, or it is not a JSON object -- overwriting any of those
            would destroy something this command does not own.
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
            f"settings file {path} is not valid JSON ({exc}); fix or remove "
            "it -- refusing to overwrite a file that may hold hand-written "
            "settings"
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
        options: the picker rows from :func:`build_model_options`.

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
    """Write the settings file atomically, through any symlink on the way.

    The temporary file is created in the target directory so ``os.replace``
    stays within one filesystem and the reader either sees the previous
    version or the new one, never a half-written file. The path is resolved
    first: ``os.replace`` on a symlink would replace the LINK with a regular
    file, silently detaching the operator's real settings file.

    Args:
        path: the settings file.
        text: the rendered document.
    """
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(text)
    os.replace(handle.name, target)


def _object_section(document: dict[str, Any], key: str) -> dict[str, Any]:
    """Return a copy of a top-level object section, creating it when absent.

    Args:
        document: the parsed settings file.
        key: the section name.

    Returns:
        A mutable copy of the section.

    Raises:
        ConfigError: the key exists but does not hold an object.
    """
    section = document.get(key, {})
    if not isinstance(section, dict):
        raise ConfigError(
            f"settings key '{key}' holds {type(section).__name__}, expected "
            "an object; fix it before syncing"
        )
    return dict(section)


def _diff(current: str, wanted: str, path: Path) -> str:
    """Render the difference between the file on disk and the wanted content.

    Args:
        current: the file's current text (empty when it does not exist).
        wanted: the text this command would write.
        path: the settings file, used for the diff headers.

    Returns:
        A unified diff without a trailing newline.
    """
    return "".join(
        difflib.unified_diff(
            current.splitlines(keepends=True),
            wanted.splitlines(keepends=True),
            fromfile=f"{path} (on disk)",
            tofile=f"{path} (routing.yaml)",
        )
    ).rstrip("\n")


def _build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser.

    Returns:
        The configured parser.
    """
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
        options = build_model_options(config)
        current, existing = read_settings(settings_path)
        wanted = render_settings(build_settings_document(existing, options))
    except (ConfigError, ValidationError, OSError) as exc:
        # OSError covers an unreadable target and a target that turned into
        # something unwritable between the read and the write: the operator
        # gets the refusal, not a traceback.
        print(f"open-harness-router: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    if current == wanted:
        print(f"OK: {settings_path} is in sync ({len(options)} models)")
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
    print(f"WROTE {settings_path} ({len(options)} models)")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
