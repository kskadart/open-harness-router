"""Pydantic schema for the ``routing.yaml`` routing file.

Describes providers, model-matching rules, and the default route. The
validator guarantees referential integrity and key safety invariants: the
default route must lead to a passthrough provider, so the orchestrator
never falls back to a fleet model.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, model_validator

ProviderType = Literal["passthrough", "openai-translate"]
MatchType = Literal["exact", "prefix", "contains", "regex"]
TokenParam = Literal["max_tokens", "max_completion_tokens", "max_output_tokens"]
ApiFlavor = Literal["chat", "responses"]
ReasoningEffort = Literal["none", "low", "medium", "high", "xhigh", "max"]

# Top-level OpenAI request parameters allowed to be dropped. messages/model
# are intentionally excluded: dropping them breaks the request's semantics.
_DROPPABLE_PARAMS: frozenset[str] = frozenset({"temperature", "top_p", "stop"})


class MatchRule(BaseModel):
    """A model-name matching rule."""

    type: MatchType
    value: str


class RoutingRule(BaseModel):
    """A routing rule: match -> provider (+ model substitution)."""

    match: MatchRule
    provider: str
    upstream_model: str | None = None


class ProviderCfg(BaseModel):
    """Configuration for a single provider.

    The ``token_param`` and ``drop_params`` fields manage compatibility with
    OpenAI-compatible upstreams, where reasoning models (e.g. GPT-5.x)
    require ``max_completion_tokens`` instead of ``max_tokens`` and reject
    ``temperature``/``top_p``.

    ``max_tokens_limit`` -- the cap on the outgoing request's ``max_tokens``.
    For reasoning upstreams (GLM-5.x) a low cap is dangerous: reasoning
    tokens burn through the budget and the upstream returns empty
    ``content`` with ``stop_reason max_tokens``. Required for
    ``openai-translate``: there's intentionally no default, so a forgotten
    value fails at startup instead of silently truncating responses. Not
    applicable to ``passthrough`` (byte-for-byte forwarding without
    conversion) and stays ``None`` there.

    ``tools_max`` -- the cap on the ``tools`` array size for openai-translate
    providers. OpenAI's hard limit is 128 elements; over that, extra MCP
    tools are dropped while built-in Claude Code tools are kept. ``0`` means
    no limit (passthrough/providers with no known limit).

    ``api_flavor`` -- which OpenAI endpoint to use: ``chat`` (the classic
    /v1/chat/completions) or ``responses`` (/v1/responses). ``responses`` is
    only valid for ``openai-translate``: on /v1/chat/completions the GPT-5.6
    family can't combine function tools with reasoning (the upstream returns
    400).

    ``reasoning_effort`` -- the reasoning level for the Responses API.
    Applied only when ``api_flavor="responses"``; ignored for ``chat``.
    """

    type: ProviderType
    base_url: str
    api_key_env: str | None = None
    ca_bundle: str | None = None
    timeout_s: int = 120
    stream_read_timeout_s: int = 600
    extra_headers: dict[str, str] = Field(default_factory=dict)
    forward_client_auth: bool = False
    token_param: TokenParam = "max_tokens"
    drop_params: list[str] = Field(default_factory=list)
    max_tokens_limit: int | None = None
    tools_max: int = 0
    api_flavor: ApiFlavor = "chat"
    reasoning_effort: ReasoningEffort = "medium"

    @model_validator(mode="after")
    def _validate_drop_params(self) -> ProviderCfg:
        """Allow dropping only safe top-level parameters.

        Returns:
            The validated configuration.

        Raises:
            ValueError: when trying to drop a critical parameter (e.g. messages).
        """
        forbidden = [p for p in self.drop_params if p not in _DROPPABLE_PARAMS]
        if forbidden:
            allowed = sorted(_DROPPABLE_PARAMS)
            raise ValueError(
                f"drop_params contains forbidden entries {forbidden}; "
                f"allowed: {allowed}"
            )
        return self

    @model_validator(mode="after")
    def _validate_api_flavor(self) -> ProviderCfg:
        """Forbid ``api_flavor="responses"`` outside of openai-translate.

        The Responses API requires request translation; passthrough forwards
        byte-for-byte and can't use it.

        Returns:
            The validated configuration.

        Raises:
            ValueError: when ``api_flavor="responses"`` is set on a
                passthrough provider.
        """
        if self.api_flavor == "responses" and self.type != "openai-translate":
            raise ValueError(
                f"api_flavor='responses' requires type='openai-translate', "
                f"got type='{self.type}'"
            )
        return self

    @model_validator(mode="after")
    def _validate_max_tokens_limit(self) -> ProviderCfg:
        """Forbid a missing ``max_tokens_limit`` on openai-translate.

        The old 4096 default applied silently and only showed up as long
        responses getting cut off. This is especially dangerous for
        reasoning upstreams: reasoning tokens burn through the budget before
        any visible text starts. A required explicit value catches the
        mistake at startup. ``passthrough`` forwards the request
        byte-for-byte without conversion, so the limit doesn't apply there.

        Returns:
            The validated configuration.

        Raises:
            ValueError: if an openai-translate provider didn't set
                ``max_tokens_limit``.
        """
        if self.type == "openai-translate" and self.max_tokens_limit is None:
            raise ValueError(
                "provider type='openai-translate' requires 'max_tokens_limit' "
                "(omitted value silently caps responses; passthrough is exempt)"
            )
        return self


class DefaultRoute(BaseModel):
    """The default route for an unmatched model."""

    provider: str


class RoutingConfig(BaseModel):
    """The root model for ``routing.yaml``."""

    version: Literal[1]
    providers: dict[str, ProviderCfg]
    rules: list[RoutingRule]
    default: DefaultRoute

    @model_validator(mode="after")
    def _validate_references(self) -> RoutingConfig:
        """Check referential integrity and safety invariants.

        Returns:
            The validated configuration.

        Raises:
            ValueError: on a broken provider reference, a missing key on an
                openai-translate provider, an invalid regex, or a default
                that isn't passthrough.
        """
        for name, cfg in self.providers.items():
            if cfg.type == "openai-translate" and not cfg.api_key_env:
                raise ValueError(
                    f"provider '{name}': openai-translate requires 'api_key_env'"
                )

        for rule in self.rules:
            if rule.provider not in self.providers:
                raise ValueError(
                    f"rule -> unknown provider '{rule.provider}'"
                )
            if rule.match.type == "regex":
                try:
                    re.compile(rule.match.value)
                except re.error as exc:
                    raise ValueError(
                        f"invalid regex '{rule.match.value}': {exc}"
                    ) from exc

        if self.default.provider not in self.providers:
            raise ValueError(
                f"default -> unknown provider '{self.default.provider}'"
            )
        if self.providers[self.default.provider].type != "passthrough":
            raise ValueError(
                "default.provider must be of type 'passthrough' "
                "(orchestrator must never fall back to a fleet model)"
            )
        return self
