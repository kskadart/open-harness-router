"""Unit tests for ``cli.validate_routing`` on ``tests/fixtures/routing_test.yaml``.

``build_runtime`` runs for real (settings, provider factory, registry) with
the environment from the shared ``_env`` fixture; the tests assert the
process exit code and the printed summary.
"""

from __future__ import annotations

import argparse
import re
import textwrap
from pathlib import Path

import pytest

from cli.validate_routing import (
    EXIT_OK,
    EXIT_ROUTE_MISMATCH,
    MISMATCHES_MARKER,
    ROUTES_MARKER,
    main,
    parse_alias_expectation,
)

# From tests/fixtures/routing_test.yaml: the contains-"GLM" rule and its
# neighbours; 4 rules plus the default route.
_GLM_ALIAS = "GLM-5.2-any-suffix"
_GLM_PROVIDER = "openai_compatible"
_GLM_UPSTREAM = "zai-org/GLM-5.2-FP8"
_OTHER_PROVIDER = "openai"
_DEFAULT_PROVIDER = "anthropic"
_FIXTURE_RULES_COUNT = 4
_UNSET_KEY_ENV = "OHR_UNSET_KEY_FOR_TEST"
_BROKEN_PROVIDER = "broken"
_SHADOWED_ALIAS = "GLM-5.2-flash"
_SHADOWING_MATCH_VALUE = "GLM"


@pytest.fixture
def unset_key_routing_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A routing file whose openai-translate provider names an unset key variable."""
    monkeypatch.delenv(_UNSET_KEY_ENV, raising=False)
    path = tmp_path / "routing_unset_key.yaml"
    path.write_text(
        textwrap.dedent(
            f"""\
            version: 1
            providers:
              anthropic:
                type: passthrough
                base_url: https://api.anthropic.com
                forward_client_auth: true
              {_BROKEN_PROVIDER}:
                type: openai-translate
                base_url: https://gateway.example.com/v1
                api_key_env: {_UNSET_KEY_ENV}
                max_tokens_limit: 32000
            rules:
              - match: {{type: exact, value: "broken-alias"}}
                provider: {_BROKEN_PROVIDER}
            default:
              provider: anthropic
            """
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def shadowed_exact_rule_routing_yaml(tmp_path: Path) -> Path:
    """A routing file whose exact rule sits behind a contains rule that captures it."""
    path = tmp_path / "routing_shadowed_exact.yaml"
    path.write_text(
        textwrap.dedent(
            f"""\
            version: 1
            providers:
              anthropic:
                type: passthrough
                base_url: https://api.anthropic.com
                forward_client_auth: true
              {_GLM_PROVIDER}:
                type: openai-translate
                base_url: https://gateway.example.com/v1
                api_key_env: OPENAI_COMPATIBLE_KEY
                max_tokens_limit: 32000
            rules:
              - match: {{type: contains, value: "{_SHADOWING_MATCH_VALUE}"}}
                provider: {_GLM_PROVIDER}
                upstream_model: {_GLM_UPSTREAM}
              - match: {{type: exact, value: "{_SHADOWED_ALIAS}"}}
                provider: {_DEFAULT_PROVIDER}
            default:
              provider: {_DEFAULT_PROVIDER}
            """
        ),
        encoding="utf-8",
    )
    return path


def test_main_alias_resolves_to_expected_provider_returns_exit_0(
    _env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """An alias matching the expected provider and upstream model passes."""
    exit_code = main(["--expect-provider", _GLM_PROVIDER, f"{_GLM_ALIAS}={_GLM_UPSTREAM}"])

    captured = capsys.readouterr()
    assert exit_code == EXIT_OK
    assert ROUTES_MARKER in captured.out
    assert f"{_GLM_ALIAS} -> {_GLM_PROVIDER} (upstream_model={_GLM_UPSTREAM})" in captured.out


def test_main_summary_reports_rules_and_routes_counts_separately(
    _env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The rule count matches /health while the route list also holds the default."""
    exit_code = main([])

    captured = capsys.readouterr()
    assert exit_code == EXIT_OK
    assert f"rules: {_FIXTURE_RULES_COUNT} (= /health rules_count)" in captured.out
    assert f"routes below: {_FIXTURE_RULES_COUNT + 1} (rules + default)" in captured.out
    # The default route is the last row: its number, the "default" match type
    # and the provider everything unmatched falls back to.
    assert re.search(
        rf"^\s*{_FIXTURE_RULES_COUNT + 1}\. default\s+-> {_DEFAULT_PROVIDER}$",
        captured.out,
        re.MULTILINE,
    )


