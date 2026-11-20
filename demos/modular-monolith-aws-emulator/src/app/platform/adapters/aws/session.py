"""boto3 client factory. The single place that knows *where* AWS is.

Target `emulator`:
  * endpoint  https://<emulator_host>:<port>  (loopback only, enforced)
  * creds     <emulator_home>/<port>/emulator_credentials (per-run keys written
              by the emulator). Passed explicitly, so ambient real credentials
              (AWS_PROFILE, env keys, IMDS) can never be used by accident.
  * TLS       <emulator_home>/<port>/ca.pem (the emulator's self-signed CA)
  * S3        path-style + SigV4 (the emulator rejects virtual-host buckets)
  * host prefix injection off (Cloud Map DiscoverInstances, SFN sync, ...)

Target `aws`: the default credential chain and regional endpoints. Nothing else
in the codebase changes between the two.
"""

from __future__ import annotations

import configparser
import ipaddress
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config

from app.config import PlatformTarget, Settings

_LOOPBACK_NAMES = {"localhost"}


@dataclass(frozen=True, slots=True)
class EmulatorEndpoint:
    host: str
    port: int
    home: Path

    @property
    def url(self) -> str:
        return f"https://{self.host}:{self.port}"

    @property
    def run_dir(self) -> Path:
        return self.home / str(self.port)

    @property
    def ca_bundle(self) -> Path:
        return self.run_dir / "ca.pem"

    @property
    def credentials_file(self) -> Path:
        return self.run_dir / "emulator_credentials"

    def credentials(self) -> tuple[str, str]:
        if not self.credentials_file.exists():
            raise RuntimeError(
                f"AWS Emulator credentials not found at {self.credentials_file}. "
                "Start it with `just emulator-up`."
            )
        parser = configparser.ConfigParser()
        parser.read(self.credentials_file)
        section = parser["default"]
        return section["aws_access_key_id"], section["aws_secret_access_key"]

    def assert_loopback(self) -> None:
        if self.host in _LOOPBACK_NAMES:
            raise ValueError(
                "use 127.0.0.1, not localhost: with a host name the JS/Java/Go SDKs "
                "switch S3 to virtual-host addressing, which the emulator rejects"
            )
        if not ipaddress.ip_address(self.host).is_loopback:
            raise ValueError(f"emulator host must be loopback, got {self.host!r}")


class AwsClientFactory:
    """Creates and caches boto3 clients for the configured target."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._clients: dict[str, Any] = {}
        self._session: boto3.Session | None = None
        self.emulator: EmulatorEndpoint | None = None
        if settings.platform_target is PlatformTarget.EMULATOR:
            self.emulator = EmulatorEndpoint(
                settings.emulator_host, settings.emulator_port, settings.emulator_home
            )
            self.emulator.assert_loopback()

    @property
    def target(self) -> PlatformTarget:
        return self._settings.platform_target

    @property
    def region(self) -> str:
        return self._settings.aws_region

    @property
    def ca_bundle(self) -> str | None:
        return str(self.emulator.ca_bundle) if self.emulator else None

    def _boto_session(self) -> boto3.Session:
        if self._session is None:
            if self.emulator:
                key, secret = self.emulator.credentials()
                self._session = boto3.Session(
                    aws_access_key_id=key,
                    aws_secret_access_key=secret,
                    region_name=self.region,
                )
            else:
                self._session = boto3.Session(region_name=self.region)
        return self._session

    def client(self, service: str) -> Any:
        with self._lock:
            if service not in self._clients:
                self._clients[service] = self._build(service)
            return self._clients[service]

    def _build(self, service: str) -> Any:
        config = Config(
            retries={"mode": "standard", "max_attempts": 3},
            signature_version="s3v4" if service == "s3" else None,
            s3={"addressing_style": "path"} if service == "s3" else None,
            inject_host_prefix=self.emulator is None,
        )
        kwargs: dict[str, Any] = {"config": config}
        if self.emulator:
            kwargs["endpoint_url"] = self.emulator.url
            kwargs["verify"] = str(self.emulator.ca_bundle)
        return self._boto_session().client(service, **kwargs)

    def account_id(self) -> str:
        return str(self.client("sts").get_caller_identity()["Account"])

    def cognito_issuer(self, user_pool_id: str) -> str:
        if self.emulator:
            return f"{self.emulator.url}/{user_pool_id}"
        return f"https://cognito-idp.{self.region}.amazonaws.com/{user_pool_id}"
