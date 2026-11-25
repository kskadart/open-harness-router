**English** | [Русский](README.ru.md)

# Open Harness Router

![The dashboard: request, error, in-flight, token and cost tiles, per-provider and per-model tables, the requests in flight, the event feed and the routing table](docs/dashboard.png)

Use configured models from multiple vendors in Claude Code and Codex through
one local router. Anthropic Messages (`POST /v1/messages`) and OpenAI Responses
(`POST /v1/responses`) share the first-match routing rules in `routing.yaml`.
Messages uses compatible passthrough or OpenAI translation. Codex uses native
Responses when configured, otherwise own-key providers use Responses ↔ Messages
adapters and the existing Chat Completions or Responses pipeline.

Start with [KICKOFF.md](KICKOFF.md) for the architecture, setup sequence,
model discovery and limitations. This README is the detailed operational reference.

The Claude Code orchestrator always stays on native Anthropic models: the
`claude-*` and alias rules route to passthrough, and the default route must be
passthrough (enforced by the config validator).

One provider registry, two entrances. Forward-proxy (`make run-proxy`) is the
primary mode for Claude Code: the client keeps the default
`ANTHROPIC_BASE_URL` and sets only `HTTPS_PROXY` and `NODE_EXTRA_CA_CERTS`, so
every CLI feature works as on a direct connection, Remote Control (`/rc`)
included. Reverse-proxy (`make run`) is the router's plain HTTP API on
`ANTHROPIC_BASE_URL=http://127.0.0.1:8787`, for `/health`, `curl` and any
client that only knows a base URL. Both are described in "Run modes" below.

> **Platform support.** The router is developed and used daily on macOS, but
> the router itself and its CLI helpers (`cli.validate_routing`,
> `cli.tls_probe`, `cli.sync_client_config`) are plain Python with no
> platform-specific code: the test suite runs on Ubuntu in CI
> (`.github/workflows/ci.yml`, `runs-on: ubuntu-latest`) and the README covers
> running the service under systemd on Linux. What is macOS-only is the
> add-provider skill's restart step: its `restart_router.sh` talks to launchd
> and exits with code 64 and a systemd hint on any other OS, and the skill's
> default log path assumes `~/Library/Logs`. On Linux, restart the service by
> hand (`systemctl --user restart ...` with your real unit name) and point
> `OHR_ERR_LOG` at your own log file. Windows is untested.

## Installation

```bash
uv sync
test -e .env || cp .env.example .env
test -e routing.yaml || cp routing.example.yaml routing.yaml
```

## Connecting Codex

