"""Provider factory built from ``routing.yaml`` configuration."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from pydantic import SecretStr

from errors import ConfigError
from providers.base import Provider
from providers.openai_translate import OpenAITranslateProvider
from providers.passthrough import PassthroughProvider
from routing.schema import ProviderCfg
from settings import Settings


def _resolve_ca_bundle(name: str, cfg: ProviderCfg, settings: Settings) -> Path | None:
    """Resolve and validate a provider's CA bundle path, if configured.

    Shared by both provider types: passthrough can now reach arbitrary
    Anthropic-compatible upstreams (including corporate/self-hosted ones
    behind a private CA), the same reason openai-translate needed this.

    Args:
        name: provider name in the registry.
        cfg: provider configuration.
        settings: application settings (certificate directory).

    Returns:
        The resolved path, or None if ``ca_bundle`` is not set.

    Raises:
        ConfigError: the configured bundle file does not exist.
    """
    if not cfg.ca_bundle:
        return None
    ca_path = settings.routing.certs_dir / cfg.ca_bundle
    if not ca_path.exists():
        raise ConfigError(f"provider '{name}': CA bundle not found: {ca_path}")
    return ca_path


def _resolve_required_api_key(name: str, cfg: ProviderCfg, settings: Settings) -> SecretStr:
    """Resolve a provider's own API key, raising if the environment variable is unset.

    Shared by both call sites in ``build_provider``: openai-translate always
    needs its own key, passthrough needs one exactly when
    ``forward_client_auth=false``. Either way the schema validator already
    guarantees ``cfg.api_key_env`` is set by the time this is called (the
    ``cast`` below reflects that guarantee rather than re-checking it) --
    what this function actually validates is the environment VARIABLE's
    value, which schema validation cannot see.

    Args:
        name: provider name in the registry.
        cfg: provider configuration.
        settings: application settings (secrets resolver).

    Returns:
        The resolved secret.

    Raises:
        ConfigError: the environment variable named by ``cfg.api_key_env``
            is unset or empty.
    """
    api_key = settings.secrets.resolve(cast(str, cfg.api_key_env))
    if api_key is None:
        raise ConfigError(
            f"provider '{name}': env '{cfg.api_key_env}' with API key is not set"
        )
    return api_key


def build_provider(name: str, cfg: ProviderCfg, settings: Settings) -> Provider:
    """Build a provider of the requested type.

    Args:
        name: provider name in the registry.
        cfg: provider configuration.
        settings: application settings (secrets, certificate directory).

    Returns:
        Ready-to-use provider.

    Raises:
        ConfigError: if a provider that needs its own key (openai-translate
            always, passthrough with ``forward_client_auth=false``) has none
            set, or the CA bundle is missing.
    """
    ca_path = _resolve_ca_bundle(name, cfg, settings)

    if cfg.type == "passthrough":
        api_key = None
        if not cfg.forward_client_auth:
            api_key = _resolve_required_api_key(name, cfg, settings)
        return PassthroughProvider(name, cfg, settings.upstream, api_key, ca_path)

    api_key = _resolve_required_api_key(name, cfg, settings)
    return OpenAITranslateProvider(name, cfg, api_key, ca_path, settings.upstream)
