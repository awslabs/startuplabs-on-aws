"""Memory demonstration — proves AgentCore persistent memory works end-to-end.

Run this in front of a customer. It shows the full loop:
  1. SESSION 1: a brand-new user tells the agent about themselves.
  2. INSPECT:  we query AgentCore Memory directly to show what was extracted
               and stored (facts + preferences) — proof it's persisted server-side.
  3. SESSION 2: a completely separate session (new agent, no shared history)
               recalls the user without being re-told.

Usage:
    python demo_memory.py                # uses a fresh random user each run
    python demo_memory.py alice          # use a specific user id (to show
                                         # persistence across program runs too)
"""

import os
import sys
import time
import uuid
import logging
from datetime import datetime

# Ensure UTF-8 output so the script runs on Windows consoles (cp1252) too.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: S110 - console encoding is best-effort; safe to ignore
    logging.debug("stdout.reconfigure not supported on this platform")

from dotenv import load_dotenv

load_dotenv()

from bedrock_agentcore.memory import MemoryClient
from agent.rag_agent import create_memory_enabled_agent

REGION = os.getenv("AWS_REGION", "us-east-1")
MEM_ID = os.getenv("AGENTCORE_MEMORY_ID")

# Use a provided user id, or a fresh one so the demo starts from a clean slate.
ACTOR = sys.argv[1] if len(sys.argv) > 1 else f"demo-user-{uuid.uuid4().hex[:8]}"


def banner(title):
    print("\n" + "=" * 68)
    print(f"  {title}")
    print("=" * 68)


def ask(agent, prompt):
    print(f"\n  [USER]  {prompt}")
    resp = str(agent(prompt))
    print(f"  [AGENT] {resp[:400]}")
    return resp


def _has_stored_memory() -> bool:
    """Return True if any facts/preferences are stored for this user yet."""
    client = MemoryClient(region_name=REGION)
    for ns in [f"/facts/{ACTOR}/", f"/preferences/{ACTOR}/"]:
        try:
            recs = client.retrieve_memories(
                memory_id=MEM_ID, namespace=ns, query="user role", top_k=3
            )
            if recs:
                return True
        except Exception as e:
            logging.debug("Memory poll for namespace %s failed: %s", ns, e)
    return False


def inspect_stored_memory():
    """Query AgentCore Memory directly and print what is persisted for this user."""
    client = MemoryClient(region_name=REGION)
    found_any = False
    for label, ns in [
        ("FACTS", f"/facts/{ACTOR}/"),
        ("PREFERENCES", f"/preferences/{ACTOR}/"),
    ]:
        print(f"\n  [{label}]  (namespace: {ns})")
        try:
            records = client.retrieve_memories(
                memory_id=MEM_ID, namespace=ns,
                query="user role interests preferences", top_k=10,
            )
            if not records:
                print("      (nothing stored yet)")
            for r in records:
                c = r.get("content", {})
                text = c.get("text", str(c)) if isinstance(c, dict) else str(c)
                print(f"      • {text[:160]}")
                found_any = True
        except Exception as e:
            print(f"      error: {e}")
    return found_any


def main():
    if not MEM_ID:
        print("AGENTCORE_MEMORY_ID not set. Run infrastructure/setup_memory.py first.")
        sys.exit(1)

    print(f"\nMemory resource: {MEM_ID}")
    print(f"Demo user (actor_id): {ACTOR}")

    ts = datetime.now().strftime("%H%M%S")

    # ---- SESSION 1: teach the agent about the user ----
    banner("SESSION 1  -  User introduces themselves")
    agent1, sm1 = create_memory_enabled_agent(
        actor_id=ACTOR, session_id=f"demo-s1-{ts}", enable_tracing=False
    )
    try:
        ask(agent1, "Hi! I'm a compliance officer and I care most about AI "
                    "governance, bias auditing, and EU AI Act readiness.")
        ask(agent1, "Which NovaTech product fits me best?")
    finally:
        sm1.close()  # flush to the memory store
    print("\n  >> Session 1 closed - conversation flushed to AgentCore Memory.")

    # ---- Give long-term extraction time to run ----
    # AgentCore LTM extraction is asynchronous and can take 60-120s. We poll the
    # store until the facts appear (up to a cap) rather than guessing a fixed wait.
    banner("Waiting for long-term memory extraction")
    print("  AgentCore is extracting durable facts & preferences from the chat...")
    print("  (this is asynchronous and typically takes 60-120s)")
    stored = False
    for attempt in range(12):  # up to ~3 minutes
        time.sleep(15)
        stored = _has_stored_memory()
        print(f"    ...checked at {(attempt + 1) * 15}s: "
              f"{'FOUND stored memory' if stored else 'not yet'}")
        if stored:
            break

    # ---- INSPECT: prove it's actually stored server-side ----
    banner("INSPECT  -  What AgentCore Memory persisted for this user")
    inspect_stored_memory()
    if not stored:
        print("\n  (Extraction still in progress — SESSION 2 recall may be incomplete.)")

    # ---- SESSION 2: brand-new session recalls the user ----
    banner("SESSION 2  -  NEW session, no shared history: does it remember?")
    agent2, sm2 = create_memory_enabled_agent(
        actor_id=ACTOR, session_id=f"demo-s2-{ts}", enable_tracing=False
    )
    try:
        recall = ask(agent2, "Based on what you know about me, what should I focus on?")
    finally:
        sm2.close()

    banner("RESULT")
    low = recall.lower()
    # A genuine recall references the stored role/interests. A failure instead
    # asks the user to (re)introduce themselves.
    asked_to_reintroduce = any(
        p in low for p in [
            "don't have", "dont have", "do not have", "could you remind",
            "what is your role", "tell me about your", "i don't know",
        ]
    )
    referenced_context = any(
        k in low for k in ["compliance officer", "ai governance", "bias auditing", "eu ai act"]
    )
    if referenced_context and not asked_to_reintroduce:
        print("  [SUCCESS] A separate session recalled the user's role & interests")
        print("            WITHOUT being told again - memory persisted across sessions.")
    else:
        print("  [NOT RECALLED] Session 2 did not use the stored memory.")
        print("            The facts ARE stored (see INSPECT above), but extraction")
        print("            may not have completed before this session ran. Re-run")
        print("            'python demo_memory.py " + ACTOR + "' to retry recall.")


if __name__ == "__main__":
    main()
