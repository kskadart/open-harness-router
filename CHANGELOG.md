# Changelog

All notable changes to this project are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are `major.minor.micro`
and every release is tagged `vX.Y.Z`.

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
