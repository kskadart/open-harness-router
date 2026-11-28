"""Codex model discovery and native ``POST /v1/responses`` forwarding."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import JSONResponse, Response

from api.adapters import openai_error, to_fastapi_response
from conversion.responses_ingress import (
    adapt_agent_messages,
    can_translate_responses,
    has_encrypted_agent_message,
    is_trusted_openai_responses_url,
    omit_codex_thread_receipts,
    unsupported_encrypted_agent_message,
)
from dependencies import LoggerDep, RegistryDep
from errors import UpstreamError, anthropic_error_body
from providers.openai_native import NativeOpenAIProvider
from providers.passthrough import PassthroughProvider
from providers.responses_bridge import handle_translated_responses
from services.codex_catalog import merge_catalogs, model_catalog
from services.monitor import Monitor

router = APIRouter()

_STANDARD_ACCESS_PROGRAMS = {"cyber": "standard"}


def _is_official_openai_responses_route(
    native: NativeOpenAIProvider,
) -> bool:
    """Identify the configured official Responses transport, including own-key setups."""
    return native.cfg.tls_verify_hostname and is_trusted_openai_responses_url(native.cfg.base_url)


def _is_trusted_openai_agent_route(
    native: NativeOpenAIProvider, responses_default: str | None
) -> bool:
    """Allow opaque agent tasks only on the authenticated official OpenAI endpoint."""
    # The native transport owns the resolved Responses URL and client-auth setting.
    return (
        native.name == responses_default
        and native.cfg.forward_client_auth
        and _is_official_openai_responses_route(native)
    )


def _normalize_custom_responses(payload: dict[str, Any]) -> dict[str, Any]:
    """Adapt Codex-only request fields for third-party Responses providers."""
    if "access_programs" in payload and payload["access_programs"] != _STANDARD_ACCESS_PROGRAMS:
        raise UpstreamError(
            "access_programs is supported only by the official OpenAI Responses endpoint. "
            "The selected custom model route cannot safely honor a nonstandard program selection.",
            400,
            "unsupported_access_programs",
        )
    if "access_programs" in payload:
        payload = payload.copy()
        del payload["access_programs"]

    return omit_codex_thread_receipts(payload)


@router.websocket("/v1/responses")
async def responses_websocket(http_request: WebSocket) -> None:
    """Signal Codex to use HTTP immediately, without retrying a rejected WebSocket.

    Codex treats handshake status 426 as the transport fallback signal.
    Its built-in OpenAI provider probes WebSockets even behind a custom URL.
    """
    await http_request.send_denial_response(Response(status_code=426))


async def _combined_catalog(
    http_request: Request,
    registry: RegistryDep,
    *,
    refresh_native: bool,
) -> Response:
    """Return the Codex-native catalog used by its model picker."""
    custom = model_catalog(
        registry.rules,
        {name: provider.cfg for name, provider in registry.providers.items()},
        set(registry.responses_providers)
        | {name for name, p in registry.providers.items() if can_translate_responses(p.cfg)},
    )
    native = registry.responses_providers.get(registry.responses_default or "")
    if (
        refresh_native
        and native is not None
        and native.cfg.forward_client_auth
        and http_request.headers.get("authorization")
    ):
        try:
            base = await native.fetch_model_catalog(
                http_request.headers,
                client_version=http_request.query_params.get("client_version"),
            )
        except UpstreamError as exc:
            return openai_error(exc.message, exc.status_code, code=exc.error_type)
    else:
        base = await asyncio.to_thread(registry.current_codex_catalog)
    # An authenticated upstream catalog is scoped to this request, never to the registry.
    return JSONResponse(merge_catalogs(base, custom), headers={"Cache-Control": "no-store"})


@router.get("/codex/models")
async def codex_models(http_request: Request, registry: RegistryDep) -> Response:
    """Serve a fresh account-scoped catalog when the client supplies native authentication."""
    return await _combined_catalog(http_request, registry, refresh_native=True)


@router.get("/v1/models")
async def list_models(http_request: Request, registry: RegistryDep) -> Response:
    """Return the same usable ids in the standard OpenAI model-list shape."""
    if "anthropic-version" in http_request.headers:
        provider = registry.providers[registry.default_provider]
        if not isinstance(provider, PassthroughProvider) or not provider.cfg.forward_client_auth:
            return JSONResponse(
                status_code=400,
                content=anthropic_error_body(
                    "invalid_request_error",
                    "Native Messages discovery is unavailable",
                ),
            )
        try:
            return to_fastapi_response(
                await provider.handle_models(
                    http_request.headers,
                    query=http_request.url.query,
                )
            )
        except UpstreamError as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content=anthropic_error_body(
                    exc.error_type,
                    exc.message,
                ),
            )
    catalog_response = await _combined_catalog(
        http_request,
        registry,
        refresh_native="client_version" in http_request.query_params,
    )
    if "client_version" in http_request.query_params or catalog_response.status_code != 200:
        return catalog_response
    catalog = json.loads(bytes(catalog_response.body))
    return JSONResponse(
        {
            "object": "list",
            "data": [
                {"id": model["slug"], "object": "model", "created": 0, "owned_by": "router"}
                for model in catalog["models"]
            ],
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/v1/responses")
async def create_response(
    http_request: Request, registry: RegistryDep, logger: LoggerDep
) -> Response:
    """Resolve by model using the shared rules, preserving native Responses bodies."""
    raw_body = await http_request.body()
    try:
        payload = json.loads(raw_body)
    except (ValueError, UnicodeDecodeError):
        return openai_error("Invalid JSON body")
    model = payload.get("model") if isinstance(payload, dict) else None
    if not isinstance(model, str) or not model:
        return openai_error("Missing 'model' field")
    if "stream" in payload and not isinstance(payload["stream"], bool):
        return openai_error("'stream' must be a boolean")
    decision = registry.resolve(model, default_provider=registry.responses_default)
    native = registry.responses_providers.get(decision.provider.name)
    if native is None and not can_translate_responses(decision.provider.cfg):
        return openai_error(
            "The selected model's provider does not support native Responses; "
            "configure responses_base_url for a Responses-capable upstream.",
            code="unsupported_model",
        )
    if native is not None and decision.upstream_model is not None:
        payload["model"] = decision.upstream_model
        raw_body = json.dumps(payload).encode()
    logger.info("route", endpoint="responses", model=model, provider=decision.provider.name)
    tracker = Monitor.current().start_request(
        model=model,
        provider=decision.provider.name,
        endpoint="responses",
        pricing=decision.pricing,
        stream=payload.get("stream", False),
    )
    try:
        if native is not None:
            official_responses_route = _is_official_openai_responses_route(native)
            trusted_agent_route = _is_trusted_openai_agent_route(
                native, registry.responses_default
            ) and payload["model"].startswith("gpt-")
            if not trusted_agent_route:
                adapted = adapt_agent_messages(payload)
                if not official_responses_route:
                    adapted = _normalize_custom_responses(adapted)
                if adapted is not payload:
                    raw_body = json.dumps(adapted).encode()
            elif not http_request.headers.get("authorization") and has_encrypted_agent_message(
                payload
            ):
                raise unsupported_encrypted_agent_message()
            result = await native.handle_responses(
                raw_body, http_request.headers, http_request, query=http_request.url.query
            )
        else:
            if has_encrypted_agent_message(payload):
                raise unsupported_encrypted_agent_message()
            payload = _normalize_custom_responses(payload)
            result = await handle_translated_responses(
                payload, decision.provider, http_request, decision.upstream_model, decision.limits
            )
    except UpstreamError as exc:
        tracker.fail(exc)
        return openai_error(exc.message, exc.status_code, code=exc.error_type)
    except BaseException as exc:
        tracker.fail(exc)
        raise
    return to_fastapi_response(tracker.attach(result))