def test_main_wrong_expect_provider_returns_exit_3(
    _env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """An alias that resolves elsewhere than --expect-provider fails with exit 3."""
    exit_code = main(["--expect-provider", _OTHER_PROVIDER, _GLM_ALIAS])

    captured = capsys.readouterr()
    assert exit_code == EXIT_ROUTE_MISMATCH
    assert MISMATCHES_MARKER in captured.err
    assert f"resolved to provider '{_GLM_PROVIDER}', expected '{_OTHER_PROVIDER}'" in captured.err


def test_main_wrong_upstream_model_returns_exit_3(
    _env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The right provider with the wrong upstream_model still fails with exit 3."""
    exit_code = main(["--expect-provider", _GLM_PROVIDER, f"{_GLM_ALIAS}=vendor/other-model"])

    captured = capsys.readouterr()
    assert exit_code == EXIT_ROUTE_MISMATCH
    assert "expected 'vendor/other-model'" in captured.err


def test_main_expect_provider_missing_from_registry_returns_exit_3(
    _env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """--expect-provider naming a provider that was not built fails even with no aliases."""
    exit_code = main(["--expect-provider", "not_configured"])

    captured = capsys.readouterr()
    assert exit_code == EXIT_ROUTE_MISMATCH
    assert "provider 'not_configured' is not in the registry" in captured.err


def test_main_unset_key_env_exits_with_the_factory_message(
    _env: None, monkeypatch: pytest.MonkeyPatch, unset_key_routing_yaml: Path
) -> None:
    """A provider whose key variable is unset aborts with the service's own startup error."""
    monkeypatch.setenv("ROUTER_CONFIG_PATH", str(unset_key_routing_yaml))

    with pytest.raises(SystemExit) as exc_info:
        main(["--expect-provider", _BROKEN_PROVIDER])

    assert f"env '{_UNSET_KEY_ENV}' with API key is not set" in str(exc_info.value.code)


def test_main_exact_rule_shadowed_by_earlier_contains_rule_exits_non_zero(
    _env: None, monkeypatch: pytest.MonkeyPatch, shadowed_exact_rule_routing_yaml: Path
) -> None:
    """An exact rule an earlier contains rule captures never receives a request."""
    monkeypatch.setenv("ROUTER_CONFIG_PATH", str(shadowed_exact_rule_routing_yaml))

    with pytest.raises(SystemExit) as exc_info:
        main(["--expect-provider", _DEFAULT_PROVIDER, _SHADOWED_ALIAS])

    message = str(exc_info.value.code)
    assert exc_info.value.code != EXIT_OK
    assert f"model id '{_SHADOWED_ALIAS}'" in message
    assert "is captured by the earlier rule" in message
    assert f"contains '{_SHADOWING_MATCH_VALUE}'" in message


def test_parse_alias_expectation_without_upstream_leaves_it_unchecked() -> None:
    """``ALIAS`` alone means the provider is checked but not the upstream model."""
    expectation = parse_alias_expectation(_GLM_ALIAS)

    assert expectation.alias == _GLM_ALIAS
    assert expectation.upstream_model is None


def test_parse_alias_expectation_with_upstream_splits_on_first_equals() -> None:
    """``ALIAS=UPSTREAM`` keeps any further ``=`` inside the upstream model."""
    expectation = parse_alias_expectation(f"{_GLM_ALIAS}=vendor/model=v2")

    assert expectation.alias == _GLM_ALIAS
    assert expectation.upstream_model == "vendor/model=v2"


def test_parse_alias_expectation_empty_alias_raises_argument_type_error() -> None:
    """``=upstream`` without an alias is rejected by argparse."""
    with pytest.raises(argparse.ArgumentTypeError, match="empty alias"):
        parse_alias_expectation("=vendor/model")
