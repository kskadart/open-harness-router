"""Endpoint ``POST /v1/messages/count_tokens``."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from api.adapters import parse_model, to_fastapi_response
from dependencies import LoggerDep, RegistryDep

router = APIRouter()


@router.post("/v1/messages/count_tokens")
async def count_tokens(
    http_request: Request,
    registry: RegistryDep,
    logger: LoggerDep,
) -> Response:
    """Count tokens: passthrough proxies it, fleet estimates locally.

    Args:
        http_request: the incoming request.
        registry: the provider registry.
        logger: the request logger.

    Returns:
        The provider's response with the input token count.
    """
    raw_body = await http_request.body()
    model = parse_model(raw_body)
    if isinstance(model, Response):
        return model

    decision = registry.resolve(model)
    logger.info(
        "route",
        endpoint="count_tokens",
        model=model,
        provider=decision.provider.name,
    )
    result = await decision.provider.count_tokens(
        raw_body, http_request.headers, decision.upstream_model
    )
    return to_fastapi_response(result)
