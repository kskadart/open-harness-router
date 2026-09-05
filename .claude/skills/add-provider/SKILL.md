---
name: add-provider
description: "Add an LLM provider and model aliases to open-harness-router from a cURL example plus optional CA certificate files: parse the curl, verify the TLS chain and the upstream, determine max_tokens_limit and context_window, write the routing.yaml provider block and exact-match alias rules, store the key in .env, validate offline, restart the launchd service with health check and rollback, run end-to-end tests. Also covers adding one more model to an existing provider."
argument-hint: "<curl command> [cert.pem ...] [provider=<name>] [alias-prefix=<pfx->] [models=<upstream-id>[,...]]"
disable-model-invocation: true
---

# Add a provider to open-harness-router

Turn a working cURL example (plus optional CA certificate files) into a
provider block and exact-match alias rules in `routing.yaml`, with the key in
`.env`, offline validation, a supervised restart of the launchd service and
end-to-end checks through the router. Also covers "one more model on an
existing provider". All commands run from the repository root.

## Read this first

- This session and every subagent reach the API THROUGH the router being
  reconfigured. `routing.yaml` and `.env` are read once at startup
  (`README.md:166-168`, `src/main.py:45`). A provider whose key variable is
  unset or whose CA bundle is missing does not degrade the router, it stops
  it from starting at all (`src/providers/factory.py:67-72`,
  `src/main.py:54-58`); launchd's KeepAlive then crash-loops it.
- Order is fixed: diagnostics -> config -> offline validation -> restart ->
  e2e. Never restart before step 8 passes.
- Only the top-level session restarts the router (step 9), in ONE Bash call,
  while no subagent is running. Subagents never restart it.
- The API key from the user's cURL is written exactly once, by the single
  append in step 3. It never appears in YAML, skill files, reports,
  subagent prompts or any other command.
- Every new line in this repository is English (`tasks/lessons.md:3-7`).
- Site-specific facts (internal hosts, key procedures, model ids) live only
  in the gitignored `references/local/`; `references/local.example.md` is
  the committed template. A matching note is read before any question.
- A provider has no model list: several models on one gateway are several
  `rules:` entries pointing at one `provider`, each with its own
  `upstream_model` (`src/routing/schema.py:39-69`); first match wins
  (`src/routing/registry.py:70-88`), matching is case-sensitive
  (`src/routing/matcher.py:26-34`). `max_tokens_limit` and `context_window`
  sit on the provider as its DEFAULTS (`src/routing/schema.py:215-216`) and
  are overridden per model on the rule (`src/routing/schema.py:67-68`): one
  gateway keeps ONE provider block even when its models need different
  limits -- never copy a provider to change a number.
- The `/model` picker and the agent frontmatter are fed from
  `client_models` on the rule (`src/routing/schema.py:39-69`), regenerated
  by `make sync-client-config` (step 12). That client config also disables
  Claude Code's own window enforcement, so `context_window` on every
  reachable openai-translate provider stops being optional -- step 6b.

Helpers (they reuse the router's own code; see `references/protocol-detection.md`
for the config table and templates):

| Command | Purpose |
|---|---|
| `PYTHONPATH=src .venv/bin/python -m cli.tls_probe match CERT.pem ...` | input fingerprints vs `certs/*.pem`: `REUSE certs/<x>` or a `cat` command |
| `PYTHONPATH=src .venv/bin/python -m cli.tls_probe probe --host H [--cafile F]` | chain / host-name verdict with the router's trust store |
| `PYTHONPATH=src .venv/bin/python -m cli.validate_routing [--expect-provider P] [ALIAS=UPSTREAM ...]` | boot-path validation and alias resolution, no sockets |
| `make sync-client-config` (`-m cli.sync_client_config [--check]`) | regenerate the client's `/model` picker rows from `routing.yaml` |
| `bash .claude/skills/add-provider/scripts/restart_router.sh ...` | kickstart + health wait + provider check + rollback |

## Steps

### 1. Parse `$ARGUMENTS`

