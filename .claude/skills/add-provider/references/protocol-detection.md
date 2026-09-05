# Protocol detection: cURL signal -> `ProviderCfg`

Field reference: `src/routing/schema.py` (`ProviderCfg`, lines 203-219 for
the field list, validators below them; `RoutingRule`, lines 39-69, for the
per-rule fields). All templates are generic; site facts belong in the
gitignored `local/` directory.

`max_tokens_limit` and `context_window` are the only fields a RULE can
override (`schema.py:67-68`): the provider block carries the gateway's
shared connection settings plus these two as DEFAULTS, and a model that
needs different numbers states them on its own rule. One gateway is always
one provider, however many models it serves.

## Signal table

| Signal in the cURL | `type` | `api_flavor` | `base_url` | auth | limits | notes |
|---|---|---|---|---|---|---|
| URL ends in `/v1/messages`, or `x-api-key` / `anthropic-version` headers | `passthrough` | omit (`responses` is rejected, `schema.py:254-258`) | URL minus `/v1/messages` (`src/providers/passthrough.py:42,374`) | `forward_client_auth: false` + `api_key_env` (`schema.py:367-375`); `auth_header: x-api-key` when the cURL sends `x-api-key`, else `bearer` | none (`max_tokens_limit` and `context_window` stay unset; an explicit `context_window` is rejected, `schema.py:307-314`) | rules carry no `upstream_model`, `max_tokens_limit` or `context_window` (`schema.py:524-531`, `:547-597`) |
| URL ends in `/chat/completions` | `openai-translate` | `chat` (default) | URL minus `/chat/completions` | `api_key_env` required (`schema.py:506-509`), Bearer only | `max_tokens_limit` required (`schema.py:279-283`); `context_window` optional, greater than `max_tokens_limit` (`schema.py:286-327`), set once the deployment's total window is known -- mandatory when the upstream answers an overflow with a 5xx (SKILL.md step 6b); both are per-model overridable on the rule, and the effective pair is validated the same way (`schema.py:547-597`); `token_param: max_completion_tokens` only if the cURL body used that field | `drop_params` (`temperature`/`top_p`/`stop`, `schema.py:29`) only after a 400 "unsupported parameter". Some gateways answer HTTP 400 with an empty body when the request lacks a `stream` field: the router always sends it explicitly; hand-written curls must include `"stream": false` too |
| URL ends in `/responses` | `openai-translate` | `responses` | URL minus `/responses` | as above | `token_param: max_output_tokens` (`routing.example.yaml:71`); `context_window` as in the chat row; `reasoning_effort` optional | `tools_max` only when the vendor documents a cap; same explicit-`stream` rule as the chat row |
| `--cacert F` or `--cacert <(cat A B)` | | | | | | `ca_bundle: <file in certs/>` (`src/providers/factory.py:36-41`); `tls_verify_hostname: false` only after `cli.tls_probe probe` exit 11 |
| `-k` / `--insecure` | | | | | | not representable: obtain the chain first |
| extra `-H` headers | | | | | | `extra_headers` (never auth headers, `schema.py:462-473`) plus `User-Agent: llm-router/0.1` |
| `"max_tokens": N` in the body | | | | | lower bound for `max_tokens_limit` | |
| `"stream": true` in the body | | | | | | no config impact; both providers stream. Keep an explicit `stream` in every direct curl (see the openai-translate rows) |

Placement in `routing.yaml`: provider blocks under `providers:` after the
existing ones; `exact` rules under `rules:` before any `prefix` /
`contains` / `regex` rule that could match the alias. Do not set
`timeout_s` -- it is parsed and ignored (`README.md:309-313`).

## Provider templates

### passthrough (Anthropic-compatible vendor, own key)

```yaml
  # <provider>: Anthropic-compatible /v1/messages on <vendor>. Own key,
  # never the client's OAuth token (forward_client_auth: false switches the
  # router to an outgoing-header allowlist and injects api_key_env).
  <provider>:
    type: passthrough
    base_url: https://<gateway-host>        # cURL URL minus /v1/messages
    forward_client_auth: false
    api_key_env: <PROVIDER>_KEY
    auth_header: x-api-key                  # or bearer -- whatever the cURL sent
    ca_bundle: <provider>_ca.pem            # private CA only; relative to certs_dir
    stream_read_timeout_s: 600
    extra_headers:
      User-Agent: llm-router/0.1
```

### openai-translate, chat completions

