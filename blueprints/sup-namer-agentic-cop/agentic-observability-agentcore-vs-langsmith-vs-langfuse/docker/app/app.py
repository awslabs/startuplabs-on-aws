"""
LangGraph Shopping Assistant — instrumented for three observability platforms
side-by-side:

  1. AgentCore Observability (AWS-native, OTEL -> X-Ray + CloudWatch)
  2. LangSmith                 (LangChain SaaS, auto-tracing)
  3. Langfuse                  (framework-agnostic SaaS / self-hostable)

Each platform's integration is grouped into a clearly labelled section below.
The goal of this sample is to show how few lines of code each one requires
and where they overlap (or compete) when run simultaneously in one process.

Auth model:
  * AgentCore   -> pod IAM role (IRSA), SigV4 to AWS OTLP endpoints. No keys.
  * LangSmith   -> static API key in a Kubernetes Secret (LANGCHAIN_API_KEY).
  * Langfuse    -> public/secret key pair in a Kubernetes Secret.

For the LangSmith and Langfuse cloud offerings, you also need to configure a
Bedrock connection in their UIs so the platforms can invoke Bedrock models
for LLM-as-a-judge evaluations. See README for the exact steps.
"""
import os
import random
import time
from typing import Annotated, Dict

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# Import BaseCache first so the forward reference in ChatBedrock resolves.
from langchain_core.caches import BaseCache  # noqa: F401
from langchain_aws import ChatBedrock
from langgraph.graph import StateGraph, MessagesState, START, END
from langgraph.prebuilt import ToolNode
from langgraph.graph.message import add_messages
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, SystemMessage
import uvicorn

# ---------------------------------------------------------------------------
# OpenTelemetry setup (shared by AgentCore Observability and Langfuse)
# ---------------------------------------------------------------------------
# AWS Distro auto-instrumentation sets up the TracerProvider for us via the
# env vars OTEL_PYTHON_DISTRO=aws_distro and OTEL_PYTHON_CONFIGURATOR=
# aws_configurator (injected into the pod by the CDK WorkloadStack; the values
# are defined in stacks/observability_config.py). We just grab the global tracer.
from opentelemetry import trace, baggage
from opentelemetry.context import attach, detach
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor

tracer = trace.get_tracer(__name__)


# ===========================================================================
# AGENTCORE OBSERVABILITY INTEGRATION  (1 of 3)
# ===========================================================================
# Traceloop's LangchainInstrumentor auto-instruments LangGraph and propagates
# session.id from RunnableConfig metadata into OTEL spans. Required for
# LangGraph sessions to show up in the AgentCore Observability GenAI console.
try:
    from opentelemetry.instrumentation.langchain import LangchainInstrumentor
    LangchainInstrumentor().instrument()
    print("✓ LangchainInstrumentor enabled - LangGraph spans will include session.id")
except ImportError as e:
    print(f"⚠ LangchainInstrumentor not available: {e}")
# All other AgentCore configuration is environment-variable driven (see
# stacks/observability_config.py -> agentcore_env(): OTEL_EXPORTER_OTLP_TRACES_ENDPOINT,
# OTEL_RESOURCE_ATTRIBUTES, OTEL_EXPORTER_OTLP_LOGS_HEADERS, etc.).


# ===========================================================================
# LANGFUSE INTEGRATION  (2 of 3)
# ===========================================================================
# Langfuse is the only one of the three platforms that needs application-level
# code beyond environment variables. The CallbackHandler captures the LangGraph
# tree, and a manual "chat-turn" span gives evaluators a clean
# (input=user prompt, output=assistant text) target — without it, evaluators
# read mid-loop framework state (e.g. {messages: [AIMessage with tool_calls]}).
#
# Auth: LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST env vars. The
# CDK WorkloadStack syncs the key pair from AWS Secrets Manager into a Kubernetes
# Secret via the Secrets Store CSI Driver (SecretProviderClass); LANGFUSE_HOST is
# set from AppConfig.
try:
    from langfuse import get_client, propagate_attributes
    from langfuse.langchain import CallbackHandler as LangfuseCallbackHandler

    langfuse_client = get_client()
    if langfuse_client.auth_check():
        langfuse_handler = LangfuseCallbackHandler()
        print("✓ Langfuse enabled - traces will be sent to "
              f"{os.getenv('LANGFUSE_HOST', 'https://cloud.langfuse.com')}")
    else:
        langfuse_client = None
        langfuse_handler = None
        print("⚠ Langfuse auth_check failed - check LANGFUSE_PUBLIC_KEY / "
              "LANGFUSE_SECRET_KEY / LANGFUSE_HOST env vars")
