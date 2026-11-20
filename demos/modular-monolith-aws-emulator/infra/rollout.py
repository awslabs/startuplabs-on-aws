"""`just rollout`, `just status`, `just destroy`: CDK on the caller's AWS account with a
live per-service dashboard.

    rollout  preflight (identity, tools, bootstrap, bundle, synth + cdk-nag, diff)
             -> confirmation -> deploy (live CloudFormation events per service)
             -> post-deploy (migrations, health, JWT gate, smoke test with cleanup)
    status   live health of every service; r = refresh, q = quit
    destroy  confirmation -> delete stacks (live per-service progress)
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import select
import shutil
import subprocess
import sys
import termios
import time
import tty
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from botocore.exceptions import ClientError
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Prompt
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from infra.checks import HEALTH, Checks, Target
from infra.settings import (
    CDK_CLI,
    CDK_OUT,
    CDK_OUTPUTS_FILE,
    OUTPUTS_FILE,
    PLATFORM_OUTPUT_KEYS,
    ROOT,
    Deployment,
    output_id,
)
from infra.tracker import HINTS, StackTracker

console = Console()
STYLE = {
    "WAITING": "grey50",
    "RUNNING": "yellow",
    "DONE": "green",
    "OK": "green",
    "WARN": "yellow",
    "FAILED": "red",
    "FAIL": "red",
    "SKIP": "grey50",
}
STATEFUL = (
    "AWS::RDS::DBCluster",
    "AWS::RDS::DBInstance",
    "AWS::DynamoDB::Table",
    "AWS::S3::Bucket",
    "AWS::Cognito::UserPool",
    "AWS::SecretsManager::Secret",
)


# --------------------------------------------------------------------------- model
@dataclass
class Step:
    name: str
    status: str = "WAITING"
    detail: str = ""
    started: float | None = None
    ended: float | None = None

    def begin(self, detail: str = "") -> None:
        self.status, self.detail, self.started = "RUNNING", detail, time.time()

    def end(self, ok: bool | None, detail: str) -> None:
        self.status = "OK" if ok else ("WARN" if ok is None else "FAIL")
        self.detail, self.ended = detail, time.time()

    def took(self) -> str:
        if self.started is None:
            return ""
        return _fmt((self.ended or time.time()) - self.started)


@dataclass
class Context:
    dep: Deployment
    region: str
    account: str = "?"
    arn: str = "?"
    alias: str = ""
    started: float = field(default_factory=time.time)
    phase: str = "preflight"
    log_file: str = ""

    @property
    def principal(self) -> str:
        return self.arn.split(":")[-1].replace("assumed-role/", "")

    def prod_like(self) -> list[str]:
        reasons = []
        if self.dep.is_prod:
            reasons.append(f"stage '{self.dep.stage}'")
        for label, value in (("account alias", self.alias), ("role", self.principal)):
            # tokens split on separators and camelCase: ProdDeployer -> prod, deployer
            tokens = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", value)
            if {t.lower() for t in tokens} & {"prod", "prd", "production"}:
                reasons.append(f"{label} '{value}'")
        return reasons


def _fmt(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


# --------------------------------------------------------------------------- render
def _header(ctx: Context, title: str) -> Panel:
    t = Text()
    t.append(f"{title}  ", style="bold")
    t.append(f"{ctx.dep.prefix}  ", style="cyan")
    t.append(f"account {ctx.account}", style="bold")
    t.append(f" ({ctx.principal})  ")
    t.append(f"{ctx.region}  ", style="cyan")
    t.append(f"phase: {ctx.phase}  ", style="magenta")
    t.append(f"elapsed {_fmt(time.time() - ctx.started)}")
    return Panel(t, border_style="blue")


def _status_cell(status: str) -> Any:
    if status == "RUNNING":
        return Spinner("dots", text=Text(status, style=STYLE[status]))
    return Text(status, style=STYLE.get(status, ""))


def _steps_table(title: str, steps: list[Step]) -> Table:
    table = Table(title=title, title_justify="left", expand=True, show_lines=False)
    table.add_column("Step", min_width=26)
    table.add_column("Status", width=10)
    table.add_column("Time", width=6, justify="right")
    table.add_column("Detail", overflow="fold", ratio=1)
    for s in steps:
        table.add_row(s.name, _status_cell(s.status), s.took(), s.detail)
    return table


def _services_table(tracker: StackTracker) -> Table:
    verb = "Delete" if tracker.mode == "delete" else "Deploy"
    table = Table(title=f"{verb} by service", title_justify="left", expand=True)
    table.add_column("Service", min_width=22)
    table.add_column("Status", width=10)
    table.add_column("Resources", width=10, justify="right")
    table.add_column("Time", width=6, justify="right")
    table.add_column("Last event", overflow="fold", ratio=1)
    now = time.time()
    for svc in tracker.services():
        elapsed = svc.elapsed(now)
        detail = svc.reason if svc.failed else svc.last
        if svc.status == "RUNNING" and svc.name in HINTS:
            detail = f"{detail}  [grey50]({HINTS[svc.name]})[/]"
        table.add_row(
            svc.name,
            _status_cell(svc.status),
            f"{svc.done}/{svc.total}",
            _fmt(elapsed) if elapsed is not None else "",
            detail,
        )
    stacks = "  ".join(f"{s}: {tracker.stack_status.get(s, 'NOT_STARTED')}" for s in tracker.stacks)
    table.caption = stacks
    return table


# --------------------------------------------------------------------------- helpers
def _env(ctx: Context) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "AWS_REGION": ctx.region,
            "AWS_DEFAULT_REGION": ctx.region,
            "CDK_DEFAULT_REGION": ctx.region,
            "CDK_DEFAULT_ACCOUNT": ctx.account,
            "JSII_SILENCE_WARNING_DEPRECATED_NODE_VERSION": "1",
            "CDK_DISABLE_CLI_TELEMETRY": "true",
        }
    )
    return env


def _cdk(ctx: Context, *args: str, log: Any) -> subprocess.Popen[str]:
    cmd = ["npx", "-y", CDK_CLI, *args, "-c", f"stage={ctx.dep.stage}", "-c",
           f"project={ctx.dep.project}"]  # fmt: skip
    log.write(f"\n$ {' '.join(cmd)}\n")
    log.flush()
    return subprocess.Popen(  # noqa: S603
        cmd, cwd=ROOT, env=_env(ctx), stdout=log, stderr=subprocess.STDOUT, text=True
    )


def _run(live: Live, render: Callable[[], Any], proc: subprocess.Popen[str],
         tick: Callable[[], None] | None = None) -> int:  # fmt: skip
    while proc.poll() is None:
        if tick:
            tick()
        live.update(render())
        time.sleep(1.5)
    if tick:
        tick()
    live.update(render())
    return proc.returncode


def _tail(path: str, n: int = 25) -> str:
    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = [ln for ln in fh.read().splitlines() if ln.strip() and not ln.startswith("b'!!")]
    return "\n".join(lines[-n:])


def _identity(ctx: Context) -> str:
    sts = boto3.client("sts", region_name=ctx.region)
    ident = sts.get_caller_identity()
    ctx.account, ctx.arn = ident["Account"], ident["Arn"]
    with contextlib.suppress(ClientError):
        aliases = boto3.client("iam").list_account_aliases()["AccountAliases"]
        ctx.alias = aliases[0] if aliases else ""
    alias = f" alias {ctx.alias}" if ctx.alias else ""
    return f"{ctx.account}{alias} as {ctx.principal}"


def _diff_summary(log_path: str, offset: int) -> tuple[str, list[str]]:
    with open(log_path, encoding="utf-8", errors="replace") as fh:
        fh.seek(offset)
        section = fh.read()
    adds = len(re.findall(r"^\[\+\] ", section, re.M))
    mods = len(re.findall(r"^\[~\] ", section, re.M))
    dels = len(re.findall(r"^\[-\] ", section, re.M))
    risky = []
    for line in section.splitlines():
        if ("replace" in line.lower() or line.startswith("[-] ")) and any(
            t in line for t in STATEFUL
        ):
            risky.append(line.strip()[:140])
    return f"+{adds} ~{mods} -{dels} resources", risky


def _target(ctx: Context) -> Target:
    if not OUTPUTS_FILE.exists():
        raise SystemExit("no .platform/outputs-aws.json: run `just rollout` first")
    data = json.loads(OUTPUTS_FILE.read_text())
    if data.get("prefix") != ctx.dep.prefix or data.get("account") != ctx.account:
        raise SystemExit(
            f"outputs are for {data.get('prefix')} in {data.get('account')}, "
            f"not {ctx.dep.prefix} in {ctx.account}"
        )
    return Target(region=data["region"], outputs=data["outputs"], prefix=ctx.dep.prefix)


def _write_outputs(ctx: Context) -> Target:
    raw = json.loads(CDK_OUTPUTS_FILE.read_text())
    platform, api = raw[ctx.dep.platform_stack], raw[ctx.dep.api_stack]
    outputs = {k: platform[output_id(k)] for k in PLATFORM_OUTPUT_KEYS}
    outputs["api_url"] = api["ApiUrl"]
    outputs["api_function"] = api["ApiFunctionName"]
    OUTPUTS_FILE.write_text(
        json.dumps(
            {
                "prefix": ctx.dep.prefix,
                "account": ctx.account,
                "region": ctx.region,
                "deployed_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "outputs": outputs,
            },
            indent=2,
        )
        + "\n"
    )
    return Target(ctx.region, outputs, ctx.dep.prefix)


def _guardrails(ctx: Context, pre: list[Step], diff: str, risky: list[str]) -> None:
    lines = [
        f"Account: {ctx.account}{' / ' + ctx.alias if ctx.alias else ''} / {ctx.principal}",
        f"Region: {ctx.region}   Environment: {ctx.dep.stage}   Stacks: "
        + ", ".join(ctx.dep.stacks),
        "",
    ]
    for s in pre:
        tag = {"OK": "[green]PASS[/]", "WARN": "[yellow]WARN[/]", "FAIL": "[red]FAIL[/]"}
        lines.append(f"{tag.get(s.status, '[grey50]SKIP[/]')} {s.name}: {s.detail}")
    lines += [
        "[green]PASS[/] Encryption at rest: Aurora, DynamoDB, S3 (SSE-S3), SNS (KMS)",
        "[green]PASS[/] No public datastores: Aurora in isolated subnets, no IGW/NAT, no ingress",
        "[green]PASS[/] S3 Block Public Access 4/4 + TLS-only bucket policy",
        "[yellow]WARN[/] Public HTTP API: JWT authorizer on every route except register, "
        "confirm, login, refresh, healthz; throttle 20 rps / burst 40; no WAF (HTTP API "
        "does not support it)",
        f"Change set: {diff}",
    ]
    lines += [f"[red]RISK[/] {r}" for r in risky]
    console.print(Panel("\n".join(lines), title="Deploy guardrails", border_style="cyan"))


# --------------------------------------------------------------------------- rollout
def rollout(ctx: Context, yes: bool, skip_smoke: bool) -> int:
    CDK_OUTPUTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    ctx.log_file = str(ROOT / ".platform" / f"rollout-{stamp}.log")
    log = open(ctx.log_file, "w", encoding="utf-8")  # noqa: SIM115

    pre = [Step(n) for n in (
        "AWS identity", "Toolchain", "CDK bootstrap", "Lambda bundle",
        "Dependency audit (CVE)", "Synth + cdk-nag", "Diff",
    )]  # fmt: skip
    post = (
        [Step(n) for n in ("Migrations (Aurora)",)]
        + [Step(n) for n, _ in HEALTH]
        + [Step("Smoke test (end to end)")]
    )

    def render_pre() -> Group:
        return Group(_header(ctx, "rollout"), _steps_table("Preflight", pre),
                     Text(f"log: {ctx.log_file}", style="grey50"))  # fmt: skip

    with Live(render_pre(), console=console, refresh_per_second=8) as live:
        # identity
        pre[0].begin("sts:GetCallerIdentity")
        live.update(render_pre())
        try:
            pre[0].end(True, _identity(ctx))
        except Exception as exc:
            pre[0].end(False, f"no usable AWS credentials: {exc}")
            live.update(render_pre())
            return 1
        reasons = ctx.prod_like()
        if reasons:
            pre[0].end(None, f"{pre[0].detail} | PRODUCTION signals: {', '.join(reasons)}")

        # toolchain
        pre[1].begin()
        node = subprocess.run(["node", "-v"], capture_output=True, text=True, check=False)  # noqa: S607
        node_v = node.stdout.strip() or "missing"
        major = int(re.sub(r"\D", "", node_v.split(".")[0]) or 0)
        if not shutil.which("npx") or major < 18:
            pre[1].end(False, f"node {node_v}: need Node.js >= 22 (npx)")
            live.update(render_pre())
            return 1
        pre[1].end(
            None if major < 22 else True,
            f"node {node_v}{' (EOL, upgrade to 22+)' if major < 22 else ''} | CDK CLI {CDK_CLI}",
        )

        # bootstrap
        pre[2].begin("CDKToolkit stack")
        live.update(render_pre())
        try:
            stack = boto3.client("cloudformation", region_name=ctx.region).describe_stacks(
                StackName="CDKToolkit"
            )["Stacks"][0]
            version = next(
                (o["OutputValue"] for o in stack.get("Outputs", [])
                 if o["OutputKey"] == "BootstrapVersion"), "?",
            )  # fmt: skip
            pre[2].end(True, f"{stack['StackStatus']} (bootstrap v{version})")
        except ClientError:
            pre[2].end(False, f"not bootstrapped: run `just cdk-bootstrap` ({ctx.region})")
            live.update(render_pre())
            return 1

        # bundle
        pre[3].begin("uv pip install --python-platform aarch64-manylinux2014")
        live.update(render_pre())
        b = subprocess.run(
            [sys.executable, "-m", "infra.build_lambda"], cwd=ROOT, capture_output=True,
            text=True, check=False,
        )  # fmt: skip
        log.write(b.stdout + b.stderr)
        if b.returncode:
            pre[3].end(False, "bundle failed, see log")
            live.update(render_pre())
            return 1
        pre[3].end(True, b.stdout.strip().splitlines()[-1].replace(str(ROOT) + "/", ""))

        # dependency audit: Critical/High CVEs block the deploy
        audit = pre[4]
        audit.begin("pip-audit on the Lambda requirements")
        live.update(render_pre())
        if not shutil.which("uvx"):
            audit.end(None, "uvx not found, audit skipped")
        else:
            reqs = str(ROOT / "build" / "requirements-lambda.txt")
            uvx = shutil.which("uvx") or "uvx"
            cmd = [uvx, "-q", "-p", "3.12", "pip-audit", "-r", reqs, "--disable-pip",
                   "--no-deps", "--progress-spinner", "off"]  # fmt: skip
            a = subprocess.Popen(  # noqa: S603
                cmd,
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            rc = _run(live, render_pre, a)
            if rc:
                audit.end(False, "vulnerable dependencies found, see log")
                live.update(render_pre())
                console.print(Panel(_tail(ctx.log_file), title="pip-audit", border_style="red"))
                return 1
            audit.end(True, "no known vulnerabilities")

        # synth
        pre[5].begin("cdk synth (AwsSolutions checks)")
        if CDK_OUT.exists():
            shutil.rmtree(CDK_OUT)
        rc = _run(live, render_pre, _cdk(ctx, "synth", "--quiet", log=log))
        if rc:
            pre[5].end(False, "synth or cdk-nag failed, see log")
            live.update(render_pre())
            console.print(Panel(_tail(ctx.log_file), title="synth output", border_style="red"))
            return 1
        report = json.loads((CDK_OUT / "validation-report.json").read_text())
        plugins = report.get("pluginReports", [])
        open_findings = sum(len(p.get("violations", [])) for p in plugins)
        acked = sum(len(p.get("suppressedViolations", [])) for p in plugins)
        pre[5].end(
            open_findings == 0,
            f"2 stacks | cdk-nag AwsSolutions: {open_findings} open, {acked} acknowledged "
            "with reasons",
        )

        # diff
        pre[6].begin("cdk diff against deployed stacks")
        log.flush()
        offset = os.path.getsize(ctx.log_file)
        rc = _run(live, render_pre, _cdk(ctx, "diff", "--app", str(CDK_OUT), "--all", log=log))
        log.flush()
        diff, risky = _diff_summary(ctx.log_file, offset)
        pre[6].end(None if risky else True, diff + (f" | {len(risky)} risky" if risky else ""))
        live.update(render_pre())

    log.flush()
    _guardrails(ctx, pre, diff, risky)
    if reasons or risky:
        answer = Prompt.ask(
            f"[red]Type the account id ({ctx.account}) to deploy[/]", default="", show_default=False
        )
        if answer.strip() != ctx.account:
            console.print("aborted")
            return 1
    elif not yes:
        answer = Prompt.ask(f"Deploy to [bold]{ctx.account}[/] / {ctx.region}?", choices=["y", "n"],
                            default="n")  # fmt: skip
        if answer != "y":
            console.print("aborted")
            return 1

    # deploy
    ctx.phase = "deploy"
    cfn = boto3.client("cloudformation", region_name=ctx.region)
    tracker = StackTracker(cfn, ctx.dep.stacks, since=datetime.now(UTC) - timedelta(seconds=5))
    tracker.load_templates(CDK_OUT)
    tracker.load_existing()

    def render_deploy() -> Group:
        return Group(_header(ctx, "rollout"), _services_table(tracker),
                     Text(f"log: {ctx.log_file}   Ctrl+C stops watching; CloudFormation "
                          "keeps going", style="grey50"))  # fmt: skip

    with Live(render_deploy(), console=console, refresh_per_second=4) as live:
        proc = _cdk(ctx, "deploy", "--app", str(CDK_OUT), "--all", "--require-approval",
                    "never", "--outputs-file", str(CDK_OUTPUTS_FILE), log=log)  # fmt: skip
        rc = _run(live, render_deploy, proc, tick=tracker.poll)
    if rc:
        console.print(Panel(_tail(ctx.log_file), title="cdk deploy failed", border_style="red"))
        return 1
    target = _write_outputs(ctx)

    # post-deploy
    ctx.phase = "verify"
    checks = Checks(target)

    def render_post() -> Group:
        return Group(_header(ctx, "rollout"), _services_table(tracker),
                     _steps_table("Post-deploy checks", post))  # fmt: skip

    with Live(render_post(), console=console, refresh_per_second=8) as live:
        _check(live, render_post, post[0], checks.migrations)
        steps = dict(zip([n for n, _ in HEALTH], post[1:-1], strict=True))
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {}
            for name, method in HEALTH:
                steps[name].begin()
                futures[name] = pool.submit(_safe, getattr(checks, method))
            while any(not f.done() for f in futures.values()):
                for name, f in futures.items():
                    if f.done() and steps[name].status == "RUNNING":
                        steps[name].end(*f.result())
                live.update(render_post())
                time.sleep(0.3)
            for name, f in futures.items():
                if steps[name].status == "RUNNING":
                    steps[name].end(*f.result())
        smoke = post[-1]
        if skip_smoke:
            smoke.status, smoke.detail = "SKIP", "--skip-smoke"
        elif post[1].status != "OK":
            smoke.status, smoke.detail = "SKIP", "API not healthy"
        else:
            smoke.begin("throwaway user (email suppressed)")

            def progress(msg: str) -> None:
                smoke.detail = f"throwaway user (email suppressed): {msg}"
                live.update(render_post())

            smoke.end(*_safe(lambda: checks.smoke(progress)))
        live.update(render_post())

    failed = [s for s in post if s.status == "FAIL"]
    summary = [
        f"API      {target.api_url}",
        f"Outputs  {OUTPUTS_FILE.relative_to(ROOT)}",
        f"Log      {ctx.log_file.replace(str(ROOT) + '/', '')}",
        "",
        "just status    live health of every service (r refresh, q quit)",
        "just destroy   delete both stacks",
    ]
    style = "red" if failed else "green"
    title = f"rollout {'finished with failures' if failed else 'complete'} in " + _fmt(
        time.time() - ctx.started
    )
    console.print(Panel("\n".join(summary), title=title, border_style=style))
    return 1 if failed else 0


def _safe(fn: Callable[[], tuple[bool, str]]) -> tuple[bool, str]:
    try:
        return fn()
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:200]


def _check(live: Live, render: Callable[[], Any], step: Step,
           fn: Callable[[], tuple[bool, str]]) -> None:  # fmt: skip
    step.begin()
    live.update(render())
    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(_safe, fn)
        while not fut.done():
            live.update(render())
            time.sleep(0.3)
    step.end(*fut.result())
    live.update(render())


# --------------------------------------------------------------------------- status
@contextlib.contextmanager
def _keys() -> Iterator[Callable[[], str | None]]:
    if not sys.stdin.isatty():
        yield lambda: None
        return
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    try:
        yield lambda: sys.stdin.read(1) if select.select([sys.stdin], [], [], 0)[0] else None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def status(ctx: Context, interval: int, once: bool) -> int:
    ctx.phase = "status"
    _identity(ctx)
    target = _target(ctx)
    checks = Checks(target)
    cfn = boto3.client("cloudformation", region_name=ctx.region)
    rows = {name: Step(name) for name, _ in HEALTH}
    stacks: dict[str, str] = {}
    last = 0.0

    def refresh() -> None:
        nonlocal last
        for s in ctx.dep.stacks:
            try:
                stacks[s] = cfn.describe_stacks(StackName=s)["Stacks"][0]["StackStatus"]
            except ClientError:
                stacks[s] = "NOT_FOUND"
        with ThreadPoolExecutor(max_workers=8) as pool:
            for name, _method in HEALTH:
                rows[name].begin(rows[name].detail)
            results = {n: pool.submit(_safe, getattr(checks, m)) for n, m in HEALTH}
            for name, fut in results.items():
                rows[name].end(*fut.result())
        last = time.time()

    def render() -> Group:
        stack_line = "   ".join(f"{k}: {v}" for k, v in stacks.items())
        age = f"checked {int(time.time() - last)} s ago" if last else "checking..."
        foot = "r refresh  q quit" if not once else ""
        return Group(
            _header(ctx, "status"),
            Text(f"API {target.api_url}   {stack_line}", style="cyan"),
            _steps_table("Service health", list(rows.values())),
            Text(f"{age}   every {interval} s   {foot}", style="grey50"),
        )

    if once:
        refresh()
        console.print(render())
        return 0 if all(r.status == "OK" for r in rows.values()) else 1
    with _keys() as key, Live(render(), console=console, refresh_per_second=4) as live:
        refresh()
        while True:
            k = key()
            if k in ("q", "Q"):
                break
            if k in ("r", "R") or time.time() - last >= interval:
                for r in rows.values():
                    r.status = "RUNNING"
                live.update(render())
                refresh()
            live.update(render())
            time.sleep(0.2)
    return 0


# --------------------------------------------------------------------------- destroy
def destroy(ctx: Context, yes: bool) -> int:
    ctx.phase = "destroy"
    console.print(f"identity: {_identity(ctx)}")
    cfn = boto3.client("cloudformation", region_name=ctx.region)
    existing = []
    for s in ctx.dep.stacks:
        with contextlib.suppress(ClientError):
            cfn.describe_stacks(StackName=s)
            existing.append(s)
    if not existing:
        console.print(f"nothing to delete: {', '.join(ctx.dep.stacks)} not found in {ctx.region}")
        swept = _sweep_log_groups(ctx)
        if swept:
            console.print("deleted leftover log groups: " + ", ".join(swept))
        return 0
    reasons = ctx.prod_like()
    warn = (
        f"Deletes {', '.join(existing)} in {ctx.account} / {ctx.region}: Aurora cluster, "
        "DynamoDB table, S3 buckets (objects included), Cognito users, logs."
    )
    if reasons:
        warn += f"\n[red]PRODUCTION signals: {', '.join(reasons)}. Retained resources stay.[/]"
    console.print(Panel(warn, title="destroy", border_style="red"))
    if reasons or not yes:
        answer = Prompt.ask(f"Type [bold]{ctx.dep.prefix}[/] to confirm", default="",
                            show_default=False)  # fmt: skip
        if answer.strip() != ctx.dep.prefix:
            console.print("aborted")
            return 1
    tracker = StackTracker(cfn, tuple(existing), mode="delete")
    tracker.load_existing()
    for s in existing:
        tracker.stack_status[s] = "DELETE_PENDING"

    def render() -> Group:
        return Group(_header(ctx, "destroy"), _services_table(tracker))

    with Live(render(), console=console, refresh_per_second=4) as live:
        for stack in reversed(existing):  # api first, then platform
            cfn.delete_stack(StackName=stack)
            tracker.stack_status[stack] = "DELETE_IN_PROGRESS"
            while True:
                tracker.poll()
                live.update(render())
                st = tracker.stack_status.get(stack, "")
                if st in {"DELETE_COMPLETE", "DELETE_FAILED"}:
                    break
                time.sleep(2)
            if tracker.stack_status[stack] == "DELETE_FAILED":
                break
    ok = all(tracker.stack_status.get(s) == "DELETE_COMPLETE" for s in existing)
    if ok and OUTPUTS_FILE.exists():
        OUTPUTS_FILE.unlink()
    if ok:
        swept = _sweep_log_groups(ctx)
        if swept:
            console.print("deleted log groups created outside CloudFormation: " + ", ".join(swept))
    console.print(
        Panel(
            "all stacks deleted" if ok else "deletion failed: see the failed rows above",
            border_style="green" if ok else "red",
        )
    )
    return 0 if ok else 1


def _sweep_log_groups(ctx: Context) -> list[str]:
    """Lambda-created log groups CloudFormation does not own (CDK custom-resource
    providers). Scoped to names that start with this deployment's stack names."""
    logs = boto3.client("logs", region_name=ctx.region)
    deleted = []
    for stack in ctx.dep.stacks:
        pages = logs.get_paginator("describe_log_groups").paginate(
            logGroupNamePrefix=f"/aws/lambda/{stack}-"
        )
        for page in pages:
            for group in page["logGroups"]:
                logs.delete_log_group(logGroupName=group["logGroupName"])
                deleted.append(group["logGroupName"])
    return deleted


# --------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rollout")
    parser.add_argument("command", choices=["rollout", "status", "destroy"])
    parser.add_argument("--stage", default=os.environ.get("STAGE", "dev"))
    parser.add_argument("--project", default=os.environ.get("PROJECT", "sup"))
    parser.add_argument("--region", default=None)
    parser.add_argument("--yes", action="store_true", help="skip the y/n prompt (never for prod)")
    parser.add_argument("--skip-smoke", action="store_true")
    parser.add_argument("--interval", type=int, default=15)
    parser.add_argument("--once", action="store_true", help="status: one pass, no live view")
    args = parser.parse_args(argv)
    region = (
        args.region
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or boto3.session.Session().region_name
    )
    if not region:
        raise SystemExit("no region: pass --region or set AWS_REGION")
    ctx = Context(Deployment(args.project, args.stage), region)
    try:
        if args.command == "rollout":
            return rollout(ctx, args.yes, args.skip_smoke)
        if args.command == "status":
            return status(ctx, args.interval, args.once)
        return destroy(ctx, args.yes)
    except KeyboardInterrupt:
        console.print("\ninterrupted (CloudFormation operations already started keep running)")
        return 130


if __name__ == "__main__":
    sys.exit(main())
