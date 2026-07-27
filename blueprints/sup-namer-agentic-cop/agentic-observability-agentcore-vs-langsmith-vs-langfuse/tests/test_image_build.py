"""Pre-flight validation for the container image build (no Docker required).

The agent image is built in AWS CodeBuild at deploy time (``deploy-time-build``),
so a bad Dockerfile ``COPY`` or a Python syntax error would otherwise only
surface minutes into a deploy. These checks shift those failures left — they need
**no Docker daemon, no AWS credentials, and no network**:

* every local ``COPY``/``ADD`` source in ``docker/Dockerfile`` exists in the
  build context (``docker/``), and
* every Python module under ``docker/app`` compiles (catches syntax errors).

The dependency-resolution check (``pip install --dry-run`` against
``docker/requirements.txt``) needs network access, so it lives in the
``make validate-image`` target rather than in this hermetic test module.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# This file lives at ``<blueprint>/tests/test_image_build.py``; the container
# build context is ``<blueprint>/docker``.
BLUEPRINT_ROOT = Path(__file__).resolve().parent.parent
DOCKER_DIR = BLUEPRINT_ROOT / "docker"
DOCKERFILE = DOCKER_DIR / "Dockerfile"
APP_DIR = DOCKER_DIR / "app"


def _copy_add_sources(dockerfile_text: str) -> list[str]:
    """Return the local ``COPY``/``ADD`` source paths from a Dockerfile.

    Handles the shell form (``COPY src... dest``) and backslash line
    continuations. Skips ``--from=<stage>`` copies (their source is a build
    stage, not the build context) and instruction flags such as ``--chown`` /
    ``--chmod``. The JSON-array ``COPY`` form is not used by this Dockerfile.
    """
    sources: list[str] = []
    # Join backslash-continued lines so a multi-line instruction is one line.
    logical = dockerfile_text.replace("\\\n", " ")
    for raw in logical.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if parts[0].upper() not in ("COPY", "ADD"):
            continue
        args = parts[1:]
        if any(arg.startswith("--from") for arg in args):
            # Multi-stage copy: the source is another build stage, not a file
            # in the build context, so there is nothing local to verify.
            continue
        args = [arg for arg in args if not arg.startswith("--")]
        if len(args) < 2:
            # Need at least one source plus the destination.
            continue
        # Everything but the final token (the destination) is a source.
        sources.extend(arg.strip('"') for arg in args[:-1])
    return sources


def test_dockerfile_exists() -> None:
    """The Dockerfile is present at the expected build-context path."""
    assert DOCKERFILE.is_file(), f"missing Dockerfile at {DOCKERFILE}"


def test_dockerfile_copy_sources_exist() -> None:
    """Every local COPY/ADD source resolves inside the build context.

    Catches the "missing file" failure — a ``COPY`` of a path that does not
    exist — before CodeBuild does, since ``docker build`` fails on it mid-deploy.
    """
    sources = _copy_add_sources(DOCKERFILE.read_text(encoding="utf-8"))
    assert sources, "expected at least one COPY/ADD source in the Dockerfile"
    missing = [src for src in sources if not (DOCKER_DIR / src).exists()]
    assert not missing, (
        "Dockerfile COPY/ADD source(s) missing from the build context "
        f"({DOCKER_DIR}): {missing}"
    )


def test_agent_app_sources_compile() -> None:
    """Every Python module under ``docker/app`` compiles (no syntax errors).

    ``compile(...)`` runs the same syntax check the image's build/runtime would,
    without importing the module or writing bytecode, so it stays fully hermetic
    (no ``__pycache__`` side effects).
    """
    py_files = sorted(APP_DIR.rglob("*.py"))
    assert py_files, f"expected at least one .py file under {APP_DIR}"
    for py_file in py_files:
        source = py_file.read_text(encoding="utf-8")
        try:
            compile(source, str(py_file), "exec")
        except SyntaxError as exc:  # pragma: no cover - exercised only on failure
            pytest.fail(f"syntax error in {py_file}: {exc}")
