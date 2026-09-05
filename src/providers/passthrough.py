"""Passthrough provider: byte-for-byte reverse proxy to an Anthropic-compatible upstream.

The request body is proxied unchanged; how the auth headers are handled
depends on ``cfg.forward_client_auth``: forwarded as-is for native Anthropic
(billed on the client's subscription), or replaced with the provider's own
key for a third-party upstream, under an allowlist rather than the client's
full header set (see ``_build_headers``). Streaming is served via
``aiter_raw`` without decompression, so the ``content-encoding`` header is
forwarded to the client as-is.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from pathlib import Path

import httpx
from pydantic import SecretStr

from const import (
    UPSTREAM_REQUEST_FAILED_MESSAGE,
    UPSTREAM_REQUEST_TIMEOUT_MESSAGE,
    UPSTREAM_STREAM_INTERRUPTED_MESSAGE,
)
from errors import UpstreamError, stream_error_event
from log import get_logger
from providers.base import ClientChannel, ProviderResult
from routing.schema import ProviderCfg, RouteLimits
from services.header_utils import (
    forward_headers,
    merge_extra_headers,
    own_key_headers,
    response_headers,
)
from services.http_transport import build_upstream_transport, build_upstream_verify
from services.retry import retry_connect
from settings import UpstreamSettings

logger = get_logger(__name__)

_MESSAGES_PATH = "/v1/messages"
_COUNT_TOKENS_PATH = "/v1/messages/count_tokens"

# Statuses for upstream transport failures: the client sees a gateway error
# rather than a router 500, and can tell upstream unavailability apart from
# timeout exhaustion by the status code.
_BAD_GATEWAY = 502
_GATEWAY_TIMEOUT = 504


def _upstream_transport_error(exc: httpx.HTTPError | httpx.StreamError) -> UpstreamError:
    """Convert an httpx transport failure into an UpstreamError without internal details.

    Args:
        exc: httpx exception raised while contacting the upstream.

    Returns:
        Domain error with a neutral message: 504 for timeouts, 502 otherwise.
    """
    if isinstance(exc, httpx.TimeoutException):
        return UpstreamError(UPSTREAM_REQUEST_TIMEOUT_MESSAGE, status_code=_GATEWAY_TIMEOUT)
    return UpstreamError(UPSTREAM_REQUEST_FAILED_MESSAGE, status_code=_BAD_GATEWAY)


# Only connection-establishment failures are safe to retry: the request has
# not reached the upstream and no response byte has reached the client. A
# drop of an already-open connection does not qualify -- a retry would
# splice two different generations together.
_RETRYABLE_CONNECT_ERRORS = (httpx.ConnectTimeout, httpx.ConnectError)


class PassthroughProvider:
    """Reverse proxy to an Anthropic-compatible upstream without protocol translation."""

    def __init__(
        self,
        name: str,
        cfg: ProviderCfg,
        upstream: UpstreamSettings,
        api_key: SecretStr | None,
        ca_bundle_path: Path | None,
    ) -> None:
        """Create the provider and its long-lived httpx client.

        ``api_key`` and ``ca_bundle_path`` have no default: every call site
        must decide explicitly whether this provider forwards the client's
        credentials (pass ``api_key=None``) or injects its own, and whether
        it verifies the upstream against a private CA. A default would let
        a future call site that forgets to wire either in type-check
        cleanly and only fail at request time, inside the handler, as an
        unexplained 500 -- instead of at startup.

        ``(api_key is None) == cfg.forward_client_auth`` is documented as an
        invariant in the schema validator and in ``factory.py``, but until
        now was never actually checked here -- a future call site
        (including a test fixture) that got the two out of sync would build
        successfully and only misbehave at request time. Checked eagerly
        below instead.

        Args:
            name: provider name in the registry.
            cfg: provider configuration.
            upstream: outbound connection settings (IPv4 binding, pool
                limits).
            api_key: the provider's own key, resolved by ``factory.py`` from
                ``cfg.api_key_env``, or ``None`` to forward the client's own
                credentials instead. Must be ``None`` exactly when
                ``cfg.forward_client_auth`` is true.
            ca_bundle_path: path to a CA bundle for verifying the upstream's
                certificate (a corporate/self-hosted Anthropic-compatible
                gateway behind a private CA), resolved and validated by
                ``factory.py`` from ``cfg.ca_bundle``, or ``None`` for the
                system's trusted roots.

        Raises:
            ValueError: ``api_key`` and ``cfg.forward_client_auth`` disagree.
        """
        if (api_key is None) != cfg.forward_client_auth:
            raise ValueError(
                f"provider '{name}': api_key must be provided if and only if "
                "cfg.forward_client_auth is false (schema validation and "
                "factory.py are supposed to guarantee this together; a "
                "call site disagreed)"
            )
        self.name = name
        self.cfg = cfg
        self._api_key = api_key
        # Precomputed once, not per request: which header the own key goes
        # into, and how to format it (raw value for x-api-key, Bearer-
        # prefixed for authorization). Only consulted when self._api_key is
        # not None; harmless to compute unconditionally otherwise. The key
        # itself stays a SecretStr and is only formatted to plaintext inside
        # _build_headers, per request, not cached here.
        self._own_key_header_name: str
        self._format_own_key_value: Callable[[SecretStr], str]
        if cfg.auth_header == "x-api-key":
            self._own_key_header_name = "x-api-key"
            self._format_own_key_value = lambda key: key.get_secret_value()
        else:
            self._own_key_header_name = "authorization"
            self._format_own_key_value = lambda key: f"Bearer {key.get_secret_value()}"
        self._retry_delays = upstream.retry_backoff_s
        timeout = httpx.Timeout(
            connect=upstream.connect_timeout_s,
            read=float(cfg.stream_read_timeout_s),
            write=60.0,
            pool=10.0,
        )
        verify = build_upstream_verify(ca_bundle_path, cfg.tls_verify_hostname)
        self._client = httpx.AsyncClient(
            base_url=cfg.base_url,
            timeout=timeout,
            transport=build_upstream_transport(upstream, verify),
        )
        # Visible at startup, not only on the invoice: which credential
        # actually goes out on the wire for this provider. Never logs the
        # key itself, only which mode is in effect.
        logger.info(
            "passthrough_auth_mode",
            provider=name,
            own_key=api_key is not None,
            auth_header=cfg.auth_header if api_key is not None else None,
        )

    async def _send_with_retry(
        self, attempt: Callable[[], Awaitable[httpx.Response]]
    ) -> httpx.Response:
        """Call the upstream, retrying connection-establishment failures.

        Args:
            attempt: coroutine performing a single upstream call.

        Returns:
            The upstream response of the first successful attempt.

        Raises:
            httpx.HTTPError: a non-retryable failure, or retries exhausted.
        """
        return await retry_connect(
            attempt,
            delays=self._retry_delays,
            retryable=_RETRYABLE_CONNECT_ERRORS,
            on_retry=lambda exc, _attempt_no, delay: logger.warning(
                "passthrough_connect_retry",
                provider=self.name,
                error_type=type(exc).__name__,
                retry_in_s=delay,
            ),
        )

    @staticmethod
    def _is_stream(raw_body: bytes) -> bool:
        """Determine whether streaming was requested, from the request body."""
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            return False
        return bool(payload.get("stream", False)) if isinstance(payload, dict) else False

    def _build_headers(self, client_headers: Mapping[str, str]) -> dict[str, str]:
        """Build the outgoing request headers per the provider's auth mode.

        ``self._api_key is None`` (equivalently ``cfg.forward_client_auth``
        is true) forwards the client's own request headers unchanged --
        native Anthropic, the client's own vendor, billed through the
        client's subscription; narrowing this path risks breaking Claude
        Code features that ride uncommon headers.

        Otherwise the provider injects its own key and switches to an
        ALLOWLIST (``own_key_headers``) instead of ``forward_headers``'s
        denylist: the request goes to a vendor the client did not choose,
        so nothing beyond the documented protocol/negotiation headers and
        the provider's own key travels there -- not the client's own
        credentials, and not any OTHER credential the client happens to
        carry (a denylist over just authorization/x-api-key would miss,
        e.g., a session cookie). The header name and value formatter were
        precomputed once in ``__init__`` (``_own_key_header_name`` /
        ``_format_own_key_value``), so there is exactly one
        ``own_key_headers`` call here instead of one per ``auth_header``
        style.

        Branching on ``self._api_key`` rather than ``cfg.forward_client_auth``
        lets mypy narrow ``self._api_key`` to non-None in the own-key
        branch below, with no ``cast`` needed.

        ``cfg.extra_headers`` is merged in LAST, after auth handling in
        either branch, via ``merge_extra_headers`` -- case-insensitively:
        client headers arrive lowercased, while ``routing.yaml`` naturally
        uses canonical casing (e.g. ``User-Agent``), and a case-sensitive
        merge would leave both castings in the result, so httpx would send
        two conflicting raw header lines. It is provider config from
        ``routing.yaml``, not client-derived data, so merging it after the
        client-header filtering above cannot reintroduce anything the
        filtering just stripped -- it can only add or override headers a
        vendor needs beyond the fixed set (e.g. an OpenRouter-style
        ``HTTP-Referer``/``X-Title`` pair). The schema validator
        (``ProviderCfg._validate_extra_headers_auth_collision``) forbids
        ``extra_headers`` from naming ``authorization``/``x-api-key``
        itself, so it cannot silently override the auth header set above.

        Args:
            client_headers: the raw incoming request headers.

        Returns:
            The client's headers unchanged (forward_client_auth=true), or
            the own-key allowlist with the provider's own auth header set;
            either way, with ``cfg.extra_headers`` merged in.
        """
        if self._api_key is None:
            fwd = forward_headers(client_headers)
        else:
            fwd = own_key_headers(
                client_headers,
                self._own_key_header_name,
                self._format_own_key_value(self._api_key),
            )
        return merge_extra_headers(fwd, self.cfg.extra_headers)

    async def _proxy(
        self,
        path: str,
        raw_body: bytes,
        client_headers: Mapping[str, str],
        client_channel: ClientChannel,
    ) -> ProviderResult:
        """Proxy a request to the upstream (streaming or regular)."""
        fwd = self._build_headers(client_headers)
        if self._is_stream(raw_body):
            return await self._proxy_stream(path, raw_body, fwd, client_channel)
        return await self._proxy_unary(path, raw_body, fwd)

    async def _proxy_unary(
        self, path: str, raw_body: bytes, fwd: dict[str, str]
    ) -> ProviderResult:
        """Proxy a regular (non-streaming) request."""
        try:
            upstream = await self._send_with_retry(
                lambda: self._client.post(path, content=raw_body, headers=fwd)
            )
        except (httpx.HTTPError, httpx.StreamError) as exc:
            logger.error(
                "passthrough_connect_error",
                provider=self.name,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise _upstream_transport_error(exc) from exc
        headers = response_headers(upstream.headers)
        # httpx has already decompressed content, so content-encoding is invalid.
        headers.pop("content-encoding", None)
        return ProviderResult(
            status_code=upstream.status_code, headers=headers, body=upstream.content
        )

    async def _proxy_stream(
        self,
        path: str,
        raw_body: bytes,
        fwd: dict[str, str],
        client_channel: ClientChannel,
    ) -> ProviderResult:
        """Proxy a streaming request, preserving the SSE bytes."""
        try:
            # The request is rebuilt on every attempt: an httpx.Request is
            # single-use, its body cannot be sent twice.
            upstream = await self._send_with_retry(
                lambda: self._client.send(
                    self._client.build_request(
                        "POST", path, content=raw_body, headers=fwd
                    ),
                    stream=True,
                )
            )
        except (httpx.HTTPError, httpx.StreamError) as exc:
            # The response to the client has not started yet -> a regular
            # gateway error is returned.
            logger.error(
                "passthrough_stream_connect_error",
                provider=self.name,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise _upstream_transport_error(exc) from exc
        headers = response_headers(upstream.headers)

        async def body_iter() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_raw():
                    if await client_channel.is_disconnected():
                        break
                    yield chunk
            except (httpx.HTTPError, httpx.StreamError) as exc:
                # The upstream dropped mid-stream: the status and headers
                # have already been sent to the client and cannot change.
                # Passthrough does not parse SSE and does not know which
                # blocks are open, so there is nothing to assemble Anthropic
                # terminal events from; instead an ``event: error`` frame is
                # sent -- valid at any point in the stream, giving the client
                # a diagnosable signal instead of a TCP drop.
                logger.warning(
                    "passthrough_stream_aborted",
                    provider=self.name,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                if not await client_channel.is_disconnected():
                    yield stream_error_event(
                        _BAD_GATEWAY, UPSTREAM_STREAM_INTERRUPTED_MESSAGE
                    ).encode()
            finally:
                await upstream.aclose()

        return ProviderResult(
            status_code=upstream.status_code, headers=headers, body=body_iter()
        )

    async def handle_messages(
        self,
        raw_body: bytes,
        client_headers: Mapping[str, str],
        client_channel: ClientChannel,
        upstream_model: str | None,
        limits: RouteLimits,
    ) -> ProviderResult:
        """Proxy ``/v1/messages`` to the Anthropic-compatible upstream.

        ``upstream_model`` and ``limits`` are ignored here: the body is
        forwarded byte-for-byte, so there is no ``model`` field to rewrite
        and no converted payload to cap or estimate. The schema rejects both
        on a rule pointing at a passthrough provider, so neither can carry a
        value an operator expected to take effect.
        """
        return await self._proxy(_MESSAGES_PATH, raw_body, client_headers, client_channel)

    async def count_tokens(
        self,
        raw_body: bytes,
        client_headers: Mapping[str, str],
        upstream_model: str | None,
        limits: RouteLimits,
    ) -> ProviderResult:
        """Proxy ``/v1/messages/count_tokens`` to the upstream's own endpoint.

        ``upstream_model`` and ``limits`` are ignored for the same reason as
        in ``handle_messages``: the count comes from the vendor, over the
        body the client sent.
        """
        fwd = self._build_headers(client_headers)
        return await self._proxy_unary(_COUNT_TOKENS_PATH, raw_body, fwd)

    async def aclose(self) -> None:
        """Close the httpx client."""
        await self._client.aclose()
