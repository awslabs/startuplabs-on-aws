"""Core RAG Agent definition using Strands Agents SDK."""

import os
from dotenv import load_dotenv
from strands import Agent
from strands.models.bedrock import BedrockModel

from agent.prompts import RAG_SYSTEM_PROMPT
from agent.tools import retrieve_from_kb
from agent.observability import setup_observability

# Load environment variables
load_dotenv()

# System prompt addition when memory is enabled, so the model actually uses
# retrieved long-term memories about the user.
MEMORY_SYSTEM_PROMPT = RAG_SYSTEM_PROMPT + """

## Memory (strict)
You have persistent memory of this user across sessions. Relevant user \
preferences and facts may be provided to you as retrieved memory context.

- Use ONLY the memories that are actually provided. Do NOT invent, guess, or \
embellish details about the user's role, interests, or history.
- If you state something about the user (e.g., their job role), it MUST come \
verbatim from the provided memory. For example, if the memory says the user is a \
"compliance officer", do not describe them as anything else.
- If no memory about a detail is available, do not speculate — either omit it or \
ask the user.
- When you do use a memory, you may briefly note it (e.g., "Based on your role as \
a compliance officer...") so the personalization is transparent.
"""


def create_memory_enabled_agent(
    actor_id: str,
    session_id: str,
    enable_tracing: bool = True,
    callback_handler=None,
):
    """Create a RAG agent backed by AgentCore Memory (persistent, cross-session).

    Returns a tuple of (agent, session_manager). The caller is responsible for
    closing the session_manager (or using it as a context manager) to flush
    pending messages to the memory store.

    Args:
        actor_id: Stable identifier for the user. The SAME actor_id across
            sessions is what enables cross-session memory (preferences/facts).
        session_id: Identifier for this specific conversation session.
        enable_tracing: Whether to enable OTEL tracing.
        callback_handler: Optional streaming callback handler.

    Returns:
        (Agent, AgentCoreMemorySessionManager) tuple.

    Raises:
        RuntimeError: if AGENTCORE_MEMORY_ID is not configured.
    """
    from bedrock_agentcore.memory.integrations.strands.config import (
        AgentCoreMemoryConfig,
        RetrievalConfig,
    )
    from bedrock_agentcore.memory.integrations.strands.session_manager import (
        AgentCoreMemorySessionManager,
    )

    memory_id = os.getenv("AGENTCORE_MEMORY_ID")
    if not memory_id:
        raise RuntimeError(
            "AGENTCORE_MEMORY_ID not set. Run infrastructure/setup_memory.py first "
            "and add the ID to your .env file."
        )

    region = os.getenv("AWS_REGION", "us-east-1")

    if enable_tracing:
        setup_observability(export_to_console=False, export_to_otlp=False)

    model_id = os.getenv("INFERENCE_MODEL_ID", "mistral.mistral-large-3-675b-instruct")
    model = BedrockModel(model_id=model_id, region_name=region)

    # Retrieve learned preferences and facts about this user across sessions,
    # plus summaries of this session.
    memory_config = AgentCoreMemoryConfig(
        memory_id=memory_id,
        session_id=session_id,
        actor_id=actor_id,
        # Thresholds are intentionally low: AgentCore memory relevance scores for
        # short factual records typically fall in the 0.3-0.5 range, so higher
        # cutoffs would filter out legitimately relevant memories.
        retrieval_config={
            "/preferences/{actorId}/": RetrievalConfig(top_k=5, relevance_score=0.2),
            "/facts/{actorId}/": RetrievalConfig(top_k=5, relevance_score=0.2),
            "/summaries/{actorId}/{sessionId}/": RetrievalConfig(
                top_k=3, relevance_score=0.2
            ),
        },
    )

    session_manager = AgentCoreMemorySessionManager(
        agentcore_memory_config=memory_config,
        region_name=region,
    )

    agent = Agent(
        model=model,
        system_prompt=MEMORY_SYSTEM_PROMPT,
        tools=[retrieve_from_kb],
        session_manager=session_manager,
        callback_handler=callback_handler,
    )

    return agent, session_manager


def create_rag_agent(
    enable_tracing: bool = True,
    trace_to_console: bool = False,
    trace_to_otlp: bool = False,
    callback_handler=None,
) -> Agent:
    """Create and configure the RAG agent.

    Creates a Strands Agent configured with:
    - Mistral Large 3 as the inference model (via Bedrock)
    - retrieve_from_kb tool for knowledge base retrieval
    - OpenTelemetry tracing for observability

    Args:
        enable_tracing: Whether to enable OTEL tracing (default: True).
        trace_to_console: Export traces to console for debugging.
        trace_to_otlp: Export traces to an OTLP endpoint.
        callback_handler: Optional callback handler for streaming responses.

    Returns:
        Configured Strands Agent instance ready for queries.
    """
    # Set up observability
    if enable_tracing:
        setup_observability(
            export_to_console=trace_to_console,
            export_to_otlp=trace_to_otlp,
        )

    # Configure the inference model (Mistral Large via Bedrock)
    model_id = os.getenv("INFERENCE_MODEL_ID", "mistral.mistral-large-3-675b-instruct")
    region = os.getenv("AWS_REGION", "us-east-1")

    model = BedrockModel(
        model_id=model_id,
        region_name=region,
    )

    # Create the agent with tools
    agent = Agent(
        model=model,
        system_prompt=RAG_SYSTEM_PROMPT,
        tools=[retrieve_from_kb],
        callback_handler=callback_handler,
    )

    return agent


def query_agent(agent: Agent, question: str) -> str:
    """Send a question to the RAG agent and get a response.

    Args:
        agent: The configured Strands Agent instance.
        question: The user's question.

    Returns:
        The agent's response as a string.
    """
    response = agent(question)
    return str(response)


def extract_retrieved_context(agent: Agent) -> str:
    """Extract the raw text of retrieve_from_kb tool results from agent messages.

    Walks the agent's most recent message history and collects the text returned
    by the retrieval tool. This is the actual context the model saw, which the UI
    uses both to display sources and to give the evaluator a grounding reference.

    Args:
        agent: The Strands Agent after a query has run.

    Returns:
        Concatenated retrieved passages as a string, or an empty string if none.
    """
    chunks = []
    for message in agent.messages:
        # Tool results come back in user-role messages as toolResult content blocks
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            tool_result = block.get("toolResult")
            if not tool_result:
                continue
            for item in tool_result.get("content", []):
                text = item.get("text") if isinstance(item, dict) else None
                # Only keep retrieval output (it carries the "Retrieved ... passages" header)
                if text and "Retrieved" in text and "passages" in text:
                    chunks.append(text)
    return "\n\n".join(chunks)


# CLI entry point for quick testing
if __name__ == "__main__":
    print("Initializing NovaTech RAG Agent...")
    agent = create_rag_agent(trace_to_console=True)

    print("\nAgent ready! Type your questions (Ctrl+C to exit):\n")
    try:
        while True:
            question = input("\n> ")
            if question.strip():
                response = query_agent(agent, question)
                print(f"\n{response}")
    except KeyboardInterrupt:
        print("\n\nGoodbye!")
