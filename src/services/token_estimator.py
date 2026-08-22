"""Rough estimate of input token count for fleet providers.

Most OpenAI-compatible upstreams have no native count_tokens, so an
approximation of ~4 characters per token is used over the request's text
content. For the passthrough provider (Anthropic) no estimate is needed:
the request is proxied to the native ``/v1/messages/count_tokens``.
"""

from __future__ import annotations

from models.claude import ClaudeTokenCountRequest

_CHARS_PER_TOKEN = 4


def _extract_text(content: object) -> str:
    """Extract text from a message's content (string or list of blocks).

    Args:
        content: the value of the content or system field.

    Returns:
        The concatenated text of all text blocks.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            text = getattr(block, "text", None)
            if text is None and isinstance(block, dict):
                text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts)
    return ""


def estimate_from_claude_request(request: ClaudeTokenCountRequest) -> int:
    """Estimate the input token count for a count_tokens request.

    Args:
        request: the token-count request in Anthropic format.

    Returns:
        The estimated input token count (minimum 1).
    """
    total_chars = 0
    if request.system is not None:
        total_chars += len(_extract_text(request.system))
    for message in request.messages:
        total_chars += len(_extract_text(message.content))
    return max(1, total_chars // _CHARS_PER_TOKEN)
