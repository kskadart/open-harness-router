"""The router's version, read from ``pyproject.toml``.

``pyproject.toml`` is the single place the release tooling bumps
(``cli.release bump``), so the running application reads its version from
there instead of repeating it in code -- ``/dashboard``, ``/dashboard/state``
and ``ohr_info{version}`` on ``/metrics`` all show what was released. The
project is not installed as a package (``[tool.uv] package = false``), so
``importlib.metadata`` cannot answer; the file sits one level above ``src``.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

PYPROJECT_PATH = Path(__file__).resolve().parent.parent / "pyproject.toml"

# What the application reports when the file is unreadable or carries no
# version: a checkout without pyproject.toml is not a release of anything.
UNKNOWN_VERSION = "0.0.0"


def project_version(pyproject_path: Path = PYPROJECT_PATH) -> str:
    """Return ``project.version`` from ``pyproject.toml``.

    Args:
        pyproject_path: the file to read; the repository's by default.

    Returns:
        The version string, or ``UNKNOWN_VERSION`` when the file is
        missing, malformed or has no ``project.version``.
    """
    try:
        document = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return UNKNOWN_VERSION
    project = document.get("project")
    version = project.get("version") if isinstance(project, dict) else None
    return version if isinstance(version, str) and version else UNKNOWN_VERSION
