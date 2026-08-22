"""Domain exceptions and error mapping to Anthropic-compatible JSON.

The client always gets a body of the form
``{"type": "error", "error": {"type": ..., "message": ...}}`` with no
internal details or stack traces; details are written to the log.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from log import get_logger

logger = get_logger(__name__)


class ConfigError(Exception):
    """Error loading or validating routing configuration."""


class UpstreamError(Exception):
    """Error contacting the upstream provider.

    Args:
        message: human-readable description.
        status_code: HTTP status returned to the client.
        error_type: error type in Anthropic format.
    """

    def __init__(
        self,
        message: str,
        status_code: int = 502,
        error_type: str = "api_error",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_type = error_type


class ProviderError(UpstreamError):
    """Internal provider-level signal: client cancellation or upstream failure.

    Replaces using ``fastapi.HTTPException`` as an internal flow-control
    mechanism between ``OpenAITranslateProvider`` and the
    ``conversion.response_converter`` converters: HTTPException in FastAPI is
    meant to terminate the ASGI request, not to pass signals between
    provider functions outside the HTTP boundary (cooperative client
    cancellation 499, classifying an upstream failure before and mid-stream).
    Both modules catch it themselves and turn it into a ProviderResult (a
    JSON response or an SSE ``event: error`` frame) before it reaches the
    ASGI level; inheriting from UpstreamError keeps the same shape
    (status_code/message/error_type) in case the exception still goes
    uncaught -- then the already-registered UpstreamError handler kicks in
    (the same class is in its MRO).
    """


def anthropic_error_body(error_type: str, message: str) -> dict[str, Any]:
    """Build an error body in Anthropic format.

    Args:
        error_type: error type (``api_error``, ``invalid_request_error``, etc.).
        message: human-readable message.

    Returns:
        A dict with ``type`` and ``error`` fields.
    """
    return {"type": "error", "error": {"type": error_type, "message": message}}


def anthropic_error_type_for_status(status_code: int) -> str:
    """Map an HTTP status to an Anthropic error type for a mid-stream error event.

    Args:
        status_code: HTTP status of the upstream error.

    Returns:
        The Anthropic error type (``authentication_error``,
        ``rate_limit_error``, ``overloaded_error``, ``api_error``).
    """
    if status_code == 401:
        return "authentication_error"
    if status_code == 429:
        return "rate_limit_error"
    if status_code in {503, 529}:
        return "overloaded_error"
    return "api_error"


def stream_error_event(status_code: int, message: str) -> str:
    """Build an Anthropic SSE ``event: error`` frame for a post-start error.

    The frame is valid at any point in an Anthropic stream and is terminal,
    so it works both for the converter (which tracks block state) and for
    passthrough (byte-for-byte proxying without parsing SSE).

    Args:
        status_code: HTTP status of the upstream error.
        message: human-readable message.

    Returns:
        An SSE string ``event: error\\ndata: {...}\\n\\n``.
    """
    body = anthropic_error_body(anthropic_error_type_for_status(status_code), message)
    return f"event: error\ndata: {json.dumps(body, ensure_ascii=False)}\n\n"


def install_exception_handlers(app: FastAPI) -> None:
    """Register exception handlers on the application.

    Args:
        app: the FastAPI instance.
    """

    @app.exception_handler(UpstreamError)
    async def _handle_upstream(_: Request, exc: UpstreamError) -> JSONResponse:
        logger.error("upstream_error", message=exc.message, status=exc.status_code)
        return JSONResponse(
            status_code=exc.status_code,
            content=anthropic_error_body(exc.error_type, exc.message),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unexpected_error", error=str(exc))
        return JSONResponse(
            status_code=500,
            content=anthropic_error_body("api_error", "Internal router error"),
        )
