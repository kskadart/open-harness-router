"""Anthropic and OpenAI protocol constants used during conversion.

Ported from claude-code-proxy (src/core/constants.py). A class of string
constants: message roles, content types, tool types, stop reasons,
Anthropic SSE event names, and delta types.
"""

# Names of built-in Claude Code tools that are kept when the ``tools`` array
# is truncated for openai-translate providers (OpenAI's hard limit is 128).
# Includes Claude Code CLI tools (CamelCase) and Anthropic built-in tools
# from the computer-use API (lowercase: computer, text_editor, bash). The
# list grows as new built-in tools are added. Source of names: the Claude
# Code environment's tool declaration (September 2026); the router's logs
# don't contain request body dumps with tools (debug level is disabled), so
# the allowlist is based on the known set of CLI tools + the computer-use API.
CLAUDE_BUILTIN_TOOL_NAMES: frozenset[str] = frozenset({
    # Claude Code CLI tools (CamelCase)
    "Agent",
    "Artifact",
    "Bash",
    "BashOutput",
    "CronCreate",
    "CronDelete",
    "CronList",
    "Edit",
    "EnterWorktree",
    "ExitWorktree",
    "Glob",
    "Grep",
    "KillShell",
    "Monitor",
    "NotebookEdit",
    "PushNotification",
    "Read",
    "RemoteTrigger",
    "ReportFindings",
    "SendMessage",
    "Skill",
    "Task",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskStop",
    "TaskUpdate",
    "TodoWrite",
    "WebFetch",
    "WebSearch",
    "Write",
    # Anthropic built-in tool names (computer-use API, lowercase)
    "bash",
    "computer",
    "text_editor",
})

# How many names of dropped MCP tools to log in the warning (not all of
# them, to avoid spamming the log when dozens of tools get truncated).
DROPPED_TOOLS_LOG_SAMPLE: int = 10

# Fields of a ``reasoning`` item that are allowed in the Responses API's
# ``input`` array. The set was determined empirically against a live
# /v1/responses: the item returned as-is is rejected by the upstream (the
# SDK's ``model_dump`` adds ``status: null`` -> "Unknown parameter:
# 'input[N].status'"), and without ``summary`` it responds "Missing required
# parameter: 'input[N].summary'". The carrier field is ``encrypted_content``:
# without it the reasoning context can't be restored.
REASONING_INPUT_FIELDS: frozenset[str] = frozenset({
    "type",
    "id",
    "summary",
    "encrypted_content",
    "content",
})

# Bounds of the reasoning-item cache across tool-call steps. An entry holds
# an encrypted blob (~1 KB per item), so the cap limits the memory of a
# long-lived process, while the TTL drops conversations no one is coming
# back to. The values aren't exposed as settings: a cache miss degrades
# gracefully (only the savings are lost), so per-environment tuning isn't
# needed.
REASONING_CACHE_MAX_ENTRIES: int = 512
REASONING_CACHE_TTL_S: float = 1800.0

# Retry budget the OpenAI SDK gets for transport failures and retryable
# upstream statuses (408/409/429 and 5xx; the SDK does not retry other
# 4xx). The SDK default is 2 attempts with 0.5s and 1s delays: ~1.5s total,
# not enough for real outage windows. Measured from logs: OpenAI's TPM-429
# asks for a 4-25s wait ("Please try again in 24.754s"), and runs of plain
# 500s on /v1/responses came in bursts of 3 in a row -- exactly what the
# default budget covered. With 5 retries the delays go 0.5, 1, 2, 4, 8 (SDK
# cap is 8s, jitter 0.75-1.0), which together with the round-trip time of
# the attempts themselves (~5s per request) covers a ~25s window. If the
# upstream sends a Retry-After header, the SDK honors it instead of the
# computed pause (accepted when 0 < Retry-After <= 60).
UPSTREAM_MAX_RETRIES: int = 5

# Neutral messages about upstream transport failures. The client only gets
# the fact of the failure and a hint to retry: the httpx exception text
# stays in the log, so internal details of the connection to the provider
# aren't exposed.
UPSTREAM_REQUEST_FAILED_MESSAGE: str = "Upstream request failed. Retry the request."
UPSTREAM_REQUEST_TIMEOUT_MESSAGE: str = "Upstream request timed out. Retry the request."
UPSTREAM_STREAM_INTERRUPTED_MESSAGE: str = "Upstream stream interrupted. Retry the request."

# Context-window guard for openai-translate providers that set
# ``context_window``. The reserve absorbs what the character heuristic in
# ``services.token_estimator`` cannot see (chat-template tokens, tool-call
# framing, upstream-side additions), so the effective budget is
# ``context_window - CONTEXT_WINDOW_RESERVE_TOKENS``.
CONTEXT_WINDOW_RESERVE_TOKENS: int = 512

# Smallest completion budget the router asks an upstream for: the floor the
# converters apply to the client's ``max_tokens``.
MIN_COMPLETION_TOKENS: int = 100

# Smallest completion budget worth sending to a reasoning upstream, and the
# floor the context-window pre-flight rejects below. Measured behaviour: a
# reasoning model handed the ~1000 tokens a nearly full window leaves spends
# them on reasoning and answers with empty ``content`` and ``stop_reason:
# max_tokens`` -- a wasted round trip the client cannot act on. Rejecting
# with ``prompt_too_long`` instead is what makes Claude Code compact and
# retry. Deliberately far above ``MIN_COMPLETION_TOKENS``: that one only
# bounds what the converters put on the wire, this one decides whether the
# request is worth sending at all.
MIN_USEFUL_COMPLETION_TOKENS: int = 4096


class Constants:
    """String constants for the Anthropic/OpenAI protocols."""

    ROLE_USER = "user"
    ROLE_ASSISTANT = "assistant"
    ROLE_SYSTEM = "system"
    ROLE_TOOL = "tool"

    CONTENT_TEXT = "text"
    CONTENT_IMAGE = "image"
    CONTENT_TOOL_USE = "tool_use"
    CONTENT_TOOL_RESULT = "tool_result"

    TOOL_FUNCTION = "function"

    STOP_END_TURN = "end_turn"
    STOP_MAX_TOKENS = "max_tokens"
    STOP_TOOL_USE = "tool_use"
    STOP_ERROR = "error"

    EVENT_MESSAGE_START = "message_start"
    EVENT_MESSAGE_STOP = "message_stop"
    EVENT_MESSAGE_DELTA = "message_delta"
    EVENT_CONTENT_BLOCK_START = "content_block_start"
    EVENT_CONTENT_BLOCK_STOP = "content_block_stop"
    EVENT_CONTENT_BLOCK_DELTA = "content_block_delta"
    EVENT_PING = "ping"

    DELTA_TEXT = "text_delta"
    DELTA_INPUT_JSON = "input_json_delta"
