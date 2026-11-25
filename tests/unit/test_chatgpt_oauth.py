"""Unit tests for the ChatGPT subscription OAuth provider pieces."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import httpx
import pytest

from auth.chatgpt_oauth import (
    ChatGPTAuthError,
    ChatGPTOAuthProvider,
    ChatGPTTokenStore,
    _b64url,
    _merge_token_response,
    build_authorize_url,
)
from routing.schema import ProviderCfg


def _fake_jwt(claims: dict) -> str:
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps(claims).encode())
    return f"{header}.{payload}.{_b64url(b'signature')}"


def _auth_claim(account_id: str = "acc-123", plan: str = "pro") -> dict:
    return {
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
            "chatgpt_plan_type": plan,
        }
    }


def _fresh_credential(account_id: str = "acc-123") -> dict:
    """A stored-layout credential that is far from expiry."""
    return {
        "access_token": _fake_jwt(_auth_claim(account_id)),
        "refresh_token": "rt-old",
        "id_token": _fake_jwt({"email": "user@example.com"}),
        "account_id": account_id,
        "email": "user@example.com",
        "plan_type": "pro",
        "expires_at": time.time() + 3600,
    }


class TestTokenStore:
    def test_roundtrip(self, tmp_path: Path) -> None:
        store = ChatGPTTokenStore(tmp_path / "auth.json")
        assert store.load() is None
        store.save(_fresh_credential())
        assert store.load()["account_id"] == "acc-123"

    def test_permissions_are_0600_immediately(self, tmp_path: Path) -> None:
        store = ChatGPTTokenStore(tmp_path / "auth.json")
        store.save(_fresh_credential())
        assert (tmp_path / "auth.json").stat().st_mode & 0o777 == 0o600

    def test_no_temp_left_behind(self, tmp_path: Path) -> None:
        store = ChatGPTTokenStore(tmp_path / "auth.json")
        store.save(_fresh_credential())
        assert not list(tmp_path.glob("*.tmp"))

    async def test_multiple_writers_leave_one_complete_credential(self, tmp_path: Path) -> None:
        path = tmp_path / "auth.json"
        records = [_fresh_credential(f"acc-{i}") for i in range(8)]
        start = threading.Barrier(len(records))

        def write(record: dict) -> None:
            writer = ChatGPTTokenStore(path)
            start.wait(timeout=5)
            writer.save(record)

        await asyncio.gather(*(asyncio.to_thread(write, record) for record in records))
        assert ChatGPTTokenStore(path).load() in records
        assert path.stat().st_mode & 0o777 == 0o600
        assert not list(tmp_path.glob("*.tmp"))

    def test_corrupt_file_is_absent_not_fatal(self, tmp_path: Path) -> None:
        path = tmp_path / "auth.json"
        path.write_text("not json")
        assert ChatGPTTokenStore(path).load() is None

    def test_valid_non_dict_json_is_absent(self, tmp_path: Path) -> None:
        path = tmp_path / "auth.json"
        path.write_text("[1, 2]")
        assert ChatGPTTokenStore(path).load() is None

    def test_missing_required_field_is_absent(self, tmp_path: Path) -> None:
        store = ChatGPTTokenStore(tmp_path / "auth.json")
        partial = _fresh_credential()
        del partial["account_id"]
        store.save(partial)
        assert store.load() is None


class TestTokenResponse:
    @pytest.mark.parametrize("invalid", [None, "", "   ", 42])
    def test_rejects_unusable_account_id_before_replacing_credential(self, invalid: object) -> None:
        previous = _fresh_credential()
        bad_token = _fake_jwt({"https://api.openai.com/auth": {"chatgpt_account_id": invalid}})
        with pytest.raises(ChatGPTAuthError, match="subscription account ID"):
            _merge_token_response(previous, {"access_token": bad_token})
        assert previous["account_id"] == "acc-123"

    def test_account_id_from_access_token(self) -> None:
        merged = _merge_token_response(
            {"refresh_token": "", "id_token": "", "account_id": "", "email": ""},
            {
                "access_token": _fake_jwt(_auth_claim("acc-9", "team")),
                "refresh_token": "rt-1",
                "id_token": _fake_jwt({"email": "a@b.c"}),
                "expires_in": 3600,
            },
        )
        assert merged["account_id"] == "acc-9"
        assert merged["plan_type"] == "team"
        assert merged["email"] == "a@b.c"
        assert merged["expires_at"] > time.time()

    def test_refresh_without_refresh_token_keeps_previous(self) -> None:
        previous = _fresh_credential()
        merged = _merge_token_response(
            previous,
            {
                "access_token": _fake_jwt(_auth_claim()),
                "expires_in": 3600,
                # no refresh_token, no id_token in the response
            },
        )
        assert merged["refresh_token"] == "rt-old"
        assert merged["account_id"] == "acc-123"
        assert merged["expires_at"] > time.time()


class TestAuthorizeUrl:
    def test_params(self) -> None:
        url = build_authorize_url("state-1", "challenge-1")
        assert "client_id=app_EMoamEEZ73f0CkXaXp7hrann" in url
        assert "code_challenge=challenge-1" in url
        assert "code_challenge_method=S256" in url
        assert "state=state-1" in url


class TestProviderHeaders:
    @pytest.mark.asyncio
    async def test_missing_credential_per_request_error(self, tmp_path: Path) -> None:
        provider = ChatGPTOAuthProvider(ChatGPTTokenStore(tmp_path / "auth.json"))
        with pytest.raises(ChatGPTAuthError, match="cli.chatgpt_login"):
            await provider.get_auth_headers()

    @pytest.mark.asyncio
    async def test_headers_from_stored_credential(self, tmp_path: Path) -> None:
        store = ChatGPTTokenStore(tmp_path / "auth.json")
        store.save(_fresh_credential())
        provider = ChatGPTOAuthProvider(store)
        headers = await provider.get_auth_headers()
        assert headers["authorization"].startswith("Bearer ")
        assert headers["chatgpt-account-id"] == "acc-123"
        assert headers["originator"] == "codex-tui"

    @pytest.mark.asyncio
    async def test_relogin_is_picked_up_without_restart(self, tmp_path: Path) -> None:
        store = ChatGPTTokenStore(tmp_path / "auth.json")
        store.save(_fresh_credential("acc-old"))
        provider = ChatGPTOAuthProvider(store)
        await provider.get_auth_headers()
        # Re-login replaces the file; the running provider must see it.
        store.save(_fresh_credential("acc-new"))
        headers = await provider.get_auth_headers()
        assert headers["chatgpt-account-id"] == "acc-new"

    @pytest.mark.asyncio
    async def test_expired_credential_triggers_refresh(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = ChatGPTTokenStore(tmp_path / "auth.json")
        expired = _fresh_credential()
        expired["expires_at"] = time.time() - 10
        store.save(expired)
        provider = ChatGPTOAuthProvider(store)

        refreshed = _fresh_credential("acc-refreshed")

        def fake_refresh(http, previous: dict) -> dict:
            assert previous["refresh_token"] == "rt-old"
            return refreshed

        monkeypatch.setattr("auth.chatgpt_oauth.refresh_tokens", fake_refresh)
        headers = await provider.get_auth_headers()
        assert headers["chatgpt-account-id"] == "acc-refreshed"
        # The new generation is persisted.
        assert store.load()["account_id"] == "acc-refreshed"

    @pytest.mark.asyncio
    async def test_shared_store_single_refresh_across_providers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = ChatGPTTokenStore(tmp_path / "auth.json")
        expired = _fresh_credential()
        expired["expires_at"] = time.time() - 10
        store.save(expired)
        first = ChatGPTOAuthProvider(ChatGPTTokenStore(tmp_path / "auth.json"))
        second = ChatGPTOAuthProvider(ChatGPTTokenStore(tmp_path / "auth.json"))

        calls: list[str] = []

        def fake_refresh(http, previous: dict) -> dict:
            calls.append(previous["refresh_token"])
            time.sleep(0.05)
            return _fresh_credential("acc-once")

        monkeypatch.setattr("auth.chatgpt_oauth.refresh_tokens", fake_refresh)
        results = await __import__("asyncio").gather(
            first.get_auth_headers(), second.get_auth_headers()
        )
        # Both providers get the refreshed credential; only ONE refresh ran.
        assert calls == ["rt-old"]
        assert all(h["chatgpt-account-id"] == "acc-once" for h in results)


class TestSchemaValidation:
    def _cfg_kwargs(self) -> dict:
        return {
            "type": "openai-translate",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_flavor": "responses",
            "chatgpt_oauth": True,
            "max_tokens_limit": 128000,
            "context_window": 1048576,
        }

    def test_valid_flag_shape(self) -> None:
        assert ProviderCfg(**self._cfg_kwargs()).chatgpt_oauth is True

    def test_flag_requires_responses_flavor(self) -> None:
        kwargs = self._cfg_kwargs()
        kwargs["api_flavor"] = "chat"
        with pytest.raises(Exception, match="api_flavor='responses'"):
            ProviderCfg(**kwargs)

    def test_flag_excludes_api_key_env(self) -> None:
        kwargs = self._cfg_kwargs()
        kwargs["api_key_env"] = "KEY"
        with pytest.raises(Exception, match="api_key_env"):
            ProviderCfg(**kwargs)

    def test_flag_pins_the_subscription_endpoint(self) -> None:
        kwargs = self._cfg_kwargs()
        kwargs["base_url"] = "https://evil.example.com/codex"
        with pytest.raises(Exception, match="chatgpt.com/backend-api/codex"):
            ProviderCfg(**kwargs)

    def test_flag_requires_tls_hostname_verification(self) -> None:
        kwargs = self._cfg_kwargs()
        kwargs["tls_verify_hostname"] = False
        with pytest.raises(Exception, match="TLS hostname verification"):
            ProviderCfg(**kwargs)


async def test_refresh_http_preserves_optional_fields(tmp_path: Path, httpx_mock) -> None:
    store = ChatGPTTokenStore(tmp_path / "auth.json")
    previous = {**_fresh_credential(), "expires_at": 1}
    store.save(previous)
    httpx_mock.add_response(
        url="https://auth.openai.com/oauth/token",
        json={
            "access_token": _fake_jwt(_auth_claim()),
            "expires_in": 3600,
        },
    )
    await ChatGPTOAuthProvider(store).get_auth_headers()
    current = store.load()
    for key in ("refresh_token", "id_token", "email"):
        assert current[key] == previous[key]


@pytest.mark.parametrize("interruption", ["relogin", "delete", "cancel"])
async def test_inflight_rotation(tmp_path: Path, monkeypatch, interruption: str) -> None:
    store = ChatGPTTokenStore(tmp_path / "auth.json")
    store.save({**_fresh_credential("old"), "expires_at": 1})
    entered, release = threading.Event(), threading.Event()
    calls = []

    def refresh(http: httpx.Client, previous: dict) -> dict:
        calls.append(previous["account_id"])
        entered.set()
        assert release.wait(5)
        return {**_fresh_credential("old"), "refresh_token": "rotated"}

    monkeypatch.setattr("auth.chatgpt_oauth.refresh_tokens", refresh)
    provider = ChatGPTOAuthProvider(store)
    task = asyncio.create_task(provider.get_auth_headers())
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        if interruption == "relogin":
            replacement = _fresh_credential("new")
            ChatGPTTokenStore(store.path).save(replacement)
        elif interruption == "delete":
            store.path.unlink()
        else:
            task.cancel()
    finally:
        release.set()
    if interruption == "relogin":
        assert (await task)["chatgpt-account-id"] == "new"
        assert store.load() == replacement
    elif interruption == "delete":
        with pytest.raises(ChatGPTAuthError):
            await task
        assert not store.path.exists()
    else:
        with pytest.raises(asyncio.CancelledError):
            await task
        await provider.get_auth_headers()
        assert store.load()["refresh_token"] == "rotated"
    assert calls == ["old"]
