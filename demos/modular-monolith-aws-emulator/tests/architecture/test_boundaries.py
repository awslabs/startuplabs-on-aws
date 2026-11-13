"""Module boundaries are part of the test suite, not only of `just lint`."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_import_contracts_hold() -> None:
    lint = Path(sys.executable).parent / "lint-imports"
    result = subprocess.run(  # noqa: S603
        [str(lint)], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
