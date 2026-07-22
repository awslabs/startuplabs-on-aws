"""Provision an Amazon Bedrock AgentCore Memory resource with long-term strategies.

Creates a memory store that gives the RAG agent persistent, cross-session memory:
- summaryMemoryStrategy: summarizes each conversation session
- userPreferenceMemoryStrategy: learns and stores user preferences over time
- semanticMemoryStrategy: extracts durable facts about the user

This is a one-time setup. It prints the resulting memory ID, which should be
saved to .env as AGENTCORE_MEMORY_ID for the agent to use.

Usage:
    python infrastructure/setup_memory.py
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv()

REGION = os.getenv("AWS_REGION", "us-east-1")
MEMORY_NAME = "NovaTechAgentMemory"


def main():
    from bedrock_agentcore.memory import MemoryClient

    print("\n" + "=" * 60)
    print("  AGENTCORE MEMORY SETUP (Long-Term)")
    print("=" * 60 + "\n")
    print(f"Region: {REGION}")

    client = MemoryClient(region_name=REGION)

    # If a memory with this name already exists, reuse it.
    try:
        existing = client.list_memories()
        for mem in existing:
            if mem.get("name") == MEMORY_NAME or mem.get("id", "").startswith(MEMORY_NAME):
                mem_id = mem.get("id")
                print(f"  ✓ Reusing existing memory: {mem_id}")
                _print_result(mem_id)
                return
    except Exception:
        # list_memories signature/behavior can vary; fall through to create.
        pass

    print(f"Creating memory '{MEMORY_NAME}' with long-term strategies...")
    print("  (this waits for the resource to become ACTIVE — ~1-2 min)")

    memory = client.create_memory_and_wait(
        name=MEMORY_NAME,
        description="Persistent cross-session memory for the NovaTech RAG agent",
        strategies=[
            {
                "summaryMemoryStrategy": {
                    "name": "SessionSummarizer",
                    "namespaces": ["/summaries/{actorId}/{sessionId}/"],
                }
            },
            {
                "userPreferenceMemoryStrategy": {
                    "name": "PreferenceLearner",
                    "namespaces": ["/preferences/{actorId}/"],
                }
            },
            {
                "semanticMemoryStrategy": {
                    "name": "FactExtractor",
                    "namespaces": ["/facts/{actorId}/"],
                }
            },
        ],
    )

    mem_id = memory.get("id")
    print(f"\n  ✓ Memory created: {mem_id}")
    _print_result(mem_id)


def _print_result(mem_id: str):
    print("\n" + "=" * 60)
    print("  DONE!")
    print("=" * 60)
    print(f"\n  Add this to your .env file:")
    print(f"    AGENTCORE_MEMORY_ID={mem_id}")
    print("\n  Namespaces configured:")
    print("    /summaries/{actorId}/{sessionId}/  - per-session summaries")
    print("    /preferences/{actorId}/            - learned user preferences")
    print("    /facts/{actorId}/                  - extracted user facts")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n  ✗ ERROR: {e}")
        sys.exit(1)
