"""Passthrough provider: byte-for-byte reverse proxy to native Anthropic.

The request body is proxied unchanged; the authorization and protocol
version headers are preserved. Streaming is served via ``aiter_raw`` without
decompression, so the ``content-encoding`` header is forwarded to the client
as-is.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping

import httpx

from const import (
    UPSTREAM_REQUEST_FAILED_MESSAGE,
    UPSTREAM_REQUEST_TIMEOUT_MESSAGE,
    UPSTREAM_STREAM_INTERRUPTED_MESSAGE,
)
from errors import UpstreamError, stream_error_event
from log import get_logger
from providers.base import ClientChannel, ProviderResult
from routing.schema import ProviderCfg
from services.header_utils import forward_headers, response_headers
from services.http_transport import build_upstream_transport
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
    """Reverse proxy to Anthropic without protocol translation."""

    def __init__(
        self, name: str, cfg: ProviderCfg, upstream: UpstreamSettings
    ) -> None:
        """Create the provider and its long-lived httpx client.

        Args:
            name: provider name in the registry.
            cfg: provider configuration.
            upstream: outbound connection settings (IPv4 binding, pool
                limits).
        """
        self.name = name
        self.cfg = cfg
        self._retry_delays = upstream.retry_backoff_s
        timeout = httpx.Timeout(
            connect=upstream.connect_timeout_s,
            read=float(cfg.stream_read_timeout_s),
            write=60.0,
            pool=10.0,
        )
        self._client = httpx.AsyncClient(
            base_url=cfg.base_url,
            timeout=timeout,
            transport=build_upstream_transport(upstream),
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

    async def _proxy(
        self,
        path: str,
        raw_body: bytes,
        client_headers: Mapping[str, str],
        client_channel: ClientChannel,
    ) -> ProviderResult:
        """Proxy a request to the upstream (streaming or regular)."""
        fwd = forward_headers(client_headers)
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
    ) -> ProviderResult:
        """Proxy ``/v1/messages`` to native Anthropic."""
        return await self._proxy(_MESSAGES_PATH, raw_body, client_headers, client_channel)

    async def count_tokens(
        self,
        raw_body: bytes,
        client_headers: Mapping[str, str],
        upstream_model: str | None,
    ) -> ProviderResult:
        """Proxy ``/v1/messages/count_tokens`` to native Anthropic."""
        fwd = forward_headers(client_headers)
        return await self._proxy_unary(_COUNT_TOKENS_PATH, raw_body, fwd)

    async def aclose(self) -> None:
        """Close the httpx client."""
        await self._client.aclose()
