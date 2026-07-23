"""Fire-and-forget invocation of the deployed AgentCore runtime.

Used by the UI (Option 1 / hybrid mode): the UI answers and evaluates using a
local agent (so it can capture retrieved sources), and *also* sends the same
question to the deployed AgentCore runtime in a background thread. That remote
invocation is what produces the CloudWatch traces / observability story — every
UI question shows up as a real runtime invocation.

The remote call is best-effort: failures are logged but never surface to the UI.
"""

import os
import json
import uuid
import threading
import logging

import boto3

logger = logging.getLogger(__name__)

# The deployed runtime ARN. Set AGENTCORE_RUNTIME_ARN in your environment
# (from `agentcore status` after deploy). Not hardcoded — this is a public repo.
def _runtime_arn() -> str:
    arn = os.getenv("AGENTCORE_RUNTIME_ARN", "")
    if not arn:
        raise RuntimeError(
            "AGENTCORE_RUNTIME_ARN is not set. After deploying the agent, run "
            "`agentcore status` and set AGENTCORE_RUNTIME_ARN in your .env."
        )
    return arn


def _runtime_region() -> str:
    # The runtime lives in us-east-1 (same as the KB).
    return os.getenv("AGENTCORE_RUNTIME_REGION", "us-east-1")


# Track in-flight background threads so they are not garbage-collected and can
# finish draining the runtime stream (a fully-drained stream = a complete trace).
_inflight_threads: list = []

# The session id of the most recent replay, for building the CloudWatch link.
_last_session_id: str | None = None


def get_last_session_id() -> str | None:
    """Return the session id of the most recent runtime replay (for CW links)."""
    return _last_session_id


def _invoke_remote(question: str, session_id: str) -> None:
    """Blocking call to the deployed runtime. Runs inside a background thread.

    Fully drains the response stream — this is required for the invocation to
    complete server-side and emit a complete CloudWatch trace. If the stream is
    only partially read, the trace can be truncated or missing.
    """
    try:
        client = boto3.client("bedrock-agentcore", region_name=_runtime_region())
        response = client.invoke_agent_runtime(
            agentRuntimeArn=_runtime_arn(),
            runtimeSessionId=session_id,
            contentType="application/json",
            accept="application/json",
            payload=json.dumps({"prompt": question}).encode("utf-8"),
        )
        # Drain the ENTIRE stream in chunks so the invocation runs to completion.
        body = response.get("response")
        if body is not None:
            try:
                for _chunk in body.iter_chunks():
                    continue  # draining the stream to completion
            except AttributeError:
                # Fallback for stream objects without iter_chunks()
                try:
                    body.read()
                except Exception as read_err:  # noqa: BLE001
                    logger.debug("Stream read while draining failed: %s", read_err)
            except Exception as drain_err:  # noqa: BLE001
                logger.debug("Stream drain failed: %s", drain_err)
        logger.info("Remote AgentCore invocation completed (session=%s)", session_id)
    except Exception as e:  # noqa: BLE001
        # Best-effort only — never break the UI over a logging call.
        logger.warning("Remote AgentCore invocation failed: %s", e)


def log_invocation_async(question: str, session_id: str | None = None) -> str:
    """Fire the deployed-runtime invocation in a background thread (non-blocking).

    Uses a NON-daemon thread so it is allowed to finish draining the runtime
    stream even if the UI moves on — daemon threads can be killed mid-stream,
    which produces truncated/missing CloudWatch traces.

    Args:
        question: The user's question to replay against the deployed runtime.
        session_id: Optional runtime session id. A valid one is generated if not
            provided (AgentCore requires 33+ characters).

    Returns:
        The session_id used, so the UI can surface it for the observability story.
    """
    # Each replay gets its OWN unique session id. Reusing one session across many
    # invocations can cause concurrent-session degradation and makes individual
    # traces harder to find; a unique id per question yields one clean, fully
    # traced invocation each time. A caller-provided prefix (the UI session) is
    # kept for grouping/readability.
    prefix = (session_id or "ui")[:12].rstrip("-")
    session_id = f"{prefix}-{uuid.uuid4().hex}{uuid.uuid4().hex}"[:48]

    global _last_session_id
    _last_session_id = session_id

    # Reap finished threads to keep the list small.
    global _inflight_threads
    _inflight_threads = [t for t in _inflight_threads if t.is_alive()]

    # Non-daemon so Python won't kill it mid-invocation; it will complete on its own.
    thread = threading.Thread(
        target=_invoke_remote, args=(question, session_id), daemon=False
    )
    thread.start()
    _inflight_threads.append(thread)
    return session_id


# --- CloudWatch deep links ------------------------------------------------

import urllib.parse


def _runtime_id() -> str:
    """Extract the runtime id (last ARN segment) for building log group names."""
    arn = os.getenv("AGENTCORE_RUNTIME_ARN", "")
    return arn.split("/")[-1] if arn else ""


def _log_group_name() -> str:
    """CloudWatch log group for the deployed runtime's DEFAULT endpoint."""
    return f"/aws/bedrock-agentcore/runtimes/{_runtime_id()}-DEFAULT"


def cloudwatch_logs_url(session_id: str | None = None) -> str:
    """Build a CloudWatch Logs console URL for the runtime's log group.

    If a session_id is provided, the URL pre-fills a filter pattern so the
    console opens focused on that UI session's invocations.

    Note: CloudWatch console URLs use a custom double-encoding scheme where
    reserved characters are escaped with '$' sequences (e.g. '/' -> '$252F').
    """
    region = _runtime_region()
    log_group = _log_group_name()

    def _cw_encode(value: str) -> str:
        # CloudWatch console encodes the value twice, then swaps % for $.
        once = urllib.parse.quote(value, safe="")
        twice = urllib.parse.quote(once, safe="")
        return twice.replace("%", "$")

    encoded_group = _cw_encode(log_group)
    base = (
        f"https://{region}.console.aws.amazon.com/cloudwatch/home"
        f"?region={region}#logsV2:log-groups/log-group/{encoded_group}"
    )
    if session_id:
        # Pre-fill a filter for the session id on the log-events view.
        filter_enc = _cw_encode(f'"{session_id}"')
        base += f"/log-events$3FfilterPattern$3D{filter_enc}"
    return base


def genai_observability_url() -> str:
    """URL to the Bedrock AgentCore GenAI Observability dashboard for the runtime."""
    region = _runtime_region()
    return (
        f"https://{region}.console.aws.amazon.com/cloudwatch/home"
        f"?region={region}#gen-ai-observability/agent-core"
    )