except ImportError as e:
    langfuse_client = None
    langfuse_handler = None
    propagate_attributes = None  # type: ignore
    print(f"⚠ Langfuse SDK not available: {e}")
except Exception as e:
    # Don't let Langfuse init failures take down the agent.
    langfuse_client = None
    langfuse_handler = None
    propagate_attributes = None  # type: ignore
    print(f"⚠ Langfuse init failed (continuing without it): {e}")


# ===========================================================================
# LANGSMITH INTEGRATION  (3 of 3)
# ===========================================================================
# Zero application code is required. The LangChain SDK auto-traces every
# Runnable invocation to LangSmith when LANGCHAIN_TRACING_V2=true and
# LANGCHAIN_API_KEY are set. Those env vars are provided by the CDK WorkloadStack
# (LANGCHAIN_API_KEY is synced from AWS Secrets Manager via the SecretProviderClass;
# the literals are defined in stacks/observability_config.py).
# ===========================================================================


# Resolve forward references on ChatBedrock (Pydantic v2 compatibility).
ChatBedrock.model_rebuild()

# Initialize FastAPI and instrument HTTP layers.
app = FastAPI(title="LangGraph Shopping Assistant")
FastAPIInstrumentor.instrument_app(app)
RequestsInstrumentor().instrument()

# Per-session compiled LangGraph instances. Reusing avoids the cost of
# recompiling the StateGraph on every request.
agent_sessions: Dict[str, object] = {}


# ---------------------------------------------------------------------------
# Tool tracing decorator
# ---------------------------------------------------------------------------
def traced_tool(func):
    """Wrap a LangChain @tool so each invocation gets its own OTEL span.

    Uses functools.wraps to preserve the wrapped function's signature so
    LangChain's @tool generates the correct JSON schema. Without that, the
    model would call the tool with a `kwargs` argument and fail every time.
    """
    import functools

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with tracer.start_as_current_span(f"tool.{func.__name__}") as span:
            span.set_attribute("tool.name", func.__name__)
            if args:
                span.set_attribute("tool.args", str(args)[:200])
            if kwargs:
                span.set_attribute("tool.kwargs", str(kwargs)[:200])
            try:
                result = func(*args, **kwargs)
                span.set_attribute("tool.result_length", len(str(result)))
                span.set_attribute("tool.success", True)
                return result
            except Exception as e:
                span.set_attribute("tool.error", True)
                span.set_attribute("tool.error_message", str(e))
                raise
    return wrapper


# ---------------------------------------------------------------------------
# Mock product catalog (in-memory; replace with your own data source)
# ---------------------------------------------------------------------------
PRODUCTS = {
    "laptop-001": {"id": "laptop-001", "name": "TechPro X1 Laptop", "category": "laptops",
                   "price": 899.99, "specs": "Intel i7, 16GB RAM, 512GB SSD, 15.6\" display",
                   "rating": 4.5, "reviews_count": 342, "in_stock": True, "stock_level": 15},
    "laptop-002": {"id": "laptop-002", "name": "UltraBook Pro", "category": "laptops",
                   "price": 1299.99, "specs": "Intel i9, 32GB RAM, 1TB SSD, 14\" display",
                   "rating": 4.8, "reviews_count": 567, "in_stock": True, "stock_level": 8},
    "laptop-003": {"id": "laptop-003", "name": "BudgetBook 2024", "category": "laptops",
                   "price": 499.99, "specs": "Intel i5, 8GB RAM, 256GB SSD, 15.6\" display",
                   "rating": 4.2, "reviews_count": 891, "in_stock": True, "stock_level": 23},
    "phone-001":  {"id": "phone-001", "name": "SmartPhone X12", "category": "phones",
                   "price": 799.99, "specs": "6.5\" OLED, 128GB, 5G, Triple Camera",
                   "rating": 4.6, "reviews_count": 1234, "in_stock": True, "stock_level": 45},
    "phone-002":  {"id": "phone-002", "name": "ProPhone Elite", "category": "phones",
                   "price": 1099.99, "specs": "6.7\" AMOLED, 256GB, 5G, Quad Camera",
                   "rating": 4.9, "reviews_count": 2103, "in_stock": False, "stock_level": 0},
    "tablet-001": {"id": "tablet-001", "name": "TabletPro 11", "category": "tablets",
                   "price": 649.99, "specs": "11\" display, 128GB, WiFi + Cellular",
                   "rating": 4.4, "reviews_count": 456, "in_stock": True, "stock_level": 12},
    "headphones-001": {"id": "headphones-001", "name": "NoiseCancel Pro", "category": "audio",
                       "price": 299.99, "specs": "Active Noise Cancelling, 30hr battery, Bluetooth 5.2",
                       "rating": 4.7, "reviews_count": 789, "in_stock": True, "stock_level": 67},
}

