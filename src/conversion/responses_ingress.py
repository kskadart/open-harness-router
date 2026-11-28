"""Translate stateless Responses requests through the existing Messages pipeline."""

from __future__ import annotations

import json
from typing import Any, cast
from urllib.parse import urlsplit

from conversion.responses_tools import CustomToolsAdapter
from errors import UpstreamError
from routing.schema import ProviderCfg, ReasoningEffort

_OPENAI_RESPONSES_BASES = {("chatgpt.com", "/backend-api/codex"), ("api.openai.com", "/v1")}


def invalid(message: str) -> UpstreamError:
    """Reject unsupported input before contacting an upstream."""
    return UpstreamError(message, 400, "invalid_request_error")


def is_trusted_openai_responses_url(url: str) -> bool:
    """Return whether a URL is an official, authenticated-safe OpenAI Responses base."""
    try:
        upstream = urlsplit(url)
        port = upstream.port
    except ValueError:
        return False
    return (
        upstream.scheme == "https"
        and upstream.username is None
        and upstream.password is None
        and port in {None, 443}
        and not upstream.query
        and not upstream.fragment
        and (upstream.hostname, upstream.path.rstrip("/")) in _OPENAI_RESPONSES_BASES
    )


def unsupported_encrypted_agent_message() -> UpstreamError:
    """Report an encrypted subagent task on a route that requires plaintext."""
    return UpstreamError(
        "Encrypted subagent prompts or context are unsupported on this model route. "
        "Use Codex Code Mode to send the task as plaintext, or use an authenticated native "
        "OpenAI GPT route for encrypted tasks.",
        400,
        "unsupported_encrypted_agent_message",
    )


def has_encrypted_agent_message(payload: dict[str, Any]) -> bool:
    """Return whether a Responses input carries opaque subagent content."""
    items = payload.get("input")
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        if item.get("encrypted_content") is not None:
            return True
        content = item.get("content")
        if isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") == "encrypted_content"
            for block in content
        ):
            return True
    return False


_RESPONSES_REASONING_EFFORTS = {"none", "low", "medium", "high", "xhigh", "max"}


def responses_reasoning_effort(payload: dict[str, Any]) -> ReasoningEffort | None:
    """Return a recognized raw Responses effort for an explicit bridge handoff."""
    reasoning = payload.get("reasoning")
    if not isinstance(reasoning, dict):
        return None
    effort = reasoning.get("effort")
    if effort in _RESPONSES_REASONING_EFFORTS:
        return cast(ReasoningEffort, effort)
    return None


def _plaintext_agent_blocks(item: dict[str, Any]) -> list[dict[str, Any]]:
    if item.get("encrypted_content") is not None:
        raise unsupported_encrypted_agent_message()
    blocks = item.get("content")
    if not isinstance(blocks, list) or not blocks:
        raise invalid("Responses agent_message must contain a non-empty content array")
    if any(
        isinstance(block, dict) and block.get("type") == "encrypted_content" for block in blocks
    ):
        raise unsupported_encrypted_agent_message()
    if any(
        not isinstance(block, dict)
        or block.get("type") != "input_text"
        or not isinstance(block.get("text"), str)
        for block in blocks
    ):
        raise invalid("Responses agent_message requires plaintext input_text blocks")
    return blocks


def adapt_agent_messages(payload: dict[str, Any]) -> dict[str, Any]:
    """Deliver plaintext Codex tasks to custom Responses providers as user messages."""
    items = payload.get("input")
    if not isinstance(items, list):
        return payload
    adapted = None
    for index, item in enumerate(items):
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        if adapted is None:
            adapted = items.copy()
        adapted[index] = {
            "type": "message",
            "role": "user",
            "content": _plaintext_agent_blocks(item),
        }
    return {**payload, "input": adapted} if adapted is not None else payload