A checklist, not a script:

- `base_url` = the cURL URL minus exactly one of `/chat/completions`,
  `/responses`, `/v1/messages`. Nothing else is stripped (a
  `/continue-dev/v1`-style prefix stays).
- Provider `type` / `api_flavor`: the signal table in
  `references/protocol-detection.md`.
- Secret: `Authorization: Bearer <...>` or `x-api-key: <...>`. `x-api-key`
  on an OpenAI-style URL cannot be expressed: the OpenAI SDK only sends
  Bearer (`src/providers/openai_translate.py:242-248`) and `extra_headers`
  may not carry auth (`src/routing/schema.py:462-473`) -> stop and ask.
- Upstream model ids: `"model"` in the body plus `models=`.
- Extra `-H` headers minus `content-type`, `accept`, `host`, `user-agent`
  and the auth header.
- Certificate files: `--cacert F`, `--cacert <(cat A B)` -> A and B, and
  bare `.pem` paths among the arguments.
- `-k` / `--insecure` -> warn: the router has no unverified mode; the chain
  must be obtained first.
- Provider name: snake_case (`provider=`, else ask). Alias prefix: ask
  (`alias-prefix=`). One alias per model = `<prefix><short-name>`; confirm
  the list with the user.
- Site note: `ls .claude/skills/add-provider/references/local/*.md`, grep
  them for the host; if one matches, read it before asking anything.
- Everything still missing goes into ONE `AskUserQuestion`.

### 2. Existing provider with the same `base_url`?

