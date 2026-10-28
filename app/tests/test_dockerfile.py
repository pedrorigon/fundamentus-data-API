from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_runtime_image_and_installer_are_immutable() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    from_lines = [line for line in dockerfile.splitlines() if line.startswith("FROM ")]
    assert len(from_lines) == 2
    assert all("@sha256:" in line for line in from_lines)


def test_container_installs_the_frozen_lock_without_pip_resolution() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY pyproject.toml uv.lock README.md ./" in dockerfile
    assert dockerfile.count("uv sync --frozen --no-dev") == 2
    assert "pip install" not in dockerfile


def test_container_context_excludes_local_credentials_and_metadata() -> None:
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert ".git" in ignored
    assert ".env" in ignored
    assert ".env.*" in ignored