PROMOTIONS = {
    "laptop-001":     {"discount": 50,  "description": "Back to School Sale - $50 off"},
    "phone-001":      {"discount": 100, "description": "Summer Sale - $100 off"},
    "headphones-001": {"discount": 30,  "description": "Limited Time - $30 off"},
}


# ---------------------------------------------------------------------------
# Shopping tools (each is auto-traced via @traced_tool)
# ---------------------------------------------------------------------------
@tool
@traced_tool
def search_products(query: str) -> str:
    """Search for products in the catalog. Returns product IDs and names matching the query."""
    time.sleep(0.1)  # Simulate network delay so traces have visible duration
    q = query.lower()
    hits = [
        {"id": pid, "name": p["name"], "price": p["price"], "rating": p["rating"]}
        for pid, p in PRODUCTS.items()
        if q in p["name"].lower() or q in p["category"].lower() or q in p["specs"].lower()
    ]
    if not hits:
        return "No products found matching your search."
    out = f"Found {len(hits)} product(s):\n"
    for h in hits:
        out += f"- {h['name']} (ID: {h['id']}) - ${h['price']} - ⭐{h['rating']}/5\n"
    return out


@tool
@traced_tool
def get_product_details(product_id: str) -> str:
    """Get detailed information about a specific product including specs, reviews, and pricing."""
    time.sleep(0.15)
    if product_id not in PRODUCTS:
        return f"Product {product_id} not found."
    p = PRODUCTS[product_id]
    details = (
        f"Product: {p['name']} (ID: {product_id})\n"
        f"Price: ${p['price']}\n"
        f"Specifications: {p['specs']}\n"
        f"Rating: ⭐{p['rating']}/5 ({p['reviews_count']} reviews)\n"
        f"Stock Status: {'✅ In Stock' if p['in_stock'] else '❌ Out of Stock'}"
    )
    if p["in_stock"]:
        details += f"\nAvailable Quantity: {p['stock_level']} units"
    return details


@tool
@traced_tool
def check_inventory(product_id: str, location: str = "default") -> str:
    """Check inventory levels for a product at a specific location."""
    time.sleep(0.08)
    if product_id not in PRODUCTS:
        return f"Product {product_id} not found."
    p = PRODUCTS[product_id]
    if not p["in_stock"]:
        return f"{p['name']} is currently out of stock at {location}. Expected restock: 2-3 weeks."
    local_stock = max(0, p["stock_level"] + random.randint(-3, 5))
    if local_stock == 0:
        return f"{p['name']} is out of stock at {location}, but available at other locations."
    if local_stock < 5:
        return f"⚠️ Low stock at {location}: Only {local_stock} units remaining!"
    return f"✅ {p['name']} is in stock at {location}: {local_stock} units available."


@tool
@traced_tool
def compare_products(product_ids: str) -> str:
    """Compare multiple products side-by-side. Provide comma-separated product IDs."""
    time.sleep(0.2)
    ids = [pid.strip() for pid in product_ids.split(",")]
    items = []
    for pid in ids:
        if pid in PRODUCTS:
            items.append(PRODUCTS[pid])
        else:
            return f"Product {pid} not found. Please check the ID."
    if len(items) < 2:
        return "Need at least 2 valid product IDs to compare."

    out = "📊 Product Comparison:\n\n"
    for p in items:
        out += (
            f"**{p['name']}**\n"
            f"  Price: ${p['price']}\n"
            f"  Specs: {p['specs']}\n"
            f"  Rating: ⭐{p['rating']}/5 ({p['reviews_count']} reviews)\n"
            f"  Stock: {'✅ Available' if p['in_stock'] else '❌ Out of Stock'}\n\n"
        )
    best_value = min(items, key=lambda p: p["price"] / p["rating"])
    highest_rated = max(items, key=lambda p: p["rating"])
    out += f"💡 Best Value: {best_value['name']}\n"
    out += f"🌟 Highest Rated: {highest_rated['name']}\n"
    return out


