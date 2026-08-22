"""Shared HTTP-boundary code for the ``/v1/messages*`` endpoints.

Parsing the request body and extracting the model name used to be
duplicated byte-for-byte between ``api/messages.py`` and
``api/count_tokens.py``; factored out here. This is also where the adapter
from the provider-neutral ``ProviderResult`` (see ``providers.base``) to
``fastapi.Response``/``StreamingResponse`` lives -- the only place where the
provider layer touches ASGI again on the router's way out.
"""

from __future__ import annotations

import json

from fastapi.responses import JSONResponse, Response, StreamingResponse

from errors import anthropic_error_body
from providers.base import ProviderResult


def parse_model(raw_body: bytes) -> str | JSONResponse:
    """Parse the request body and extract the model name.

    Args:
        raw_body: raw request body.

    Returns:
        The model name from the ``model`` field, or a ready-made 400
        JSONResponse for invalid JSON or a missing/empty ``model`` field.
    """
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return JSONResponse(
            status_code=400,
            content=anthropic_error_body("invalid_request_error", "Invalid JSON body"),
        )

    model = payload.get("model") if isinstance(payload, dict) else None
    if not isinstance(model, str) or not model:
        return JSONResponse(
            status_code=400,
            content=anthropic_error_body("invalid_request_error", "Missing 'model' field"),
        )
    return model


def to_fastapi_response(result: ProviderResult) -> Response:
    """Adapt a ProviderResult into a ``fastapi.Response``/``StreamingResponse``.

    ``content-type`` is pulled out of the headers and passed as the separate
    ``media_type`` parameter -- this way Starlette itself decides whether to
    add ``charset`` (for ``text/*``), matching the behavior providers used
    to do by hand (``headers.pop("content-type", None)`` before constructing
    the Response).

    Args:
        result: the provider-neutral result of handling the request.

    Returns:
        A ``StreamingResponse`` if the body is an async byte stream,
        otherwise a plain ``Response``.
    """
    headers = dict(result.headers)
    media_type = headers.pop("content-type", None)
    if isinstance(result.body, bytes):
        return Response(
            content=result.body,
            status_code=result.status_code,
            headers=headers,
            media_type=media_type,
        )
    return StreamingResponse(
        result.body,
        status_code=result.status_code,
        headers=headers,
        media_type=media_type,
    )
