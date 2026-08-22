"""Application configuration via pydantic-settings.

Settings are split by domain (server, routing, logging) and aggregated
into the root :class:`Settings` class. Required parameters are declared
as fields with no default: validation fails when `Settings` is created,
so separate imperative checks aren't needed.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_HOST_LIST_SEPARATOR = ","


def _split_hosts(raw: str) -> frozenset[str]:
    """Parse a comma-separated list of host names.

    Args:
        raw: raw string from settings, e.g. ``api.anthropic.com,localhost``.

    Returns:
        A set of lowercased host names with empty entries and trailing dots
        removed: with a trailing dot the name wouldn't match a request host.
    """
    return frozenset(
        part.strip().lower().rstrip(".")
        for part in raw.split(_HOST_LIST_SEPARATOR)
        if part.strip().strip(".")
    )


class ServerSettings(BaseSettings):
    """HTTP server parameters."""

    model_config = SettingsConfigDict(
        env_prefix="ROUTER_SERVER_", env_file=".env", extra="ignore"
    )

    host: str = "127.0.0.1"
    port: int = 8787


class UpstreamSettings(BaseSettings):
    """Parameters for the router's outgoing connections to all upstreams.

    Attributes:
        force_ipv4: bind outgoing sockets to IPv4. Needed where IPv6
            connectivity doesn't actually work but a default IPv6 route is
            still advertised -- for example left behind by an inactive VPN
            tunnel. On such a network the system doesn't report "network
            unreachable"; it silently sends packets into the void, and a
            connection attempt hangs until the ``connect`` timeout. Anthropic,
            OpenAI, and other upstreams publish AAAA records, so the
            resolver regularly returns the IPv6 address first. Unlike curl,
            httpx does not do Happy Eyeballs: it tries addresses one at a
            time, each with its own full timeout.
        max_connections: cap on concurrent connections per upstream.
        max_keepalive_connections: how many connections to keep open
            between requests.
        keepalive_expiry_s: idle duration after which a connection is
            closed. A moderate value matters after a network failure: a
            connection can be left "half-dead" -- TCP still considers it
            alive, but no traffic flows over it -- and reusing such a
            connection yields a timeout instead of a response.
        connect_timeout_s: cap on establishing the TCP connection. A live
            host answers within tens of milliseconds, so a long wait here
            means an unreachable route -- what helps is a retry, not
            patience. The value multiplies by the number of attempts, so
            together with retries it stays small.
        retry_backoff_s: pauses between connection retries, written as a
            comma-separated string in the environment. The number of pauses
            also sets the number of retries: an empty value disables them.
            A retry is safe only before the first response byte has been
            sent to the client.
    """

    model_config = SettingsConfigDict(
        env_prefix="ROUTER_UPSTREAM_",
        env_file=".env",
        extra="ignore",
        # Complex fields come through verbatim: retry_backoff_s carries the
        # plain "1,2,3,5,8" form parsed by its own validator, which the
        # default JSON decoding of list fields would reject at startup.
        enable_decoding=False,
    )

    force_ipv4: bool = True
    max_connections: int = 100
    max_keepalive_connections: int = 20
    keepalive_expiry_s: float = 30.0
    connect_timeout_s: float = 5.0
    retry_backoff_s: list[float] = [1, 2, 3, 5, 8]

    @field_validator("retry_backoff_s", mode="before")
    @classmethod
    def _parse_retry_backoff(cls, value: object) -> object:
        """Parse comma-separated retry pauses from the environment.

        Args:
            value: raw "1,2,3,5,8"-style string from the environment, or an
                already-structured list when constructed in code.

        Returns:
            The pauses as a list; an empty string disables retries.
        """
        if isinstance(value, str):
            return [
                float(part.strip())
                for part in value.split(_HOST_LIST_SEPARATOR)
                if part.strip()
            ]
        return value


class RoutingSettings(BaseSettings):
    """Paths to the provider registry YAML and the certificate root."""

    model_config = SettingsConfigDict(
        env_prefix="ROUTER_", env_file=".env", extra="ignore"
    )

    config_path: Path
    certs_dir: Path = Path("./certs")


class ProxySettings(BaseSettings):
    """Forward-proxy mode parameters (accepting CONNECT, MITM, proxy chaining).

    ``ca_dir`` is separate from ``RoutingSettings.certs_dir`` (that one holds
    upstream provider CA bundles, read as ready-made files) -- here the
    router itself generates and persistently stores the root CA's private
    key across restarts.

    Host lists (``mitm_hosts``, ``no_proxy_hosts``) are declared as
    comma-separated strings rather than ``list[str]``: pydantic-settings
    reads collections from the environment only as JSON, and a variable like
    ``["api.anthropic.com"]`` is awkward in ``.env``. The parsed values are
    returned by :meth:`mitm_host_set` and :meth:`no_proxy_host_set`.

    Attributes:
        enabled: whether to start the forward-proxy listener at all.
            Disabled by default: the ASGI listener costs nothing and gets in
            no one's way, whereas the forward-proxy requires the client to
            separately trust a self-signed CA (see ``NODE_EXTRA_CA_CERTS`` in
            the README) -- so the only real choice the user makes is whether
            to start the forward-proxy, not whether to start the ASGI
            listener. The False default guarantees that an already running
            service (uvicorn directly as a factory) doesn't change behavior
            on its next restart.
        ca_dir: directory storing the root CA for MITM termination.
        host: interface the proxy port listens on.
        port: port accepting CONNECT; differs from the ASGI server port so
            both modes can run at the same time.
        mitm_hosts: allowlist of hosts for which TLS is terminated and the
            request is handled by the provider registry. All other hosts go
            through a blind tunnel without decryption.
        connect_timeout_s: cap on establishing the outgoing connection and
            waiting for the upstream proxy's response.
        tls_handshake_timeout_s: cap on the TLS handshake -- both with the
            client (MITM termination) and with the real upstream.
        client_request_timeout_s: cap on reading the next request from the
            client: the ``CONNECT`` line, headers, and HTTP request body on
            the decrypted MITM connection. It also bounds idling in
            keep-alive -- waiting for the NEXT request on the same
            connection uses the same read as finishing the current one, so
            there's no separate idle setting.
        upstream_headers_timeout_s: cap on waiting for the upstream
            response's status and headers -- separate from the body: a
            streaming response can run for minutes, and the body timeout
            must not interrupt it.
        idle_timeout_s: cap on silence between chunks of an already started
            transfer -- the upstream response body and the blind tunnel
            (including the tunnel after a protocol switch). Idle, not a
            total transfer cap: a long legitimate stream must not be cut off
            as long as bytes keep flowing.
        upstream_proxy_url: address of a corporate HTTP proxy such as
            ``http://proxy.corp.internal:3128`` through which the router
            itself reaches the outside world. Empty -- direct connections.
        upstream_ca_bundle: CA bundle for VERIFYING certificates on the
            outgoing side (needed if a corporate proxy does TLS inspection).
            Not to be confused with ``ca_dir``: that holds the CA the router
            uses to sign the certificate presented to the client on the
            incoming side. Empty -- the system's trusted root store.
        no_proxy_hosts: hosts that always go direct, even when
            ``upstream_proxy_url`` is set.
        retry_max_attempts: how many times in total (including the first) to
            try establishing the outgoing TCP/TLS connection to the target.
            A retry is safe only at this step -- before a single byte of the
            response has reached the client and before a single byte of the
            request has reached the upstream.
        retry_backoff_base_s: base pause before the first retry; subsequent
            pauses double (0.25, 0.5, 1.0, ...) up to ``retry_backoff_max_s``.
        retry_backoff_max_s: cap on the pause between retries.
    """

    model_config = SettingsConfigDict(
        env_prefix="ROUTER_PROXY_", env_file=".env", extra="ignore"
    )

    enabled: bool = False
    ca_dir: Path = Path("./proxy-ca")
    host: str = "127.0.0.1"
    port: int = 8788
    mitm_hosts: str = "api.anthropic.com"
    connect_timeout_s: float = 10.0
    tls_handshake_timeout_s: float = 10.0
    client_request_timeout_s: float = 30.0
    upstream_headers_timeout_s: float = 30.0
    idle_timeout_s: float = 60.0
    upstream_proxy_url: str | None = None
    upstream_ca_bundle: Path | None = None
    no_proxy_hosts: str = ""
    retry_max_attempts: int = 3
    retry_backoff_base_s: float = 0.25
    retry_backoff_max_s: float = 2.0

    def mitm_host_set(self) -> frozenset[str]:
        """Parse the MITM host allowlist.

        Returns:
            A set of lowercased host names; empty entries are dropped.
        """
        return _split_hosts(self.mitm_hosts)

    def no_proxy_host_set(self) -> frozenset[str]:
        """Parse the list of hosts that bypass the upstream proxy.

        Returns:
            A set of lowercased host names; empty entries are dropped.
        """
        return _split_hosts(self.no_proxy_hosts)


class LoggingSettings(BaseSettings):
    """Logging settings."""

    model_config = SettingsConfigDict(
        env_prefix="ROUTER_LOG_", env_file=".env", extra="ignore"
    )

    conf_path: Path = Path("./logging_conf.json")
    level: str = "INFO"


class SecretsResolver(BaseSettings):
    """Lazily read provider secrets by environment variable name.

    Variable names are set in routing.yaml (the ``api_key_env`` field), so
    adding a new provider doesn't require editing the settings class.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def resolve(self, env_var: str) -> SecretStr | None:
        """Return an environment variable's value as a secret.

        Args:
            env_var: name of the environment variable holding the provider key.

        Returns:
            The secret, or None if the variable is unset or empty.
        """
        value = os.getenv(env_var)
        return SecretStr(value) if value else None


class Settings(BaseSettings):
    """Root application settings."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    server: ServerSettings = Field(default_factory=ServerSettings)
    # RoutingSettings has a required config_path field (from the
    # environment), so mypy sees default_factory as a call with a missing
    # argument -- ignored.
    routing: RoutingSettings = Field(default_factory=RoutingSettings)  # type: ignore[arg-type]
    proxy: ProxySettings = Field(default_factory=ProxySettings)
    upstream: UpstreamSettings = Field(default_factory=UpstreamSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    secrets: SecretsResolver = Field(default_factory=SecretsResolver)
