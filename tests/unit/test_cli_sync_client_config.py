"""Unit tests for ``cli.sync_client_config``.

The routing file is written under ``tmp_path`` and ``ROUTER_CONFIG_PATH``
points at it, so the tests never read the operator's real ``routing.yaml``;
``--settings-path`` also stays under ``tmp_path``, so the real
``~/.claude`` is never touched. No provider key is needed: the command
loads the configuration only (``load_routing_config``), it does not build
the registry.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from cli.sync_client_config import (
    DEFAULT_SETTINGS_PATH,
    ENFORCEMENT_ENV_VALUE,
    ENFORCEMENT_ENV_VAR,
    EXIT_CONFIG_ERROR,
    EXIT_OK,
    EXIT_OUT_OF_SYNC,
    build_model_options,
    main,
)
from routing.config_loader import load_routing_config
from routing.matcher import match_model
from routing.schema import RoutingConfig, RoutingRule

# A routing file shaped like the live one: a passthrough rule that the
# picker must skip, a contains-rule and a prefix-rule that can only be
# listed through client_models, and an exact rule that falls back to its
# own match value.
_FLEET_ROUTING: dict[str, Any] = {
    "version": 1,
    "providers": {
        "anthropic": {
            "type": "passthrough",
            "base_url": "https://api.anthropic.com",
        },
        "fleet_chat": {
            "type": "openai-translate",
            "base_url": "https://gateway.example.com/v1",
            "api_key_env": "FLEET_CHAT_KEY",
            "max_tokens_limit": 65536,
            "context_window": 206650,
        },
        "fleet_exact": {
            "type": "openai-translate",
            "base_url": "https://gateway.example.com/v1",
            "api_key_env": "FLEET_CHAT_KEY",
            "max_tokens_limit": 32000,
            "context_window": 223680,
        },
        "fleet_responses": {
            "type": "openai-translate",
            "base_url": "https://api.example.com/v1",
            "api_key_env": "FLEET_RESPONSES_KEY",
            "api_flavor": "responses",
            "token_param": "max_output_tokens",
            "max_tokens_limit": 128000,
            "context_window": 1050000,
        },
    },
    "rules": [
        {"match": {"type": "prefix", "value": "claude-"}, "provider": "anthropic"},
        {
            "match": {"type": "contains", "value": "GLM"},
            "provider": "fleet_chat",
            "upstream_model": "vendor/GLM-Test",
            "client_models": ["vendor/GLM-Test"],
        },
        {
            "match": {"type": "exact", "value": "fleet-minimax"},
            "provider": "fleet_exact",
            "upstream_model": "Vendor/MiniMax-Test",
        },
        {
            "match": {"type": "prefix", "value": "gpt-"},
            "provider": "fleet_responses",
            "client_models": ["gpt-test-sol", "gpt-test-terra"],
        },
    ],
    "default": {"provider": "anthropic"},
}

_EXPECTED_OPTIONS: list[dict[str, str]] = [
    {
        "model": "vendor/GLM-Test",
        "label": "vendor/GLM-Test",
        "description": "fleet_chat -- window 206650, max output 65536",
    },
    {
        "model": "fleet-minimax",
        "label": "fleet-minimax",
        "description": "fleet_exact -- window 223680, max output 32000",
    },
    {
        "model": "gpt-test-sol",
        "label": "gpt-test-sol",
        "description": "fleet_responses -- window 1050000, max output 128000",
    },
    {
        "model": "gpt-test-terra",
        "label": "gpt-test-terra",
        "description": "fleet_responses -- window 1050000, max output 128000",
    },
]


def _clone_routing() -> dict[str, Any]:
    """Deep copy of the fleet routing fixture for mutation in tests.

    Returns:
        A fresh mapping equal to ``_FLEET_ROUTING``.
    """
    return yaml.safe_load(yaml.safe_dump(_FLEET_ROUTING))


def _first_matching_rule(config: RoutingConfig, model: str) -> RoutingRule | None:
    """Resolve a model the way ``ProviderRegistry.resolve`` does: first match wins.

    Args:
        config: the loaded routing configuration.
        model: model id exactly as a client would send it.

    Returns:
        The winning rule, or ``None`` when the model falls through to the
        default route.
    """
    return next(
        (rule for rule in config.rules if match_model(rule.match, model)), None
    )


@pytest.fixture
def write_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[[dict[str, Any]], Path]:
    """Provide a writer for the routing file that also sets ``ROUTER_CONFIG_PATH``."""

    def write(raw: dict[str, Any]) -> Path:
        path = tmp_path / "routing_fleet.yaml"
        path.write_text(yaml.safe_dump(raw), encoding="utf-8")
        monkeypatch.setenv("ROUTER_CONFIG_PATH", str(path))
        return path

    return write


@pytest.fixture
def fleet_routing(write_routing: Callable[[dict[str, Any]], Path]) -> Path:
    """The unmodified fleet routing fixture, already active for this test."""
    return write_routing(_clone_routing())


@pytest.fixture
def settings_path(tmp_path: Path) -> Path:
    """Target settings file inside ``tmp_path`` -- never the real ``~/.claude``."""
    return tmp_path / "open-harness-router.settings.json"


def test_build_model_options_lists_client_models_and_exact_values_in_rule_order(
    fleet_routing: Path,
) -> None:
    """Rows come from client_models, or an exact rule's own value, in rule order."""
    options = build_model_options(load_routing_config(fleet_routing))

    assert options == _EXPECTED_OPTIONS


