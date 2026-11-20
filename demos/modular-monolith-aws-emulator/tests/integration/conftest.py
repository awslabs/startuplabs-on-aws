"""Integration fixtures: real adapters against the running AWS Emulator.

`just test-integration` starts the emulator and runs the bootstrap first. If the
emulator or the outputs file is missing, tests fail with the command to run.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.container import build_platform, load_modules
from app.main import create_app
from app.platform.adapters.aws.session import AwsClientFactory

ROOT = Path(__file__).resolve().parents[2]


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def emu_settings() -> Settings:
    outputs = ROOT / ".platform/outputs.json"
    if not outputs.exists():
        pytest.fail("no .platform/outputs.json: run `just bootstrap` (or `just test-integration`)")
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        outputs_file=outputs,
        strangler_config=ROOT / "strangler.yaml",
        identity_auto_confirm=True,
    ).with_outputs()


@pytest.fixture(scope="session")
def clients(emu_settings: Settings) -> AwsClientFactory:
    f = AwsClientFactory(emu_settings)
    assert f.account_id() == "000000000000", "not talking to the emulator"
    return f


@pytest.fixture(scope="session")
def api(emu_settings: Settings, clients: AwsClientFactory) -> Iterator[TestClient]:
    platform = build_platform(emu_settings, load_modules())
    with TestClient(create_app(emu_settings, platform)) as c:
        yield c
