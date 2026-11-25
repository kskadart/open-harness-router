"""ChatGPT subscription OAuth for the openai-translate provider.

Implements the same OAuth2 PKCE flow the Codex CLI uses against
auth.openai.com, so the router holds its own credential for a ChatGPT
subscription: login once (``python -m cli.chatgpt_login``), the tokens are
stored under ``state/chatgpt_auth.json`` (0600) and refreshed
automatically. The router never reads the Codex CLI's own ``auth.json`` --
this is a separate, independently issued credential for the same account.

Credential writers (the login CLI and any number of running router
processes) coordinate through one advisory lock on the store's directory,
so a re-login can never interleave with a refresh's read-modify-write.

Request contract for the subscription backend (verified against the Codex
CLI binary and the CLIProxyAPI reference implementation):

* endpoint: ``https://chatgpt.com/backend-api/codex`` (Responses API)
* ``authorization: Bearer <access_token>``
* ``chatgpt-account-id: <chatgpt_account_id from the access token claims>``
* ``originator: codex-tui`` and the Codex CLI User-Agent

The subscription credential is only ever attached to that exact HTTPS
endpoint (``SUBSCRIPTION_ENDPOINT``): a provider configured with any other
``base_url`` is a startup error, and the auth hook re-checks the request
destination before attaching the bearer.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

try:
    import fcntl
except ImportError:  # POSIX-only; Windows is untested (see README)
    fcntl = None  # type: ignore[assignment]

AUTH_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
# The public Codex CLI client id, shared by every Codex-family client.
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REDIRECT_URI = "http://localhost:1455/auth/callback"
REDIRECT_PORT = 1455
REDIRECT_PATH = "/auth/callback"
SCOPES = "openid email profile offline_access"

ORIGINATOR = "codex-tui"
USER_AGENT = "codex-tui/0.171.0 (Mac OS 26.5.2; arm64) iTerm.app/3.6.11 (codex-tui; 0.171.0)"

# The only upstream the subscription credential may ever reach. The schema
# rejects a chatgpt_oauth provider with any other base_url, and the auth
# hook below refuses to attach the bearer to a different destination.
SUBSCRIPTION_ENDPOINT = "https://chatgpt.com/backend-api/codex"

# Refresh 5 minutes before expiry; tokens live ~1 hour.
_REFRESH_MARGIN_S = 300

_REQUIRED_STORE_FIELDS = ("access_token", "refresh_token", "account_id", "expires_at")


class ChatGPTAuthError(RuntimeError):
    """Login, refresh or credential-store failure."""


def _b64url(raw: bytes) -> str:
    """Base64url without padding, as the PKCE and JWT specs require."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _parse_jwt_claims(token: str) -> dict[str, Any]:
    """Decode the payload of a JWT without verifying its signature.

    The token is used only as a self-assertion of the account id; the
    access token itself is what authenticates requests upstream.
    """
    try:
        payload = token.split(".")[1]
        padding = "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload + padding))
        if not isinstance(claims, dict):
            raise ValueError("expected JWT object")
        return claims
    except (IndexError, ValueError) as exc:
        raise ChatGPTAuthError("token is not a valid JWT") from exc


def _extract_auth_fields(access_token: str, id_token: str | None) -> dict[str, str]:
    """Account id and plan from the ACCESS token's ``.../auth`` claim.

    The id_token only carries the platform org id and email; the ChatGPT
    account id required by the ``chatgpt-account-id`` header lives in the
    access token.
    """
    claims = _parse_jwt_claims(access_token)
    auth = claims.get("https://api.openai.com/auth", {})
    account_id = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
    if not isinstance(account_id, str) or not account_id.strip():
        raise ChatGPTAuthError("access token is missing its subscription account ID")
    fields = {
        "account_id": account_id,
        "plan_type": str(auth.get("chatgpt_plan_type", "")),
    }
    if id_token:
        try:
            fields["email"] = str(_parse_jwt_claims(id_token).get("email", ""))
        except ChatGPTAuthError:
            fields["email"] = ""
    return fields