def test_build_model_options_skips_rules_serving_a_passthrough_provider(
    fleet_routing: Path,
) -> None:
    """The native ``claude-`` rule contributes no row: passthrough has no window."""
    options = build_model_options(load_routing_config(fleet_routing))

    assert all(not option["model"].startswith("claude-") for option in options)


def test_every_emitted_model_routes_back_to_the_rule_that_emitted_it(
    fleet_routing: Path,
) -> None:
    """First match wins, so each offered id must reach its own rule, not an earlier one."""
    config = load_routing_config(fleet_routing)
    options = build_model_options(config)

    for option in options:
        model = option["model"]
        owner = next(
            rule
            for rule in config.rules
            if model in rule.client_models
            or (rule.match.type == "exact" and model == rule.match.value)
        )
        assert _first_matching_rule(config, model) is owner
    assert len(options) == len(_EXPECTED_OPTIONS)


def test_every_emitted_model_is_a_declared_id_never_a_match_pattern(
    fleet_routing: Path,
) -> None:
    """The regression this feature exists for: ``gpt-`` or ``GLM`` must never be offered.

    A ``prefix``/``contains``/``regex`` match value is a pattern, not a
    model id -- sending it upstream verbatim yields a vendor 404.
    """
    config = load_routing_config(fleet_routing)
    pattern_values = {
        rule.match.value for rule in config.rules if rule.match.type != "exact"
    }

    offered = {option["model"] for option in build_model_options(config)}

    assert offered.isdisjoint(pattern_values)


def test_every_emitted_model_reaches_the_upstream_as_a_rewritten_or_verbatim_id(
    fleet_routing: Path,
) -> None:
    """What the upstream receives is the rule's ``upstream_model`` or the id itself.

    An exact rule may forward the alias verbatim only because the alias is
    the upstream's own id; a rule that renames it must do so through
    ``upstream_model``. Either way the value sent upstream is a concrete
    model id declared in the configuration.
    """
    config = load_routing_config(fleet_routing)

    for option in build_model_options(config):
        rule = _first_matching_rule(config, option["model"])
        assert rule is not None
        upstream_model = rule.upstream_model or option["model"]
        assert upstream_model in {rule.upstream_model, option["model"]}
        assert rule.match.type == "exact" or upstream_model != rule.match.value


