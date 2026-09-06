# LOCAL NOTE -- <site name> (not committed)

Copy this file to `local/<site>.md` and fill it in. The `local/` directory is
gitignored; the skill greps the `Hosts:` line to find the note for a gateway.

Hosts: <host-a>, <host-b>

## Gateway(s)

- Gateway A: `https://<host-a>/<path>/v1`, protocol <OpenAI chat | OpenAI
  responses | Anthropic messages>, CA chain <public | `certs/<file>.pem`:
  <issuer names, SHA-256 prefixes>>, host name <verified | mismatch ->
  `tls_verify_hostname: false` since <YYYY-MM-DD>>, network <public | VPN
  only>. Provider `<name>`, env `<NAME>_KEY`.
- Gateway B: ...

## Key issuance

- Where: <portal URL or team>; credential kind: <personal API key | team
  key>; format hints: <prefix, length>; what is NOT a key (the usual cause
  of a 401): <portal login, SSO password, ...>.

## Models

| gateway | upstream id | context | max output / limit used | source | date |
|---|---|---|---|---|---|
| A | `<vendor>/<model>` | <N> | <N> / <max_tokens_limit> | <GET /models field | probe error | doc> | <YYYY-MM-DD> |

## Troubleshooting mapping

| symptom | cause / action |
|---|---|
| 401 <exact upstream text> | <which credential was used by mistake> |
| 404 <exact upstream text> | <exact model id to use> |
| timeout | <VPN / network hint> |
| TLS error | <which chain, which file> |

## Canonical checks

```sh
# direct (key from .env, never inline)
K="$(sed -n 's/^<NAME>_KEY=//p' .env)"
curl -sS -m 30 --cacert certs/<file>.pem -H "Authorization: Bearer $K" https://<host-a>/<path>/v1/models | jq '.data[].id'
# through the router
curl -sS -m 120 http://127.0.0.1:8787/v1/messages -H 'Content-Type: application/json' -H 'anthropic-version: 2023-06-01' \
  -d '{"model":"<alias>","max_tokens":64,"messages":[{"role":"user","content":"pong"}]}' | jq -c '{stop_reason, content}'
```
