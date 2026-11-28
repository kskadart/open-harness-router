# Codex Work thread receipts in Responses requests

Codex Work can put a thread-creation receipt or a message-delivery receipt
in Responses input history. Both are encoded as `function_call_output`
items even though they have no corresponding model tool call in that
history. The plaintext subagent task arrives separately as an
`agent_message`. The Responses-to-Messages adapter requires a call ID for a
tool result, and native third-party providers can reject an unpaired result.

## Receipt recognition

A direct object entry in the Responses `input` array is a thread receipt
when all of the following hold:

- Its type is `function_call_output` and its namespace is `codex_tui`.
- Its name is a nonempty string. `create_thread` and `send_message_to_thread`
  are observed operations. New operations with the same envelope qualify.
- Its `call_id` is absent, null, or the empty string. Other values, including
  whitespace strings and non-string values, leave the item outside the contract.
- Its `internal_chat_message_metadata_passthrough` is a JSON object. An empty
  object qualifies. Recognition does not depend on any particular metadata key.

Recognition is case-sensitive and uses the item's envelope. It does not examine
nested content or the receipt's opaque output. An item with a different
namespace, missing or non-object metadata, or an invalid name follows ordinary
route handling.

## Routing behavior

- Native third-party Responses and translated Responses routes omit these
  receipts before forwarding or translating the input. Use the same
  recognition rules for every provider and model on these routes. Receipt
  output and lifecycle metadata are omitted together. The adapter does not
  create a call ID or a replacement message.
- The remaining items retain their relative order. Plaintext agent tasks
  retain their existing conversion to user content once on each path. Tool
  calls and results with a nonempty call ID keep their existing handling,
  including results from the same namespace and tool names as the receipts.
  Filtering does not mutate the original request.
- A request containing only receipts yields an empty input array. The
  Messages bridge applies its existing empty-message validation. Native
  upstreams validate empty input against their own Responses semantics,
  including any server-side response reference in the request.
- Native OpenAI Responses routes keep these items when the resolved
  Responses base URL passes the router's existing official-endpoint check
  and TLS hostname verification is enabled. This applies to both
  client-authenticated and router-owned key routes. Existing changes such
  as model alias substitution still apply. The authenticated native GPT
  path forwards the original bytes when the route needs no such changes.

## Acceptance checks

- A third-party native route omits both kinds of receipt, including examples
  with omitted, null, and empty call IDs, and with different metadata objects.
  A new operation with the same envelope is omitted as well.
  Inspect the upstream input for the original task and the paired tool call
  and result in order.
- A translated route completes a plaintext subagent request containing a
  thread-creation receipt, preserving the paired call and result in the Chat
  Completions request. A result that lacks both the receipt envelope and
  the `call_id` key still fails as an invalid translated request before an
  upstream call. Items outside the envelope with an explicit null or empty
  call ID retain their existing route-specific handling.
- Native third-party requests containing only items outside the receipt
  contract retain those items. Official OpenAI routes preserve both receipts,
  including on explicit routes and routes using a router-owned key.
