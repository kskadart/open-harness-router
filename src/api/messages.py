"""Endpoint ``POST /v1/messages`` dispatching by model name."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from api.adapters import parse_model, to_fastapi_response
from dependencies import LoggerDep, RegistryDep

router = APIRouter()


@router.post("/v1/messages")
async def create_message(
    http_request: Request,
    registry: RegistryDep,
    logger: LoggerDep,
) -> Response:
    """Resolve the provider by model and pass the request to it.

    Args:
        http_request: the incoming request (raw body and headers).
        registry: the provider registry.
        logger: the request logger.

    Returns:
        The provider's response (regular or streaming).
    """
    raw_body = await http_request.body()
    model = parse_model(raw_body)
    if isinstance(model, Response):
        return model

    decision = registry.resolve(model)
    logger.info(
        "route",
        endpoint="messages",
        model=model,
        provider=decision.provider.name,
        upstream_model=decision.upstream_model,
    )
    # http_request structurally satisfies ClientChannel (see providers.base):
    # the provider only needs is_disconnected(), not the whole ASGI request.
    result = await decision.provider.handle_messages(
        raw_body, http_request.headers, http_request, decision.upstream_model, decision.limits
    )
    return to_fastapi_response(result)
