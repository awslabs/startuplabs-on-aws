"""AgentCore Runtime entry point for the NovaTech Agentic RAG agent.

Wraps a Strands Agent (Mistral Large 3 + Bedrock Knowledge Base retrieval)
for deployment on Amazon Bedrock AgentCore Runtime.
"""

import os
import boto3
from strands import Agent, tool
from strands.agent.conversation_manager.null_conversation_manager import (
    NullConversationManager,
)
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from model.load import load_model

app = BedrockAgentCoreApp()
log = app.logger


RAG_SYSTEM_PROMPT = """You are NovaTech Assistant, an intelligent support agent for NovaTech Solutions. \
You help users find information about NovaTech's products (NovaPlatform, NovaInsight, NovaGuard), \
APIs, troubleshooting guides, and company information.

## Instructions
1. **Always retrieve relevant context** before answering. Use the `retrieve_from_kb` tool to search \
the knowledge base for information relevant to the user's query.
2. **Be strictly grounded — no embellishment**: Provide ONLY information explicitly supported by the \
retrieved context. Do NOT add general knowledge, assumptions, or plausible-sounding details not in \
the retrieved passages. Avoid speculative qualifiers ("typically", "usually", "may include") and do \
not invent free trials, upgrade paths, permissions, navigation steps, commands, contacts, or pricing \
that are not in the context. If a detail is missing, say it is not covered in the documentation. A \
shorter, fully-grounded answer is better than a longer one with unsupported claims.
3. **Cite your sources**: Mention which document or section information came from. Label any caveat \
not from the sources as "(not from the knowledge base)".
4. **Be conversational but professional**: Use markdown formatting when it improves readability.
5. **Admit uncertainty**: If the retrieved context is ambiguous, let the user know and suggest next steps.
"""


@tool
def retrieve_from_kb(query: str, num_results: int = 8) -> str:
    """Retrieve relevant documents from the Bedrock Knowledge Base.

    Searches the NovaTech documentation knowledge base using semantic similarity
    to find the most relevant passages for answering user questions. Retrieves
    a generous number of passages by default so that answer-bearing sections
    (e.g. pricing tables or webhook lists lower in a document) are included even
    when a document's overview section ranks higher for the query terms.

    Args:
        query: The search query to find relevant documents.
        num_results: Number of results to retrieve (default: 8, max: 15).

    Returns:
        A formatted string containing the retrieved document passages with
        source attribution and relevance scores.
    """
    knowledge_base_id = os.getenv("KNOWLEDGE_BASE_ID")
    if not knowledge_base_id:
        return "Error: KNOWLEDGE_BASE_ID environment variable not set."

    # The Knowledge Base is regional. Use a dedicated env var so retrieval always
    # targets the KB's region, independent of the runtime's own AWS_REGION
    # (AgentCore sets AWS_REGION to the runtime region, which may differ).
    region = os.getenv("KNOWLEDGE_BASE_REGION", "us-east-1")
    num_results = min(max(num_results, 1), 15)

    try:
        client = boto3.client("bedrock-agent-runtime", region_name=region)
        response = client.retrieve(
            knowledgeBaseId=knowledge_base_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={
                "vectorSearchConfiguration": {
                    "numberOfResults": num_results,
                    # HYBRID combines semantic (vector) similarity with keyword/BM25
                    # matching, improving recall for queries with strong lexical terms.
                    "overrideSearchType": "HYBRID",
                }
            },
        )

        results = response.get("retrievalResults", [])
        if not results:
            return f"No relevant documents found for query: '{query}'."

        formatted = []
        for i, result in enumerate(results, 1):
            content = result.get("content", {}).get("text", "No content available")
            score = result.get("score", 0.0)
            location = result.get("location", {})
            source = "Unknown source"
            if location.get("type") == "S3":
                s3_uri = location.get("s3Location", {}).get("uri", "")
                source = s3_uri.split("/")[-1] if s3_uri else "S3 document"
            formatted.append(
                f"### Result {i} (Score: {score:.3f})\n"
                f"**Source:** {source}\n\n{content}\n"
            )

        header = f'## Retrieved {len(results)} relevant passages for: "{query}"\n\n'
        return header + "\n---\n\n".join(formatted)

    except Exception as e:
        return f"Error retrieving from knowledge base: {str(e)}"


tools = [retrieve_from_kb]

MEMORY_SYSTEM_PROMPT = RAG_SYSTEM_PROMPT + """

## Memory (strict)
You have persistent memory of this user across sessions. Relevant user \
preferences and facts may be provided as retrieved memory context.
- Use ONLY the memories actually provided. Do NOT invent or embellish details \
about the user's role, interests, or history.
- Any statement you make about the user must come verbatim from the provided \
memory (e.g., if memory says the user is a "compliance officer", do not describe \
them as anything else).
- If no memory about a detail is available, do not speculate.
- When you use a memory, briefly note it (e.g., "Based on your role as ...") so \
the personalization is transparent.
"""

# Cache the model client (safe to share); build a fresh Agent per invocation so
# that message history / captured sources don't leak across concurrent requests.
_model = None


