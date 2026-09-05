"""Unified routing provider contract.

A provider receives the original Claude Code request (Anthropic format) and
returns a response in the same format. Implementations: passthrough
(reverse proxy to Anthropic) and openai-translate (translation to OpenAI and
back).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from routing.schema import ProviderCfg, RouteLimits


@runtime_checkable
class ClientChannel(Protocol):
    """Client channel contract, visible to the provider outside of the HTTP transport.

    Providers and response converters really only need one operation --
    checking for a mid-stream connection drop for cooperative cancellation.
    ``fastapi.Request`` structurally satisfies the protocol (it has the same
    async method), so it can be passed as-is from an ASGI handler without an
    explicit cast. When a provider is invoked outside ASGI (raw-socket
    forward-proxy, incoming Responses endpoint), any other implementation of
    this protocol works just as well.
    """

    async def is_disconnected(self) -> bool:
        """Return True if the client dropped the connection."""
        ...


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """Provider response, independent of ASGI/FastAPI.

    The provider returns its result in this form instead of
    ``fastapi.Response``, so it can be called outside the ASGI cycle
    (raw-socket forward-proxy, incoming Responses endpoint). Adaptation into
    ``fastapi.Response`` / ``StreamingResponse`` happens at the API boundary
    (``api/adapters.py``) -- the only place where the provider layer touches
    ASGI again.

    Attributes:
        status_code: HTTP status of the response.
        headers: response headers, including ``content-type``;
            ``content-length`` is not among them -- the transport recomputes
            it.
        body: response body: ready-made bytes for a regular response, or an
            async byte stream for an SSE stream.
    """

    status_code: int
    headers: dict[str, str]
    body: bytes | AsyncIterator[bytes]


@runtime_checkable
class Provider(Protocol):
    """Upstream provider contract.

    ``upstream_model`` and ``limits`` come from the routing decision rather
    than from ``cfg``: one provider serves every rule pointing at it, and a
    rule may rename the model and override the provider's token limits for
    the models it serves (``routing/registry.py``, ``RouteLimits``).
    """

    name: str
    cfg: ProviderCfg

    async def handle_messages(
        self,
        raw_body: bytes,
        client_headers: Mapping[str, str],
        client_channel: ClientChannel,
        upstream_model: str | None,
        limits: RouteLimits,
    ) -> ProviderResult:
        """Handle a ``/v1/messages`` request and return the response to the client."""
        ...

    async def count_tokens(
        self,
        raw_body: bytes,
        client_headers: Mapping[str, str],
        upstream_model: str | None,
        limits: RouteLimits,
    ) -> ProviderResult:
        """Handle a ``/v1/messages/count_tokens`` request."""
        ...

    async def aclose(self) -> None:
        """Close the provider's network clients."""
        ...
