"""One-time browser login for the ChatGPT subscription credential.

Run from the repository root::

    PYTHONPATH=src .venv/bin/python -m cli.chatgpt_login

Opens the OpenAI OAuth consent page in the default browser, waits for the
redirect on localhost:1455, exchanges the code, and stores the tokens in
``state/chatgpt_auth.json`` (0600). Re-run whenever the refresh token is
revoked (e.g. after a password change or signing out of all sessions).
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

import httpx

from auth.chatgpt_oauth import (
    REDIRECT_PATH,
    REDIRECT_PORT,
    ChatGPTAuthError,
    ChatGPTTokenStore,
    build_authorize_url,
    exchange_code,
)

STORE_PATH = Path("state/chatgpt_auth.json")
_TIMEOUT_S = 600


def _pkce() -> tuple[str, str]:
    """PKCE verifier and S256 challenge for the authorization request."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def main() -> int:
    store = ChatGPTTokenStore(STORE_PATH)
    if store.path.exists():
        answer = input(
            f"{store.path} already exists; log in again and replace it? [y/N] "
        ).strip().lower()
        if answer not in ("y", "yes"):
            print("aborted; existing credential kept")
            return 0

    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    url = build_authorize_url(state, challenge)
    code_holder: dict[str, str] = {}

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        """Only the exact callback path counts; anything else is ignored.

        A missing or mismatched state, a favicon request or any other
        stray GET must NOT resolve or end the login: the handler keeps
        serving until a valid callback arrives or the deadline passes.
        """

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != REDIRECT_PATH:
                self.send_response(404)
                self.end_headers()
                return
            params = urllib.parse.parse_qs(parsed.query)
            if params.get("state", [""])[0] != state:
                # Wrong state: not our callback; do not resolve the login.
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"state mismatch; retry the login")
                return
            if "code" in params:
                code_holder["code"] = params["code"][0]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"Login complete. You can close this tab.")
            else:
                code_holder["error"] = params.get("error", ["unknown"])[0]
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Login failed; check the terminal.")

        def log_message(self, *args: object) -> None:
            pass  # keep the console clean

    server = http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), CallbackHandler)
    deadline = threading.Event()

    def serve_until_resolved() -> None:
        while not code_holder and not deadline.is_set():
            server.handle_request()

    thread = threading.Thread(target=serve_until_resolved, daemon=True)
    thread.start()

    print(f"Opening {url}")
    webbrowser.open(url)
    print("waiting for the login redirect on http://localhost:1455/auth/callback ...")
    thread.join(timeout=_TIMEOUT_S)
    deadline.set()
    server.server_close()

    if "error" in code_holder:
        print(f"login failed: {code_holder['error']}", file=sys.stderr)
        return 1
    if "code" not in code_holder:
        print("timed out waiting for the login redirect", file=sys.stderr)
        return 1

    try:
        with httpx.Client() as token_http:
            data = exchange_code(token_http, code_holder["code"], verifier)
    except ChatGPTAuthError as exc:
        print(f"token exchange failed: {exc}", file=sys.stderr)
        return 1

    store.save(data)
    print(f"stored ChatGPT subscription credential for {data.get('email') or 'account'}")
    print(f"account_id: {data.get('account_id')}")
    print(f"plan: {data.get('plan_type') or 'unknown'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