@tool
@traced_tool
def get_recommendations(product_id: str) -> str:
    """Get product recommendations based on a given product."""
    time.sleep(0.12)
    if product_id not in PRODUCTS:
        return f"Product {product_id} not found."
    p = PRODUCTS[product_id]
    recs = sorted(
        (x for pid, x in PRODUCTS.items() if x["category"] == p["category"] and pid != product_id),
        key=lambda x: x["rating"],
        reverse=True,
    )[:3]
    if not recs:
        return f"No recommendations available for {p['name']}."
    out = f"Customers who viewed {p['name']} also liked:\n\n"
    for r in recs:
        out += f"- {r['name']} - ${r['price']} - ⭐{r['rating']}/5\n"
    return out


@tool
@traced_tool
def calculate_shipping(product_id: str, zip_code: str) -> str:
    """Calculate shipping cost and estimated delivery time for a product to a zip code."""
    time.sleep(0.1)
    if product_id not in PRODUCTS:
        return f"Product {product_id} not found."
    p = PRODUCTS[product_id]
    if not p["in_stock"]:
        return f"{p['name']} is out of stock - cannot calculate shipping."

    first = int(zip_code[0]) if zip_code and zip_code[0].isdigit() else 5
    if first <= 3:
        cost, days = 5.99, "2-3"
    elif first <= 6:
        cost, days = 8.99, "3-5"
    else:
        cost, days = 12.99, "4-6"

    note = "✅ FREE SHIPPING (orders over $500)" if p["price"] > 500 else f"Standard Shipping: ${cost}"
    return (
        f"Shipping to {zip_code}:\n"
        f"{note}\n"
        f"Estimated Delivery: {days} business days\n"
        f"Product: {p['name']}"
    )


@tool
@traced_tool
def check_promotions(product_id: str) -> str:
    """Check for current promotions and deals on a product."""
    time.sleep(0.08)
    if product_id not in PRODUCTS:
        return f"Product {product_id} not found."
    p = PRODUCTS[product_id]
    if product_id in PROMOTIONS:
        promo = PROMOTIONS[product_id]
        final = p["price"] - promo["discount"]
        return (
            f"🎉 Active Promotion on {p['name']}!\n\n"
            f"{promo['description']}\n"
            f"Original Price: ${p['price']}\n"
            f"Discount: -${promo['discount']}\n"
            f"Final Price: ${final}\n\n"
            f"Promotion ends soon - order now!"
        )
    return f"No current promotions for {p['name']}. Check back during our seasonal sales!"


# ---------------------------------------------------------------------------
# LangGraph state and agent construction
# ---------------------------------------------------------------------------
class AgentState(MessagesState):
    messages: Annotated[list, add_messages]


