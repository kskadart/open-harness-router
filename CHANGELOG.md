# Changelog

All notable changes to this project are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are `major.minor.micro`
and every release is tagged `vX.Y.Z`.

## [0.3.0] - 2026-11-01

### Added

- show the generation throughput of every model on the dashboard: a line per model over the selected window, in tokens per second -- the output tokens over the generation time (from the first content token to the last response byte, not over the whole request, whose prompt processing would dominate it), token-weighted per bucket and per model (the sum of the tokens over the sum of the generation windows, never the mean of the per-request ratios), with the average time to the first token in the table under the chart; only a streamed `/v1/messages` answer with a generation window of at least 250 ms qualifies, and the rows recorded before the first-token timing existed carry none and do not appear (`RequestStore.tps`, `UsageScanner.first_delta_monotonic`, `TPS_MIN_GENERATION_MS`) ([#36](https://github.com/kskadart/open-harness-router/pull/36))
- show the configured list prices on the dashboard: the routes table lists every route's `input / output / cache` prices per 1M tokens, with `(provider)` marking a route that inherits its provider's numbers, and the cost cell of every Providers-table row names the prices its model is billed at and where they come from (`rule` or `provider`), resolved the way the request path resolves them; the same numbers are served under `pricing` and `history.breakdown` in `GET /dashboard/state` (`ProviderRegistry.describe_pricing`/`resolve_pricing`, `PricingCfg.rates`) -- display only, nothing is billed and nothing is sent upstream ([#36](https://github.com/kskadart/open-harness-router/pull/36))
- read the persisted statistics over a chosen window: a range selector above the tiles governs the whole page (tiles, provider/model tables and trend all read the same period), `GET /dashboard/state` takes `window=` (`24h`, `7d`, `30d`, `all`, `custom` with `start=`/`end=`) and returns `history` with the window's totals, its per-provider/per-model breakdown and its bucketed series (hourly for `24h`, daily otherwise); a range reaching past the retention floor is clamped to it and reported as `clamped`, and an unknown window or an inverted range answers `400` (`RequestStore.range_totals`/`range_series`/`grouped_totals`, `resolve_window`) ([#36](https://github.com/kskadart/open-harness-router/pull/36))
- persist the request log in SQLite so the dashboard's statistics survive a restart: one row per finished routed request (165 bytes incl. its timestamp index; ~17/172/574 MiB per year at 300/3k/10k requests per day), kept for a rolling year and pruned at startup and daily (`ROUTER_STORAGE_*`, `src/services/request_store.py`); a storage failure is logged and shown in the window snapshot, never raised into the request path ([#36](https://github.com/kskadart/open-harness-router/pull/36))
- list a third-party `passthrough` provider's models in the `/model` picker: a provider in own-key mode (`forward_client_auth: false`, e.g. MiniMax behind its own key) serves a vendor whose model ids the client does not know, so its rules are advertised like an `openai-translate` rule's, and the row names the upstream as the owner of the window and the output cap instead of numbers this side does not know; a native passthrough provider (`forward_client_auth: true`) keeps contributing no rows, since the client already lists the `claude-*` ids ([#36](https://github.com/kskadart/open-harness-router/pull/36))

### Changed

- rewrite the dashboard page: Preact 11.1.1 with htm 3.1.1 and Chart.js 4.6.1, vendored byte for byte under `src/api/static/vendor` -- no build step and no CDN at runtime, the page loads same-origin files only, served by `GET /dashboard/static/...` from a fixed whitelist; the three tables (Providers, Models, Throughput) become ONE Providers table with the model rows under their provider and the columns requests / errors / average latency / TPS / tokens in / tokens out / cache / cost; the charts are Chart.js ones whose tooltip stays inside the plot instead of leaving the canvas at the right edge; the range selector governs every statistic but "in flight", and the page says "data since ..." when the store is younger than the selected window; `GET /dashboard/state` gains `history.breakdown` (one row per provider and model, with the prices the registry resolves that model at) and `history.data_since` (`RequestStore.breakdown`/`first_ts`, `ProviderRegistry.resolve_pricing`) ([#36](https://github.com/kskadart/open-harness-router/pull/36))
- a pattern rule (`prefix`/`contains`/`regex`) on an own-key passthrough provider now needs `client_models` to be advertised: it names a pattern, not an id, so without the list there is nothing to offer, and if no other rule can be listed either the run fails instead of writing an empty picker ([#36](https://github.com/kskadart/open-harness-router/pull/36))

### Fixed

- give a streamed upstream response back to the provider's connection pool when the client leaves before the response headers reach it (Esc in Claude Code before the first token): the body was never started, so its own cleanup never ran, each such request kept a pooled connection checked out, and once `max_connections` (100) had leaked every request on that provider waited out the 10 s pool timeout and got `504`; `ProviderResult.release` now closes the upstream response whether or not the body ran, in the MITM session and in the ASGI app alike, and the dashboard no longer keeps such a request in flight ([#35](https://github.com/kskadart/open-harness-router/pull/35))
- stop Claude Code agents on a Kimi model from dying on the second turn: from then on the conversation carries `tool_addition` blocks (beta `mid-conversation-tool-changes-2026-07-01`) that Kimi answers with `400 Invalid request Error`, and an own-key `passthrough` provider forwarded them byte-for-byte; the new `fold_tool_additions` flag (`ProviderCfg`, valid only on `passthrough` with `forward_client_auth: false`) removes the blocks (dropping a message left empty) and sends the tools the model could see, with `defer_loading` removed, logging one `tool_additions_folded` event per rewritten request, while a body with nothing to fold still goes out as the original bytes; the visibility rule is now one function shared with the translated providers (`resolve_visible`, `fold_tool_additions` in `services/tool_visibility.py`), which also fixes the translated providers ignoring the `{"type": "tool_definition", "definition": {...}}` shape Claude Code sends, so a definition-carrying block neither counted as a reference nor supplied its tool ([#36](https://github.com/kskadart/open-harness-router/pull/36))

## [0.2.1] - 2026-10-02

### Fixed

- name the transport failure behind an upstream "Connection error." with the httpx cause chain (for example an expired gateway certificate), and raise the open-file soft limit to 8192 at startup, reported as `open_files_limit` in `proxy_startup`, after launchd's 256 descriptors ran out under a burst of tunnels ([#32](https://github.com/kskadart/open-harness-router/pull/32))
- ride out a gateway's output-token quota window: a `422`/`429` refusal with a "retry in N minutes" hint closes a per-provider gate, and requests wait for the window (`quota_wait_max_s`, default 300 s) and leave one by one; past the budget the client gets `429 rate_limit_error` with `retry-after` capped at 60 s instead of a status that ends the Claude Code turn ([#33](https://github.com/kskadart/open-harness-router/pull/33))

## [0.2.0] - 2026-09-22

### Added

- hold Claude Code message threads for translated providers: a `thread` continuation is rebuilt from the router's record of the previous turn, and a miss is answered with a 400 that makes the client resend the conversation (`x-ohr-capability-rejected`, `rejected` on the dashboard) ([#26](https://github.com/kskadart/open-harness-router/pull/26))
- hide deferred tools until the conversation surfaces them: a `defer_loading` tool goes upstream only once a `tool_addition` block references it, the `DeferredToolPlaceholder` never ([#28](https://github.com/kskadart/open-harness-router/pull/28))
- deferred tools: keep a redefined tool, skip an invalid definition, exclude the placeholder by name ([#29](https://github.com/kskadart/open-harness-router/pull/29))
- keep a silent translated stream alive with `ping` events after `STREAM_KEEPALIVE_INTERVAL_S` (10 s) of silence, logged as `stream_keepalive`, so Claude Code's watchdogs do not abort a reasoning or queued request ([#30](https://github.com/kskadart/open-harness-router/pull/30))

### Fixed

- report the version from pyproject.toml ([#25](https://github.com/kskadart/open-harness-router/pull/25))
- relay a unary passthrough response as received, `content-encoding` included: Brotli bodies reached the client undecodable, so Claude Code's auto-mode classifier verdicts were unreadable and its checks fell back to the session model ([#27](https://github.com/kskadart/open-harness-router/pull/27))

## [0.1.1] - 2026-09-07

### Added

- live activity page, Prometheus metrics, Grafana stack ([#21](https://github.com/kskadart/open-harness-router/pull/21))
- window the events feed, widen the tables, show the page in the README ([#22](https://github.com/kskadart/open-harness-router/pull/22))

### Fixed

- keep the working .env out of the test run ([#19](https://github.com/kskadart/open-harness-router/pull/19))
- forward the request target's query string upstream ([#20](https://github.com/kskadart/open-harness-router/pull/20))

### Documentation

- open with the dashboard screenshot ([#23](https://github.com/kskadart/open-harness-router/pull/23))

## [0.1.0] - 2026-09-06

### Added

- add per-provider tls_verify_hostname keeping chain verification ([#2](https://github.com/kskadart/open-harness-router/pull/2))
- enforce model context windows and generate the client picker ([#8](https://github.com/kskadart/open-harness-router/pull/8))
- ship the add-provider skill with routing and TLS helpers ([#9](https://github.com/kskadart/open-harness-router/pull/9))
- add the release skill and changelog tooling ([#15](https://github.com/kskadart/open-harness-router/pull/15))

### Changed

- stop tracking .launchd-label ([#11](https://github.com/kskadart/open-harness-router/pull/11))

### Fixed

- enforce passthrough auth modes, stop credential leaks to third-party upstreams ([#1](https://github.com/kskadart/open-harness-router/pull/1))
- keep client system messages routable and drop lost text blocks ([#3](https://github.com/kskadart/open-harness-router/pull/3))

### Documentation

- describe the add-provider skill and add a Russian translation ([#10](https://github.com/kskadart/open-harness-router/pull/10))
- describe run-proxy and the proxy env vars accurately ([#12](https://github.com/kskadart/open-harness-router/pull/12))
- fix the cross-reference to the routing rules section ([#13](https://github.com/kskadart/open-harness-router/pull/13))
- rewrite the Russian README as a native text ([#14](https://github.com/kskadart/open-harness-router/pull/14))
- separate the two run modes and say what each is for ([#16](https://github.com/kskadart/open-harness-router/pull/16))
- open with a plain description of what the router does ([#17](https://github.com/kskadart/open-harness-router/pull/17))