Codex connects to the HTTP listener using the OpenAI Responses protocol:
`POST /v1/responses`. The router applies the same first-match routing rules
as for Claude Code. When a native Responses endpoint is configured, request
bodies and streaming events retain their native format, including tool calls,
images and encrypted reasoning. Other own-key providers use Responses ↔ Messages
adapters, including the existing Chat Completions translation where configured.
See [provider compatibility and adapter limitations](#provider-compatibility)
below before selecting a route.

Codex's built-in image tool also uses its configured provider base URL.
The router forwards native JSON `POST /v1/images/generations` and
`POST /v1/images/edits` through the authenticated `responses_default` below.
These requests always use that trusted native fallback, independently of the
selected assistant model and custom routing rules. With the existing Codex
ChatGPT sign-in, no additional server-side OpenAI API key is needed; account
access and quotas are enforced upstream. Own-key custom providers do not
receive the native image request or client credentials. Image requests reuse
the native transport and cleanup, bypass Responses tool adaptation, and do
not contribute to text-generation TPS or inherit text-model pricing.

For a Kimi Code subscription, add this provider and rule to `routing.yaml`
(or add the new fields to an existing Kimi Code provider/rule):

```yaml
providers:
  kimi_code:
    type: passthrough
    base_url: https://api.kimi.com/coding/
    responses_base_url: https://api.kimi.com/coding/v1
    responses_custom_tools_as_functions: [exec]
    forward_client_auth: false
    api_key_env: KIMI_API_KEY
    auth_header: x-api-key

rules:
  - match: {type: prefix, value: "kimi-"}
    provider: kimi_code
    client_models: ["kimi-for-coding"]
    codex:
      display_name: Kimi K2.8 Preview (kimi-for-coding)
      context_window: 1048576
      reasoning_efforts: [low, high, max]
      default_reasoning_effort: high
      input_modalities: [text, image]
```

Keep the existing native Anthropic provider, its rules and the default route.
Set `KIMI_API_KEY` in the router's `.env`. The router uses the same provider
key for both clients; Codex does not need a copy of the key. Messages uses
the configured `auth_header`; own-key native Responses uses Bearer auth.
The example capabilities follow the
[Kimi Code Codex guide](https://www.kimi.com/code/docs/en/third-party-tools/codex.html);
adjust them for the model and membership tier you use.

## Using the ChatGPT subscription from Claude Code

`chatgpt_oauth` providers route the Anthropic Messages path (Claude Code)
to the ChatGPT subscription backend: the router holds **its own** OAuth
credential — obtained through a one-time browser login, never by reading
the Codex CLI's `auth.json` — and translates Messages requests into
Responses requests against `chatgpt.com/backend-api/codex`. Billing goes
through the subscription quota, the same way the Anthropic passthrough
provider goes through the Claude Code subscription.

One-time login (opens the browser, stores the credential in
`state/chatgpt_auth.json` with 0600 permissions, gitignored):

```bash
PYTHONPATH=src .venv/bin/python -m cli.chatgpt_login
```

Then add the provider and rules to `routing.yaml`:

```yaml
providers:
  chatgpt_subscription:
    type: openai-translate
    base_url: https://chatgpt.com/backend-api/codex
    api_flavor: responses
    chatgpt_oauth: true
    max_tokens_limit: 128000
    context_window: 1048576
    timeout_s: 180

rules:
  - match: {type: exact, value: "OAI/gpt-6.2-sol"}
    provider: chatgpt_subscription
    upstream_model: gpt-6.2-sol
```

Constraints enforced at startup: the `base_url` is pinned to the
subscription endpoint exactly (the credential is a live account bearer and
must never reach another destination), TLS hostname verification is
required, `api_key_env` is rejected (OAuth replaces the static key), and
the provider may not be `responses_default` — the native Responses
transport is deliberately not built for it, so Codex clients must not fall
back to it.

Routing notes: the subscription backend is streaming-only and rejects
stored responses, so the provider forces `stream: true` (aggregating the
terminal SSE event for non-streaming clients) and sends `store: false`;
both apply only to `chatgpt_oauth` routes, ordinary API-key Responses
routes are unchanged. Requests ask for `reasoning.encrypted_content` and
the router replays the returned reasoning items before the corresponding
tool call on later turns, keeping multi-turn tool use coherent. Tokens
refresh automatically; re-run the login CLI after account changes that
revoke the refresh token. The subscription credential store uses POSIX advisory
locks (macOS/Linux) to coordinate token rotation and login across processes.
Leave the lock files alongside the credential while the router is running.

To keep **all normal Codex models plus custom models in the same picker**,
add a native Responses fallback and merge the catalog from your normal Codex
installation. For ChatGPT subscription sign-in:

```yaml
providers:
  # Add alongside anthropic and kimi_code, not instead of them.
  codex_openai:
    type: passthrough
    base_url: https://chatgpt.com/backend-api/codex
    responses_base_url: https://chatgpt.com/backend-api/codex
    forward_client_auth: true
    responses_forward_client_auth: true

# Top-level fields, separate from the Anthropic default.
responses_default:
  provider: codex_openai
codex_catalog_path: ~/.codex/models_cache.json
```

Start normal Codex once to populate its account catalog before generating the
combined profile. The local cache bootstraps model selection and the preview
snapshot; `--model` selects the profile default instead of the first entry.
ChatGPT subscription profiles use live discovery through the router by default,
so new/retired OpenAI models and changed capabilities are obtained from the
account's current upstream catalog rather than a pinned local file. Native
visibility, instructions and capabilities are preserved; configured custom
models are appended. Explicit rules still take precedence over
`responses_default`; `default` continues to control Messages requests.

Set each model's supported/default reasoning levels under its rule's `codex`
metadata. Switching with `-m` keeps profile-level settings: an explicit saved
`model_reasoning_effort = "none"` can still be sent to a model that rejects it.
Catalog metadata does not override that explicit value. Select a supported
effort with `-c 'model_reasoning_effort="high"'` when required, or use separate
profiles for models needing different defaults.

Custom catalog entries receive model-specific identity instructions while
retaining the native coding workflow. Without them Codex's default prompt
identifies the agent as GPT-6 even when inference goes to another vendor.
Use the router's route/upstream logs to verify routing; model self-reports
alone are not evidence of the serving backend. Restart Codex after regenerating
the catalog so it loads the updated model instructions.

The combined profile keeps OpenAI authentication enabled: Codex supplies its
existing subscription credential, and the native fallback forwards it back
to the configured OpenAI endpoint. Own-key custom providers still strip that
credential and inject their own key. For OpenAI API-key sign-in, use
`https://api.openai.com/v1` for both native URLs instead. Choose the endpoint
matching your sign-in method. The router never reads Codex's authentication
file or stores its login tokens.

```bash
PYTHONPATH=src uv run python -m cli.validate_routing --expect-provider kimi_code kimi-for-coding
make full-check
# Start the HTTP listener, or restart your existing router service after validation.
make run
```

In another terminal, generate and install a dedicated Codex profile:

```bash
make sync-codex-config  # preview files under state/codex/, no user defaults changed
PYTHONPATH=src uv run python -m cli.sync_codex_config --output-dir ~/.codex
codex -p open-harness-router
```

In Codex CLI 0.169.0, `/model` lists the native catalog plus **Kimi K2.8 Preview**
(`kimi-for-coding`) when the fallback/catalog settings above are configured.
Without those settings, it lists only the explicitly configured custom models.
The profile uses a separate `open-harness-router.config.toml`. The generator
also writes a catalog snapshot, but a ChatGPT subscription profile leaves
`model_catalog_json` unset so it does not block native discovery. Use
`--static-catalog` to pin the snapshot explicitly. API-key and custom-only
profiles retain local snapshots. Remove any effective `model_catalog_json`
from other configuration layers to use live discovery; it takes precedence
over remote fetching. Native-auth configurations keep
`model_provider = "openai"` and set `openai_base_url` to the router. This preserves the provider identity saved in
normal Codex sessions, so they can be resumed through the same profile:

```bash
codex -p open-harness-router resume SESSION_ID
```

#### Run custom models as Codex subagents

For a Multi-Agent V2 parent with Code Mode, such as GPT-6.2 Sol, ask Codex
to call `spawn_agent` with `model=cl-deepseek-v4.1-flash` or
`model=kimi-for-coding`, assign the task, and wait for the result. Regenerate
the standard profile after upgrading the router. The profile permits the
nested Code Mode spawn when there is a custom Responses route or a translated
route and retains live native discovery.

Codex delivers nested Code Mode tasks as plaintext `agent_message` items.
The router converts them to user messages on custom Responses and translated
routes. For a direct V2 call with `agent_message.encrypted_content`, the router
forwards the message only to a `gpt-` model on the authenticated OpenAI
`responses_default` with client authentication forwarding and hostname
verification enabled for the official OpenAI Responses endpoint. Other routes
reject the encrypted task locally before contacting the upstream, so direct
encrypted spawn fails on custom routes.
The response is HTTP 400 with code `unsupported_encrypted_agent_message` and
instructions for sending the task as plaintext through Codex Code Mode.
Codex's inert `{"cyber":"standard"}` `access_programs` marker is removed on
custom routes. Other program selections fail locally with
`unsupported_access_programs`, while official OpenAI Responses routes receive
the field unchanged.
For Chat Completions providers that accept `reasoning_effort`, set
`forward_chat_reasoning_effort: true` on the `openai-translate` provider to
forward the client's explicit effort.

#### Transport details

The profile disables request-body compression so the router can read the model
field. The router declines WebSocket handshakes with Codex's HTTP fallback
signal (426), and streaming continues over HTTP without repeated reconnects.
The combined entrypoint and `make run` select Uvicorn's `websockets` backend;
use `--ws websockets` for a custom Uvicorn command too. The pinned Uvicorn
version's automatic SansIO backend logs an incomplete-handshake error after
an otherwise successful denial response.
Custom-only configurations still use their dedicated provider identity.

Earlier router profiles used `open-harness-router` as the provider identity even
with native models. The generator retains that provider definition for those
saved sessions; select it explicitly when resuming one of them:

```bash
codex -p open-harness-router -c 'model_provider="open-harness-router"' resume SESSION_ID
```

Do not edit session metadata to change providers. Close a session in its
previous window/client before resuming it elsewhere: Codex allows only one
active writer per saved session.

Use `--base-url http://127.0.0.1:PORT/v1` when generating the profile if your
HTTP listener uses a different port. Profiles in this form were verified
with CLI 0.169.0. Desktop setup and picker behavior must be verified separately;
the generated profile does not change the desktop's default provider.

### Provider compatibility

API-key `openai-translate` providers with `api_flavor: responses` support native
forwarding using their existing `base_url`. The `chatgpt_oauth` subscription
provider uses Responses for Messages translation only: it has no native Responses
transport and cannot be `responses_default`. The flavor is invalid for
`passthrough`. Set `responses_base_url`, including the API prefix
(usually `/v1`), to enable native forwarding independently of Messages routing.
Verify that endpoint with text, streaming, function calls and tool-result
continuation before enabling it. Native Responses requests bypass the Messages
translation settings, including `drop_params`, token caps and quota waiting;
the upstream enforces its limits.

Own-key providers without a native Responses endpoint also support Codex:
OHR maps Responses to Messages, uses the existing provider pipeline (including
its Chat Completions conversion when configured), and maps the result back to
Responses JSON or SSE. This path preserves model aliases, token limits,
provider credentials, function and freeform tools, namespaces, tool results,
text/image input, streaming, usage, cancellation and upstream errors. Client
OpenAI credentials are never forwarded to the Messages adapter. Providers
requiring a client's Anthropic credentials cannot use this adapter with a
Codex login; configure an own-key provider or a native Responses endpoint.

Translated requests must carry the full conversation in `input`;
`previous_response_id`, server conversations, item references, structured output
and hosted tools other than optional web search are rejected with a 400 error.
Optional hosted `web_search` definitions are omitted with a router warning and
an explicit instruction to the model that hosted search is unavailable; forced
hosted search is rejected. Client-side search tools remain usable. Native
Responses forwarding keeps its original hosted-tool support.

The model picker includes explicit ids from exact rules or `client_models` for
both native and translated providers. Restart the router after changing routes.
Live ChatGPT profiles receive the changes on the client's next catalog refresh;
static profiles require catalog regeneration with `make sync-codex-config` (or
the installation command above) and a Codex restart. Optional `codex` metadata controls the display
name, client context window, supported/default reasoning efforts and input
modalities; it does not change upstream limits.

For upstreams that only support `apply_patch` as a freeform custom tool,
`responses_custom_tools_as_functions: [exec]` adapts Codex Code Mode's `exec`
to a JSON function tool. The router converts replayed calls and results and
restores custom tool calls in JSON/SSE responses. Codex still executes the tool.
Definitions inside namespaces and replayed `additional_tools` items are
adapted too. For these configured upstreams, current request-level definitions
take precedence over duplicate historical definitions, followed by the latest
historical definition; redundant declarations and empty wrappers are removed
without removing conversation messages, tool calls or results.
Other tools and providers retain their native format. Tool arguments are
decoded after the complete function arguments arrive. The example label is
the Kimi API's reported K2.8 Preview model behind `kimi-for-coding`; check the
upstream `/models` metadata if that vendor-managed alias changes.

Model discovery preserves the requesting client's protocol:

- Codex's `GET /v1/models?client_version=...` receives a native Codex catalog.
  With an explicitly authenticated native Responses fallback, OHR forwards the
  caller's authorization, account header and client version to the upstream
  catalog endpoint and merges configured custom entries. No authenticated
  catalog is shared across callers or written to the bootstrap file. Upstream
  errors are returned instead of silently presenting stale account metadata.
- `GET /codex/models` serves the same merged format. Without caller auth, the
  configured local bootstrap file is reloaded on each read; an invalid update
  retains the last valid file snapshot with a warning.
- `GET /v1/models` without protocol markers keeps the standard OpenAI ID list.
  With `anthropic-version`, it forwards the Messages default provider's native
  Anthropic catalog, preserving auth, pagination and response format. CONNECT
  mode already forwards native model discovery directly upstream.

OHR does not force an already-open client's picker/cache to refresh or replace
client-bundled aliases. Custom provider model IDs remain explicit routing
configuration, not unrestricted upstream discovery. For a separate remote
catalog URL, see [Codex gateway discovery](https://learn.chatgpt.com/docs/enterprise/roll-out-a-gateway).

## Run modes

Both entrances share one provider registry and route custom models identically.

### Forward-proxy

`make run-proxy`. The client sets `HTTPS_PROXY=http://127.0.0.1:8788` and
`NODE_EXTRA_CA_CERTS` to the router's root CA, and leaves `ANTHROPIC_BASE_URL`
at its default. The router terminates TLS for `api.anthropic.com` and sends only
`POST /v1/messages` and `POST /v1/messages/count_tokens` into the provider
registry; other paths on that host reach the real upstream untouched, and other
hosts get a blind tunnel.

Use it for Claude Code as the daily tool: the CLI still believes it talks to
`api.anthropic.com`, so the full feature set works. Remote Control (`/rc`),
server-managed settings, MCP tool search and fine-grained tool streaming on
their defaults, session sync, usage display, everything the CLI does there.

That belief also switches on one first-party protocol feature the router has
to serve itself: message threads (Claude Code v2.1.278+, beta
`message-threads-2026-08-12`). The first request of a turn carries the whole
conversation with `thread: {"type": "create"}`; the tool-result steps that
follow carry only the delta -- the attribution block as the entire `system`,
the new tool results, no earlier messages, no tools -- with `thread: {"type":
"continue", "previous_message_id": ...}`, expecting the upstream to hold the
rest under the id of the previous response. Passthrough forwards both
untouched: Anthropic holds the thread. For `openai-translate` the router
holds it (`src/services/thread_store.py`): every response handed out under a
`thread` field is recorded together with the request that produced it, and a
continuation is rebuilt, before translation, into the full request the client
would have sent without threads -- the held system prompt with the
attribution block refreshed from the delta, the held messages, the recorded
assistant turn, the delta's messages, the held tools. The store keeps the
last 64 responses for an hour (`THREAD_STORE_*` in `src/const.py`; the client
only ever continues from its latest one). A continuation it cannot resume --
after a restart, an eviction or the TTL -- is answered with `400
invalid_request_error` without calling the upstream, and the CLI resends the
same step as a full `create` request (measured on 2.1.278, whatever the error
wording). Forwarding such a delta instead would make the upstream answer a
request with no system prompt and no question: a gateway that fills in a
default prompt of its own greets the user in the middle of a tool loop. The
log shows `thread_resumed` for every rebuilt request and
`thread_continue_rejected` with a `reason` for every miss; the reject carries
`x-ohr-capability-rejected: thread_continue` and shows in the dashboard feed
as `rejected`, not as an error. Behind a custom `ANTHROPIC_BASE_URL` the CLI
never sends threads.

The price: trust the root CA once and run the proxy listener with
`ROUTER_PROXY_ENABLED=true`. `HTTPS_PROXY` covers ALL outbound traffic of the
CLI process, so a router that is down takes the whole CLI off the network, not
only inference. The setup steps are in the "Forward-proxy" section below.

### Reverse-proxy

`make run`. Claude Code sets `ANTHROPIC_BASE_URL=http://127.0.0.1:8787` and
needs no certificate. The same listener serves the Messages endpoints,
`/health`, the dashboard and metrics, plus Codex's `/v1/responses`,
`/v1/models`, `/codex/models` and the native JSON Images endpoints above.
Claude Code's picker is still built by
`make sync-client-config`; the Codex setup is described above.

This is the router's plain HTTP API: any client that only takes a base URL
(Anthropic SDK scripts, other agent frameworks, IDE plugins), `curl`,
`/health`, the end-to-end checks and the restart script. It runs on Linux, in
containers and in CI without a MITM step, and it is the fallback when the proxy
layer misbehaves. Claude Code works here too when `/rc` is not needed.

Not supported or changed for Claude Code in this mode:

- Remote Control (`/rc`): disabled by the CLI whenever `ANTHROPIC_BASE_URL` is
  not `api.anthropic.com` (Claude Code docs, remote-control, Requirements;
  since v2.1.196).
- Server-managed settings: not applied for a non-first-party host (docs,
  feature-availability).
- MCP tool search: off by default for a non-first-party host; managed settings
  can turn it back on since v2.1.227 (docs, feature-availability, MCP servers).
- Fine-grained tool streaming: off by default behind a custom base URL;
  `CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING=1` turns it on (docs,
  llm-gateway-protocol, Feature pass-through).
- A startup probe `HEAD /api/hello` gets 404 from the router. Harmless, the
  session runs normally.

Still works, measured on 2026-09-06 with a non-interactive `claude -p` run
against 8787: the CLI sends only inference to the base URL, so account and
usage, telemetry, bootstrap and feature flags, the MCP registry and session
sync go to `api.anthropic.com` directly and keep working. OAuth subscription
login and token refresh, prompt caching and `anthropic-beta` headers (passthrough
forwards them), `count_tokens` (served by the router) and the fast-mode
availability check (goes direct) work in both modes.

| | Forward-proxy | Reverse-proxy |
| --- | --- | --- |
| Client setting | `HTTPS_PROXY` -> 8788 | `ANTHROPIC_BASE_URL` -> 8787 |
| Certificate step | `NODE_EXTRA_CA_CERTS`, mandatory | none |
| Custom-model routing | same registry | same registry |
| Remote Control | works | disabled by the CLI |
| Server-managed settings | applied | not applied |
| MCP tool search / fine-grained streaming | defaults kept | off by default |
| `/health` and `curl` | not on this port | yes, on 8787 |
| Clients other than Claude Code | need HTTPS proxy support | any base-URL client |
| When the router is down | CLI loses all network | only inference fails |
| Where the setup is described | "Forward-proxy" | "Connecting Claude Code" |

How to choose: forward-proxy for Claude Code, unless `/rc` is not needed or the
client cannot use an HTTPS proxy. `make run-proxy` keeps both listeners in one
process, so both serve at once: a session through 8788, checks through 8787.

## Configuration

The provider and rule registry lives in `routing.yaml`. Adding a
provider/model is a YAML edit, no code changes. Provider types:

- `passthrough` -- reverse-proxy to an Anthropic-compatible upstream, no
  translation. Setting `upstream_model` on a routing rule pointing at a
  passthrough provider is a startup error: byte-for-byte proxying forwards
  the request body, including the `model` field, unchanged and cannot
  rewrite it (`upstream_model` is only meaningful for `openai-translate`).
  Responses are relayed as received, streaming and regular alike: the body
  bytes and the upstream's `content-encoding` reach the client untouched,
  so the encoding the client negotiated (Brotli, for Claude Code) is the
  client's to decode. Decoding in the router would leave the body encoded
  whenever the upstream picks a codec the router lacks, which is exactly
  what used to turn the auto-mode classifier's unary verdicts into
  unreadable JSON.
  The request target's query string travels with the body: Claude Code
  posts to `/v1/messages?beta=true`, and the upstream sees the same target.
  Supports `ca_bundle` (for a corporate/self-hosted Anthropic-compatible
  gateway behind a private CA, same as `openai-translate` below) and
  `extra_headers` (merged in after this provider's own auth handling, so it
  cannot reintroduce a stripped client credential -- but it also must not
  name `authorization`/`x-api-key` itself, or it would silently override
  the auth header this provider forwards or injects; rejected at startup);
- `openai-translate` -- translates to OpenAI ChatCompletions (its own
  `base_url`, key via `api_key_env`, optional `ca_bundle` in `certs/`). On
  the `chat` flavor, `system`-role messages that the client appends inside
  `messages` (not the top-level `system` prompt) are forwarded as user
  text: some open-model chat templates stop generating on a trailing
  system message and return an empty completion. Deferred tools stay
  hidden as they do on the Claude API: a tool marked `defer_loading`
  (Claude Code's MCP tool search) goes upstream only once a
  `tool_addition` block in the conversation references it, and the
  `DeferredToolPlaceholder` entry never does -- so a translated model sees
  the tools it could actually call, not every MCP schema, and cannot call
  a tool Claude Code has not loaded. A reference to a tool the request
  does not define is logged as `tool_addition_unresolved` and skipped.
  A translated stream is kept alive the way the Claude API keeps its own:
  when no visible event has been produced for `STREAM_KEEPALIVE_INTERVAL_S`
  (10 s, `src/const.py`) -- a reasoning upstream thinking, a gateway
  holding the request in its queue -- the router sends the client a `ping`
  event, so Claude Code's silent-stream watchdogs do not abort the request
  and resend the whole conversation. A stream that needed pings is logged
  as `stream_keepalive` with the count and the longest gap.

`passthrough` has two mutually exclusive auth modes, set by
`forward_client_auth` (`ProviderCfg`, `src/routing/schema.py`):

- `forward_client_auth: true` (the default, for backward compatibility)
  forwards the client's own request headers unchanged, including
  `authorization`/`x-api-key` -- the native-Anthropic path, billed through
  the Claude Code OAuth subscription. Because this path legitimately
  proxies the client's full request to the client's own vendor, it is not
  narrowed to a fixed set of headers (Claude Code features can ride
  uncommon ones). Setting `api_key_env` alongside it is a startup error:
  the client's credential is what reaches the upstream, so a configured key
  here would silently do nothing;
- `forward_client_auth: false` injects the provider's own key from
  `api_key_env` instead, under an ALLOWLIST of outgoing headers -- not a
  denylist over just `authorization`/`x-api-key`, which would still leak
  any other credential the client happens to carry (e.g. a cookie session
  token) to a vendor the client did not choose. Only `anthropic-version`,
  `anthropic-beta`, `content-type`, `accept`, `accept-encoding`, and
  `user-agent` (kept verbatim, never rewritten -- Kimi's terms of service
  treat client-identifier tampering as grounds for suspending the
  membership) are forwarded, plus the injected key. This is what makes a
  third-party Anthropic-compatible upstream usable via passthrough (Kimi,
  Z.ai, DeepSeek, MiniMax, ...) without ever letting the client's Claude
  Code subscription credential -- or any other credential it carries --
  reach it: Anthropic bans using a subscription credential with
  third-party products. `api_key_env` is required in this mode (startup
  error if missing). `auth_header` (`bearer` or `x-api-key`, default
  `bearer`, rejected at startup if set anywhere this mode isn't active)
  picks which header the own key goes into -- third-party upstreams
  disagree on this even when both expose an otherwise Anthropic-compatible
  endpoint.

  An own-key provider may also set `fold_tool_additions: true` (rejected on
  any other provider) for an upstream that answers `400` to Claude Code's
  mid-conversation `tool_addition` blocks, which appear from the second turn
  of an agent loop (Kimi: "Invalid request Error"). The router then removes
  the blocks and sends only the tools the model could see -- deferred ones
  once referenced, `DeferredToolPlaceholder` never, `defer_loading` dropped
  -- the one exception to byte-for-byte forwarding; a request with nothing to
  fold is forwarded untouched.

Because of this, `default.provider` must be a passthrough provider with
`forward_client_auth: true`, not merely `type: passthrough` -- an unmatched
model must never silently land on a third-party vendor billed on its own
key, any more than on a fleet model (see "How rules work" below).

At startup, each passthrough provider logs a `passthrough_auth_mode` event
(`provider`, `own_key`, `auth_header`) -- so which credential actually goes
out on the wire for that provider is visible in the logs, not only on the
vendor's invoice.

### Upgrading

`forward_client_auth` now defaults to `true` and is enforced (previously it
was declared but never read). If an existing `routing.yaml` has a
passthrough provider with `forward_client_auth: false` and no
`api_key_env`, the router now refuses to start -- the validation error
names the missing field and how to fix it (add `api_key_env`, or switch to
`forward_client_auth: true` if forwarding the client's own credentials was
intended). Since `ProviderRegistry.build` constructs every provider
eagerly, this failure blocks the whole router, not just that one provider,
so it surfaces immediately rather than as an in-production surprise.

Two more previously-inert fields on passthrough are now enforced/honored,
with the same eager-boot consequence as above (one bad provider blocks the
whole router, not just itself):

- `api_key_env` on a `forward_client_auth: true` (the default) passthrough
  provider used to be accepted and silently ignored; it is now a startup
  error (the client's own credentials are what get forwarded, so a
  configured key would never be used).
- `ca_bundle` on a passthrough provider used to be accepted and silently
  ignored (the transport always verified against the system trust roots
  regardless); it is now resolved and applied. A `ca_bundle` left over from
  before this change either points at a file that doesn't exist (startup
  `ConfigError`) or, if it does exist, now actually replaces the system
  trust roots for that provider's outgoing connections -- a passthrough
  provider that never needed a private CA (e.g. native `api.anthropic.com`)
  would start failing every request with TLS verification errors (502) if
  it happened to carry a stray `ca_bundle`. Remove `ca_bundle` from any
  passthrough provider that doesn't actually sit behind a private CA.

Three new rule-level validators can break an existing config at startup
when a rule no longer satisfies them. Each error names the offending rule
and how to fix it; as above, one bad rule blocks the whole router:

- Every model id a rule advertises -- its `client_models` entries and, for
  an `exact` rule, its own match value -- is now checked against the rules
  above it. An `exact` rule whose id an earlier
  `prefix`/`contains`/`regex` rule already captures used to load silently
  and never match; it is now a startup error naming both rules. Same for an
  id listed in two rules. Move the exact rule above the broader one, or
  delete it.
- Rule-level `max_tokens_limit` and `context_window` are now only allowed
  on rules that point at an `openai-translate` provider; on a `passthrough`
  rule they are a startup error, like `upstream_model` already was.
- `context_window`, where set, must exceed `max_tokens_limit` plus
  `CONTEXT_WINDOW_RESERVE_TOKENS` (512) plus `MIN_USEFUL_COMPLETION_TOKENS`
  (4096); a smaller window is a startup error because no request could ever
  fit. Only configs that set the new field are affected.

Required variable: `ROUTER_CONFIG_PATH` (path to `routing.yaml`).
Provider secrets are set via environment variables whose names are declared in
`routing.yaml` (`api_key_env`).

Same pattern as `.env`/`.env.example`: `routing.example.yaml` is the example
checked into the repo, `routing.yaml` is your personal working config
(`cp routing.example.yaml routing.yaml`, see "Installation"), already in
`.gitignore` and never committed.

`routing.example.yaml` is a minimal working example: out of the box only
`anthropic` is active (passthrough, billed through the Claude Code OAuth
subscription, no key needed). That's enough to get the router running right
after cloning. The other providers (GLM via a corporate gateway, OpenAI
GPT-5.x on the Responses API, Kimi/Moonshot AI, etc.) are commented out in the
file as templates, along with the routing rules that point to them -- to
enable a provider, uncomment its block under `providers:`, the matching rule
under `rules:`, and set the key environment variable (`api_key_env` in the
template) in your own `routing.yaml`.

Edit `routing.yaml` freely (multiple providers, internal corporate hosts,
etc.) -- git ignores it, so repo updates (`routing.example.yaml`) never
conflict with your personal keys and hosts.

## Working with routing

### How to add a provider

Adding a provider/model is an edit to your own `routing.yaml`; no code
changes are needed:

1. Uncomment the relevant template block under `providers:` (e.g.
   `openai_compatible`, `openai`, `kimi`, or one of the templates below) and
   set the real `base_url` (for a corporate gateway, also a `ca_bundle` in
   `certs/`).
2. Set `api_key_env` -- the name of the environment variable holding the
   key -- and put the actual key in `.env`.
3. Uncomment (or write your own) matching rule under `rules:`.
4. Restart the router -- the config is read once at startup and is not
   hot-reloaded (for launchd -- `launchctl kickstart -k`, for systemd --
   `systemctl --user restart`, in a terminal -- restart `make run`).

### The add-provider skill

A guided version of the procedure above ships as a Claude Code skill,
`.claude/skills/add-provider`: it turns a working cURL example (plus the CA
certificate files, when the gateway sits behind a private CA) into a provider
block, alias rules, a key in `.env` and a restarted router. It is
user-invoked only (`disable-model-invocation: true` in its frontmatter) --
the model never starts it on its own.

```
/add-provider <curl command> [cert.pem ...] [provider=<name>] [alias-prefix=<pfx->] [models=<upstream-id>[,...]]
```

Everything the cURL does not carry (provider name, alias prefix, the list of
models) is asked for in one question, so `/add-provider <curl> [cert.pem ...]`
is enough to start.

What it does, in this order: parses the cURL into provider fields (`base_url`
minus the endpoint suffix, `type` and `api_flavor`, auth header, upstream
model ids, extra headers); appends the key to `.env` under the `api_key_env`
name, in a single command, so the value never reaches a later one; matches
the given certificates against the bundles in `certs/` and builds a new
bundle only when none of them covers the input; smoke-tests the upstream
directly, bypassing the forward-proxy, before any config is touched;
determines `max_tokens_limit` and `context_window` per model and decides
which number belongs on the provider as its default and which on a rule;
writes the provider block and one `exact` rule per model into `routing.yaml`
after a timestamped backup; validates the result offline; restarts the
service with a health check and an automatic rollback; runs end-to-end
checks through the router for every alias; and, optionally, writes subagent
files and regenerates the client config. A cURL whose `base_url` already
belongs to a provider in `routing.yaml` takes the "one more model" path
instead: one new rule, the existing key and bundle, no second provider block.
Certificates found in `proxy-ca/` are copied, never modified -- that
directory belongs to the forward-proxy's own CA.

Three helper commands do the work that would otherwise be guesswork; all of
them are useful on their own and run from the repository root:

```bash
PYTHONPATH=src .venv/bin/python -m cli.validate_routing \
  [--expect-provider NAME] [ALIAS[=UPSTREAM_MODEL] ...]
PYTHONPATH=src .venv/bin/python -m cli.tls_probe match CERT.pem [CERT.pem ...] [--provider NAME]
PYTHONPATH=src .venv/bin/python -m cli.tls_probe probe --host HOST [--port 443] [--cafile certs/BUNDLE.pem]
make sync-client-config
```

- `cli.validate_routing` builds the provider registry through
  `main.build_runtime` -- the same code the service runs at startup, minus
  the listening socket -- so a missing key variable, an unreadable CA bundle
  or a misrouted alias surfaces here instead of crash-looping the service
  after a restart. The `=== ROUTES ===` table prints each route's effective
  `max_tokens_limit` and `context_window`, and every positional
  `ALIAS=UPSTREAM_MODEL` is resolved and compared with what the rules
  actually produce. Exit 0 -- the config builds and all expectations hold;
  1 -- it does not build, with the same `open-harness-router: ...` message
  the service would write to its err.log; 3 -- an alias resolved to a
  different provider or upstream model than expected (or
  `--expect-provider` names a provider that was not built).
- `cli.tls_probe match` fingerprints the given PEM files and compares them
  with every bundle in `certs/`: it prints `REUSE certs/<name>` (exit 0)
  when one bundle already contains all of them, otherwise the `cat` command
  that would create a new one (exit 10), and exits 2 when an input cannot be
  read or holds no certificate. It never writes a file itself. Every
  `WARNING:` line goes to stderr, including the certificates (subject,
  notAfter, sha256) a reused bundle carries beyond the input -- the new
  provider will trust those too. Neither subcommand uses exit 1 -- that is
  the status Python gives an uncaught exception, so a crash can never be
  read as a result.
- `cli.tls_probe probe` performs one TLS handshake with exactly the trust
  store the router would use for that provider (`httpx.create_ssl_context`
  over `build_upstream_verify` in `src/services/http_transport.py`, not a
  default context whose roots depend on the interpreter build), and builds
  it with `trust_env=False` exactly as the router's own transport does, so
  `SSL_CERT_FILE` and `SSL_CERT_DIR` in the shell cannot move the verdict.
  That is what separates a host-name mismatch from a broken chain:
  `CHAIN_OK_HOSTNAME_OK` (exit 0) needs nothing beyond `ca_bundle`,
  `BUNDLE_UNUSABLE` (2) means the `--cafile` bundle could not be loaded and
  no handshake was attempted, `CHAIN_OK_HOSTNAME_MISMATCH` (11) is the only
  case that justifies
  `tls_verify_hostname: false`, `CHAIN_FAIL` (12) means wrong or incomplete
  certificates, and `CONNECT_FAIL` (13) means there was no TLS session at
  all (DNS, VPN, port, firewall).
- `make sync-client-config` regenerates the `/model` picker rows from the
  new rules (see "Model picker and client settings").

The restart is one blocking command rather than a bare `launchctl kickstart`:

```bash
bash .claude/skills/add-provider/scripts/restart_router.sh \
  --expect-provider NAME --routing-backup routing.yaml.bak-<TS>
```

It returns only after `/health` answers 200 from a NEW pid whose provider
list contains the expected name -- right after `kickstart -k` the old
process can still answer with the old config while it drains, and an
already-known provider name alone cannot tell the two apart. The script
needs the real launchd label: it defaults to the placeholder
`com.example.open-harness-router`, resolved as env `OHR_LAUNCHD_LABEL` ->
a gitignored `.launchd-label` file at the repository root (its first
non-blank line) -> the placeholder; without either, a zero-argument run
exits 65 with a hint naming the file. On timeout -- or on a startup error
in the error log (`open-harness-router: ...` line or a traceback), still
only when `--routing-backup` was given -- it keeps the rejected config as
`routing.yaml.failed-<epoch>`, copies the backup back over `routing.yaml`,
restarts again and exits 1 (0 -- healthy on the new config, 2 -- bad
arguments, an empty `.launchd-label`, the service down, or rollback
impossible, 64 -- not macOS, 65 -- the service is not loaded in the user's
launchd domain). This restart step is launchd-only: on any other OS the
script exits with code 64 and a systemd hint, so on Linux restart the
service by hand and point `OHR_ERR_LOG` at your own log.
`OHR_HEALTH_TIMEOUT_S` bounds the wait in wall-clock seconds. Only the
top-level session should run it, and only with no subagents in flight:
during a Bash call the session itself holds no open stream, but a running
subagent does, and `kickstart -k` cuts it off.

Site-specific facts -- internal gateway hosts, where keys are issued, which
model ids a deployment serves, which chain it presents -- stay out of the
repository. They live in
`.claude/skills/add-provider/references/local/`, gitignored via
`.claude/skills/*/references/local/`, and the skill reads the note matching
a host before asking anything. The committed template is
`.claude/skills/add-provider/references/local.example.md`.

### How rules work

Rules under `rules:` are checked top to bottom, and the first match wins
(`ProviderRegistry.resolve`, `src/routing/registry.py`) -- order matters:
specific rules must come before general ones, or a broader rule will shadow
them first.

`match.type` values (`src/routing/matcher.py`):

- `exact` -- exact match of the model name;
- `prefix` -- model name starts with `value`;
- `contains` -- `value` occurs anywhere in the model name as a substring;
- `regex` -- `re.search(value, model)` (a search, not anchored to the start).

`upstream_model` in a rule (optional) -- the name the request is sent
upstream under, when it must differ from what the client sent. Only valid on
rules pointing at an `openai-translate` provider: on `passthrough`, setting
it is a startup error (see "Configuration" above).

`max_tokens_limit` and `context_window` in a rule (optional) -- per-model
limits that override the provider's own values for the models this rule
serves; unset means the provider's value applies. One gateway usually hosts
several models whose limits differ (one silently clamps an oversized
completion budget, the next answers HTTP 500; one is served on a 223K
window, the next on a 1M one), and without the override each of them needs
its own provider block repeating the same `base_url`, `api_key_env`,
`ca_bundle` and `extra_headers`. Give the provider the conservative pair as
its default and raise it on the rule of a model that was measured. Both are
startup errors on a rule pointing at a `passthrough` provider (no
conversion, so neither the cap nor the pre-flight ever runs), and the
EFFECTIVE pair must satisfy the same invariant the provider's does --
`context_window` greater than `max_tokens_limit`.

`client_models` in a rule (optional) -- the exact model ids a client may
send for this rule, used by `make sync-client-config` to build the `/model`
picker (see "Model picker and client settings"). A `prefix`/`contains`/`regex`
match value is a pattern and not a usable id, so those rules can only be
advertised through this list; an `exact` rule already is its own id. Each
entry is validated at startup against its own rule and against the rules
above it -- an id an earlier rule also matches would never arrive here.

If no rule matches, `default.provider` is used. The schema
(`src/routing/schema.py`) requires `default.provider` to be a `passthrough`
provider with `forward_client_auth: true` -- this is a safety invariant: an
unmatched model must never accidentally end up on a fleet provider or on a
third-party vendor billed on its own key (see "Configuration" above for the
two passthrough auth modes).

### How to check where a model will go

For every `/v1/messages` request (and `/v1/messages/count_tokens`) the router
logs a `route` event with the fields `model`, `provider`, and (for
`/v1/messages` only) `upstream_model` (`src/api/messages.py`,
`src/api/count_tokens.py`). Example log line:

```json
{"event": "route", "endpoint": "messages", "model": "claude-sonnet-5", "provider": "anthropic", "upstream_model": null, "level": "info", "timestamp": "2026-08-21T09:12:03.123456Z"}
```

To filter for just these events:

```bash
tail -f ~/Library/Logs/open-harness-router.log | jq 'select(.event == "route")'
```

This is the main routing-debugging tool -- `provider` and `upstream_model`
immediately show whether the expected rule fired.

To see the full routing table without opening the YAML, the startup event is
enough: on every launch the router logs the pool of served models in the
`routes` field -- in the `startup` event for ASGI mode and `proxy_startup`
for forward-proxy. Entries appear in the same order rules are checked when
resolving a route, with the default entry last:

```bash
tail -f ~/Library/Logs/open-harness-router.log | jq 'select(.event == "startup") | .routes[]'
```

```json
{"match_type": "prefix", "match_value": "claude-", "provider": "anthropic"}
{"match_type": "contains", "match_value": "GLM", "provider": "openai_compatible", "upstream_model": "zai-org/GLM-5.2-FP8", "max_tokens_limit": 65536, "context_window": 206650}
{"match_type": "default", "provider": "anthropic"}
```

The two limits are the effective ones for that route (the rule's overrides
folded onto the provider's values), so two models on one provider show
their own numbers; fields the route does not have are omitted.

Useful after editing the config: if the expected rule is missing from the
list, or sits below a broader one, either the router restarted with a stale
file or the rule order is wrong.

### Provider compatibility settings

Fields of an `openai-translate` provider (`ProviderCfg`,
`src/routing/schema.py`) that should not be copied blindly:

- `max_tokens_limit` -- required for `openai-translate` (without it the
  provider fails validation at startup). Caps `max_tokens` on the outgoing
  request; for reasoning models a low cap is dangerous -- reasoning tokens
  burn the budget before any visible text appears, and the upstream returns
  empty `content` with `stop_reason max_tokens`. It is the default for
  every rule on this provider; a rule may override it per model (see
  "How rules work").
- `context_window` -- optional, the deployment's total context in tokens
  (prompt plus completion). Must be greater than `max_tokens_limit` plus
  `CONTEXT_WINDOW_RESERVE_TOKENS` (512) plus `MIN_USEFUL_COMPLETION_TOKENS`
  (4096). See "Context window and token counting" below.
- `drop_params` -- which top-level request parameters to strip before
  sending (only `temperature`, `top_p`, `stop` are allowed); needed when the
  upstream errors out on an unsupported parameter instead of silently
  ignoring it.
- `token_param` -- which field the upstream expects the output limit in:
  `max_tokens` (default), `max_completion_tokens`, or `max_output_tokens`
  (Responses API) -- different upstreams name it differently.
- `tools_max` -- a hard limit on the size of the `tools` array (`0` -- no
  limit); when trimming, the client's built-in tools are kept and the MCP
  tail is cut off.
- `api_flavor` -- `chat` (`/v1/chat/completions`, default) or `responses`
  (`/v1/responses`, allowed only for `openai-translate`): some reasoning
  models reject function tools combined with reasoning on the chat endpoint
  and return a 400.
- `quota_wait_max_s` -- how long a request may wait for the provider's
  output-token quota window to reopen (default 300, `0` disables the wait; Claude Code itself gives up on
  response headers after 360 s).
  Some gateways meter completion tokens per key over a sliding window of a
  few minutes and refuse with a status Claude Code treats as final (seen:
  `422` "Превышен лимит completion-токенов ... Повторите попытку через 3
  мин."), which kills a turn or a subagent although the same request would
  pass minutes later. A refusal carrying such a retry hint (a `422` or `429`
  with quota wording and "retry in N seconds/minutes") closes a gate shared
  by all of the provider's requests: they wait until the hinted moment and
  leave one by one, two seconds apart, instead of each spending another
  refusal. A window longer than the budget is answered with
  `429 rate_limit_error` and `retry-after`. The header is capped at 60 s:
  Claude Code retries a `retry-after: 60` exactly a minute later, but gives
  up at once on a longer one; the error message names the real window. Log events: `upstream_quota_window_closed`,
  `upstream_quota_wait`, `upstream_quota_exhausted`.

`timeout_s` on `ProviderCfg` (either provider type) is currently unused --
only `connect_timeout_s` (`ROUTER_UPSTREAM_CONNECT_TIMEOUT_S`, shared
across all upstreams) and `stream_read_timeout_s` (per provider) ever reach
`httpx.Timeout`. Setting `timeout_s` in `routing.yaml` parses without error
but has no effect on either provider type.

### Context window and token counting

`context_window` (`ProviderCfg`, `src/routing/schema.py`) -- the upstream
model's total context size in tokens, prompt plus completion. Optional and
`openai-translate`-only: an explicit value on `passthrough` is a startup
error (the body is forwarded byte-for-byte and never estimated), and the
value must be greater than `max_tokens_limit` plus
`CONTEXT_WINDOW_RESERVE_TOKENS` (512) plus `MIN_USEFUL_COMPLETION_TOKENS`
(4096). Unset means no estimate and no pre-flight: the request goes
upstream exactly as before.

The number the guard actually uses is the EFFECTIVE one for the resolved
route: `RoutingRule.context_window` where the matching rule sets it, the
provider's value otherwise (same for `max_tokens_limit`). The registry
resolves the pair once per request and hands it to the provider
(`RouteLimits`, `src/routing/registry.py`), which is what lets one gateway
serve several models with different windows from a single provider block --
the conservative pair on the provider, the measured one on the rule of each
model that was probed.

`make sync-client-config` writes
`CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT=1` (see "Model picker
and client settings"), so from then on the router's pre-flight is the only
guard: an `openai-translate` model with no effective `context_window`
(neither on its rule nor on its provider) has none at all, which is why
`cli.sync_client_config` refuses to advertise such a model.

Set it to the deployment's real limit, not the model card's: a serving
stack such as vLLM caps at its `max_model_len`, which is often lower than
the architecture allows. Ways to find it: the model's entry in the
upstream's `/v1/models` (fields such as `max_model_len` or
`context_length`, when the upstream exposes them), the vendor's
documentation, or the upstream's own error text on an oversized test
request (an OpenAI-compatible context-length 400 usually names the limit).
`/add-provider` (`.claude/skills/add-provider`) walks through this as part
of its guided procedure.

`POST /v1/messages/count_tokens` is served per provider type
(`src/api/count_tokens.py`):

- `passthrough` -- proxied to the upstream's own
  `/v1/messages/count_tokens`, so the count is the vendor's;
- `openai-translate` -- estimated locally
  (`src/services/token_estimator.py`), since most OpenAI-compatible
  upstreams have no count endpoint. The count request goes through the
  same request builder as `/v1/messages`, so it is a character heuristic
  over the exact converted wire payload, calibrated 2026-09-04 against
  `usage.input_tokens` of two fleet models to over-count by 1.10-1.33.

Overflow behaviour when `context_window` is set
(`OpenAITranslateProvider._enforce_context_window`,
`src/providers/openai_translate.py`), streaming and non-streaming alike,
before any upstream call:

- budget = `context_window - CONTEXT_WINDOW_RESERVE_TOKENS` (512,
  `src/const.py`; the reserve absorbs what the heuristic cannot see --
  chat-template tokens, tool-call framing, upstream-side additions);
- if the estimated prompt leaves less than `MIN_USEFUL_COMPLETION_TOKENS`
  (4096, `src/const.py`) of that budget, the request is rejected without
  calling the upstream:
  HTTP 400 `invalid_request_error` with the message `prompt is too long:
  <N> tokens > <M> maximum (capability_rejected: prompt_too_long)`, and a
  `context_window_reject` log event (`provider`, `model`, `estimate`,
  `context_window`);
- otherwise the completion budget -- `max_tokens` (under the provider's
  `token_param` name) or `max_output_tokens` on the `responses` flavor --
  is clamped to what remains, and a `context_window_clamp` event
  (`estimate`, `requested`, `clamped`) is logged only when the value
  actually changed.

Upstream context-length 400s (texts such as `maximum context length`,
`context length exceeded`, `prompt is too long`) are remapped to the same
400 `invalid_request_error` shape with the same token, so the client sees
one error whether the router or the upstream caught the overflow. The
remap covers 400s only: an upstream that answers an overflow with a 5xx
instead (seen on a self-hosted fleet deployment) is retried by the SDK
like any server error and never reaches the remap -- there the pre-flight
above is the only protection, which is the main reason to set
`context_window` for such a deployment.

How Claude Code reacts, from its official docs:

- `count_tokens` is optional in the gateway protocol: when the endpoint is
  absent, Claude Code counts context usage through the inference endpoint
  instead (`llm-gateway-protocol.md`, "Optional endpoints and startup
  traffic"). The router serves it for both provider types, so counts do
  not consume inference requests.
- A request rejected because the input plus `max_tokens` exceeds the
  context limit is retried with a reduced `max_tokens`; Claude Code stops
  retrying and compacts when no reduction can fit (`errors.md`,
  "Automatic retries"). The recovery matches on the error wording, so a
  gateway that wraps errors breaks it unless the message carries a stable
  `capability_rejected:` token (`llm-gateway-protocol.md`, "Automatic
  retry and error forwarding"); `capability_rejected: prompt_too_long` is
  treated the same as `Prompt is too long`, recognized from Claude Code
  v2.1.228 (`errors.md`, "Prompt is too long"). That is why the router's
  reject and remap messages carry the token.
- For a model ID it does not recognize (a fleet alias), Claude Code
  compacts at the context window it assumes for the ID;
  `CLAUDE_CODE_MAX_CONTEXT_TOKENS` declares the window it should assume
  instead (applies directly when the ID neither starts with `claude-` nor
  contains `[1m]`), and the auto-compact threshold is set with
  `/autocompact <value>` (saved as the `autoCompactWindow` setting), the
  `--autocompact` flag, or `CLAUDE_CODE_AUTO_COMPACT_WINDOW`
  (`model-config.md`, "Context window and auto-compaction"). With
  `CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT=1` it compacts
  only after the API rejects the conversation with a too-long error it
  recognizes -- the router's reject and remap produce exactly that
  wording. Either way the router's guard is the backstop for when the
  assumed window exceeds the deployment's real one.

## Running

```bash
make run          # reverse-proxy: uvicorn main:create_app --factory
                  # (port ROUTER_SERVER_PORT, default 8787)
make run-proxy    # both listeners in one process:
                  # ROUTER_PROXY_ENABLED=true python -m entrypoint
                  # (port ROUTER_PROXY_PORT, default 8788)
```

Forward-proxy details (why it's needed, how to point a client at it,
certificate trust) -- in the "Forward-proxy" section below.

## Running in the background

For everyday use it's more convenient to run the router as a background
service rather than in an interactive terminal. Below: launchd for macOS (the
primary option) and systemd for Linux.

### macOS (launchd)

A user-level LaunchAgent (no `sudo`, starts at login). Example for
reverse-proxy mode:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.example.open-harness-router</string>

    <key>ProgramArguments</key>
    <array>
        <string>/path/to/open-harness-router/.venv/bin/uvicorn</string>
        <string>main:create_app</string>
        <string>--factory</string>
        <string>--host</string>
        <string>127.0.0.1</string>
        <string>--port</string>
        <string>8787</string>
        <string>--app-dir</string>
        <string>src</string>
    </array>

    <key>WorkingDirectory</key>
    <string>/path/to/open-harness-router</string>

    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>SoftResourceLimits</key>
    <dict>
        <key>NumberOfFiles</key>
        <integer>8192</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>/Users/USERNAME/Library/Logs/open-harness-router.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/USERNAME/Library/Logs/open-harness-router.err.log</string>
</dict>
</plist>
```

Key fields:

- `Label` -- a unique service identifier, used in every `launchctl` command.
- `ProgramArguments` -- the absolute path to `uvicorn` inside `.venv`, not
  `uv run uvicorn`: launchd runs agents with a minimal `PATH`
  (`/usr/bin:/bin:/usr/sbin:/sbin`), where `uv` is usually unavailable. The
  arguments after the binary are the same as in `make run`
  (`--factory --host --port --app-dir src`).
- `WorkingDirectory` -- the project root. Important: `Settings` (see
  `src/settings.py`) reads `.env` via a relative path (`env_file=".env"`),
  and pydantic-settings resolves it relative to the current working
  directory -- so a separate `EnvironmentVariables` block in the plist isn't
  needed; a `.env` in the project root with the required
  `ROUTER_CONFIG_PATH` and the provider keys is enough.
- `RunAtLoad` -- start immediately when the agent loads (i.e. at user login).
- `KeepAlive` -- launchd restarts the process on crash.
- `ThrottleInterval` -- the minimum interval between automatic restarts, in
  seconds, so a crash loop doesn't hog the CPU and flood the log.
- `SoftResourceLimits` / `NumberOfFiles` -- the process's open-file limit.
  launchd starts agents with 256 (`launchctl limit maxfiles`), and a
  forward-proxy holds two descriptors per tunnel plus one per connection
  attempt in flight, so a burst of parallel clients (a package manager
  fanning out through `HTTPS_PROXY`, a fleet of subagents) exhausts 256 in
  seconds: `accept()` fails with `EMFILE` and everything behind the router
  goes offline. The service raises its own soft limit to 8192 at startup
  (`services.open_files`; the ceiling in force is logged as
  `open_files_limit` in `proxy_startup`), so the plist entry is a second
  line of defense that also documents the intent.
- `StandardOutPath` / `StandardErrorPath` -- stdout and stderr go to separate
  files.

`bin/open-harness-router` in the repository is a ready-made
`ProgramArguments` target: it execs `.venv/bin/python -m entrypoint` from
the repository root, so the macOS Background Items entry carries the
service name instead of `python`. Use it in place of the `uvicorn` path
above when one agent should also be able to start the forward-proxy; it
needs `PYTHONPATH=src` in `EnvironmentVariables`, because `src` is not
installed into the venv.

First save the plist above to `~/Library/LaunchAgents/<your label>.plist`,
replacing the placeholder `com.example.open-harness-router` label and the
`/path/to/...` program path with the real values. Then open the LaunchAgent:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.open-harness-router.plist
```

Put the same label into a `.launchd-label` file at the repository root (one
line, gitignored): the add-provider skill's restart script looks the service up
by that label.

For management (current `bootstrap`/`bootout` syntax, the `gui/<uid>` domain of
the user session):

```bash
# enable (after the plist is placed in ~/Library/LaunchAgents/)
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.open-harness-router.plist

# disable
launchctl bootout gui/$(id -u)/com.example.open-harness-router

# restart without unloading
launchctl kickstart -k gui/$(id -u)/com.example.open-harness-router

# status (state, pid, last exit code)
launchctl print gui/$(id -u)/com.example.open-harness-router
```

`kickstart` restarts the process under the plist as it was loaded: after
editing the plist (for example the `SoftResourceLimits` block), run `bootout`
and then `bootstrap` again, otherwise the change never reaches the process.

The old `launchctl load/unload ~/Library/LaunchAgents/<label>.plist` syntax
still works, but Apple recommends `bootstrap`/`bootout`.

Logs go to the files from `StandardOutPath`/`StandardErrorPath`. The format
is structured JSON (structlog + a custom JSON formatter, see
`logging_conf.json`), one record per line, so it's easiest to view with `jq`:

```bash
tail -f ~/Library/Logs/open-harness-router.log | jq .
tail -f ~/Library/Logs/open-harness-router.log | jq 'select(.level == "error")'
```

#### Both modes at once

`make run-proxy` serves both ports from one process (`python -m entrypoint`
with `ROUTER_PROXY_ENABLED=true`), but under launchd the forward-proxy can
also stay a separate agent: `python -m proxy.server` is still a standalone
entry point that starts the proxy alone, and it needs its own `Label`, its
own `ProgramArguments`, its own log files. `python -m` has no equivalent of
the `--app-dir` flag that `uvicorn` uses to add `src/` to `sys.path`, so it
needs an explicit `EnvironmentVariables` with `PYTHONPATH=src` instead (in
the Makefile this is done by `PYTHONPATH=src uv run python -m entrypoint`;
launchd doesn't read the Makefile, so the variable must be set in the plist):

```xml
    <key>Label</key>
    <string>com.example.open-harness-router-proxy</string>

    <key>ProgramArguments</key>
    <array>
        <string>/path/to/open-harness-router/.venv/bin/python</string>
        <string>-m</string>
        <string>proxy.server</string>
    </array>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONPATH</key>
        <string>src</string>
    </dict>

    <key>WorkingDirectory</key>
    <string>/path/to/open-harness-router</string>

    <key>StandardOutPath</key>
    <string>/Users/USERNAME/Library/Logs/open-harness-router-proxy.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/USERNAME/Library/Logs/open-harness-router-proxy.err.log</string>
```

(`RunAtLoad`, `KeepAlive`, `ThrottleInterval` -- same as in the first plist.)
Both services can run in parallel: reverse-proxy listens on
`ROUTER_SERVER_PORT` (default 8787), forward-proxy on
`ROUTER_PROXY_PORT` (default 8788) -- two processes, each with its own
provider registry and its own connection pools, sharing nothing but
`routing.yaml` and the environment variables holding provider keys. One
agent running `python -m entrypoint` with `ROUTER_PROXY_ENABLED=true` and
`PYTHONPATH=src` in `EnvironmentVariables` covers both ports instead, from
a single process on a single registry.

### Linux (systemd)

A minimal user-level unit (`~/.config/systemd/user/open-harness-router.service`):

```ini
[Unit]
Description=open-harness-router reverse-proxy
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/open-harness-router
ExecStart=/path/to/open-harness-router/.venv/bin/uvicorn main:create_app --factory --host 127.0.0.1 --port 8787 --app-dir src
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

For forward-proxy -- a separate unit with `ExecStart=.../python -m
proxy.server` and `Environment=PYTHONPATH=src` (same reason as for launchd
above).

```bash
systemctl --user daemon-reload
systemctl --user enable --now open-harness-router.service
systemctl --user status open-harness-router.service
journalctl --user -u open-harness-router.service -f
```

launchd, systemd, and `docker stop` all stop the process the same way, via
`SIGTERM`. This matters for forward-proxy: signal handling and draining of
active connections are implemented in `src/proxy/server.py`
(`_SHUTDOWN_SIGNALS`, `_drain_connections`), so a graceful service stop
(`systemctl stop`, `launchctl bootout`) waits for open streaming responses to
finish instead of cutting them off mid-stream.

## Connecting Claude Code

You can resume a Claude Code conversation containing `thinking` or
`redacted_thinking` blocks on a translated route. The router omits these
model-specific blocks during translation and preserves visible text and tool calls.
It also accepts reasoning blocks with omitted payloads and empty tool results.
If another block prevents translation, the 400 identifies the first failing field
or block. For an unsupported block type, resume the conversation on a route
that supports it, or start a new conversation with a task summary.

Authentication on the native path is the claude.ai OAuth subscription. This
is a configuration documented as supported (`llm-gateway.md`, "Subscriptions
and gateways"): with `ANTHROPIC_BASE_URL` set and no gateway credential, the
CLI keeps the OAuth subscription as its credential and sends Bearer requests
to the custom URL. The router forwards them byte-for-byte to
`api.anthropic.com`, including the `anthropic-beta` header (required for
OAuth). Billing runs against the subscription, no per-token charges.

1. Log in to Claude Code with the subscription: `/login` (claude.ai OAuth).
2. Run the CLI without `ANTHROPIC_API_KEY` -- an API key takes priority over
   the subscription and will override it.

```bash
env -u ANTHROPIC_API_KEY \
  ANTHROPIC_BASE_URL=http://127.0.0.1:8787 \
  CLAUDE_CODE_SUBAGENT_MODEL=zai-org/GLM-5.2-FP8 \
  claude
```

The main conversation runs on native `claude-*` models (passthrough to
Anthropic via the subscription), subagents run on the model from
`CLAUDE_CODE_SUBAGENT_MODEL` (e.g. GLM via the corporate-gateway template,
see "Configuration" -- the provider and rule need to be uncommented). For
per-agent control, instead of the global variable you can set `model:` in
the frontmatter of individual agents under `.claude/agents/*.md`. That directory
and its files are your own Claude Code configuration, not part of this
repository -- create them yourself if they don't exist yet.

This is reverse-proxy mode; what the CLI turns off under a custom
`ANTHROPIC_BASE_URL`, Remote Control included, is listed in "Run modes" above.

### Model picker and client settings

Claude Code's `/model` picker does not discover the router's models on its own:
gateway model discovery keeps only ids containing `claude` or `anthropic`.
The `/v1/models` endpoint added for Codex does not change that filtering.
The way to list arbitrary ids
is the `modelPicker.options` setting (Claude Code v2.1.242 and newer), which
the CLI reads only from managed settings, from a file passed with
`--settings`, and from user settings.

`~/.claude/settings.json` is the wrong file to generate: the CLI writes to it
during a session (`/model` stores `model`, `/effort` stores `modelSettings`),
so a regenerated copy would drop what the session had just saved. Instead the
router generates a dedicated file and the wrapper passes it explicitly:

```bash
make sync-client-config      # -> ~/.claude/open-harness-router.settings.json
```

The command (`src/cli/sync_client_config.py`, also runnable as
`PYTHONPATH=src .venv/bin/python -m cli.sync_client_config`) reads
`routing.yaml` and writes two things:

- `modelPicker.options` -- one row per offered model, labelled with its
  provider and the effective context window and output cap of that model's
  own rule (so two models sharing a provider advertise their own limits). A
  `prefix`/`contains`/`regex` rule without `client_models` is skipped with a
  warning naming the rule, and the run fails only when no model can be
  listed at all. Rules serving a `passthrough` provider in NATIVE mode
  (`forward_client_auth: true`) are skipped -- native `claude-*` ids are
  already in the picker. A `passthrough` provider in OWN-KEY mode
  (`forward_client_auth: false`) is a third-party vendor whose model ids the
  client does not know, so its rules ARE listed, like an `openai-translate`
  rule's; the window and the output cap belong to that upstream, so the row
  says so instead of naming numbers this side does not know. Such a rule
  still needs `client_models` when its match is a pattern: without them no
  id can be offered, and if no other rule can be listed either, the run
  fails.
- `env.CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT` = `"1"` --
  without it, Claude Code compacts proactively against the window it assumes
  for an unrecognised model id, which has nothing to do with the real window
  of the deployment behind the router. The variable applies to unrecognised
  ids only, so `claude-*` sessions keep their normal compaction (see
  "Context window and token counting").

Every other key in the file is preserved -- at the top level and inside
`env`/`modelPicker`, so a hand-set `modelPicker.replaceBuiltInOptions`
survives a regeneration. The write is atomic (temporary file plus
`os.replace`), keeps the file's previous permission mode, and leaves the
previous version intact if the write fails; an unparsable file is a refusal
rather than an overwrite. `--check` reports whether the file matches
`routing.yaml` without writing anything (exit 3 when it does not), and
`--settings-path` targets a different file.

Wire the file into the session -- it takes effect in a new session only:

```bash
env -u ANTHROPIC_API_KEY \
  ANTHROPIC_BASE_URL=http://127.0.0.1:8787 \
  claude --settings ~/.claude/open-harness-router.settings.json
```

### Claude Code without an Anthropic subscription

For a local router with own-key model routes, generate a separate Claude Code
profile with `--gateway-only` and launch it with the `cld` wrapper:

```bash
PYTHONPATH=src uv run python -m cli.sync_client_config --gateway-only
./bin/cld
```

For use from other directories, link `bin/cld` into a directory on your `PATH`.
The wrapper forwards Claude Code arguments. `cld --codex-login` as the first
argument opens the Codex browser sign-in before launching Claude Code and stops
if sign-in fails. That sign-in applies to Codex sessions, including the Codex
plugin, while this profile's `/model` picker uses the configured own-key routes.

Open `/model` in Claude Code 2.1.242 or later. The picker offers the configured
own-key models and the client's Default row. The generator points the client at
the loopback router and sets a local bearer marker for Claude Code's gateway
mode. The marker is not a secret or an access control mechanism. OHR uses the
configured upstream keys to run models. The first listed model backs Default
and the built-in model aliases, unless the profile already maps those to an
offered model. Rerun the generator after changing model routes.

This profile has separate user settings and sessions in `~/.claude-ohr`.
Project settings still apply. The first launch can ask about theme and workspace
trust. Tools, skills, hooks and local agents use the standard Claude Code
runtime, with model capabilities determined by the selected route. Remote
Control and cloud sessions require eligible Claude account authentication.
Auto mode and Fast mode have separate model and provider requirements. See
[gateway setup](https://code.claude.com/docs/en/llm-gateway-connect),
[Remote Control requirements](https://code.claude.com/docs/en/remote-control#requirements),
and [cloud sessions](https://code.claude.com/docs/en/claude-code-on-the-web).

## Forward-proxy

Setup for the mode described in "Run modes" above: the router works not as a
reverse-proxy (`ANTHROPIC_BASE_URL` pointing at the router), but as an HTTP
proxy (the client uses `HTTPS_PROXY`), so the client starts with its base URL
untouched and routing happens at the proxy level.

How it works: the router accepts `CONNECT`; for hosts on the allowlist
(`ROUTER_PROXY_MITM_HOSTS`, by default only `api.anthropic.com`) it
terminates TLS with its own certificate and routes `POST /v1/messages` and
`POST /v1/messages/count_tokens` through the same provider registry as
reverse-proxy mode. Every other path on that host is forwarded to the real
upstream unchanged -- this matters, because that's where feature flags,
telemetry, and client token refresh live. Every other host is a blind tunnel
with no decryption.

```mermaid
flowchart TB
    Client[Claude Code CLI] -->|CONNECT api.anthropic.com| Proxy[open-harness-router forward-proxy]
    Proxy -->|TLS termination for allowlisted host| Route{request path}
    Route -->|"POST /v1/messages(/count_tokens)"| Registry[Provider registry routing.yaml]
    Route -->|other paths on this host| RealUpstream[api.anthropic.com unchanged]
    Registry --> Providers[anthropic / openai_compatible / openai / kimi]
    Client -->|CONNECT other host| Tunnel[Blind tunnel, no decryption]
```

### Trusting the root certificate (mandatory step)

Without this step the mode won't work: the Node.js client won't accept the
router's self-signed certificate, and Claude Code will throw a cryptic TLS
error. The path to the root certificate is printed in the log at proxy
startup (the `proxy_startup` event, `root_certificate` field); by default
it's `./proxy-ca/rootCA.pem` (the directory is set by
`ROUTER_PROXY_CA_DIR`, the file is generated on first run).

The certificate is NOT installed into the OS system trust store, and it
shouldn't be: trust is scoped to a single process via the
`NODE_EXTRA_CA_CERTS` environment variable.

```bash
make run-proxy
```

In another terminal, without `ANTHROPIC_BASE_URL`:

```bash
env -u ANTHROPIC_BASE_URL \
  HTTPS_PROXY=http://127.0.0.1:8788 \
  NODE_EXTRA_CA_CERTS=./proxy-ca/rootCA.pem \
  claude
```

### Behind a corporate proxy

If the router itself needs to reach the outside world through a corporate
proxy:

- `ROUTER_PROXY_UPSTREAM_PROXY_URL` -- the upstream proxy address; empty
  -- direct connections (credentials are allowed in the URL:
  `http://user:secret@proxy:3128`);
- `ROUTER_PROXY_UPSTREAM_CA_BUNDLE` -- the CA bundle used to verify
  certificates on the outgoing side (needed when the corporate proxy does
  TLS inspection);
- `ROUTER_PROXY_NO_PROXY_HOSTS` -- a comma-separated list of hosts that
  always go direct, bypassing the corporate proxy.

Other mode variables: `ROUTER_PROXY_ENABLED` (default `false`; the combined
entrypoint starts the forward-proxy only when it is `true`, and a bare
`uvicorn main:create_app --factory` ignores it, logging a
`proxy_enabled_without_entrypoint` warning), `ROUTER_PROXY_HOST` /
`ROUTER_PROXY_PORT` (interface and port for accepting `CONNECT`, default
`127.0.0.1:8788`), `ROUTER_PROXY_CONNECT_TIMEOUT_S` (timeout for
establishing the outgoing connection and waiting for the upstream proxy's
response).

## Dashboard

`http://127.0.0.1:8787/dashboard` (the ASGI listener, in both run modes)
shows what the router is doing without a log file: the tiles (requests,
errors, in flight, tokens, cost), two charts (requests and errors per
bucket, and the generation throughput per model), ONE Providers table with
the model rows under their provider, the requests in flight, a feed of the
last events, the routing table, and the forward-proxy facts (listen
address, MITM allowlist, root CA expiry). The page polls
`GET /dashboard/state`, which returns the same picture as JSON, so it can
be read with `curl`/`jq` or scraped by anything else.

Click **View error details** on the Errors tile to expand **Request errors**
and inspect failures from the selected time range. The section is collapsed by
default, like Routing; click its heading to expand or collapse it. Direct links
to `#request-errors` also expand it. **Request errors** shows the latest 100 matching persisted
requests, including those from previous processes, with time, model, provider,
endpoint, HTTP status, recorded error type and an explanation. It uses the same
failure predicate as the tile and excludes deliberate capability rejections.
HTTP 200 describes the response headers and can coexist with an interrupted or
failed stream. `CancelledError` records interruption before completion; existing
rows do not identify whether cancellation, disconnect or shutdown caused it.
Only request metadata is stored, not prompts, headers or response bodies.
The separate **Events** feed remains a bounded view of the current process;
request rows now display their recorded error type as well as HTTP status.

The Routing panel separates the Messages, Codex Responses and Codex Images
endpoint defaults from the shared model rules. Responses can use a different
fallback from Messages; Images bypasses model rules and requires the explicitly
authenticated native fallback. The panel shows the configuration loaded by the
running process, not edits waiting on disk: restart the router after changing
`routing.yaml`. Switching a client's selected model does not change these rules.

The page is Preact 11.1.1 + htm 3.1.1 with Chart.js 4.6.1, vendored
byte-for-byte under `src/api/static/vendor` -- no build step, and no CDN at
runtime: the page loads same-origin files only, served by
`GET /dashboard/static/...` from a fixed whitelist.

What feeds it (`src/services/monitor.py`):

- every routed `/v1/messages` and `/v1/messages/count_tokens` request, in
  both entrances, from the routing decision to the last response byte. Token
  counts are read off the Anthropic-format response on its way to the
  client (the `usage` object of a JSON body, or the `message_start` and
  `message_delta` events of an SSE stream) without buffering or changing a
  byte, and the same reading works for translated responses. A gzip or
  deflate body, stream or not (what `api.anthropic.com` sends to a client
  that accepts it; passthrough relays it compressed), is inflated for the
  reading only; one in an encoding the stdlib cannot inflate (`br`, `zstd`
  -- what `api.anthropic.com` picks for Claude Code's unary requests) is
  relayed untouched and counts no tokens. A `400` the translated provider
  answers on purpose (a message-thread continuation it cannot resume, see
  "Forward-proxy" under "Run modes") is listed with a `rejected` field and
  counted as a request, not an error;
- the log events worth seeing: what the forward-proxy tunnelled or relayed
  outside the routed paths, retries, context-window clamps and rejects, and
  every warning or error.

The Tokens tile and provider input columns show **total input including cache**;
output is separate. Internally, ordinary input, cache reads and cache writes are
disjoint buckets for pricing. OpenAI Responses reports reads and writes under
`input_tokens_details.cached_tokens` and `cache_write_tokens`; Chat Completions
uses `prompt_tokens_details`. Anthropic reports `cache_read_input_tokens` and
`cache_creation_input_tokens` separately from `input_tokens`. OHR observes the
upstream counters; it does not create the providers' prompt caches itself.
Missing write counters count as zero in numeric aggregates, so the dashboard
labels writes as **reported**: zero can mean no reported writes, not necessarily
no caching. Records captured before cache-write parsing was fixed retain their
original values; OHR cannot reconstruct missing writes from cache reads or
uncached input. Likewise, historical translated input counts are not rewritten.

The cost column needs prices: an optional `pricing` block on a provider
(the default for its rules) or on a rule (that model only), in USD per
million tokens -- `input`, `output`, and optionally `cache_write` and
`cache_read` (unset cache prices count at the `input` price, an upper
bound). The router never bills anything and never sends these numbers
anywhere; on a subscription-billed passthrough route the column shows what
the same traffic would cost at API prices. Without `pricing` the column
reads `n/a` and the token columns still work.

The pricing itself is on the page too, so the cost column is readable
without opening `routing.yaml`: the cost cell of every provider and model
row carries the list prices it was computed at (`in $ / out $ / cache $`
per million, with the source -- `rule` or `provider`), and the routing
table in the "Routing" block lists the same prices for every route, with
`(provider)` marking the routes that inherit their provider's numbers
rather than setting their own. It is the same resolution the cost column
uses (`effective_pricing`), so a rule that overrides its provider is
visible as such.

```yaml
providers:
  anthropic:
    type: passthrough
    base_url: https://api.anthropic.com
    pricing: {input: 3, output: 15, cache_write: 3.75, cache_read: 0.3}
```

Two charts sit above the Providers table: the requests and errors per
bucket, and the generation throughput of every model over the selected
window (a line per model) -- see "Persistent request log" below.

The in-process counters live in the router process and reset on restart;
the feed keeps the last 500 entries. The same window is also persisted, so
the statistics survive a restart -- see "Persistent request log" below. The
state names models, providers, request paths and tunnelled hosts, never
bodies or credentials; like `/health`, it is meant for the loopback
interface the listener binds by default.

### Persistent request log

Every finished routed request is also written to a SQLite file, so the
dashboard's statistics outlive the process: the counters reset on restart
only in memory, while the rolling window is read back from disk.

- **What is stored**: one row per finished routed request -- the timestamp,
  request id, endpoint, model, provider, status, error name, stream flag,
  deliberate-rejection name, duration, the four token counts, the cost and
  the time to the first content token (a streamed answer only). No bodies,
  no credentials, nothing else.
- **Where**: `ROUTER_STORAGE_DB_PATH` (default `./state/monitor.db`; the
  parent directory is created on first use and `state/` is gitignored).
- **How long**: `ROUTER_STORAGE_RETENTION_DAYS`, default `365`. Rows older
  than the cutoff are pruned once at startup and then once a day -- the
  oldest are dropped, a rolling year is kept.
- **Size**: a row measures 165 bytes including its timestamp index entry
  (probe: 200,000 rows -> 31.5 MiB, `page_size` 4096). Per year that is
  ~17 MiB at 300 requests/day, ~172 MiB (~14 MiB/month) at 3,000/day, and
  ~574 MiB at 10,000/day. That is why a full year is kept and why rows are
  pruned rather than sampled: sampling would bias the series the dashboard
  shows.
- **Window**: the dashboard reads a rolling `ROUTER_STORAGE_WINDOW_DAYS`
  (default `30`); a window wider than the retention is refused at startup.
- **Time windows**: the range selector sits above the tiles and governs the
  whole page, Kibana-style -- the tiles, the charts, the Providers table and
  the trend all read the same period, so nothing has to be compared across
  two different windows. It offers `Today`, `5 min`, `30 min`, `60 min`,
  `6 hours`, `12 hours`, `24h`, `7d`, `30d`, `all` and a
  custom range with dates, hours and minutes in your browser's local time.
  Click the current range in the header to open **Quick select** or
  **Custom range**. Presets apply immediately; custom edits apply only with
  **Apply range**. The picker remembers your five most recently applied
  ranges. Escape or a click outside closes it without applying a draft;
  the adjacent refresh button reloads the current interval immediately.
  The start is inclusive and the end is exclusive; leave the end blank to
  follow activity through now. The selected instants are stored in the URL
  and sent as UTC ISO-8601 timestamps to `GET /dashboard/state` as `window=` and
  `start=`/`end=`, so the same figures come out of `curl`/`jq` without the
  page. Existing date-only API ranges still include the whole end day.
  The minute, hour and day presets (`5m`, `30m`, `60m`, `6h`, `12h`, `24h`,
  `7d`, `30d` in the API) are rolling -- a burst of heavy traffic leaving
  the window makes the totals drop -- and the page labels them so; `Today`
  runs from the reader's local midnight and only grows until the next one.
  The server does not know that time zone, so the page sends `Today` as a
  custom range starting at local midnight with an open end. A window of a
  day or less (`24h`, `Today`) is bucketed by hour, everything longer by day; a
  range reaching before the retention floor is clamped to it rather than
  silently read as whole (and the page says "clamped to retention"), and an
  unknown window or an inverted range answers `400`. When the store is
  younger than the selected window the page says "data since ..." instead of
  showing an empty range (the timestamp comes from `history.data_since`).
  Only "in flight" and the event feed stay process-local, since they
  describe now. Without storage the same page falls back to the in-memory
  counters of the process and labels them "since start".
- **Throughput (TPS)**: the chart above the Providers table shows the
  generation speed of every model over the selected window, each model its
  own line -- none is folded into an "other" series. A line keeps its style
  across windows: the first eight models get the eight palette colours as
  solid lines, the next ones reuse the colours dashed, dotted and
  dash-dotted, and the legend shows each line's colour and dash. Throughput
  is the output tokens over the *generation* time -- from the first content
  token to the last response byte -- and deliberately not over the whole
  request: the prompt processing of a two-million-token prompt would
  dominate the total duration and understate the generation speed badly.
  Streamed `/v1/messages` and `/v1/responses` answers qualify (a unary answer has no
  token to time), and only when its generation window is at least 250 ms --
  a gateway that buffers the stream delivers every token in one burst at the
  end, which would read as tens of thousands of tok/s. Every figure is
  token-weighted: the sum of the output tokens over the sum of the
  generation windows, never the mean of the per-request ratios. The
  Providers table carries the same numbers per model plus the average time
  to the first token (TTFT). Rows recorded before this measurement existed
  carry no first-token timing and therefore do not appear in the chart at
  all.
- **Version and live streams**: `pyproject.toml` is the single version source
  updated by the release helper. The header and metrics report the version
  captured when the service starts. When the checkout version changes, the
  header shows that a restart is required; publishing a release does not
  automatically replace a running process. "In flight" counts active requests
  right now, including Codex streams waiting for upstream headers, and returns
  to zero when they finish. TPS appears after a measured stream completes.
  Closing a stream after its terminal event is normal client cleanup; closing
  it before that event still counts as an error. Existing historical rows
  without timing cannot be backfilled into TPS, and old error records are kept.
- **Failures never break a request**: if the database cannot be opened,
  written or read (read-only or full disk, corrupt file, a busy lock) the
  error is logged and carried into the window snapshot as `storage_error`;
  the model response is delivered as usual. Set `ROUTER_STORAGE_ENABLED=false`
  to turn the log off entirely and keep the old in-memory-only behavior.

### Prometheus and Grafana

The same counters are served as Prometheus text on `GET /metrics` (no
client library, `src/api/metrics.py`): `ohr_requests_total`,
`ohr_errors_total`, `ohr_request_duration_seconds` (a summary's `_sum` and
`_count`), `ohr_tokens_total` with a `kind` label (`input`, `output`,
`cache_write`, `cache_read`), `ohr_cost_usd_total` and
`ohr_priced_requests_total`, all labelled by `provider` and `model`; plus
the gauges `ohr_inflight_requests` by `provider`, `ohr_uptime_seconds`,
`ohr_forward_proxy_enabled` and `ohr_info{version}`. Per-provider totals
are a `sum by (provider)` away.

`monitoring/` holds a ready stack for it: a `docker-compose.yml` with
Prometheus (scraping `host.docker.internal:8787/metrics` every 5 s, 30 days
of retention) and Grafana with the data source and a dashboard provisioned
(requests, errors, in-flight, tokens by kind, cost, average duration, and a
totals table). Both bind to the loopback interface.

```bash
cd monitoring && docker compose up -d
open http://127.0.0.1:3000        # admin / admin
```

The event feed itself stays in the JSON log (see "How to check where a model
will go"); a log viewer such as `lnav` or a Loki stack reads it as is.

## Releases

Versions are `major.minor.micro`, and every release is tagged `vX.Y.Z`. The
release notes live in `CHANGELOG.md` at the repository root, in
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format: one section
per version, newest first. When the file does not exist yet, the first
`/release` run creates it.

A pull request counts as released once its number appears as `#N` in a
`CHANGELOG.md` section whose version has been tagged; until `/release tag`
runs, the newest section is a draft and can be regenerated. The merged list
comes from GitHub (`gh pr list`), not from `git log`, because a rebase merge
leaves no `#N` in the commit subject. Release PRs themselves (titles starting
with `chore(release):`) are excluded, so a release never lists its own commit.

Releases are cut by the `/release` skill (`.claude/skills/release`,
user-invoked only) in two phases. Phase 1, `/release [major|minor|micro|X.Y.Z]`,
collects the unreleased PRs, asks you to confirm the version and the entry
list, then creates the branch `chore/release-vX.Y.Z` with the new changelog
section, the bumped `pyproject.toml` version and a refreshed `uv.lock`, and
opens a PR. You merge that PR yourself. Phase 2, `/release tag`, runs after
the merge: it tags the release PR's merge commit `vX.Y.Z`, pushes the tag and
publishes a GitHub release with that changelog section as the notes. Neither
phase restarts the running service.

The deterministic steps are a CLI, useful on their own from the repository
root:

```bash
PYTHONPATH=src .venv/bin/python -m cli.release version [--bump {major,minor,micro}]
PYTHONPATH=src .venv/bin/python -m cli.release collect [--repo OWNER/NAME] [--changelog PATH]
PYTHONPATH=src .venv/bin/python -m cli.release changelog --version X.Y.Z \
  [--date YYYY-MM-DD] [--input prs.json] [--changelog PATH] [--write]
PYTHONPATH=src .venv/bin/python -m cli.release bump --to X.Y.Z
PYTHONPATH=src .venv/bin/python -m cli.release notes --version X.Y.Z [--changelog PATH]
```

## Development

```bash
make full-check   # ruff + mypy + pytest
make test
make lint
make typecheck
```

## Structure

```
src/
  entrypoint.py      ASGI listener + optional forward-proxy in one process
  main.py            create_app() factory + lifespan; build_runtime() builds settings + registry
  settings.py        pydantic-settings by domain (server, routing, proxy, logging, secrets)
  log.py             structlog JSON
  version.py         the application version, read from pyproject.toml (the file cli.release bumps)
  const.py           constants
  dependencies.py    FastAPI DI providers (Depends)
  errors.py          domain exceptions, Anthropic error format
  api/               Messages, Responses, Images, model discovery, health, dashboard and metrics
  conversion/        Messages <-> Chat Completions/Responses and Responses ingress/output mappers
  models/            pydantic schemas for Anthropic requests
  providers/         passthrough, OpenAI translation/native forwarding, Responses bridge
  proxy/             forward-proxy: CONNECT, MITM TLS, certificates, tunnel, HTTP/1.1 session
  routing/           routing.yaml schema, matcher, loader, registry
  services/          headers, reasoning-context cache, message-thread store, token estimation, dashboard monitor
.claude/skills/      Claude Code skills: add-provider, release
bin/                 launcher for launchd: execs .venv python -m entrypoint
monitoring/          Prometheus + Grafana stack for /metrics (docker compose), provisioned dashboard
routing.example.yaml example provider/rule registry (in git)
routing.yaml         personal provider/rule registry (gitignored, cp from the example)
certs/               your own CA bundles for upstream providers (create as needed)
proxy-ca/            forward-proxy root CA (generated on first run)
CHANGELOG.md         release notes, one section per version (created by the first /release)
KICKOFF.md           concise project orientation, setup and limitations
docs/                README assets (dashboard screenshot)
```