def create_agent(session_id: str):
    """Compile a fresh LangGraph agent for a session.

    Uses Bedrock Claude via the standard ChatBedrock client. The default
    model is configurable via the MODEL_ID env var.
    """
    model_id = os.getenv("MODEL_ID", "us.anthropic.claude-sonnet-5")
    region = os.getenv("AWS_REGION", "us-east-1")

    # Newer Claude models (e.g. Sonnet 5) reject `temperature` with a
    # ValidationException, so it is not sent by default. Set the TEMPERATURE env
    # var only when pointing MODEL_ID at an older model that still supports it.
    model_kwargs = {"max_tokens": int(os.getenv("MAX_TOKENS", "2000"))}
    temperature = os.getenv("TEMPERATURE")
    if temperature:
        model_kwargs["temperature"] = float(temperature)

    model = ChatBedrock(
        model_id=model_id,
        region_name=region,
        model_kwargs=model_kwargs,
    )

    tools = [
        search_products, get_product_details, check_inventory,
        compare_products, get_recommendations, calculate_shipping, check_promotions,
    ]
    model_with_tools = model.bind_tools(tools)

    def should_continue(state: AgentState):
        last = state["messages"][-1]
        if hasattr(last, "tool_calls") and last.tool_calls:
            return "tools"
        return END

    def call_model(state: AgentState):
        with tracer.start_as_current_span("agent.call_model") as span:
            messages = state["messages"]
            span.set_attribute("message_count", len(messages))

            system_message = SystemMessage(content="""You are a helpful shopping assistant with access to product search and information tools.

Help customers find the right products by:
1. Searching for products based on their needs
2. Providing detailed product information
3. Checking inventory and availability
4. Comparing products when customers are deciding between options
5. Suggesting recommendations for similar products
6. Calculating shipping costs when relevant
7. Checking for promotions and deals

Be friendly, helpful, and proactive in using tools to give customers complete information.""")

            with tracer.start_as_current_span("bedrock.invoke") as bedrock_span:
                bedrock_span.set_attribute("model_id", model_id)
                response = model_with_tools.invoke([system_message] + messages)
                if hasattr(response, "tool_calls") and response.tool_calls:
                    bedrock_span.set_attribute("tool_calls_count", len(response.tool_calls))
                    bedrock_span.set_attribute(
                        "tool_names",
                        ",".join([tc.get("name", "") for tc in response.tool_calls]),
                    )
            return {"messages": [response]}

    def traced_tool_node(state: AgentState):
        with tracer.start_as_current_span("agent.tools_execution") as span:
            last = state["messages"][-1]
            if hasattr(last, "tool_calls") and last.tool_calls:
                span.set_attribute("tool_calls_count", len(last.tool_calls))
                for i, tc in enumerate(last.tool_calls):
                    span.set_attribute(f"tool_{i}_name", tc.get("name", "unknown"))
            result = ToolNode(tools).invoke(state)
            span.set_attribute("tool_results_count", len(result.get("messages", [])))
            return result

    workflow = StateGraph(AgentState)
    workflow.add_node("agent", call_model)
    workflow.add_node("tools", traced_tool_node)
    workflow.add_edge(START, "agent")
    workflow.add_conditional_edges("agent", should_continue, ["tools", END])
    workflow.add_edge("tools", "agent")
    return workflow.compile()


def get_or_create_agent(session_id: str):
    if session_id not in agent_sessions:
        agent_sessions[session_id] = create_agent(session_id)
    return agent_sessions[session_id]


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------
class PromptRequest(BaseModel):
    prompt: str = Field(..., description="The user's question or request")
    session_id: str = Field(default="default", description="Session identifier for conversation continuity")


class AgentResponse(BaseModel):
    response: str = Field(..., description="The agent's response")
    session_id: str = Field(..., description="Session identifier")


# ---------------------------------------------------------------------------
# Helper: pull a clean string out of the final LangChain message
# ---------------------------------------------------------------------------
def _extract_response_text(invoke_result) -> str:
    m = invoke_result["messages"][-1]
    if hasattr(m, "content"):
        c = m.content
        if isinstance(c, list):
            txt = ""
            for block in c:
                if isinstance(block, dict) and "text" in block:
                    txt += block["text"]
                elif hasattr(block, "text"):
                    txt += block.text
            return txt
        return str(c)
    return str(m)


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
async def health_check():
    return {"status": "healthy"}


