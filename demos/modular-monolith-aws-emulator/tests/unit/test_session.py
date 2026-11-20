from __future__ import annotations

from pathlib import Path

import pytest

from app.config import PlatformTarget, Settings
from app.platform.adapters.aws.session import AwsClientFactory, EmulatorEndpoint


def _emulator_home(tmp_path: Path) -> Path:
    run = tmp_path / "9297"
    run.mkdir()
    (run / "emulator_credentials").write_text(
        "[default]\naws_access_key_id = AKIAEMU\naws_secret_access_key = secretemu\n"
    )
    (run / "ca.pem").write_text("dummy")
    return tmp_path


def test_rejects_non_loopback_and_localhost(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="loopback"):
        EmulatorEndpoint("10.0.0.5", 9297, tmp_path).assert_loopback()
    with pytest.raises(ValueError, match=r"127\.0\.0\.1"):
        EmulatorEndpoint("localhost", 9297, tmp_path).assert_loopback()


def test_emulator_clients_use_emulator_endpoint_and_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Real-looking ambient credentials must be ignored in emulator mode.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAREALREAL")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "real")
    s = Settings(_env_file=None, emulator_home=_emulator_home(tmp_path))  # type: ignore[call-arg]
    f = AwsClientFactory(s)
    s3 = f.client("s3")
    assert s3.meta.endpoint_url == "https://127.0.0.1:9297"
    creds = s3._request_signer._credentials
    assert creds.access_key == "AKIAEMU"
    assert f.cognito_issuer("pool") == "https://127.0.0.1:9297/pool"


def test_aws_target_uses_regional_issuer() -> None:
    s = Settings(_env_file=None, platform_target=PlatformTarget.AWS, aws_region="sa-east-1")  # type: ignore[call-arg]
    f = AwsClientFactory(s)
    assert f.emulator is None
    assert f.cognito_issuer("p") == "https://cognito-idp.sa-east-1.amazonaws.com/p"


def test_missing_emulator_credentials_message(tmp_path: Path) -> None:
    s = Settings(_env_file=None, emulator_home=tmp_path)  # type: ignore[call-arg]
    with pytest.raises(RuntimeError, match="just emulator-up"):
        AwsClientFactory(s).client("sts")