Search `routing.yaml` with the Grep tool -- `git grep` does not see
gitignored files (`tasks/lessons.md:9-15`). If a provider already uses this
`base_url`, take the "one more model" path: skip steps 3 and 4 and the
provider block of step 7; run the smoke test of step 5 with the existing key
(variable name from that provider's `api_key_env`); measure the new model's
`max_tokens_limit` and `context_window` (step 6) and, where they differ from
the provider's defaults, put them on the NEW RULE rather than on the
provider (a second provider block for the same gateway is never the answer,
and lowering the provider's defaults would silently retighten every other
model on it); add one exact rule (step 7); then steps 8-13. In step 9 omit
`--expect-provider` (the name is already in `/health`; the pid change
proves the restart).

### 3. Secret -> `.env` (first, once)

Done first so every later curl reads the key from the file and the value
enters the transcript exactly once:

```sh
test "$(grep -c '^<PROVIDER>_KEY=' .env)" -eq 0 \
  && { [ -z "$(tail -c1 .env)" ] || printf '\n' >> .env; } \
  && printf '<PROVIDER>_KEY=%s\n' '<value from the cURL>' >> .env && ls -l .env
```

The variable is inert until the restart (`src/main.py:45`). To undo:
`sed -i '' '/^<PROVIDER>_KEY=/d' .env`. Never add site-specific variables
to `.env.example`.

### 4. Certificates (only when the cURL carried any)

```sh
PYTHONPATH=src .venv/bin/python -m cli.tls_probe match <cert.pem ...> --provider <provider>
```

Prints `REUSE certs/<x>.pem` (exit 0) when an existing bundle already holds
every input certificate, otherwise (exit 10) the command
`cat A B > certs/<provider>_ca.pem` -- run it by hand (exit 2 = an input
file that cannot be read or parsed). Then:

```sh
PYTHONPATH=src .venv/bin/python -m cli.tls_probe probe --host <host> [--cafile certs/<bundle>.pem]
```

| exit | verdict | action |
|---|---|---|
| 0 | `CHAIN_OK_HOSTNAME_OK` | `ca_bundle` only; do not set `tls_verify_hostname` |
| 2 | `BUNDLE_UNUSABLE` | `--cafile` holds no usable certificate, nothing was probed -- fix the bundle |
| 11 | `CHAIN_OK_HOSTNAME_MISMATCH` | the only case for `tls_verify_hostname: false`, with a dated English comment naming the SAN and the condition for removing the flag |
| 12 | `CHAIN_FAIL` | wrong or incomplete certificates -- stop |
| 13 | `CONNECT_FAIL` | network / VPN / DNS -- stop, check with `curl -v` |

Exit 1 is never a verdict: it is what Python exits with on an uncaught
exception, so treat it as a broken invocation, not as a mismatch.

The probe verdict is authoritative: it uses exactly the trust store the
router will use (`httpx.create_ssl_context` over
`src/services/http_transport.py:27-57`). Never write into `proxy-ca/` (the
forward-proxy MITM CA, `src/settings.py:131-137`); files found there are
copied, not deleted.

### 5. Direct upstream smoke test, before touching the config

Key from `.env`, never inline; headers go to stderr so `jq` only sees the body:

```sh
K="$(sed -n 's/^<PROVIDER>_KEY=//p' .env)"
curl -sS -m 120 --noproxy '*' --cacert certs/<bundle>.pem -D /dev/stderr -X POST "<base_url>/chat/completions" \
  -H "Authorization: Bearer $K" -H 'Content-Type: application/json' \
  -d '{"model":"<upstream id>","stream":false,"messages":[{"role":"user","content":"Reply with exactly one word: pong"}],"max_tokens":64}' \
  | jq -c '{model, finish:.choices[0].finish_reason, text:.choices[0].message.content, reasoning:(.choices[0].message.reasoning_content // null), usage, err:(.error // .detail // null)}'
```

Two details every direct-upstream curl in this skill carries:

- `--noproxy '*'`: the session environment may set
  `HTTPS_PROXY=http://127.0.0.1:8788` (the router's own forward proxy);
  a direct check must bypass it, or it measures the router, not the upstream.
- an explicit `"stream": false` (or `true`) in the body: some gateways answer
  HTTP 400 with an EMPTY body when the field is absent. The router always
  sends it; hand-written curls must too.

One call per model; a non-null `reasoning` shows that thinking is on for
that deployment (relevant for step 6). Passthrough variant:
`POST <base_url>/v1/messages` with `--noproxy '*'`, `x-api-key: $K` (or
`Authorization: Bearer $K`), `anthropic-version: 2023-06-01` and
`"stream": false` in the body. Drop `--cacert` for a public CA. For a host
with probe exit 11 the system curl cannot do chain-only verification: use
`-k` for this check only and note that the chain was verified by the probe.

### 6. `max_tokens_limit` and `context_window` (openai-translate only)

Two numbers PER MODEL: the output cap (6a), then the deployment's total
window (6b). They live on the provider as its defaults (used by every rule
that does not override them) and on each rule that needs its own pair -- so
measure them per model, then decide where each number goes:

- new provider, one model -> both on the provider;
- new provider, several models -> the CONSERVATIVE pair on the provider (so
  a model added later is guarded before anyone probes it), the measured pair
  on the rule of every model that exceeds it;
- existing provider, one more model -> leave the provider alone; the model's
  numbers go on its own rule when they differ from the defaults.

Record every number with its source in a YAML comment, next to the block it
lands in (templates in `references/protocol-detection.md`).

#### 6a. `max_tokens_limit` -- the output cap

Semantics: the outgoing `max_tokens` is
`min(max(client, 100), max_tokens_limit)`, silently
(`src/conversion/request_converter.py:138-141`), where `max_tokens_limit` is
the rule's value when it sets one and the provider's otherwise. Too low ->
reasoning models return empty `content` with `stop_reason: max_tokens`
(`src/routing/schema.py:119-128`); too high -> a vLLM-style upstream answers
400. Ladder:

1. `curl -sS --noproxy '*' --cacert ... -H "Authorization: Bearer $K" <base_url>/models | jq '(.data // [])[] | {id, max_model_len, context_length, context_window, max_output_tokens}'`
   -- a 404 means the gateway exposes no model list; go to 2.
2. Probe each model with the step 5 body and `"max_tokens": 1000000`
   (keep `"stream": false`). Read the answer:
   - "maximum context length is N" = the window; "max_tokens must be at
     most N" = the output ceiling;
   - HTTP 200 = the upstream clamps silently; the ceiling is NOT
     discoverable by probing -- use the vendor/cookbook figure;
   - HTTP 400/500 on the oversized value (e.g. `Error processing request`)
     = the upstream does NOT clamp: an over-limit `max_tokens` fails the
     whole request, so the cap must stay conservative (a documented value,
     or the largest value that still returned 200). Note WHICH status it
     was: a 5xx here also decides 6b.
3. `AskUserQuestion` with the collected numbers (vendor docs, cookbook).

When the models on one gateway differ (one clamps, one fails; one thinks,
one does not), keep the ONE provider block: give it the lowest measured cap
as its default and write the higher cap as `max_tokens_limit:` on the rule
of the model that was probed at that value (`src/routing/schema.py:67-68`).
Duplicating the provider to carry a second number is the mistake this
override exists to remove. Record the source of each number and the probe
outcome in a YAML comment next to it.

#### 6b. `context_window` -- the deployment's total window

Semantics (`src/routing/schema.py:130-145`; README "Context window and token
counting", `README.md:315-440`): schema-optional, `openai-translate` only,
must be greater than `max_tokens_limit` (`src/routing/schema.py:286-327`).
Unset = no estimate, no pre-flight, the request goes upstream unchanged. A
rule may override it for one model (`src/routing/schema.py:67-68`); the
EFFECTIVE pair -- rule value where set, provider value otherwise -- is
validated the same way at startup, in a message naming the rule index
(`src/routing/schema.py:547-597`).

Precondition, non-negotiable for any model a client will actually drive:
the generated client config sets
`CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT=1` (step 12), which
stops Claude Code from compacting against its own assumed window for an
unrecognised model id -- every id this router serves. The pre-flight is
then the ONLY guard, so a provider without `context_window` has none;
`cli.sync_client_config` refuses to advertise such a model and exits 1.
Measure the window before offering the model.
Set = before any upstream call the router estimates the converted wire
body (`src/services/token_estimator.py`, a character heuristic calibrated
to over-count -- estimate/usage 1.10-1.33 on 2026-09-04; the same estimate
answers `/v1/messages/count_tokens`,
`src/providers/openai_translate.py:772-797`) against `context_window - 512`:
a prompt leaving less than 100 tokens is rejected without an upstream call
-- HTTP 400 `invalid_request_error`, `prompt is too long: <N> tokens > <M>
maximum (capability_rejected: prompt_too_long)`, log event
`context_window_reject`; otherwise the completion budget is clamped to what
remains, log event `context_window_clamp` (`_enforce_context_window`,
`src/providers/openai_translate.py:709-770`). Claude Code reads the token,
retries with a smaller `max_tokens`, then compacts. The window used is the
route's effective one, resolved per request by the registry (`RouteLimits`,
`src/routing/registry.py:70-88`).

Set the DEPLOYMENT's window, not the model card's: a serving stack caps at
its `max_model_len`, often below the architecture's maximum. Ladder:

1. The `/models` entry from 6a: `max_model_len`, `context_length` or
   `context_window`, when the gateway exposes them.
2. Vendor documentation or the deployment's model card / cookbook (a
   cookbook that sets `CLAUDE_CODE_AUTO_COMPACT_WINDOW` for the model
   names the window it was sized for).
3. The upstream's own error text on the oversized request of 6a probe 2:
   an OpenAI-compatible context-length 400 usually names the limit, and
   the router remaps such 400s to the same `capability_rejected:
   prompt_too_long` shape (`src/providers/openai_translate.py:78-84`,
   `:469-476`). If the upstream answers an overflow with a 5xx instead
   (`Error processing request`), the SDK retries it like any server error
   and the remap never fires: the router pre-flight is the ONLY protection
   for that deployment, so `context_window` MUST be set there.
4. `AskUserQuestion` with the numbers and their sources. When only the
   architecture figure is known (config.json `max_position_embeddings`),
   set it with a comment saying it is unverified and say so in the report:
   an over-large value protects nothing. Leaving the field unset is only an
   option for a model that will NOT be listed in the picker -- see the
   precondition above.

### 7. Edit `routing.yaml`

First: `cp -p routing.yaml routing.yaml.bak-$(date +%s)` and REMEMBER the
exact file name (it is passed literally to step 9; `ls -t` is unreliable
because `cp -p` preserves mtime). Templates: `references/protocol-detection.md`.
Rules: English comments; no dead `timeout_s` (`README.md:309-313`);
`extra_headers: {User-Agent: llm-router/0.1}`; one `exact` rule per model,
placed before any `prefix`/`contains`/`regex` rule that could match the
alias (exact rules shadow nothing themselves); `upstream_model` is
forbidden on passthrough (`src/routing/schema.py:524-531`) -- there the
alias is what the client sends -- and so are `max_tokens_limit` and
`context_window` (nothing is converted, so neither would ever run).

A model whose limits differ from its provider's defaults carries them on its
own rule -- `max_tokens_limit:` and/or `context_window:` beside
`upstream_model:` -- with the probe or document they came from in the
comment. A model that matches the defaults gets neither.

A `prefix`/`contains`/`regex` rule ALSO needs `client_models: [...]` with
the exact ids clients may send: its match value is a pattern, and `gpt-` or
`GLM` sent upstream verbatim is a vendor 404. An `exact` rule needs no list
(its value is the id). Startup validation
(`src/routing/schema.py:599-654`) rejects an entry the rule's own match
does not accept, one an earlier rule captures, and one listed twice.
Without `client_models` step 12 exits 1 for that rule.

### 8. Offline validation

```sh
PYTHONPATH=src .venv/bin/python -m cli.validate_routing --expect-provider <provider> \
  <alias>=<upstream id> [<alias>=<upstream id> ...]
```

Exit 0 = schema valid, every provider built (keys, CA bundles), each alias
resolves to the expected provider and upstream model. The `=== ROUTES ===`
table prints each route's EFFECTIVE `max_tokens_limit` and `context_window`,
so a per-rule override is visible there: check that the new model's row
carries the numbers of step 6. Anything else: fix and
repeat before restarting. Exit 1 prints the same `open-harness-router: ...`
message the service would die with.

### 9. Restart -- top-level session only, one command

```sh
bash .claude/skills/add-provider/scripts/restart_router.sh \
  --expect-provider <provider> --routing-backup routing.yaml.bak-<TS>
```

During a Bash call the session itself has no open stream, but parallel
subagents do and `kickstart -k` cuts them off: restart only with no agents
running. The command returns only after `/health` answers 200 from a new
pid listing the provider; on timeout it restores the backup, restarts
again and exits 1 (2 = still down). Forbidden alternatives: `nohup`, `uv`
wrappers, `make run`, a manual `python -m entrypoint` (second process on
8787/8788), `bootout`/`bootstrap` (plist edits only). Not macOS: the script
exits 64; use `systemctl --user restart open-harness-router.service` and
poll `/health` (`README.md:623-627`).

### 10. End-to-end through the router, for EVERY alias

```sh
curl -sS -m 120 http://127.0.0.1:8787/v1/messages -H 'Content-Type: application/json' \
  -H 'anthropic-version: 2023-06-01' \
  -d '{"model":"<alias>","max_tokens":64,"messages":[{"role":"user","content":"Reply with exactly one word: pong"}]}' \
  | jq -c '{type, model, stop_reason, content, usage, error}'
curl -sN -m 120 http://127.0.0.1:8787/v1/messages -H 'Content-Type: application/json' \
  -H 'anthropic-version: 2023-06-01' \
  -d '{"model":"<alias>","max_tokens":64,"stream":true,"messages":[{"role":"user","content":"Reply with exactly one word: pong"}]}' \
  | grep -E '^event:' | sort | uniq -c
grep '"event": "route"' ~/Library/Logs/open-harness-router.log | tail -4 | jq -c '{model, provider, upstream_model}'
grep '"event": "upstream error' ~/Library/Logs/open-harness-router.log | grep '<provider>' | tail -3
grep '"event": "empty_completion"' ~/Library/Logs/open-harness-router.log | grep '<provider>' | tail -3
```

Expected: `type: message`, `stop_reason: end_turn`, non-empty text;
`message_start ... message_stop`; `route` events with the right provider and
`upstream_model`; no `upstream error` and no `empty_completion` for the new
provider.

### 11. Optional: subagent files (ask first)

`~/.claude/agents/<alias>-coder.md` plus a copy in
`~/.claude/optional/fleet-agents/` (template in
`references/protocol-detection.md`). Add the `agents/` copy to
`~/.claude/.gitignore` (copies are ignored by name there) and the alias to
the mapping list in `~/.claude/optional/fleet-agents/README.md`. The
`model:` value in the frontmatter is one of the ids listed in the rule's
`client_models` (or the `exact` alias). A new agent type may stay invisible
to the current session until it is reloaded; the proof of the route is
step 10, not a dispatch. No commits in `~/.claude` either.

### 12. Client model picker

```sh
make sync-client-config          # -> ~/.claude/open-harness-router.settings.json
```

Regenerates `modelPicker.options` (one row per offered model) and
`env.CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT = "1"` from
`routing.yaml`, preserving every other key in that file; the write is
atomic. Each row shows that model's EFFECTIVE window and output cap, so two
models on one provider advertise their own numbers. Exit 1 = a rule the
picker cannot express (non-exact without `client_models`, step 7) or an
offered model with no effective `context_window`, on neither its rule nor
its provider (step 6b) -- fix `routing.yaml`, re-run step 8, and note
that the running router already serves the config either way. `--check`
reports drift without writing (exit 3).

The file reaches the CLI only through `claude --settings
~/.claude/open-harness-router.settings.json` in the user's wrapper, and
only in a NEW session -- never edit `~/.claude/settings.json`, which the
CLI rewrites during a session. Recognised `claude-*` ids keep their normal
compaction; the variable applies to unknown ids only.

### 13. Final report (template)

- provider name and type; table alias -> upstream model;
- bundle used and the probe verdict; `tls_verify_hostname` yes/no and why;
- the NAME of the key variable (never the value);
- `max_tokens_limit` and `context_window` per model with their sources and
  where each landed (provider default vs. rule override) -- or why
  `context_window` stays unset AND the model is not offered in the picker;
- `client_models` written for each non-exact rule, and the picker rows
  `make sync-client-config` produced (step 12);
- smoke and e2e results (status, stop_reason, event counts);
- files touched; rollback:
  `cp -p routing.yaml.bak-<TS> routing.yaml && bash .claude/skills/add-provider/scripts/restart_router.sh`
  and only THEN `sed -i '' '/^<PROVIDER>_KEY=/d' .env`.

## Troubleshooting

| symptom | meaning / action |
|---|---|
| 401 from the upstream | wrong kind of credential (portal login instead of an API key, or a key for another gateway) |
| 404 / "model not found" | the upstream id must match exactly; check `GET /models` (a 404 there = no model list on this gateway) |
| HTTP 400 with an empty body | the gateway requires an explicit `stream` field; the router always sends it, but hand-written curls must too (`"stream": false`) |
| direct curl hits the router instead of the upstream | `HTTPS_PROXY` points at the router's forward proxy (8788); add `--noproxy '*'` |
| timeout, no response | network or the gateway itself: a direct `curl -v` to the host first, then VPN |
| curl `(60) unable to get local issuer certificate` | wrong CA bundle for this host |
| curl `(60) no alternative certificate subject name matches` | host-name mismatch: probe exit 11 territory |
| `open-harness-router: provider 'x': env 'X_KEY' with API key is not set` | `.env` line missing or misspelled; fix before the restart |
| `open-harness-router: provider 'x': CA bundle not found` | `ca_bundle` is relative to `certs_dir` (`certs/`) |
| Claude Code talks to api.anthropic.com despite `ANTHROPIC_BASE_URL` | OAuth credentials win; isolate `CLAUDE_CONFIG_DIR` for that session |
| e2e: empty `content`, `stop_reason: max_tokens` | `max_tokens_limit` too low for a reasoning model (step 6a); raise it on THAT model's rule, not on the provider shared with the others |
| e2e: 400 on long prompts | `max_tokens_limit` above the upstream ceiling for this model (step 6a); lower it on the model's rule |
| 400 `prompt is too long ... (capability_rejected: prompt_too_long)` plus a `context_window_reject` log event | the router pre-flight: the estimated prompt does not fit `context_window - 512 - 100`. Expected for an oversized context (Claude Code compacts); on a prompt the upstream is known to accept, `context_window` is below the deployment's real window (step 6b) |
| empty final message from a subagent, or `output_tokens: 1` with `stop_reason: end_turn` | check the router log for the `empty_completion` warning (`src/providers/openai_translate.py:631-650`, `src/conversion/response_converter.py:335-343`, `:811-819`): the upstream closed the turn with no text and no tool call. The router forwards in-conversation system messages as user text on the chat flavor (`src/conversion/request_converter.py:84-106`) because some open-model chat templates stop on a trailing system message; if the warning still appears, capture the wire body and compare what the upstream received |
| a small curl smoke test gets an HTML 403 | an anti-bot WAF in front of the gateway; it fires on small bodies (< ~8 KB) with shell-like text. Use an innocuous prompt (the step 5 body) or a body over ~8 KB; real Claude Code bodies are much larger and unaffected |
| identical bodies get identical, instantly returned replies | a response cache on the gateway keyed by body; add a nonce to the prompt when repeating a test |

## Forbidden

- Printing, logging, quoting or committing the API key; any command other
  than the step 3 append that contains it.
- `git commit` / `git push` without an explicit user instruction.
- Writing into `proxy-ca/`, or deleting anything found only via `git grep`.
- `tls_verify_hostname: false` without a probe exit 11 verdict.
- Adding a provider whose key variable is not in `.env` (it takes the
  router down at the next start).
- Leaving the router stopped, or starting a second router process.
- Russian (or any non-English) text in new repository lines.
- Site-specific hosts, model ids or portal URLs in `routing.example.yaml`,
  `.env.example`, `README.md` or any committed skill file.
- Writing anything into `references/local/` that the user did not provide.

## Examples

```
/add-provider curl -sS https://<gateway-host>/<path>/v1/chat/completions -H "Authorization: Bearer <KEY>" -H "Content-Type: application/json" -d '{"model":"<vendor>/<model-a>","messages":[{"role":"user","content":"ping"}]}' root_ca.pem sub_ca.pem provider=corp_gateway alias-prefix=corp- models=<vendor>/<model-a>,<vendor>/<model-b>
```

New provider on a corporate OpenAI-compatible gateway behind a private CA:
two certificate files, two models, aliases `corp-<model-a>` and
`corp-<model-b>`, key variable `CORP_GATEWAY_KEY`.

```
/add-provider curl -sS https://<gateway-host>/<path>/v1/chat/completions -H "Authorization: Bearer <KEY>" -d '{"model":"<vendor>/<model-c>","messages":[]}' provider=corp_gateway alias-prefix=corp-
```

Same `base_url` as an existing provider: the "one more model" path of
step 2 -- one new exact rule, existing key and bundle, restart without
`--expect-provider`.

```
/add-provider curl https://<vendor-host>/v1/messages -H "x-api-key: <KEY>" -H "anthropic-version: 2023-06-01" -d '{"model":"<vendor-model>","max_tokens":64,"messages":[{"role":"user","content":"ping"}]}' provider=vendor_anthropic
```

Anthropic-compatible vendor: `type: passthrough` with
`forward_client_auth: false`, `auth_header: x-api-key`, no
`max_tokens_limit`, rules without `upstream_model` (the alias is the
vendor's own model id).
