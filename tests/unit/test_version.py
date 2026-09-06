"""Unit tests for ``version.project_version``."""

from __future__ import annotations

import tomllib
from pathlib import Path

from version import PYPROJECT_PATH, UNKNOWN_VERSION, project_version

_REPO_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def test_default_path_is_the_repository_pyproject() -> None:
    assert PYPROJECT_PATH == _REPO_PYPROJECT


def test_reads_the_released_version() -> None:
    expected = tomllib.loads(_REPO_PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    assert project_version() == expected
    assert project_version() != UNKNOWN_VERSION


def test_reads_a_given_file(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\nversion = "9.8.7"\n', encoding="utf-8")
    assert project_version(pyproject) == "9.8.7"


def test_missing_file_reports_the_unknown_version(tmp_path: Path) -> None:
    assert project_version(tmp_path / "nope.toml") == UNKNOWN_VERSION


def test_malformed_or_versionless_file_reports_the_unknown_version(tmp_path: Path) -> None:
    broken = tmp_path / "broken.toml"
    broken.write_text("[project\n", encoding="utf-8")
    assert project_version(broken) == UNKNOWN_VERSION
    versionless = tmp_path / "versionless.toml"
    versionless.write_text('[project]\nname = "x"\n', encoding="utf-8")
    assert project_version(versionless) == UNKNOWN_VERSION
