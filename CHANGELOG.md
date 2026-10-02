# Changelog

All notable changes to this project are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are `major.minor.micro`
and every release is tagged `vX.Y.Z`.

## [Unreleased]

### Added

- ride out an upstream's output-token quota window: a gateway refusal with a "retry in N minutes" hint (`422`/`429` with quota wording) closes a per-provider gate, the refused request and every request that arrives meanwhile wait for the window and leave one by one, and a window longer than `quota_wait_max_s` (default 300 s) becomes `429 rate_limit_error` with `retry-after` (capped at 60 s, the most Claude Code honors) instead of a status that ends the Claude Code turn or subagent
- raise the process's open-file soft limit to 8192 at startup (`services.open_files`) and report the ceiling in force as `open_files_limit` in `proxy_startup`: launchd hands agents 256, and on 2026-09-22 a burst of parallel tunnels exhausted it (`socket.accept() out of system resource`), taking every client behind the router offline; the README plist carries the matching `SoftResourceLimits`

### Fixed

- name the transport failure behind an upstream "Connection error.": the openai-translate 502 and its `upstream error` log entry now carry the httpx cause chain (for example `[SSL: CERTIFICATE_VERIFY_FAILED] ... certificate has expired`), so an expired gateway certificate is no longer indistinguishable from a network outage

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