def _merge_token_response(previous: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    """Merge a token endpoint response into the stored credential layout.

    A refresh response may omit ``refresh_token`` and ``id_token`` (the old
    values stay valid); only ``access_token`` and ``expires_in`` are
    always present. Merging BEFORE normalization means no optional field is
    ever an unconditional lookup.
    """
    merged = dict(previous)
    merged["access_token"] = response["access_token"]
    if response.get("refresh_token"):
        merged["refresh_token"] = response["refresh_token"]
    if response.get("id_token"):
        merged["id_token"] = response["id_token"]
    merged.update(_extract_auth_fields(merged["access_token"], merged.get("id_token")))
    merged["expires_at"] = time.time() + int(response.get("expires_in", 3600))
    return merged


def build_authorize_url(state: str, code_challenge: str) -> str:
    """PKCE authorization URL for the browser step of the login."""
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": "login",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(http: httpx.Client, code: str, code_verifier: str) -> dict[str, Any]:
    """Exchange the authorization code for tokens; returns the stored layout."""
    response = http.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": code_verifier,
        },
        headers={"Accept": "application/json"},
        timeout=30.0,
    )
    if response.status_code != 200:
        raise ChatGPTAuthError(f"token exchange failed: {response.status_code} {response.text}")
    body = response.json()
    if not body.get("access_token") or not body.get("refresh_token"):
        raise ChatGPTAuthError("token exchange response missing access_token/refresh_token")
    return _merge_token_response(
        {"refresh_token": "", "id_token": "", "account_id": "", "email": "", "plan_type": ""},
        body,
    )


def refresh_tokens(http: httpx.Client, previous: dict[str, Any]) -> dict[str, Any]:
    """Refresh the access token; returns the next stored generation.

    The FULL previous credential is the merge base, so a response that
    omits the optional ``refresh_token``/``id_token`` replacements keeps
    the previous values -- the id_token and the email included -- instead
    of clearing them.
    """
    response = http.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": previous["refresh_token"],
            "scope": "openid profile email",
        },
        headers={"Accept": "application/json"},
        timeout=30.0,
    )
    if response.status_code != 200:
        raise ChatGPTAuthError(f"token refresh failed: {response.status_code} {response.text}")
    body = response.json()
    if not body.get("access_token"):
        raise ChatGPTAuthError("token refresh response missing access_token")
    # The refresh token itself carries the previous credential forward (an
    # omitted replacement keeps the old one valid).
    return _merge_token_response(previous, body)


class ChatGPTTokenStore:
    """JSON credential store with atomic writes and 0600 permissions.

    The temporary file is created with mode 0600 BEFORE any credential
    bytes are written, with a unique name per save, so no other process
    ever reads broader permissions on the file's contents, and concurrent
    saves cannot interfere.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any] | None:
        """Read and shape-validate the stored credential.

        Returns None when there is no usable credential; a file that
        exists but does not hold the required fields is treated as absent
        rather than aborting the registry.
        """
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or any(not data.get(f) for f in _REQUIRED_STORE_FIELDS):
            return None
        return data

    @contextlib.contextmanager
    def locked(self, purpose: str) -> Iterator[None]:
        """Lock a stable sidecar, not the credential inode replaced by each save."""
        if fcntl is None:
            raise ChatGPTAuthError("subscription credential locking requires POSIX")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(f".{self.path.name}.{purpose}.lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def save(self, data: dict[str, Any]) -> None:
        with self.locked("write"):
            self._replace(data)

    def publish_refresh(self, previous: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
        with self.locked("write"):
            current = self.load()
            if current is None:
                raise ChatGPTAuthError("subscription credential removed; run cli.chatgpt_login")
            if current != previous:
                return current
            self._replace(new)
            return new

    def _replace(self, data: dict[str, Any]) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(json.dumps(data, indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise


class ChatGPTOAuthProvider:
    """Use disk as the source of truth; never resurrect a removed credential."""

    def __init__(self, store: ChatGPTTokenStore) -> None:
        self._store = store

    def _require_data(self) -> dict[str, Any]:
        data = self._store.load()
        if data is None:
            raise ChatGPTAuthError(
                f"no ChatGPT subscription credential at {self._store.path}; run cli.chatgpt_login"
            )
        return data

    @staticmethod
    def _is_fresh(data: dict[str, Any]) -> bool:
        return float(data["expires_at"]) > time.time() + _REFRESH_MARGIN_S

    def _rotate(self) -> dict[str, Any]:
        # The worker owns the lock, HTTP client and persistence. Cancelling a
        # waiter cannot interrupt rotation after the server consumed the token.
        # Login takes only the short write lock, never the HTTP-duration lock.
        with self._store.locked("refresh"):
            previous = self._require_data()
            if self._is_fresh(previous):
                return previous
            with httpx.Client() as http:
                new = refresh_tokens(http, previous)
            return self._store.publish_refresh(previous, new)

    async def get_auth_headers(self) -> dict[str, str]:
        data = self._require_data()
        if not self._is_fresh(data):
            data = await asyncio.to_thread(self._rotate)
        return self._headers(data)

    def _headers(self, data: dict[str, Any]) -> dict[str, str]:
        return {
            "authorization": f"Bearer {data['access_token']}",
            "chatgpt-account-id": data.get("account_id", ""),
            "originator": ORIGINATOR,
            "user-agent": USER_AGENT,
        }
