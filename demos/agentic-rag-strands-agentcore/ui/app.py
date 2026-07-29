"""Streamlit chat interface for the NovaTech RAG Agent."""

import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from agent.rag_agent import (
    create_rag_agent,
    create_memory_enabled_agent,
    extract_retrieved_context,
)
from agent.live_eval import evaluate_all
from agent.runtime_logger import (
    log_invocation_async,
    cloudwatch_logs_url,
    genai_observability_url,
    get_last_session_id,
)
from strands_evals.extractors import tools_use_extractor

import uuid as _uuid

# --- Page Configuration (must be the first Streamlit call) ---
st.set_page_config(
    page_title="NovaTech AI Assistant",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Stable per-UI-session runtime session id (used to group CloudWatch traces).
# Initialized before the sidebar renders so it's always available.
if "runtime_session_id" not in st.session_state:
    st.session_state.runtime_session_id = (
        f"ui-{_uuid.uuid4().hex}{_uuid.uuid4().hex}"[:48]
    )

# --- Custom CSS ---
st.markdown(
    """
<style>
    .main-header {
        font-size: 2.2rem;
        font-weight: 700;
        color: #1a1a2e;
        margin-bottom: 0;
    }
    .sub-header {
        font-size: 1rem;
        color: #6c757d;
        margin-top: -10px;
        margin-bottom: 20px;
    }
    .stChatMessage {
        border-radius: 12px;
    }
    .sidebar-info {
        background-color: #f8f9fa;
        border-radius: 8px;
        padding: 15px;
        margin-bottom: 15px;
    }
    .metric-card {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        border-radius: 10px;
        padding: 15px;
        color: white;
        text-align: center;
        margin-bottom: 10px;
    }
    .metric-value {
        font-size: 1.5rem;
        font-weight: bold;
    }
    .metric-label {
        font-size: 0.8rem;
        opacity: 0.9;
    }
</style>
""",
    unsafe_allow_html=True,
)


# --- Sidebar ---
with st.sidebar:
    st.image("https://img.icons8.com/fluency/96/artificial-intelligence.png", width=60)
    st.markdown("### NovaTech AI Assistant")
    st.markdown("---")

    st.markdown("#### 💡 About")
    st.markdown(
        """
    This is an **Agentic RAG** demo powered by:
    - 🧠 **Strands Agents SDK**
    - 📚 **Bedrock Knowledge Bases**
    - ⚡ **Mistral Large 3** (Inference)
    - 📊 **Titan Embeddings V2**
    - 🔍 **OpenTelemetry** (Observability)
    """
    )

    st.markdown("---")
    st.markdown("#### 📖 Knowledge Base")
    st.markdown(
        """
    The agent has access to NovaTech documentation:
    - Company Overview
    - NovaPlatform Technical Docs
    - NovaInsight BI Platform
    - NovaGuard AI Safety Toolkit
    - API Reference
    - Troubleshooting Guide
    """
    )

    st.markdown("---")
    st.markdown("#### 🎯 Try These Questions")
    example_questions = [
        "What is NovaTech Solutions?",
        "How much does NovaPlatform cost?",
        "What fairness metrics does NovaGuard support?",
        "My training job is stuck in PENDING. Help!",
        "How do I authenticate with the API?",
        "Compare NovaInsight pricing plans",
        "What data sources are supported?",
    ]

    for q in example_questions:
        if st.button(f"💬 {q}", key=f"btn_{q[:20]}", use_container_width=True):
            st.session_state.pending_question = q

    st.markdown("---")
    st.markdown("#### 🧠 Persistent Memory")
    st.markdown(
        "Enable **AgentCore Memory** so the assistant remembers you across "
        "sessions — your role, preferences, and past context."
    )
    enable_memory = st.toggle(
        "Enable persistent memory", value=False,
        help="Uses Amazon Bedrock AgentCore Memory for cross-session recall",
    )
    memory_user_id = st.text_input(
        "Your user ID (for memory)", value="demo-customer",
        help="The same ID recalls your memory across sessions. Try closing and "
             "reopening with the same ID.",
        disabled=not enable_memory,
    )

    st.markdown("---")
    st.markdown("#### ⚙️ Settings")
    show_traces = st.toggle("Show trace info", value=False)
    show_sources = st.toggle("Show retrieval sources", value=True)
    enable_eval = st.toggle("Enable response evaluation", value=True,
                            help="Score each answer with a Strands Evals LLM judge")

    st.markdown("---")
    st.markdown("#### 📡 Observability")
    st.markdown(
        "Every question is also sent to the **deployed AgentCore runtime**, so "
        "each invocation is captured in **CloudWatch** traces (token usage, "
        "latency, tool calls)."
    )
    st.caption(f"Runtime session: `{st.session_state.get('runtime_session_id', 'n/a')[:24]}…`")

    st.markdown("---")
    st.caption("Built with Strands Agents • Deployed on AgentCore")


# --- Main Content ---
st.markdown('<p class="main-header">🤖 NovaTech AI Assistant</p>', unsafe_allow_html=True)
st.markdown(
    '<p class="sub-header">Ask me anything about NovaTech products, APIs, and troubleshooting</p>',
    unsafe_allow_html=True,
)

# --- Session State Initialization ---
if "messages" not in st.session_state:
    st.session_state.messages = []
if "agent_mode" not in st.session_state:
    st.session_state.agent_mode = None  # tracks (enable_memory, user_id)


def _ensure_agent(enable_memory: bool, user_id: str):
    """Build (or rebuild) the agent when the memory configuration changes.

    Uses a memory-enabled agent (AgentCore Memory, cross-session recall) when
    enabled, otherwise the standard in-session agent.
    """
    desired = (enable_memory, user_id.strip() if user_id else "")
    if st.session_state.agent_mode == desired and st.session_state.get("agent"):
        return

    with st.spinner("Initializing agent..."):
        if enable_memory and desired[1]:
            try:
                agent, sm = create_memory_enabled_agent(
                    actor_id=desired[1],
                    session_id=st.session_state.runtime_session_id,
                    enable_tracing=True,
                    callback_handler=None,
                )
                st.session_state.agent = agent
                st.session_state.memory_session_manager = sm
                st.session_state.memory_active = True
            except Exception as e:
                st.session_state.agent = create_rag_agent(
                    enable_tracing=True, trace_to_console=False, callback_handler=None
                )
                st.session_state.memory_active = False
                st.warning(f"Memory unavailable, using standard agent: {e}")
        else:
            st.session_state.agent = create_rag_agent(
                enable_tracing=True, trace_to_console=False, callback_handler=None
            )
            st.session_state.memory_active = False
    st.session_state.agent_mode = desired


_ensure_agent(enable_memory, memory_user_id)

if "query_count" not in st.session_state:
    st.session_state.query_count = 0
if "msg_counter" not in st.session_state:
    st.session_state.msg_counter = 0
if "eval_results" not in st.session_state:
    # Maps message id -> evaluation result dict
    st.session_state.eval_results = {}


def _score_color(score: float):
    """Return (color, verdict) for a 0-1 score band."""
    if score >= 0.75:
        return "#1a9850", "Strong"
    if score >= 0.5:
        return "#f0ad4e", "Acceptable"
    return "#d9534f", "Weak"


def render_trace_info(trajectory=None):
    """Render the Trace Information panel with config, tool trajectory, and
    CloudWatch deep links for the current runtime session."""
    # Use the session id of the most recent runtime replay (each question uses
    # its own unique session), falling back to the UI session.
    session_id = get_last_session_id() or st.session_state.get("runtime_session_id", "")
    with st.expander("🔍 Trace Information"):
        # Tools that actually fired this turn (from the captured trajectory)
        tools_fired = []
        for t in (trajectory or []):
            name = t.get("name") if isinstance(t, dict) else t
            if name:
                tools_fired.append(name)

        st.json(
            {
                "model": os.getenv(
                    "INFERENCE_MODEL_ID", "mistral.mistral-large-3-675b-instruct"
                ),
                "knowledge_base_id": os.getenv("KNOWLEDGE_BASE_ID", "not-configured"),
                "region": os.getenv("KNOWLEDGE_BASE_REGION", "us-east-1"),
                "tools_available": ["retrieve_from_kb"],
                "tools_invoked_this_turn": tools_fired or ["(none)"],
                "runtime_session_id": session_id,
            }
        )

        st.markdown("**📡 View this session's traces in AWS:**")
        st.markdown(
            f"- [CloudWatch Logs (filtered to this session)]({cloudwatch_logs_url(session_id)})"
        )
        st.markdown(
            f"- [GenAI Observability dashboard]({genai_observability_url()})"
        )
        st.caption(
            "Traces are produced by the deployed AgentCore runtime and take "
            "~1–2 minutes to index in CloudWatch."
        )


import re as _re


def _parse_sources(sources_text: str):
    """Parse the raw retrieved-sources blob into structured entries.

    Returns a list of dicts: {rank, score, source, snippet}. The blob format is
    produced by retrieve_from_kb: '### Result N (Score: X)\\n**Source:** file\\n\\n<text>'.
    """
    entries = []
    # Split on the "### Result N (Score: ...)" markers
    parts = _re.split(r"### Result \d+ \(Score: ([\d.]+)\)", sources_text)
    # parts[0] is the header; then alternating [score, body, score, body, ...]
    for i in range(1, len(parts), 2):
        score = parts[i]
        body = parts[i + 1] if i + 1 < len(parts) else ""
        src_match = _re.search(r"\*\*Source:\*\*\s*(.+)", body)
        source = src_match.group(1).strip() if src_match else "unknown"
        # Everything after the Source line is the snippet
        snippet = body
        if src_match:
            snippet = body[src_match.end():].strip()
        # Trim trailing separator noise
        snippet = snippet.strip().lstrip("-").strip()
        entries.append({
            "score": float(score) if score else 0.0,
            "source": source,
            "snippet": snippet,
        })
    return entries


def render_sources(sources_text: str, key_prefix: str = ""):
    """Render retrieved sources as an elegant ranked list of documents.

    Shows one row per unique source document with its best relevance score and a
    'chips' summary. Full passage text is tucked into a per-result expander so the
    panel stays clean.
    """
    entries = _parse_sources(sources_text)
    if not entries:
        st.caption("No structured sources available.")
        return

    # Deduplicate by source doc, keeping the highest score and counting hits
    by_doc = {}
    for e in entries:
        d = by_doc.setdefault(e["source"], {"score": e["score"], "count": 0})
        d["count"] += 1
        d["score"] = max(d["score"], e["score"])

    # Summary chips: one line per document, sorted by score
    st.caption(f"Retrieved {len(entries)} passages from {len(by_doc)} documents:")
    for doc, info in sorted(by_doc.items(), key=lambda x: -x[1]["score"]):
        pct = int(round(info["score"] * 100))
        hits = f" · {info['count']} passages" if info["count"] > 1 else ""
        st.markdown(
            f"<div style='padding:6px 10px;margin-bottom:6px;border-radius:6px;"
            f"background:#f1f3f5;border-left:4px solid #667eea'>"
            f"📄 <b>{doc}</b> "
            f"<span style='color:#667eea;font-weight:600'>· relevance {pct}%</span>"
            f"<span style='color:#868e96;font-size:0.85rem'>{hits}</span></div>",
            unsafe_allow_html=True,
        )

    # Full passages available on demand, kept out of the way
    with st.expander("Show full passage text"):
        for idx, e in enumerate(entries, 1):
            st.markdown(f"**{idx}. {e['source']}** — relevance {int(round(e['score']*100))}%")
            st.text(e["snippet"][:1200] + ("…" if len(e["snippet"]) > 1200 else ""))
            st.divider()


def render_evaluation(msg_id: str, question: str, answer: str,
                      context: str = "", trajectory: list | None = None):
    """Render the 'Evaluate this answer' control and multi-dimension results."""
    results = st.session_state.eval_results.get(msg_id)

    if results is None:
        if st.button("🧪 Evaluate this answer", key=f"eval_btn_{msg_id}"):
            with st.spinner("Scoring across 4 dimensions with Strands Evals..."):
                st.session_state.eval_results[msg_id] = evaluate_all(
                    question, answer, context, trajectory or []
                )
            st.rerun()
        return

    # Overall banner
    overall = results.get("overall")
    if overall is not None:
        ocolor, overdict = _score_color(overall)
        st.markdown(
            f"<div style='padding:10px;border-radius:8px;background:{ocolor};"
            f"color:white;margin-bottom:10px'>"
            f"<b>Overall Evaluation Score: {overall:.2f} ({overdict})</b> "
            f"<span style='font-size:0.8rem'>· Strands Evals · LLM-as-judge</span>"
            f"</div>",
            unsafe_allow_html=True,
        )

    # Per-dimension breakdown
    dimensions = [
        ("Output Quality", "Groundedness + relevance + clarity vs retrieved context"),
        ("Retrieval Relevance", "Are the retrieved passages relevant to the question"),
        ("Helpfulness", "7-level scale: how actionable/useful the answer is"),
        ("Trajectory", "Did the agent use its tools correctly"),
    ]
    cols = st.columns(4)
    for col, (dim, _desc) in zip(cols, dimensions):
        r = results.get(dim, {})
        with col:
            if "error" in r:
                st.metric(dim, "N/A")
                st.caption(r["error"][:60])
            else:
                sc = r["score"]
                color, _ = _score_color(sc)
                st.markdown(
                    f"<div style='text-align:center;padding:6px;border-radius:8px;"
                    f"background:{color};color:white'>"
                    f"<div style='font-size:1.3rem;font-weight:bold'>{sc:.2f}</div>"
                    f"<div style='font-size:0.65rem'>{'PASS' if r['test_pass'] else 'FAIL'}</div>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
                st.caption(dim)

    # Detailed reasoning per dimension
    with st.expander("📋 Judge reasoning (per dimension)"):
        for dim, desc in dimensions:
            r = results.get(dim, {})
            st.markdown(f"**{dim}** — _{desc}_")
            if "error" in r:
                st.caption(f"Not available: {r['error']}")
            else:
                st.markdown(r.get("reason", "_(no reason provided)_"))
            st.markdown("---")

def render_architecture_tab():
    """Render the architecture diagram and component breakdown."""
    st.markdown("### 🏗️ Solution Architecture")
    st.markdown(
        "End-to-end agentic RAG built on **Strands Agents**, deployed on "
        "**Amazon Bedrock AgentCore**, with hybrid retrieval, robust evaluations, "
        "persistent memory, and full observability."
    )

    dot = """
    digraph architecture {
        rankdir=TB;
        bgcolor="transparent";
        node [style="filled,rounded", shape=box, fontname="Helvetica", fontsize=11,
              color="#4B5563", fontcolor="white"];
        edge [color="#6B7280", fontname="Helvetica", fontsize=9, fontcolor="#374151"];

        ui   [label="Streamlit UI\\n(Chat - Sources - Live Evals)", fillcolor="#764ba2"];
        agent[label="Strands Agent (RAG)\\nMistral Large 3", fillcolor="#667eea"];

        subgraph cluster_tools {
            label="Agent Tools"; style="rounded,dashed"; color="#9CA3AF"; fontcolor="#6B7280";
            retrieve [label="retrieve_from_kb\\n(hybrid search)", fillcolor="#0ea5e9"];
        }

        kb    [label="Bedrock Knowledge Base\\nTitan Embeddings V2", fillcolor="#059669"];
        vector[label="OpenSearch Serverless\\n(vector store)", fillcolor="#047857"];
        s3    [label="S3\\n7 markdown docs", fillcolor="#10b981"];

        memory[label="AgentCore Memory\\n(preferences - facts - summaries)", fillcolor="#d97706"];
        runtime[label="AgentCore Runtime\\n(deployed, auto-scaling)", fillcolor="#dc2626"];
        cw    [label="CloudWatch\\nOTel traces", fillcolor="#7c3aed"];
        evals [label="Strands Evals\\n(4 dimensions, LLM judge)", fillcolor="#be185d"];

        ui -> agent [label="question"];
        agent -> retrieve;
        retrieve -> kb -> vector;
        kb -> s3 [label="ingest", style=dashed];
        agent -> memory [label="recall / store", dir=both];
        ui -> runtime [label="hybrid replay", style=dashed];
        runtime -> cw [label="traces"];
        ui -> evals [label="score answer"];
        runtime -> memory [style=dashed];
    }
    """
    st.graphviz_chart(dot, use_container_width=True)

    st.markdown("---")
    st.markdown("#### Component Stack")
    st.markdown(
        """
| Layer | Technology |
|-------|-----------|
| Agent Framework | **Strands Agents SDK** |
| Inference Model | **Mistral Large 3** (Amazon Bedrock) |
| Retrieval | **Bedrock Knowledge Base** + hybrid (vector + keyword) search |
| Embeddings | **Amazon Titan Embeddings V2** (1024-dim) |
| Vector Store | **Amazon OpenSearch Serverless** |
| Persistent Memory | **Amazon Bedrock AgentCore Memory** (semantic, preference, summary) |
| Evaluations | **Strands Evals** — Output Quality, Retrieval Relevance, Helpfulness, Trajectory |
| Hosting | **Amazon Bedrock AgentCore Runtime** (deployed, auto-scaling) |
| Observability | **OpenTelemetry → CloudWatch** (token usage, latency, tool calls) |
| UI | **Streamlit** |
        """
    )
    st.caption(
        "Note: the UI answers/evaluates via a local agent (to capture sources) and "
        "also replays each question to the deployed AgentCore runtime, so every "
        "invocation is traced in CloudWatch."
    )


def render_chat_tab():
    """Render the chat interface: metrics, history, and input handling."""
    # --- Display Metrics ---
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Queries This Session", st.session_state.query_count)
    with col2:
        st.metric("Knowledge Docs", "7 documents")
    with col3:
        st.metric("Model", "Mistral Large 3")
    with col4:
        if st.session_state.get("memory_active"):
            st.metric("🧠 Memory", f"ON · {memory_user_id}")
        else:
            st.metric("🧠 Memory", "Off")

    if st.session_state.get("memory_active"):
        st.info(
            f"🧠 **Persistent memory active** for user `{memory_user_id}`. The assistant "
            "remembers your role, preferences, and context across sessions via "
            "Amazon Bedrock AgentCore Memory.",
            icon="🧠",
        )

    st.markdown("---")

    # --- Chat History ---
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            if message["role"] == "assistant" and show_sources and message.get("sources"):
                with st.expander("📄 Retrieved Sources"):
                    render_sources(message["sources"], key_prefix=message.get("id", ""))
            # Show evaluation control for answered assistant messages
            if (
                message["role"] == "assistant"
                and enable_eval
                and message.get("id")
                and message.get("question")
                and not message["content"].startswith("❌")
            ):
                render_evaluation(
                    message["id"],
                    message["question"],
                    message["content"],
                    message.get("sources", ""),
                    message.get("trajectory", []),
                )

    # --- Handle pending question from sidebar buttons ---
    if "pending_question" in st.session_state:
        prompt = st.session_state.pending_question
        del st.session_state.pending_question
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            with st.spinner("Searching knowledge base and generating answer..."):
                try:
                    response = st.session_state.agent(prompt)
                    response_text = str(response)
                    st.markdown(response_text)
                    st.session_state.query_count += 1

                    log_invocation_async(prompt, st.session_state.runtime_session_id)

                    sources = extract_retrieved_context(st.session_state.agent)
                    trajectory = tools_use_extractor.extract_agent_tools_used_from_messages(
                        st.session_state.agent.messages
                    )
                    st.session_state.msg_counter += 1
                    msg_id = f"msg_{st.session_state.msg_counter}"
                    if show_sources and sources:
                        with st.expander("📄 Retrieved Sources"):
                            render_sources(sources, key_prefix=msg_id)

                    message_data = {
                        "role": "assistant",
                        "content": response_text,
                        "id": msg_id,
                        "question": prompt,
                        "sources": sources,
                        "trajectory": trajectory,
                    }
                    if show_traces:
                        render_trace_info(trajectory)
                    st.session_state.messages.append(message_data)
                    if enable_eval:
                        render_evaluation(msg_id, prompt, response_text, sources, trajectory)
                except Exception as e:
                    error_msg = f"❌ Error: {str(e)}"
                    st.error(error_msg)
                    st.session_state.messages.append(
                        {"role": "assistant", "content": error_msg}
                    )

    # --- Chat Input ---
    if prompt := st.chat_input("Ask about NovaTech products, APIs, or troubleshooting..."):
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            with st.spinner("Searching knowledge base and generating answer..."):
                try:
                    response = st.session_state.agent(prompt)
                    response_text = str(response)
                    st.markdown(response_text)
                    st.session_state.query_count += 1

                    log_invocation_async(prompt, st.session_state.runtime_session_id)

                    sources = extract_retrieved_context(st.session_state.agent)
                    trajectory = tools_use_extractor.extract_agent_tools_used_from_messages(
                        st.session_state.agent.messages
                    )
                    st.session_state.msg_counter += 1
                    msg_id = f"msg_{st.session_state.msg_counter}"
                    if show_sources and sources:
                        with st.expander("📄 Retrieved Sources"):
                            render_sources(sources, key_prefix=msg_id)

                    message_data = {
                        "role": "assistant",
                        "content": response_text,
                        "id": msg_id,
                        "question": prompt,
                        "sources": sources,
                        "trajectory": trajectory,
                    }
                    if show_traces:
                        render_trace_info(trajectory)
                    st.session_state.messages.append(message_data)
                    if enable_eval:
                        render_evaluation(msg_id, prompt, response_text, sources, trajectory)
                except Exception as e:
                    error_msg = f"❌ Error: {str(e)}"
                    st.error(error_msg)
                    st.session_state.messages.append(
                        {"role": "assistant", "content": error_msg}
                    )


# --- Tabs: Chat + Architecture ---
tab_chat, tab_arch = st.tabs(["💬 Chat", "🏗️ Architecture"])
with tab_chat:
    render_chat_tab()
with tab_arch:
    render_architecture_tab()


# --- Footer ---
st.markdown("---")
st.caption(
    "Powered by Strands Agents SDK | Amazon Bedrock Knowledge Bases | "
    "Mistral Large 3 | Deployed on AgentCore"
)
