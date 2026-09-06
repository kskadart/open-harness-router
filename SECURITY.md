# Security Policy

## Forward-proxy mode: local root CA and TLS interception

`make run-proxy` starts the router in forward-proxy mode (the client uses
`HTTPS_PROXY` instead of `ANTHROPIC_BASE_URL`). On first run it generates a
local root CA under `proxy-ca/` (directory configurable via
`ROUTER_PROXY_CA_DIR`, default `./proxy-ca`; certificate file `rootCA.pem`).

For hosts listed in `ROUTER_PROXY_MITM_HOSTS` (default: only
`api.anthropic.com`), the router terminates TLS with this certificate and
inspects/routes `POST /v1/messages` (and `/count_tokens`) through the
provider registry -- this is a deliberate man-in-the-middle, required to
route requests by model. Every other path on an allowlisted host, and every
other host entirely, is forwarded as a blind tunnel: the router relays
encrypted bytes without ever decrypting them.

The root CA:

- is generated locally on the machine running the router and never leaves it;
- is not installed into the OS-wide or browser trust store, and should not
  be -- trust is scoped to a single client process via the
  `NODE_EXTRA_CA_CERTS` environment variable (see README, "Trusting the
  root certificate");
- should not be trusted any more broadly than the user's own need to run
  this router. Do not add `proxy-ca/rootCA.pem` to a system-wide trust
  store, and do not share its private key.

Reverse-proxy mode (`make run`, the default: the client points
`ANTHROPIC_BASE_URL` at the router) generates no certificate and performs
no TLS interception; all outbound traffic to configured providers is a
normal TLS client connection.

## Dashboard

`GET /dashboard` and `GET /dashboard/state` on the ASGI listener expose the
router's activity: model ids, provider names, request paths on the MITM
host, the hosts the forward-proxy tunnelled to, token counts and the
configured prices. They never expose request or response bodies, headers,
keys or tokens. The listener binds `127.0.0.1` by default; if
`ROUTER_SERVER_HOST` is widened, put the dashboard behind the same access
control as the rest of the API.

## Reporting a vulnerability

Please report security issues by opening a GitHub issue on this repository:
https://github.com/kskadart/open-harness-router/issues