def _is_codex_thread_receipt(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    name = item.get("name")
    return (
        item.get("type") == "function_call_output"
        and item.get("namespace") == "codex_tui"
        and isinstance(name, str)
        and name != ""
        and item.get("call_id") in (None, "")
        and isinstance(item.get("internal_chat_message_metadata_passthrough"), dict)
    )


def omit_codex_thread_receipts(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove local Codex Work lifecycle records before third-party translation."""
    items = payload.get("input")
    if not isinstance(items, list):
        return payload
    filtered = [item for item in items if not _is_codex_thread_receipt(item)]
    return {**payload, "input": filtered} if len(filtered) != len(items) else payload


def can_translate_responses(cfg: ProviderCfg) -> bool:
    """Own-key upstreams can use the Messages adapter without leaking Codex auth."""
    return cfg.type == "openai-translate" or not cfg.forward_client_auth


def _leaves(tools: list[dict[str, Any]], namespace: str = "") -> list[dict[str, Any]]:
    result = []
    for tool in tools:
        if tool.get("type") == "namespace":
            result.extend(_leaves(tool.get("tools", []), tool["name"]))
        else:
            result.append({**tool, **({"namespace": namespace} if namespace else {})})
    return result


def _content(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        raise invalid("Responses message content must be text or an array")
    result = []
    for block in content:
        kind = block.get("type")
        if kind in {"input_text", "output_text", "text"}:
            result.append({"type": "text", "text": block["text"]})
        elif kind == "input_image":
            url = block.get("image_url", "")
            if not isinstance(url, str) or not url:
                raise invalid("Image input requires image_url")
            if url.startswith("data:"):
                prefix, separator, data = url.partition(",")
                if not separator or not prefix.endswith(";base64"):
                    raise invalid("Image data URLs must contain base64 data")
                source = {"type": "base64", "media_type": prefix[5:-7], "data": data}
            else:
                source = {"type": "url", "url": url}
            result.append({"type": "image", "source": source})
        else:
            raise invalid(f"Unsupported Responses content type: {kind}")
    return result


class ResponsesRequestMapper:
    """Preserve tool identity, including namespaces and freeform tools, across turns."""

    def __init__(self, payload: dict[str, Any]) -> None:
        definitions = list(payload.get("tools") or [])
        items = payload.get("input", [])
        if isinstance(items, list):
            for item in items:
                if item.get("type") == "additional_tools":
                    definitions.extend(item.get("tools", []))
        names = [t["name"] for t in _leaves(definitions) if t.get("type") == "custom"]
        if isinstance(items, list):
            names.extend(i["name"] for i in items if i.get("type") == "custom_tool_call")
        self.custom = CustomToolsAdapter(payload, names)
        self.omitted_tools: list[str] = []
        self.names: dict[str, dict[str, str]] = {}
        self.payload = self.custom.request(payload)

    def tool_name(self, name: str, namespace: str = "") -> str:
        """Encode namespace identity in an upstream-safe function name."""
        encoded = f"{namespace}__{name}" if namespace else name
        identity = {"name": name, **({"namespace": namespace} if namespace else {})}
        if encoded in self.names and self.names[encoded] != identity:
            raise invalid("Tool names collide after namespace conversion")
        self.names[encoded] = identity
        return encoded

    def request(self, max_tokens: int) -> dict[str, Any]:
        """Build a Messages request consumed by either existing provider type."""
        p = self.payload
        if p.get("previous_response_id") or p.get("conversation"):
            raise invalid(
                "Translated Responses requires full input history; server state is unsupported"
            )
        if p.get("text", {}).get("format", {}).get("type", "text") != "text":
            raise invalid("Structured output is not supported by the Messages adapter")
        messages: list[dict[str, Any]] = []
        systems = [p["instructions"]] if p.get("instructions") else []
        items = p.get("input", [])
        if isinstance(items, str):
            items = [{"role": "user", "content": items}]
        if not isinstance(items, list):
            raise invalid("Responses input must be text or an array")
        definitions = list(p.get("tools") or [])
        for item in items:
            if item.get("type") == "additional_tools":
                definitions.extend(item.get("tools", []))
        tools: dict[str, dict[str, Any]] = {}
        for tool in _leaves(definitions):
            if tool.get("type") in {"web_search", "web_search_preview"}:
                self.omitted_tools.append(tool["type"])
                continue
            if tool.get("type") != "function":
                raise invalid(
                    f"Unsupported hosted tool in translated Responses: {tool.get('type')}"
                )
            name = self.tool_name(tool["name"], tool.get("namespace", ""))
            if name not in tools:
                tools[name] = {
                    "name": name,
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("parameters", {"type": "object"}),
                }
        for item in items:
            kind = item.get("type", "message")
            if kind in {"reasoning", "additional_tools"}:
                continue  # Opaque reasoning belongs to the original model, not another upstream.
            if kind == "message":
                role = item.get("role", "user")
                content = _content(item.get("content", ""))
                if role in {"system", "developer"}:
                    if any(b["type"] != "text" for b in content):
                        raise invalid("System messages must contain text")
                    systems.extend(b["text"] for b in content)
                    continue
                if role not in {"assistant", "user"}:
                    raise invalid(f"Unsupported message role: {role}")
            elif kind == "agent_message":
                role = "user"
                content = _content(_plaintext_agent_blocks(item))
            elif kind == "function_call":
                role = "assistant"
                try:
                    arguments = json.loads(item.get("arguments", "{}"))
                except (ValueError, TypeError) as exc:
                    raise invalid("Function arguments must be a JSON object") from exc
                if not isinstance(arguments, dict):
                    raise invalid("Function arguments must be a JSON object")
                content = [
                    {
                        "type": "tool_use",
                        "id": item["call_id"],
                        "name": self.tool_name(item["name"], item.get("namespace", "")),
                        "input": arguments,
                    }
                ]
            elif kind == "function_call_output":
                role = "user"
                output = item.get("output", "")
                content = [
                    {
                        "type": "tool_result",
                        "tool_use_id": item["call_id"],
                        "content": _content(output) if isinstance(output, list) else output,
                    }
                ]
            else:
                raise invalid(f"Unsupported Responses input item: {kind}")
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"].extend(content)
            else:
                messages.append({"role": role, "content": content})
        if self.omitted_tools:
            systems.append(
                "Hosted web search is unavailable on this translated route. "
                "Use only the provided client tools; do not claim to have searched the web."
            )
            if p.get("tool_choice") == "required" and not tools:
                raise invalid("Required hosted web search is unavailable on this route")
        if not messages:
            raise invalid("Responses input must contain at least one message")
        requested_tokens = p.get("max_output_tokens", max_tokens)
        if (
            isinstance(requested_tokens, bool)
            or not isinstance(requested_tokens, int)
            or requested_tokens < 1
        ):
            raise invalid("max_output_tokens must be a positive integer")
        result: dict[str, Any] = {
            "model": p["model"],
            "messages": messages,
            "max_tokens": min(requested_tokens, max_tokens),
            "stream": p.get("stream", False),
        }
        if systems:
            result["system"] = "\n\n".join(systems)
        for field in ("temperature", "top_p"):
            if field in p:
                result[field] = p[field]
        if tools:
            result["tools"] = list(tools.values())
        choice = p.get("tool_choice")
        if isinstance(choice, dict):
            if choice.get("type") != "function":
                raise invalid("Unsupported tool_choice in translated Responses")
            result["tool_choice"] = {
                "type": "tool",
                "name": self.tool_name(choice["name"], choice.get("namespace", "")),
            }
        elif choice in {"auto", "required", "none"}:
            result["tool_choice"] = {"type": {"required": "any"}.get(choice, choice)}
        if p.get("parallel_tool_calls") is False:
            result.setdefault("tool_choice", {"type": "auto"})["disable_parallel_tool_use"] = True
        effort = p.get("reasoning", {}).get("effort")
        if effort in {"low", "medium", "high", "xhigh", "max"}:
            result["output_config"] = {"effort": effort}
        return result

    def restore_item(self, item: dict[str, Any], *, added: bool = False) -> dict[str, Any]:
        if item.get("type") == "function_call":
            item = {**item, **self.names.get(item["name"], {"name": item["name"]})}
        return self.custom.item(item, added=added)