```yaml
  # <provider>: OpenAI-compatible chat completions on <gateway-host>
  # (<what it is, which CA chain, where the key is issued>).
  <provider>:
    type: openai-translate
    base_url: https://<gateway-host>/<path>/v1   # cURL URL minus /chat/completions
    api_key_env: <PROVIDER>_KEY
    ca_bundle: <bundle>.pem                       # omit for a public CA
    # tls_verify_hostname: false   # ONLY after cli.tls_probe probe exit 11:
    #   "<YYYY-MM-DD>: leaf SAN is <...>, host is <...>; remove once the
    #   certificate covers the host name"
    # Default cap for every model on this provider; a model with a
    # different one overrides it on its own rule (see the rule template).
    # With several models here, put the LOWEST measured cap in this block.
    # Source: <GET /models field | probe error text | vendor doc>,
    # <YYYY-MM-DD>. Raise only after re-probing.
    max_tokens_limit: <N>
    # Total window of the DEPLOYMENT (prompt + completion), greater than
    # max_tokens_limit; the default for every model here, same override rule
    # as above. Source: <GET /models max_model_len | vendor doc |
    # context-length error text>, <YYYY-MM-DD>. Unset = no pre-flight guard;
    # required when the upstream answers an overflow with a 5xx (the
    # router's pre-flight is then the only protection).
    context_window: <N>
    # token_param: max_completion_tokens   # only if the cURL body used it
    # drop_params: [temperature, top_p]    # only after a 400 "unsupported parameter"
    extra_headers:
      User-Agent: llm-router/0.1
```

### openai-translate, responses

```yaml
  # <provider>: OpenAI Responses API on <gateway-host>.
  <provider>:
    type: openai-translate
    base_url: https://<gateway-host>/v1           # cURL URL minus /responses
    api_key_env: <PROVIDER>_KEY
    api_flavor: responses
    token_param: max_output_tokens
    # reasoning_effort: high                      # optional, responses only
    # tools_max: 128                              # only when the vendor documents a cap
    # Source of the cap: <vendor doc>, <YYYY-MM-DD>. Default for every
    # model on this provider; per-model values go on the rule.
    max_tokens_limit: <N>
    # Total window of the deployment (prompt + completion), greater than
    # max_tokens_limit; source and date as above. Unset = no pre-flight.
    context_window: <N>
    extra_headers:
      User-Agent: llm-router/0.1
```

## Rule template (one per model)

```yaml
  # <provider>: exact aliases only, one per model. Placed before any
  # prefix/contains/regex rule that could match these names; raw upstream
  # ids are intentionally not routed -- clients send the alias.
  - match: {type: exact, value: "<prefix><short-name>"}
    provider: <provider>
    upstream_model: <vendor>/<upstream-model-id>   # omit on passthrough
```

One gateway, several models with different limits -- ONE provider block
holding the conservative defaults, each deviating model stating its own
numbers on its rule (both fields are forbidden on a passthrough rule):

```yaml
  # <model-a> keeps the provider's defaults -- they are its measured pair.
  - match: {type: exact, value: "<prefix><model-a>"}
    provider: <provider>
    upstream_model: <vendor>/<model-a>
  # <model-b> was probed higher: <probe outcome>, <YYYY-MM-DD>.
  - match: {type: exact, value: "<prefix><model-b>"}
    provider: <provider>
    upstream_model: <vendor>/<model-b>
    max_tokens_limit: <N>
    context_window: <N>
```

A `prefix`/`contains`/`regex` rule additionally needs `client_models` --
its match value is a pattern, so there is nothing for the `/model` picker
or an agent's `model:` frontmatter to send:

```yaml
  # Exact ids a client may send for this rule; the match value itself is a
  # pattern and would reach the upstream verbatim as a 404.
  - match: {type: prefix, value: "<prefix>"}
    provider: <provider>
    client_models: ["<prefix><model-a>", "<prefix><model-b>"]
```

Validation at startup (`schema.py:599-654`): every entry must be accepted
by its own rule, must not be captured by an earlier rule (first match
wins) and must not be listed twice. `make sync-client-config` turns these
ids into the client's picker rows and exits 1 on a non-exact rule that has
none.

## Subagent file template (`~/.claude/agents/<alias>-coder.md`)

```markdown
---
name: <alias>-coder
description: Codegen and mechanical-edits subagent on <Model> (via open-harness-router -> <provider>). Use for writing code, porting and routine edits when a native Anthropic model is not required.
model: <alias>
tools: Read, Grep, Glob, Edit, Write, Bash, TodoWrite, mcp__context7__resolve-library-id, mcp__context7__query-docs
---

You are a codegen subagent running on <Model>, for mechanical edits and
routine codegen. Follow the subagent executor contract in
`rules/orchestrator-delegation.md`. You are a terminal executor: if the task
turns out too big, report that fact to the parent -- it will decompose.
```

Keep a copy in `~/.claude/optional/fleet-agents/`, add the `agents/` copy to
`~/.claude/.gitignore`, and list the alias in that directory's README. The
`<alias>` in `model:` is an `exact` rule's value or one of the rule's
`client_models` entries -- the same ids the picker offers.
