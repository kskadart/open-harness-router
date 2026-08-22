"""Load and validate ``routing.yaml`` into the :class:`RoutingConfig` model."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from errors import ConfigError
from routing.schema import RoutingConfig


def load_routing_config(path: Path) -> RoutingConfig:
    """Load and validate the routing file.

    Args:
        path: path to ``routing.yaml``.

    Returns:
        The validated routing configuration.

    Raises:
        ConfigError: if the file is missing, isn't valid YAML, or fails
            schema validation.
    """
    if not path.exists():
        raise ConfigError(f"routing config not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"routing config must be a mapping: {path}")
    try:
        return RoutingConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid routing config {path}: {exc}") from exc