def test_main_absent_settings_file_creates_it_with_picker_and_enforcement_env(
    fleet_routing: Path, settings_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing settings file is created with both the picker rows and the env var."""
    exit_code = main(["--settings-path", str(settings_path)])

    captured = capsys.readouterr()
    assert exit_code == EXIT_OK
    assert str(settings_path) in captured.out
    document = json.loads(settings_path.read_text(encoding="utf-8"))
    assert document["modelPicker"]["options"] == _EXPECTED_OPTIONS
    assert document["env"][ENFORCEMENT_ENV_VAR] == ENFORCEMENT_ENV_VALUE
    assert settings_path.read_text(encoding="utf-8").endswith("\n")


def test_main_existing_settings_file_preserves_unknown_sibling_keys(
    fleet_routing: Path, settings_path: Path
) -> None:
    """Every other key survives the merge, inside ``modelPicker`` and ``env`` too."""
    settings_path.write_text(
        json.dumps(
            {
                "permissions": {"allow": ["Bash(ls:*)"]},
                "env": {"SOME_OTHER_VAR": "keep-me"},
                "modelPicker": {
                    "replaceBuiltInOptions": True,
                    "options": [{"model": "stale", "label": "stale"}],
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    exit_code = main(["--settings-path", str(settings_path)])

    document = json.loads(settings_path.read_text(encoding="utf-8"))
    assert exit_code == EXIT_OK
    assert document["permissions"] == {"allow": ["Bash(ls:*)"]}
    assert document["env"]["SOME_OTHER_VAR"] == "keep-me"
    assert document["env"][ENFORCEMENT_ENV_VAR] == ENFORCEMENT_ENV_VALUE
    assert document["modelPicker"]["replaceBuiltInOptions"] is True
    assert document["modelPicker"]["options"] == _EXPECTED_OPTIONS


def test_main_unparsable_settings_file_returns_exit_1_and_keeps_the_file(
    fleet_routing: Path, settings_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unparsable JSON is a refusal, never an overwrite of the operator's file."""
    settings_path.write_text("{ not json", encoding="utf-8")

    exit_code = main(["--settings-path", str(settings_path)])

    captured = capsys.readouterr()
    assert exit_code == EXIT_CONFIG_ERROR
    assert "not valid JSON" in captured.err
    assert settings_path.read_text(encoding="utf-8") == "{ not json"


def test_main_settings_section_that_is_not_an_object_returns_exit_1(
    fleet_routing: Path, settings_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ``modelPicker`` that is not an object cannot be merged into."""
    settings_path.write_text(json.dumps({"modelPicker": []}), encoding="utf-8")

    exit_code = main(["--settings-path", str(settings_path)])

    captured = capsys.readouterr()
    assert exit_code == EXIT_CONFIG_ERROR
    assert "expected an object" in captured.err


def test_main_provider_without_context_window_returns_exit_1(
    write_routing: Callable[[dict[str, Any]], Path],
    settings_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Listing a model whose provider has no window would leave it unguarded.

    The generated file disables the client's own unknown-model compaction,
    so the router's pre-flight is the only remaining guard -- an
    openai-translate provider without ``context_window`` must not be
    offered.
    """
    raw = _clone_routing()
    del raw["providers"]["fleet_chat"]["context_window"]
    write_routing(raw)

    exit_code = main(["--settings-path", str(settings_path)])

    captured = capsys.readouterr()
    assert exit_code == EXIT_CONFIG_ERROR
    assert "context_window" in captured.err
    assert not settings_path.exists()


def test_main_non_exact_rule_without_client_models_is_skipped_with_a_warning(
    write_routing: Callable[[dict[str, Any]], Path],
    settings_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A pattern rule that names no ids costs its own rows, not the whole run.

    Uncommenting the shipped ``kimi-`` example used to abort the sync for
    every other model; the rule contributes nothing and the run continues.
    """
    raw = _clone_routing()
    del raw["rules"][3]["client_models"]
    write_routing(raw)

    exit_code = main(["--settings-path", str(settings_path)])

    captured = capsys.readouterr()
    assert exit_code == EXIT_OK
    assert "prefix 'gpt-'" in captured.err
    assert "client_models" in captured.err
    document = json.loads(settings_path.read_text(encoding="utf-8"))
    assert document["modelPicker"]["options"] == _EXPECTED_OPTIONS[:2]


def test_main_every_rule_skipped_returns_exit_1(
    write_routing: Callable[[dict[str, Any]], Path],
    settings_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Nothing left to offer is still a configuration error, not an empty picker."""
    raw = _clone_routing()
    del raw["rules"][1]["client_models"]
    del raw["rules"][3]["client_models"]
    raw["rules"][2]["match"] = {"type": "contains", "value": "minimax"}
    write_routing(raw)

    exit_code = main(["--settings-path", str(settings_path)])

    captured = capsys.readouterr()
    assert exit_code == EXIT_CONFIG_ERROR
    assert "client_models" in captured.err
    assert not settings_path.exists()


def test_main_settings_path_pointing_at_a_directory_is_refused(
    fleet_routing: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A directory target is refused with a message, never a traceback."""
    directory = tmp_path / "settings-dir"
    directory.mkdir()

    exit_code = main(["--settings-path", str(directory)])

    captured = capsys.readouterr()
    assert exit_code == EXIT_CONFIG_ERROR
    assert "is a directory" in captured.err
    assert list(directory.iterdir()) == []


def test_main_settings_path_symlink_writes_through_it_and_keeps_the_link(
    fleet_routing: Path, tmp_path: Path
) -> None:
    """A symlinked settings file stays a symlink: os.replace must not clobber it."""
    real_target = tmp_path / "real-settings.json"
    real_target.write_text("{}\n", encoding="utf-8")
    link = tmp_path / "linked-settings.json"
    link.symlink_to(real_target)

    exit_code = main(["--settings-path", str(link)])

    assert exit_code == EXIT_OK
    assert link.is_symlink()
    assert link.readlink() == real_target
    document = json.loads(real_target.read_text(encoding="utf-8"))
    assert document["modelPicker"]["options"] == _EXPECTED_OPTIONS


def test_main_check_on_a_synced_file_returns_exit_0(
    fleet_routing: Path, settings_path: Path
) -> None:
    """After a write, ``--check`` reports the file as in sync."""
    assert main(["--settings-path", str(settings_path)]) == EXIT_OK

    assert main(["--settings-path", str(settings_path), "--check"]) == EXIT_OK


def test_main_check_on_a_missing_file_returns_exit_3_and_writes_nothing(
    fleet_routing: Path, settings_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--check`` reports the difference and leaves the filesystem untouched."""
    exit_code = main(["--settings-path", str(settings_path), "--check"])

    captured = capsys.readouterr()
    assert exit_code == EXIT_OUT_OF_SYNC
    assert "modelPicker" in captured.err
    assert not settings_path.exists()


def test_main_check_after_an_external_edit_returns_exit_3(
    fleet_routing: Path, settings_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hand-edited picker row is reported as out of sync, not silently kept."""
    assert main(["--settings-path", str(settings_path)]) == EXIT_OK
    document = json.loads(settings_path.read_text(encoding="utf-8"))
    document["modelPicker"]["options"] = [{"model": "stale", "label": "stale"}]
    settings_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    exit_code = main(["--settings-path", str(settings_path), "--check"])

    captured = capsys.readouterr()
    assert exit_code == EXIT_OUT_OF_SYNC
    assert "stale" in captured.err


def test_main_run_twice_leaves_the_file_byte_identical(
    fleet_routing: Path, settings_path: Path
) -> None:
    """The command is idempotent: a second run produces no diff."""
    assert main(["--settings-path", str(settings_path)]) == EXIT_OK
    first = settings_path.read_bytes()

    assert main(["--settings-path", str(settings_path)]) == EXIT_OK

    assert settings_path.read_bytes() == first


def test_main_leaves_no_temporary_file_behind(
    fleet_routing: Path, settings_path: Path
) -> None:
    """The atomic write renames its temp file instead of leaving it in place."""
    main(["--settings-path", str(settings_path)])

    assert [entry.name for entry in settings_path.parent.iterdir() if ".tmp" in entry.name] == []


def test_default_settings_path_is_a_dedicated_file_in_the_user_claude_directory() -> None:
    """The default target is never ``~/.claude/settings.json`` -- the CLI rewrites it."""
    assert DEFAULT_SETTINGS_PATH.parent == Path.home() / ".claude"
    assert DEFAULT_SETTINGS_PATH.name == "open-harness-router.settings.json"


def test_build_model_options_shows_per_rule_limits_when_two_rules_share_one_provider(
    write_routing: Callable[[dict[str, Any]], Path],
) -> None:
    """One gateway, two models: each row carries the numbers its own rule resolves to.

    The rows must not all repeat the provider block's defaults -- that is
    the whole point of the per-rule override (a model with a tighter cap
    next to one with a wider window on the same base_url).
    """
    raw = _clone_routing()
    raw["rules"][2]["provider"] = "fleet_chat"
    raw["rules"][2]["max_tokens_limit"] = 32000
    raw["rules"][2]["context_window"] = 223680
    routing_path = write_routing(raw)

    options = build_model_options(load_routing_config(routing_path))

    assert [option["description"] for option in options[:2]] == [
        "fleet_chat -- window 206650, max output 65536",
        "fleet_chat -- window 223680, max output 32000",
    ]


def test_build_model_options_accepts_a_rule_window_where_the_provider_declares_none(
    write_routing: Callable[[dict[str, Any]], Path],
) -> None:
    """The guard is the EFFECTIVE window: a rule may supply the one the provider lacks."""
    raw = _clone_routing()
    del raw["providers"]["fleet_chat"]["context_window"]
    raw["rules"][1]["context_window"] = 206650
    routing_path = write_routing(raw)

    options = build_model_options(load_routing_config(routing_path))

    assert options[0]["description"] == "fleet_chat -- window 206650, max output 65536"
