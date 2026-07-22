#!/bin/bash
# Deploy the Agentic RAG Agent to Amazon Bedrock AgentCore Runtime
#
# Prerequisites:
#   - AgentCore CLI installed: npm install -g @aws/agentcore
#   - AWS credentials configured
#   - Knowledge Base already set up (run infrastructure/setup_knowledge_base.py first)
#
# Usage:
#   chmod +x deploy/deploy.sh
#   ./deploy/deploy.sh

set -e

echo "============================================"
echo "  Deploying Agentic RAG Agent to AgentCore"
echo "============================================"
echo ""

# Check prerequisites
if ! command -v agentcore &> /dev/null; then
    echo "❌ AgentCore CLI not found. Install with:"
    echo "   npm install -g @aws/agentcore"
    exit 1
fi

if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3.10+ required"
    exit 1
fi

# Load environment variables
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
fi

echo "📋 Configuration:"
echo "   Region:           ${AWS_REGION:-us-east-1}"
echo "   Model:            ${INFERENCE_MODEL_ID:-mistral.mistral-large-3-675b-instruct}"
echo "   Knowledge Base:   ${KNOWLEDGE_BASE_ID:-not-set}"
echo ""

# Initialize the AgentCore project if not already done
if [ ! -f "agentcore.json" ]; then
    echo "🔧 Initializing AgentCore project..."
    agentcore init \
        --name "novatech-rag-agent" \
        --framework strands \
        --runtime python3.12
fi

echo "🚀 Deploying agent..."
agentcore deploy \
    --entry-point deploy/agentcore_handler.py \
    --handler handler

echo ""
echo "============================================"
echo "  ✓ Deployment Complete!"
echo "============================================"
echo ""
echo "To invoke your agent:"
echo "  agentcore invoke --input 'What is NovaTech Solutions?'"
echo ""
echo "To view logs:"
echo "  agentcore logs --follow"
echo ""
echo "To check status:"
echo "  agentcore status"
