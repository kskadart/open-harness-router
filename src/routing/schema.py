"""Pydantic schema for the ``routing.yaml`` routing file.

Describes providers, model-matching rules, and the default route. The
validator guarantees referential integrity and key safety invariants: the
default route must lead to a passthrough provider with
``forward_client_auth: true``, so the orchestrator never falls back to a
fleet model or a third-party vendor billed on its own key.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from const import CONTEXT_WINDOW_RESERVE_TOKENS, MIN_USEFUL_COMPLETION_TOKENS
from routing.matcher import match_model

ProviderType = Literal["passthrough", "openai-translate"]
MatchType = Literal["exact", "prefix", "contains", "regex"]
TokenParam = Literal["max_tokens", "max_completion_tokens", "max_output_tokens"]
ApiFlavor = Literal["chat", "responses"]
ReasoningEffort = Literal["none", "low", "medium", "high", "xhigh", "max"]
AuthHeaderStyle = Literal["bearer", "x-api-key"]

# Top-level OpenAI request parameters allowed to be dropped. messages/model
# are intentionally excluded: dropping them breaks the request's semantics.
_DROPPABLE_PARAMS: frozenset[str] = frozenset({"temperature", "top_p", "stop"})


class MatchRule(BaseModel):
    """A model-name matching rule."""

    type: MatchType
    value: str


class RoutingRule(BaseModel):
    """A routing rule: match -> provider (+ model substitution and limits).

    ``client_models`` lists the exact model ids a client may send for this
    rule -- the rows the ``/model`` picker offers and the values that go
    into an agent's ``model:`` frontmatter. It exists because a
    ``prefix``/``contains``/``regex`` match value is a pattern, not a usable
    model id: a rule matching ``prefix: "gpt-"`` cannot advertise itself as
    ``gpt-``, which would reach the upstream verbatim and come back a 404.
    An ``exact`` rule needs no list -- its match value already is the id,
    and is checked as one. Empty by default;
    ``RoutingConfig._validate_client_models`` checks that every advertised
    id really lands on this rule under first-match-wins.

    ``max_tokens_limit`` and ``context_window`` override the provider's own
    values for the models this rule serves; ``None`` (the default) keeps the
    provider's. One gateway usually hosts several models whose limits differ
    -- one clamps an oversized completion budget, the next answers HTTP 500;
    one is served on a 223K window, the next on a 1M one. Without the
    override each of them needs its own provider block duplicating the same
    ``base_url``, ``api_key_env``, ``ca_bundle`` and ``extra_headers``. Both
    are rejected on a rule pointing at ``passthrough`` (no conversion, no
    pre-flight -- ``RoutingConfig._validate_rule_limits``), which also holds
    the provider's own invariant for the EFFECTIVE pair.
    """

    match: MatchRule
    provider: str
    upstream_model: str | None = None
    max_tokens_limit: int | None = None
    context_window: int | None = None
    client_models: list[str] = Field(default_factory=list)


def advertised_model_ids(rule: RoutingRule) -> list[str]:
    """Return the model ids a client may send for one rule.

    The single source of truth for what a rule offers: the validator checks
    exactly the ids ``cli.sync_client_config`` writes into the picker.

    Args:
        rule: the routing rule.

    Returns:
        The rule's ``client_models``, or the match value of an ``exact``
        rule when the list is empty; an empty list for a
        ``prefix``/``contains``/``regex`` rule that names no ids -- its
        match value is a pattern and cannot be sent as a model id.
    """
    if rule.client_models:
        return list(rule.client_models)
    return [rule.match.value] if rule.match.type == "exact" else []


def minimum_context_window(max_tokens_limit: int) -> int:
    """Return the smallest window the pre-flight can serve a request under.

    The guard in ``providers.openai_translate`` subtracts
    ``CONTEXT_WINDOW_RESERVE_TOKENS`` from the window and rejects a request
    that cannot still carry ``MIN_USEFUL_COMPLETION_TOKENS``, so a window
    merely larger than the completion cap would reject every request. This
    check uses the same floor as the guard, so a configuration that starts
    can serve at least one request.

    Args:
        max_tokens_limit: the effective cap on the completion budget.

    Returns:
        The value ``context_window`` must exceed.
    """
    return max_tokens_limit + CONTEXT_WINDOW_RESERVE_TOKENS + MIN_USEFUL_COMPLETION_TOKENS


@dataclass(frozen=True, slots=True)
class RouteLimits:
    """The token limits one resolved route runs under.

    Carried from ``ProviderRegistry.resolve`` to the provider instead of two
    loose ints: a provider serves several rules, so neither number can be
    read off ``self.cfg`` at request time any more.

    Attributes:
        max_tokens_limit: the effective cap on the outgoing completion
            budget; ``None`` only for ``passthrough`` (no conversion).
        context_window: the effective total window, or ``None`` when
            neither the rule nor the provider declares one (no pre-flight).
    """

    max_tokens_limit: int | None
    context_window: int | None

    @classmethod
    def resolve(cls, provider: ProviderCfg, rule: RoutingRule | None) -> RouteLimits:
        """Fold a rule's overrides onto the provider's defaults.

        Args:
            provider: configuration of the provider the route lands on.
            rule: the matched rule, or ``None`` for the default route.

        Returns:
            The limits this route's requests are built and checked against.
        """
        if rule is None:
            return cls(provider.max_tokens_limit, provider.context_window)
        return cls(
            provider.max_tokens_limit
            if rule.max_tokens_limit is None
            else rule.max_tokens_limit,
            provider.context_window if rule.context_window is None else rule.context_window,
        )


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
    conversion) and stays ``None`` there. Serves as the default for every
    rule on this provider; a rule may override it for its own models
    (``RoutingRule.max_tokens_limit``).

    ``context_window`` -- the upstream model's total context size in
    tokens (prompt plus completion). When set on an ``openai-translate``
    provider, the converted request is estimated before dispatch: a prompt
    that leaves less than ``MIN_USEFUL_COMPLETION_TOKENS`` of the window
    (minus ``CONTEXT_WINDOW_RESERVE_TOKENS``) is rejected with an Anthropic-shaped
    ``invalid_request_error`` carrying ``capability_rejected:
    prompt_too_long``, otherwise the completion budget is clamped so the
    two fit. Schema-optional, but required in practice for any model the
    client can pick: ``cli.sync_client_config`` disables the client's own
    window enforcement, so this pre-flight is the only guard left. Must
    exceed ``max_tokens_limit`` plus ``CONTEXT_WINDOW_RESERVE_TOKENS`` plus
    ``MIN_USEFUL_COMPLETION_TOKENS`` (``minimum_context_window``) -- a window the
    completion cap, the reserve and the minimum completion together can
    fill leaves no room for any prompt. Not applicable to ``passthrough`` (no
    conversion, no estimate): an explicit value there is a startup error.
    Like ``max_tokens_limit``, it is the default for every rule on this
    provider and may be overridden per rule
    (``RoutingRule.context_window``).

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

    ``forward_client_auth`` -- passthrough auth mode; only applies to
    ``passthrough`` -- an explicit value on ``openai-translate`` (which
    always uses its own ``api_key_env``) is a startup error, not a silent
    no-op. Defaults to ``true``: before this field had any effect, every
    passthrough provider unconditionally forwarded the client's headers, so
    the default preserves that behavior for configs that don't set it
    explicitly. ``true`` forwards the client's own credentials unchanged
    (native Anthropic, billed through the Claude Code subscription).
    ``false`` strips the client's ``authorization``/``x-api-key`` and
    injects the provider's own key from ``api_key_env`` instead, so a
    third-party Anthropic-compatible upstream never sees the client's OAuth
    token -- Anthropic bans using a subscription credential with
    third-party products (Feb 2026 policy update).

    ``auth_header`` -- which header carries the injected key when
    ``forward_client_auth=false``: ``bearer`` sets
    ``authorization: Bearer <key>``, ``x-api-key`` sets the raw key in
    ``x-api-key``. Third-party Anthropic-compatible upstreams disagree on
    this (e.g. Kimi's Moonshot-branded endpoint expects Bearer, its Claude
    Code subscription endpoint expects x-api-key). An explicit value
    anywhere else (passthrough with ``forward_client_auth=true``, or
    ``openai-translate``) is a startup error, same reasoning as
    ``forward_client_auth`` above.

    ``tls_verify_hostname`` -- when false, the outbound TLS connection
    skips only the hostname match against the leaf certificate; the
    certificate chain is still FULLY verified against ``ca_bundle`` (or the
    system roots): ``check_hostname=False`` with ``verify_mode`` kept at
    ``CERT_REQUIRED``, never ``verify=False``. Exists for internal dev
    gateways whose leaf certificate does not cover their FQDN.

    ``ca_bundle`` and ``extra_headers`` apply to both provider types:
    ``ca_bundle`` verifies the upstream against a private CA (a
    corporate/self-hosted Anthropic-compatible gateway); ``extra_headers``
    is merged in after this provider's own auth handling on EITHER type
    (client forwarding or own-key injection for passthrough; the
    ``api_key_env``-derived key for openai-translate) and may not name
    ``authorization``/``x-api-key`` -- on either type, that would silently
    override the auth header/key this provider forwards, injects, or
    resolves.
    """

    type: ProviderType
    base_url: str
    api_key_env: str | None = None
    ca_bundle: str | None = None
    tls_verify_hostname: bool = True
    timeout_s: int = 120
    stream_read_timeout_s: int = 600
    extra_headers: dict[str, str] = Field(default_factory=dict)
    forward_client_auth: bool = True
    auth_header: AuthHeaderStyle = "bearer"
    token_param: TokenParam = "max_tokens"
    drop_params: list[str] = Field(default_factory=list)
    max_tokens_limit: int | None = None
    context_window: int | None = None
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

    @model_validator(mode="after")
    def _validate_context_window(self) -> ProviderCfg:
        """Reject ``context_window`` where it has no effect or cannot hold a prompt.

        The pre-flight estimate only runs on ``openai-translate``
        (``providers/openai_translate.py``); passthrough forwards the body
        byte-for-byte and never sees a token count, so an explicit value
        there is the same "configured value that quietly does nothing"
        trap ``_validate_passthrough_auth`` eliminates. ``model_fields_set``
        distinguishes an explicit value from the ``None`` default. A window
        not greater than ``max_tokens_limit`` would reject every request
        (the completion cap alone fills it), so it is a startup error too.

        Returns:
            The validated configuration.

        Raises:
            ValueError: ``context_window`` explicitly set on a
                non-openai-translate provider, or not greater than
                ``max_tokens_limit``.
        """
        if "context_window" not in self.model_fields_set:
            return self
        if self.type != "openai-translate":
            raise ValueError(
                "context_window only applies to openai-translate providers; "
                f"remove it (type='{self.type}' forwards the request "
                "byte-for-byte and never estimates tokens)"
            )
        if (
            self.context_window is not None
            and self.max_tokens_limit is not None
            and self.context_window <= minimum_context_window(self.max_tokens_limit)
        ):
            raise ValueError(
                "context_window must be greater than max_tokens_limit plus the "
                "pre-flight reserve plus the minimum completion budget (got "
                f"context_window={self.context_window}, "
                f"max_tokens_limit={self.max_tokens_limit}, "
                f"reserve={CONTEXT_WINDOW_RESERVE_TOKENS}, "
                f"minimum useful completion={MIN_USEFUL_COMPLETION_TOKENS}); the three "
                "together would fill the window and every prompt would be "
                "rejected"
            )
        return self

    @model_validator(mode="after")
    def _validate_passthrough_auth(self) -> ProviderCfg:
        """Enforce that ``forward_client_auth`` and ``api_key_env`` never disagree.

        ``forward_client_auth=false`` means the router injects its own key
        instead of the client's credentials -- without ``api_key_env`` this
        would either fail at request time (no key to inject) or, worse,
        silently keep forwarding the client's Claude Code OAuth token to a
        third-party upstream. ``forward_client_auth=true`` means the
        client's own credentials are forwarded unchanged; an ``api_key_env``
        set alongside it used to be silently ignored (``providers/factory.py``
        returned before key resolution) -- that trap is now a startup error
        instead.

        Only applies to ``passthrough``: ``openai-translate`` always
        requires its own ``api_key_env`` regardless of this field (checked
        separately in ``RoutingConfig._validate_references``), so an
        explicit ``forward_client_auth`` there is rejected too -- same
        "configured value that quietly does nothing" trap.

        Returns:
            The validated configuration.

        Raises:
            ValueError: ``forward_client_auth`` explicitly set on a
                non-passthrough provider; passthrough with
                ``forward_client_auth=false`` and no ``api_key_env``; or
                passthrough with ``forward_client_auth=true`` and an
                ``api_key_env`` set.
        """
        if self.type != "passthrough":
            if "forward_client_auth" in self.model_fields_set:
                raise ValueError(
                    "forward_client_auth only applies to passthrough "
                    f"providers; remove it (type='{self.type}' always uses "
                    "its own api_key_env, forward_client_auth has no effect)"
                )
            return self
        if not self.forward_client_auth and not self.api_key_env:
            raise ValueError(
                "passthrough with forward_client_auth=false requires "
                "'api_key_env' (the router injects this key in place of "
                "the client's own credentials); if you meant to forward "
                "the client's own credentials instead, set "
                "forward_client_auth=true (or remove the field -- it "
                "now defaults to true)"
            )
        if self.forward_client_auth and self.api_key_env:
            raise ValueError(
                "passthrough with forward_client_auth=true must not set "
                "'api_key_env' (the client's own credentials are "
                "forwarded; an own key here would be silently ignored); if "
                "you meant to configure an own-key third-party upstream "
                "instead, set forward_client_auth=false"
            )
        return self

    @model_validator(mode="after")
    def _validate_auth_header_usage(self) -> ProviderCfg:
        """Reject an explicitly set ``auth_header`` where it has no effect.

        ``auth_header`` only matters for passthrough with
        ``forward_client_auth=false`` -- it picks which header the injected
        own key goes into. Setting it anywhere else (passthrough with
        ``forward_client_auth=true``, or ``openai-translate``) is exactly
        the "configured value that quietly does nothing" trap
        ``_validate_passthrough_auth`` already eliminates for
        ``api_key_env``; ``model_fields_set`` distinguishes an explicit
        value from the ``bearer`` default, so only the former is rejected.

        The two disqualifying conditions (wrong provider type vs. wrong
        passthrough mode) get separate messages: a shared message built
        from ``self.type != "passthrough" or self.forward_client_auth``
        would tell an ``openai-translate`` user to "set
        forward_client_auth=false", which is impossible to act on -- that
        field is rejected outright on ``openai-translate``
        (``_validate_passthrough_auth``).

        Returns:
            The validated configuration.

        Raises:
            ValueError: ``auth_header`` is explicitly set on a
                non-passthrough provider, or on passthrough with
                ``forward_client_auth=true``.
        """
        if "auth_header" not in self.model_fields_set:
            return self
        if self.type != "passthrough":
            raise ValueError(
                "auth_header only applies to passthrough providers; remove "
                f"it (type='{self.type}' always uses its own api_key_env "
                "directly, with no header-style choice)"
            )
        if self.forward_client_auth:
            raise ValueError(
                "auth_header has no effect on passthrough with "
                "forward_client_auth=true (the client's own credentials "
                "are forwarded, not an injected key); remove it, or set "
                "forward_client_auth=false with 'api_key_env' if an "
                "own-key upstream was intended"
            )
        return self

    @model_validator(mode="after")
    def _validate_extra_headers_auth_collision(self) -> ProviderCfg:
        """Forbid ``extra_headers`` from naming the auth header, on EITHER provider type.

        ``passthrough`` merges ``extra_headers`` in AFTER its own auth
        handling (``providers/passthrough.py`` ``_build_headers``):
        forwarding the client's credentials, or injecting the provider's own
        key under an allowlist. If ``extra_headers`` also names
        ``authorization``/``x-api-key`` it would silently override that
        header -- the client's forwarded credential, or the provider's own
        injected key -- with no startup error and no runtime signal.

        ``openai-translate`` has the exact same trap, verified against the
        installed OpenAI SDK (``openai/_base_client.py``): ``extra_headers``
        is passed as ``default_headers`` to ``AsyncOpenAI``, and
        ``_build_headers`` there computes
        ``{**self._auth_headers(...), **self.default_headers}`` --
        ``default_headers`` (which folds in our ``extra_headers`` last) is
        spread AFTER ``_auth_headers`` (the ``api_key_env``-derived
        ``Authorization`` header), so an ``extra_headers["Authorization"]``
        silently wins over the configured key there too.

        Returns:
            The validated configuration.

        Raises:
            ValueError: ``extra_headers`` sets ``authorization`` or
                ``x-api-key`` (case-insensitively).
        """
        conflicting = {name.lower() for name in self.extra_headers} & {
            "authorization",
            "x-api-key",
        }
        if conflicting:
            raise ValueError(
                f"extra_headers must not set {sorted(conflicting)} (merged "
                "in after this provider's own auth handling on both "
                "passthrough and openai-translate, so it would silently "
                "override the auth header/key this provider forwards or "
                "injects); use a header name that doesn't collide"
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
                openai-translate provider, an invalid regex, an
                ``upstream_model`` on a rule pointing at a passthrough
                provider, or a default that isn't a passthrough provider
                forwarding the client's own credentials.
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
            rule_provider_type = self.providers[rule.provider].type
            if rule.upstream_model is not None and rule_provider_type == "passthrough":
                raise ValueError(
                    f"rule -> provider '{rule.provider}': upstream_model is not "
                    "supported on passthrough (byte-for-byte proxying forwards "
                    "the request body, including the model field, unchanged and "
                    "cannot rewrite it; drop upstream_model or route to an "
                    "openai-translate provider)"
                )

        if self.default.provider not in self.providers:
            raise ValueError(
                f"default -> unknown provider '{self.default.provider}'"
            )
        default_cfg = self.providers[self.default.provider]
        if default_cfg.type != "passthrough" or not default_cfg.forward_client_auth:
            raise ValueError(
                "default.provider must be passthrough with "
                "forward_client_auth=true (orchestrator must never fall "
                "back to a fleet model or a third-party vendor billed on "
                "its own key)"
            )
        return self

    @model_validator(mode="after")
    def _validate_rule_limits(self) -> RoutingConfig:
        """Check the per-rule limit overrides against their provider.

        Runs after ``_validate_references``, which has already rejected a
        rule naming an unknown provider. Two invariants, both startup
        errors for the same reason the provider-level ones are: a value
        that quietly does nothing, or a pair no request can satisfy, is
        only visible at runtime as a route that always fails.

        Returns:
            The validated configuration.

        Raises:
            ValueError: an override on a rule pointing at a passthrough
                provider, or an effective ``context_window`` not greater
                than the effective ``max_tokens_limit``.
        """
        for position, rule in enumerate(self.rules):
            provider = self.providers[rule.provider]
            overridden = [
                name
                for name in ("max_tokens_limit", "context_window")
                if getattr(rule, name) is not None
            ]
            if overridden and provider.type == "passthrough":
                raise ValueError(
                    f"rule #{position + 1} ('{rule.match.value}') -> provider "
                    f"'{rule.provider}': {overridden} not supported on "
                    "passthrough (the request body is forwarded byte-for-byte, "
                    "so neither the completion cap nor the context pre-flight "
                    "ever runs and the value would quietly do nothing); drop "
                    "the override or route to an openai-translate provider"
                )
            limits = RouteLimits.resolve(provider, rule)
            if (
                limits.context_window is not None
                and limits.max_tokens_limit is not None
                and limits.context_window
                <= minimum_context_window(limits.max_tokens_limit)
            ):
                raise ValueError(
                    f"rule #{position + 1} ('{rule.match.value}') -> provider "
                    f"'{rule.provider}': effective context_window must be "
                    "greater than effective max_tokens_limit plus the "
                    "pre-flight reserve plus the minimum completion budget "
                    f"(got context_window={limits.context_window}, "
                    f"max_tokens_limit={limits.max_tokens_limit}, "
                    f"reserve={CONTEXT_WINDOW_RESERVE_TOKENS}, "
                    f"minimum useful completion={MIN_USEFUL_COMPLETION_TOKENS}, rule "
                    "overrides folded onto the provider's values); the three "
                    "together would fill the window and every prompt would be "
                    "rejected"
                )
        return self

    @model_validator(mode="after")
    def _validate_client_models(self) -> RoutingConfig:
        """Check that every advertised model id really lands on its own rule.

        Lives on ``RoutingConfig`` rather than ``RoutingRule`` because the
        answer depends on rule ORDER: resolution is first-match-wins
        (``routing/registry.py``), so an id an earlier rule also matches
        never reaches the rule that advertises it. Both failure modes are
        silent at runtime -- the client picks the row and the request lands
        on another provider, or on the default route -- so they are startup
        errors instead.

        Returns:
            The validated configuration.

        Raises:
            ValueError: an id listed by more than one rule, an id its own
                rule's match does not accept, or an id captured by an
                earlier rule.
        """
        owner_of: dict[str, int] = {}
        for position, rule in enumerate(self.rules):
            for model in advertised_model_ids(rule):
                if model in owner_of:
                    raise ValueError(
                        f"model id '{model}' is listed by rule "
                        f"#{owner_of[model] + 1} and rule #{position + 1}; a "
                        "model id belongs to exactly one rule"
                    )
                owner_of[model] = position
                if not match_model(rule.match, model):
                    raise ValueError(
                        f"rule #{position + 1} ({rule.match.type} "
                        f"'{rule.match.value}') offers model id "
                        f"'{model}', which its own match does not accept; the "
                        "client would be offered a model this rule never "
                        "receives"
                    )
                shadow = next(
                    (
                        earlier
                        for earlier in range(position)
                        if match_model(self.rules[earlier].match, model)
                    ),
                    None,
                )
                if shadow is not None:
                    raise ValueError(
                        f"model id '{model}' of rule "
                        f"#{position + 1} is captured by the earlier rule "
                        f"#{shadow + 1} ({self.rules[shadow].match.type} "
                        f"'{self.rules[shadow].match.value}'); first match "
                        f"wins, so the request never reaches rule "
                        f"#{position + 1}"
                    )
        return self