@app.post("/chat", response_model=AgentResponse)
async def chat(request: PromptRequest):
    """Main chat endpoint.

    This single handler is where all three observability platforms light up:
      * AgentCore: session.id in OTEL baggage + span attribute; Traceloop
        emits LangGraph spans to the X-Ray OTLP endpoint.
      * LangSmith: auto-traced by the LangChain SDK (env-var driven).
      * Langfuse:  CallbackHandler captures the LangGraph tree; a manual
        "chat-turn" span gives evaluators a clean (input, output) target.
    """
    with tracer.start_as_current_span("chat_request") as span:
        try:
            # --- AgentCore: propagate session.id via OTEL baggage -----------
            ctx = baggage.set_baggage("session.id", request.session_id)
            token = attach(ctx)

            try:
                # Span attributes — both dotted and underscored, for max
                # compatibility across AgentCore console versions.
                span.set_attribute("session.id", request.session_id)
                span.set_attribute("session_id", request.session_id)
                span.set_attribute("prompt_length", len(request.prompt))

                if not request.prompt or len(request.prompt.strip()) == 0:
                    span.set_attribute("error", "empty_prompt")
                    raise HTTPException(status_code=400, detail="Prompt cannot be empty")

                with tracer.start_as_current_span("get_or_create_agent"):
                    agent = get_or_create_agent(request.session_id)

                initial_state = {"messages": [HumanMessage(content=request.prompt)]}

                with tracer.start_as_current_span("agent.graph_execution") as agent_span:
                    agent_span.set_attribute("prompt", request.prompt[:200])
                    agent_span.set_attribute("agent.type", "langgraph")
                    agent_span.set_attribute("agent.session_id", request.session_id)

                    # LangChain RunnableConfig that all three platforms consume:
                    #  * metadata.session.id -> Traceloop -> AgentCore spans
                    #  * callbacks           -> Langfuse CallbackHandler
                    #  * LangSmith picks everything up implicitly via env vars
                    invoke_config = {
                        "metadata": {"session.id": request.session_id},
                    }
                    if langfuse_handler is not None:
                        invoke_config["callbacks"] = [langfuse_handler]

                    # Wrap agent.invoke in an explicit Langfuse "chat-turn"
                    # span. Live evaluation rules can only target observations
                    # (spans), so we need a span whose input/output are the
                    # clean user prompt and final assistant text — without
                    # this, evaluators read mid-loop LangGraph state.
                    if propagate_attributes is not None and langfuse_client is not None:
                        with propagate_attributes(
                            session_id=request.session_id,
                            trace_name="chat-request",
                        ):
                            with langfuse_client.start_as_current_observation(
                                as_type="span",
                                name="chat-turn",
                                input=request.prompt,
                            ) as turn_span:
                                result = agent.invoke(initial_state, config=invoke_config)
                                response_text = _extract_response_text(result)
                                turn_span.update(output=response_text)
                    else:
                        result = agent.invoke(initial_state, config=invoke_config)
                        response_text = _extract_response_text(result)

                    # Surface execution stats on the trace.
                    tool_call_count = 0
                    for msg in result["messages"]:
                        if hasattr(msg, "tool_calls") and msg.tool_calls:
                            tool_call_count += len(msg.tool_calls)
                    agent_span.set_attribute("message_count", len(result["messages"]))
                    agent_span.set_attribute("total_tool_calls", tool_call_count)
                    agent_span.set_attribute("execution.completed", True)

                span.set_attribute("response_length", len(response_text))
                span.set_attribute("success", True)

                # Force-flush so traces land promptly during demos.
                trace.get_tracer_provider().force_flush(timeout_millis=1000)

                return AgentResponse(response=response_text, session_id=request.session_id)
            finally:
                detach(token)

        except HTTPException:
            raise
        except Exception as e:
            span.set_attribute("error", True)
            span.set_attribute("error_message", str(e))
            raise HTTPException(status_code=500, detail=f"Agent execution failed: {str(e)}")


# ---------------------------------------------------------------------------
# Shutdown handling
# ---------------------------------------------------------------------------
@app.on_event("shutdown")
async def shutdown_event():
    """Flush in-flight spans/events from all three pipelines."""
    trace.get_tracer_provider().force_flush(timeout_millis=5000)
    trace.get_tracer_provider().shutdown()
    if langfuse_client is not None:
        try:
            langfuse_client.flush()
        except Exception as e:
            print(f"Langfuse flush on shutdown failed: {e}")


if __name__ == "__main__":
    import signal
    import sys

    def signal_handler(sig, frame):
        print("Shutting down gracefully...")
        trace.get_tracer_provider().force_flush(timeout_millis=5000)
        trace.get_tracer_provider().shutdown()
        if langfuse_client is not None:
            try:
                langfuse_client.flush()
            except Exception as e:
                print(f"Langfuse flush on signal failed: {e}")
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    port = int(os.getenv("PORT", "8000"))
    # Bind host is configurable via the HOST env var. The default is 0.0.0.0
    # because this app is designed to run as a container on EKS, where the
    # process must listen on all interfaces inside the pod's network
    # namespace for the Kubernetes Service (and liveness / readiness probes)
    # to reach it. Override with HOST=127.0.0.1 when running outside a
    # container if you want loopback-only.
    host = os.getenv("HOST", "0.0.0.0")  # nosec B104
    uvicorn.run(app, host=host, port=port)
