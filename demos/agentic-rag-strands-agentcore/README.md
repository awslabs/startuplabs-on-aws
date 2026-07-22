# Agentic RAG Demo

An agentic Retrieval-Augmented Generation (RAG) demo built with **Strands Agents SDK**, deployed on **Amazon Bedrock AgentCore**, with robust evaluations and observability.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Streamlit UI                             │
├─────────────────────────────────────────────────────────────┤
│                  Strands Agent (RAG)                         │
│  ┌───────────────┐  ┌──────────────┐  ┌─────────────────┐  │
│  │ Retrieve Tool │  │  Use LLM     │  │  OTel Tracing   │  │
│  └───────┬───────┘  └──────────────┘  └─────────────────┘  │
│          │                                                   │
│  ┌───────▼───────────────────────────────────────────────┐  │
│  │       Amazon Bedrock Knowledge Base                    │  │
│  │  (Titan Embeddings V2 + OpenSearch Serverless)         │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                              │
│  Inference: Mistral Large (via Bedrock)                      │
├─────────────────────────────────────────────────────────────┤
│              Amazon Bedrock AgentCore Runtime                 │
│        (Auto-scaling, built-in security, OTEL)               │
└─────────────────────────────────────────────────────────────┘
```

## Components

| Component | Technology |
|-----------|-----------|
| Agent Framework | Strands Agents SDK |
| Inference Model | Mistral Large 3 (via Bedrock) |
| Embeddings | Amazon Titan Embeddings V2 |
| Vector Store | Amazon OpenSearch Serverless |
| Knowledge Base | Amazon Bedrock Knowledge Bases |
| Evaluations | Strands Agents Evals SDK |
| Observability | OpenTelemetry (built-in Strands tracing) |
| Hosting | Amazon Bedrock AgentCore Runtime |
| UI | Streamlit |

## Project Structure

```
agentic-rag-demo/
├── README.md
├── requirements.txt
├── pyproject.toml
├── .env.example
├── infrastructure/
│   ├── setup_knowledge_base.py      # KB + vector store provisioning
│   └── upload_data.py               # Upload markdown docs to S3
├── data/
│   └── knowledge_docs/              # Synthetic markdown knowledge docs
├── agent/
│   ├── __init__.py
│   ├── rag_agent.py                 # Core RAG agent definition
│   ├── tools.py                     # Custom tools (retrieve, summarize)
│   ├── prompts.py                   # System prompts
│   └── observability.py             # OpenTelemetry setup
├── evaluations/
│   ├── __init__.py
│   ├── eval_output.py               # Output quality evaluations
│   ├── eval_trajectory.py           # Tool usage trajectory evaluations
│   ├── eval_retrieval.py            # Retrieval relevance evaluations
│   ├── eval_helpfulness.py          # Helpfulness evaluations
│   ├── run_all_evals.py             # Run complete eval suite
│   └── test_cases.py                # Eval test cases
├── ui/
│   └── app.py                       # Streamlit chat interface
└── deploy/
    ├── agentcore_handler.py         # AgentCore entry point
    └── deploy.sh                    # Deployment script
```

## Quick Start

### Prerequisites

- Python 3.10+
- AWS account with Bedrock access (Mistral Large, Titan Embeddings)
- AWS CLI configured with appropriate credentials
- Node.js 20+ (for AgentCore CLI)

### 1. Install Dependencies

```bash
cd agentic-rag-demo
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
cp .env.example .env
```

Then edit `.env`. This project intentionally hardcodes **no** account IDs, ARNs,
or resource IDs — you supply them via environment variables after provisioning:

| Variable | Required | Set it to |
|----------|----------|-----------|
| `AWS_REGION` | yes | e.g. `us-east-1` |
| `S3_BUCKET_NAME` | yes | a globally-unique bucket name |
| `KNOWLEDGE_BASE_ID` | after KB setup | output of `setup_knowledge_base.py` |
| `AGENTCORE_MEMORY_ID` | for memory | output of `setup_memory.py` |
| `AGENTCORE_RUNTIME_ARN` | after deploy | from `agentcore status` |
| `INFERENCE_MODEL_ID` | optional | defaults to Mistral Large 3 |

**Security note (OpenSearch Serverless network access):** `setup_knowledge_base.py`
creates a **non-public (VPC-scoped)** collection network policy by default. Provide
VPC endpoint IDs via `OPENSEARCH_VPC_ENDPOINTS` for production. For quick local
experimentation only, you may set `OPENSEARCH_ALLOW_PUBLIC=true` to allow public
access — this is **not** recommended and prints a warning.

### 3. Set Up Knowledge Base (first time)

```bash
python infrastructure/setup_knowledge_base.py
python infrastructure/upload_data.py
```

### 4. Run Locally

```bash
streamlit run ui/app.py
```

### 5. Run Evaluations

```bash
python evaluations/run_all_evals.py
```

### 6. Deploy to AgentCore

```bash
npm install -g @aws/agentcore
agentcore deploy
```

## Embeddings Note

This project uses **Amazon Titan Embeddings V2** (`amazon.titan-embed-text-v2:0`) as the embeddings model for the Knowledge Base. OpenAI's Ada model is not natively supported as an embedding provider within Bedrock Knowledge Bases, so we use Titan which provides excellent multilingual embedding quality at 1024 dimensions.

## Observability

The agent emits OpenTelemetry-compliant spans automatically via Strands' built-in telemetry:
- Agent invocations
- Model inference calls (latency, token usage)
- Tool executions (retrieve calls, parameters, results)
- Event loop cycles

Traces can be exported to CloudWatch, Jaeger, or any OTEL-compatible backend.
