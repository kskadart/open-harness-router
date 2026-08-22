"""Provider factory built from ``routing.yaml`` configuration."""

from __future__ import annotations

from errors import ConfigError
from providers.base import Provider
from providers.openai_translate import OpenAITranslateProvider
from providers.passthrough import PassthroughProvider
from routing.schema import ProviderCfg
from settings import Settings


def build_provider(name: str, cfg: ProviderCfg, settings: Settings) -> Provider:
    """Build a provider of the requested type.

    Args:
        name: provider name in the registry.
        cfg: provider configuration.
        settings: application settings (secrets, certificate directory).

    Returns:
        Ready-to-use provider.

    Raises:
        ConfigError: if openai-translate has no key, or the CA bundle is
            missing.
    """
    if cfg.type == "passthrough":
        return PassthroughProvider(name, cfg, settings.upstream)

    api_key = settings.secrets.resolve(cfg.api_key_env) if cfg.api_key_env else None
    if api_key is None:
        raise ConfigError(
            f"provider '{name}': env '{cfg.api_key_env}' with API key is not set"
        )

    ca_path = None
    if cfg.ca_bundle:
        ca_path = settings.routing.certs_dir / cfg.ca_bundle
        if not ca_path.exists():
            raise ConfigError(f"provider '{name}': CA bundle not found: {ca_path}")

    return OpenAITranslateProvider(name, cfg, api_key, ca_path, settings.upstream)
