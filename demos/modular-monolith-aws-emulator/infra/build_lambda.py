"""Build the API Lambda bundle (arm64, python3.12) into build/lambda without Docker."""

from __future__ import annotations

import shutil
import subprocess
import sys

from infra.settings import LAMBDA_BUILD, ROOT


def run(*cmd: str) -> None:
    subprocess.run(cmd, cwd=ROOT, check=True)  # noqa: S603


def main() -> int:
    if LAMBDA_BUILD.exists():
        shutil.rmtree(LAMBDA_BUILD)
    LAMBDA_BUILD.mkdir(parents=True)
    reqs = LAMBDA_BUILD.parent / "requirements-lambda.txt"
    run(
        "uv", "export", "--quiet", "--frozen", "--no-dev", "--no-group", "infra",
        "--extra", "aws", "--no-emit-project", "--no-hashes", "-o", str(reqs),
    )  # fmt: skip
    run(
        "uv", "pip", "install", "--quiet", "--target", str(LAMBDA_BUILD), "-r", str(reqs),
        "--python-platform", "aarch64-manylinux2014", "--python-version", "3.12",
        "--only-binary", ":all:",
    )  # fmt: skip
    shutil.copytree(
        ROOT / "src" / "app",
        LAMBDA_BUILD / "app",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "legacy"),
    )
    shutil.copy(ROOT / "strangler.yaml", LAMBDA_BUILD / "strangler.yaml")
    size = sum(p.stat().st_size for p in LAMBDA_BUILD.rglob("*") if p.is_file())
    print(f"lambda bundle: {LAMBDA_BUILD} ({size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