def _get_model():
    global _model
    if _model is None:
        _model = load_model()
    return _model


def _build_agent(actor_id: str = None, session_id: str = None):
    """Build the agent. If AGENTCORE_MEMORY_ID is configured and an actor_id is
    provided, attach AgentCore Memory for persistent cross-session recall.

    Returns (agent, session_manager). session_manager is None when memory is off;
    the caller must close it to flush pending memory writes.
    """
    memory_id = os.getenv("AGENTCORE_MEMORY_ID")
    region = os.getenv("AWS_REGION", "us-east-1")

    if memory_id and actor_id:
        try:
            from bedrock_agentcore.memory.integrations.strands.config import (
                AgentCoreMemoryConfig,
                RetrievalConfig,
            )
            from bedrock_agentcore.memory.integrations.strands.session_manager import (
                AgentCoreMemorySessionManager,
            )

            memory_config = AgentCoreMemoryConfig(
                memory_id=memory_id,
                session_id=session_id or f"session-{actor_id}",
                actor_id=actor_id,
                retrieval_config={
                    "/preferences/{actorId}/": RetrievalConfig(top_k=5, relevance_score=0.2),
                    "/facts/{actorId}/": RetrievalConfig(top_k=5, relevance_score=0.2),
                    "/summaries/{actorId}/{sessionId}/": RetrievalConfig(top_k=3, relevance_score=0.2),
                },
            )
            session_manager = AgentCoreMemorySessionManager(
                agentcore_memory_config=memory_config, region_name=region
            )
            agent = Agent(
                model=_get_model(),
                system_prompt=MEMORY_SYSTEM_PROMPT,
                tools=tools,
                session_manager=session_manager,
            )
            log.info("Agent built WITH AgentCore Memory (actor=%s)", actor_id)
            return agent, session_manager
        except Exception as e:
            log.warning("Memory init failed, falling back to stateless agent: %s", e)

    agent = Agent(
        model=_get_model(),
        system_prompt=RAG_SYSTEM_PROMPT,
        tools=tools,
        conversation_manager=NullConversationManager(),
    )
    return agent, None


def _extract_sources_and_trajectory(agent: Agent):
    """Pull retrieved passages and the tool-call trajectory from agent messages.

    Returns (sources_text, trajectory_list). This lets the client display the
    retrieved context and evaluate retrieval/groundedness, since tool results
    are otherwise not part of the streamed response.
    """
    sources_chunks = []
    trajectory = []
    for message in agent.messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            # Capture tool-use (trajectory)
            tool_use = block.get("toolUse")
            if tool_use and tool_use.get("name"):
                trajectory.append(tool_use["name"])
            # Capture tool results (retrieved passages)
            tool_result = block.get("toolResult")
            if tool_result:
                for item in tool_result.get("content", []):
                    text = item.get("text") if isinstance(item, dict) else None
                    if text and "Retrieved" in text and "passages" in text:
                        sources_chunks.append(text)
    return "\n\n".join(sources_chunks), trajectory


def _extract_prompt(payload: dict):
    """Accept harness-style messages[], tool_results[], or plain prompt string payloads."""
    if "messages" in payload:
        return payload["messages"]
    if "tool_results" in payload:
        return [
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": tr["toolUseId"],
                            "status": tr.get("status", "success"),
                            "content": tr.get("content", []),
                        }
                    }
                    for tr in payload["tool_results"]
                ],
            }
        ]
    return payload.get("prompt", "")


def _resolve_identity(payload: dict, context) -> tuple:
    """Determine (actor_id, session_id) for memory.

    actor_id (the stable user identity that enables cross-session recall) is taken
    from the payload; session_id prefers the AgentCore runtime session id from the
    context, falling back to a payload value.
    """
    actor_id = payload.get("actor_id") or payload.get("user_id")
    session_id = payload.get("session_id")
    # AgentCore provides the runtime session id on the context object.
    if not session_id and context is not None:
        session_id = getattr(context, "session_id", None) or getattr(
            context, "runtime_session_id", None
        )
    return actor_id, session_id


@app.entrypoint
async def invoke(payload, context):
    """Invoke the RAG agent and stream the response.

    If the payload includes an ``actor_id`` and AGENTCORE_MEMORY_ID is configured,
    the agent uses AgentCore Memory for persistent cross-session recall. The full
    execution is captured in CloudWatch via built-in OpenTelemetry instrumentation.
    """
    log.info("Invoking NovaTech RAG Agent...")

    actor_id, session_id = _resolve_identity(payload, context)
    agent, session_manager = _build_agent(actor_id=actor_id, session_id=session_id)
    prompt = _extract_prompt(payload)

    try:
        async for event in agent.stream_async(prompt):
            if not isinstance(event, dict) or "event" not in event:
                continue
            cbs = event["event"].get("contentBlockStart")
            if cbs is not None and not cbs.get("start"):
                continue
            yield event
    finally:
        # Flush pending memory writes so this turn is persisted for future sessions.
        if session_manager is not None:
            try:
                session_manager.close()
            except Exception as e:
                log.warning("Failed to flush memory session: %s", e)


if __name__ == "__main__":
    app.run()
