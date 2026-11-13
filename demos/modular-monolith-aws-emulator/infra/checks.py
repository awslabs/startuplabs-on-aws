"""Post-deploy checks against the real AWS resources. Each returns (ok, detail)."""

from __future__ import annotations

import base64
import contextlib
import json
import secrets
import ssl
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import PlatformTarget, Settings
from app.container import load_modules
from app.platform.adapters.aws.rds_data_sql import RdsDataSqlDatabase
from app.platform.adapters.aws.session import AwsClientFactory
from app.repository import migrations

Result = tuple[bool, str]


@dataclass
class Target:
    region: str
    outputs: dict[str, str]
    prefix: str

    @property
    def api_url(self) -> str:
        return self.outputs["api_url"]

    def settings(self) -> Settings:
        fields = {k: v for k, v in self.outputs.items() if k in Settings.model_fields}
        base = Settings(
            _env_file=None,  # type: ignore[call-arg]
            platform_target=PlatformTarget.AWS,
            aws_region=self.region,
        )
        return base.model_copy(update=fields)


def _unsigned_token() -> str:
    """A syntactically valid JWT with alg=none: the gateway must reject it."""

    def segment(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{segment({'alg': 'none'})}.{segment({'sub': 'forged'})}."


class Checks:
    def __init__(self, target: Target) -> None:
        self.t = target
        self.clients = AwsClientFactory(target.settings())
        self.http = httpx.Client(timeout=30.0, verify=ssl.create_default_context())

    def c(self, service: str) -> Any:
        return self.clients.client(service)

    def _sql(self) -> RdsDataSqlDatabase:
        o = self.t.outputs
        return RdsDataSqlDatabase(self.clients, o["db_cluster_arn"], o["db_secret_arn"], "app")

    # -- platform --------------------------------------------------------
    def migrations(self) -> Result:
        db = self._sql()
        started = time.monotonic()
        deadline = started + 120
        while True:  # first call may wake an auto-paused cluster
            try:
                applied = migrations.apply(
                    db, [m.migrations for m in load_modules() if m.migrations]
                )
                break
            except Exception as exc:
                if time.monotonic() > deadline:
                    return False, f"{type(exc).__name__}: {exc}"[:160]
                time.sleep(5)
        took = time.monotonic() - started
        return True, f"applied {applied or 'nothing new'} ({took:.1f} s)"

    def cognito(self) -> Result:
        pool = self.c("cognito-idp").describe_user_pool(
            UserPoolId=self.t.outputs["cognito_user_pool_id"]
        )["UserPool"]
        issuer = self.clients.cognito_issuer(pool["Id"])
        keys = self.http.get(f"{issuer}/.well-known/jwks.json").json()["keys"]
        users = pool.get("EstimatedNumberOfUsers", 0)
        return True, f"{pool['Id']} | {users} users | JWKS {len(keys)} keys"

    def aurora(self) -> Result:
        started = time.monotonic()
        rows = self._sql().execute("SELECT count(*) AS n FROM users")
        took = time.monotonic() - started
        note = " (resumed from pause)" if took > 5 else ""
        return True, f"Data API ok, users={rows[0]['n']}, {took:.1f} s{note}"

    def dynamodb(self) -> Result:
        name = self.t.outputs["profiles_table"]
        ddb = self.c("dynamodb")
        table = ddb.describe_table(TableName=name)["Table"]
        pitr = ddb.describe_continuous_backups(TableName=name)["ContinuousBackupsDescription"]
        pitr_on = pitr["PointInTimeRecoveryDescription"]["PointInTimeRecoveryStatus"]
        return table["TableStatus"] == "ACTIVE", f"{name} {table['TableStatus']} | PITR {pitr_on}"

    def s3(self) -> Result:
        bucket = self.t.outputs["assets_bucket"]
        s3 = self.c("s3")
        bpa = s3.get_public_access_block(Bucket=bucket)["PublicAccessBlockConfiguration"]
        enc = s3.get_bucket_encryption(Bucket=bucket)["ServerSideEncryptionConfiguration"]
        algo = enc["Rules"][0]["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"]
        ok = all(bpa.values())
        return ok, f"Block Public Access {'4/4' if ok else 'INCOMPLETE'} | {algo}"

    def lambda_fn(self) -> Result:
        resp = self.c("lambda").invoke(
            FunctionName=self.t.outputs["word_count_function"],
            Payload=json.dumps({"text": "rollout check rollout"}).encode(),
        )
        payload = json.loads(resp["Payload"].read())
        ok = resp["StatusCode"] == 200 and payload.get("words") == 3
        return ok, f"word-count -> {payload.get('words')} words"

    def api_health(self) -> Result:
        r = self.http.get(f"{self.t.api_url}/healthz")
        body = r.json() if r.status_code == 200 else {}
        ok = r.status_code == 200 and body.get("target") == "aws"
        return ok, f"HTTP {r.status_code} v{body.get('version', '?')} target={body.get('target')}"

    def jwt_gate(self) -> Result:
        """Anonymous and forged tokens must be refused by the gateway, not the app."""
        anon = self.http.get(f"{self.t.api_url}/api/v1/auth/me")
        forged = self.http.get(
            f"{self.t.api_url}/api/v1/profiles/me",
            headers={"authorization": f"Bearer {_unsigned_token()}"},
        )
        docs = self.http.get(f"{self.t.api_url}/openapi.json")
        gateway = {"message": "Unauthorized"}
        ok = (
            anon.status_code == 401
            and anon.json() == gateway
            and forged.status_code == 401
            and docs.status_code == 401
        )
        return (
            ok,
            f"no token {anon.status_code}, forged {forged.status_code}, "
            f"/openapi.json {docs.status_code}",
        )

    def alarms(self) -> Result:
        resp = self.c("cloudwatch").describe_alarms(AlarmNamePrefix=f"{self.t.prefix}-")
        states = [a["StateValue"] for a in resp["MetricAlarms"]]
        firing = states.count("ALARM")
        summary = ", ".join(f"{s.lower()} {states.count(s)}" for s in sorted(set(states)))
        return firing == 0, f"{len(states)} alarms: {summary}" if states else "no alarms found"

    # -- end-to-end ------------------------------------------------------
    def smoke(self, log: Callable[[str], None] = lambda _m: None) -> Result:
        """Throwaway user through the public API. Cleans up everything it creates."""
        o = self.t.outputs
        idp = self.c("cognito-idp")
        email = f"rollout-smoke-{uuid.uuid4().hex[:10]}@example.com"
        password = f"{secrets.token_urlsafe(18)}Aa1!"
        subject: str | None = None
        api = self.t.api_url
        try:
            idp.admin_create_user(
                UserPoolId=o["cognito_user_pool_id"],
                Username=email,
                MessageAction="SUPPRESS",
                UserAttributes=[
                    {"Name": "email", "Value": email},
                    {"Name": "email_verified", "Value": "true"},
                    {"Name": "name", "Value": "Rollout Smoke"},
                ],
            )
            idp.admin_set_user_password(
                UserPoolId=o["cognito_user_pool_id"],
                Username=email,
                Password=password,
                Permanent=True,
            )
            log("login")
            r = self.http.post(
                f"{api}/api/v1/auth/login", json={"email": email, "password": password}
            )
            if r.status_code != 200:
                return False, f"login HTTP {r.status_code}: {r.text[:120]}"
            h = {"authorization": f"Bearer {r.json()['access_token']}"}
            log("me")
            me = self.http.get(f"{api}/api/v1/auth/me", headers=h)
            if me.status_code != 200:
                return False, f"/me HTTP {me.status_code}: {me.text[:120]}"
            subject = me.json()["id"]
            log("profile")
            prof = self.http.get(f"{api}/api/v1/profiles/me", headers=h)
            if prof.status_code != 200:
                return False, f"/profiles/me HTTP {prof.status_code}"
            log("avatar")
            up = self.http.post(
                f"{api}/api/v1/profiles/me/avatar", headers=h, json={"content_type": "image/png"}
            ).json()
            put = self.http.put(
                up["url"], content=b"\x89PNG", headers={"content-type": "image/png"}
            )
            down_url = self.http.get(f"{api}/api/v1/profiles/me/avatar", headers=h).json()["url"]
            got = self.http.get(down_url).content
            if put.status_code != 200 or got != b"\x89PNG":
                return False, f"avatar PUT {put.status_code}, GET {len(got)} bytes"
            log("job")
            job = self.http.post(
                f"{api}/api/v1/jobs/word-count", headers=h, json={"text": "a b a"}
            ).json()
            if job.get("result", {}).get("words") != 3:
                return False, f"word-count {job}"
            log("authz")
            admin = self.http.get(f"{api}/api/v1/auth/users", headers=h).status_code
            out = self.http.post(f"{api}/api/v1/auth/logout", headers=h).status_code
            if admin != 403 or out != 204:
                return False, f"admin route {admin} (want 403), logout {out}"
            return True, "login, me, profile, avatar PUT/GET, Lambda job, 403 admin, logout"
        finally:
            log("cleanup")
            self._cleanup(email, subject)

    def _cleanup(self, email: str, subject: str | None) -> None:
        o = self.t.outputs
        with contextlib.suppress(Exception):
            self.c("cognito-idp").admin_delete_user(
                UserPoolId=o["cognito_user_pool_id"], Username=email
            )
        if subject:
            with contextlib.suppress(Exception):
                self._sql().execute("DELETE FROM users WHERE id = :id", {"id": subject})
            with contextlib.suppress(Exception):
                self.c("dynamodb").delete_item(
                    TableName=o["profiles_table"], Key={"pk": {"S": subject}}
                )
            with contextlib.suppress(Exception):
                self.c("s3").delete_object(
                    Bucket=o["assets_bucket"], Key=f"avatars/{subject}/avatar.png"
                )


HEALTH: list[tuple[str, str]] = [
    ("API /healthz", "api_health"),
    ("JWT gate (gateway)", "jwt_gate"),
    ("Identity (Cognito)", "cognito"),
    ("Database (Aurora Data API)", "aurora"),
    ("Documents (DynamoDB)", "dynamodb"),
    ("Storage (S3)", "s3"),
    ("Compute (Lambda)", "lambda_fn"),
    ("Alarms", "alarms"),
]
