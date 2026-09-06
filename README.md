**English** | [Русский](README.ru.md)

# Open Harness Router

![The dashboard: request, error, in-flight, token and cost tiles, per-provider and per-model tables, the requests in flight, the event feed and the routing table](docs/dashboard.png)

Run models from any vendor inside the Claude Code CLI. The router is one program
that accepts requests in the Anthropic Messages API format
(`POST /v1/messages`), so Claude Code connects to it as if it were Anthropic
itself. For each request the router reads the model name and, by the rules in
`routing.yaml`, sends it to one of the configured providers. Anthropic models go
to Anthropic unchanged (passthrough). Every other model is translated into the
OpenAI format and sent to a third-party API: OpenAI GPT-5.x (including the
Responses API, needed for reasoning models with tools), Kimi (Moonshot AI), Qwen
(Alibaba Cloud Model Studio), Grok (xAI), GLM behind a corporate
OpenAI-compatible gateway, DeepSeek, OpenRouter, or a local vLLM instance.

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
cp .env.example .env                    # no required key to start, see below
cp routing.example.yaml routing.yaml    # personal provider registry, not committed to the repo
```

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

The price: trust the root CA once and run the proxy listener with
`ROUTER_PROXY_ENABLED=true`. `HTTPS_PROXY` covers ALL outbound traffic of the
CLI process, so a router that is down takes the whole CLI off the network, not
only inference. The setup steps are in the "Forward-proxy" section below.

### Reverse-proxy

`make run`. The client sets `ANTHROPIC_BASE_URL=http://127.0.0.1:8787` and
needs no certificate. On 8787 the router serves `/health`, `/v1/messages` and
`/v1/messages/count_tokens`; any other path answers 404. There is no
`/v1/models`, so the model picker is built by `make sync-client-config`.

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
  system message and return an empty completion.

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

The `/model` picker does not discover the router's models on its own: gateway
model discovery keeps only ids containing `claude` or `anthropic`, and the
router serves no `/v1/models` endpoint anyway. The way to list arbitrary ids
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
  listed at all. Rules serving a `passthrough` provider are skipped --
  native `claude-*` ids are already in the picker.
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
shows what the router is doing without a log file: the requests in flight
and how long they have been running, per-provider and per-model counters
(requests, errors, average duration, tokens in/out/cache, cost), a feed of
the last events, the routing table, and the forward-proxy facts (listen
address, MITM allowlist, root CA expiry). The page polls
`GET /dashboard/state`, which returns the same picture as JSON, so it can
be read with `curl`/`jq` or scraped by anything else.

What feeds it (`src/services/monitor.py`):

- every routed `/v1/messages` and `/v1/messages/count_tokens` request, in
  both entrances, from the routing decision to the last response byte. Token
  counts are read off the Anthropic-format response on its way to the
  client (the `usage` object of a JSON body, or the `message_start` and
  `message_delta` events of an SSE stream) without buffering or changing a
  byte, and the same reading works for translated responses. A gzip or
  deflate stream (what `api.anthropic.com` sends to a client that accepts
  it; passthrough relays it compressed) is inflated for the reading only;
  a stream in an encoding the stdlib cannot inflate (`br`, `zstd`) is
  relayed untouched and counts no tokens;
- the log events worth seeing: what the forward-proxy tunnelled or relayed
  outside the routed paths, retries, context-window clamps and rejects, and
  every warning or error.

The cost column needs prices: an optional `pricing` block on a provider
(the default for its rules) or on a rule (that model only), in USD per
million tokens -- `input`, `output`, and optionally `cache_write` and
`cache_read` (unset cache prices count at the `input` price, an upper
bound). The router never bills anything and never sends these numbers
anywhere; on a subscription-billed passthrough route the column shows what
the same traffic would cost at API prices. Without `pricing` the column
reads `n/a` and the token columns still work.

```yaml
providers:
  anthropic:
    type: passthrough
    base_url: https://api.anthropic.com
    pricing: {input: 3, output: 15, cache_write: 3.75, cache_read: 0.3}
```

The counters live in the router process (they reset on restart) and the
feed keeps the last 500 entries. The state names models, providers, request
paths and tunnelled hosts, never bodies or credentials; like `/health`, it
is meant for the loopback interface the listener binds by default.

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
  const.py           constants
  dependencies.py    FastAPI DI providers (Depends)
  errors.py          domain exceptions, Anthropic error format
  api/               /v1/messages, /v1/messages/count_tokens, /health, /dashboard, /metrics, request adapters
  conversion/        Anthropic <-> OpenAI translation (request/response)
  models/            pydantic schemas for Anthropic requests
  providers/         passthrough, openai-translate, base interface, factory
  proxy/             forward-proxy: CONNECT, MITM TLS, certificates, tunnel, HTTP/1.1 session
  routing/           routing.yaml schema, matcher, loader, registry
  services/          headers, reasoning-context cache, token estimation, dashboard monitor
.claude/skills/      Claude Code skills: add-provider, release
bin/                 launcher for launchd: execs .venv python -m entrypoint
monitoring/          Prometheus + Grafana stack for /metrics (docker compose), provisioned dashboard
routing.example.yaml example provider/rule registry (in git)
routing.yaml         personal provider/rule registry (gitignored, cp from the example)
certs/               your own CA bundles for upstream providers (create as needed)
proxy-ca/            forward-proxy root CA (generated on first run)
CHANGELOG.md         release notes, one section per version (created by the first /release)
docs/                README assets (dashboard screenshot)
```
