"""Amazon Bedrock AgentCore Runtime entry point.

This module wraps the RAG agent for deployment on AgentCore Runtime.
AgentCore handles HTTP serving, scaling, and security automatically.

Deployment:
    agentcore deploy
"""

import os
import sys

# Add project root to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from strands import Agent
from strands.models.bedrock import BedrockModel

from agent.prompts import RAG_SYSTEM_PROMPT
from agent.tools import retrieve_from_kb


def build_agent() -> Agent:
    """Build the RAG agent for AgentCore deployment.

    This factory function is called by AgentCore Runtime to create
    the agent instance. AgentCore manages the HTTP layer and scaling.

    Returns:
        Configured Strands Agent ready to handle requests.
    """
    model_id = os.getenv("INFERENCE_MODEL_ID", "mistral.mistral-large-3-675b-instruct")
    region = os.getenv("AWS_REGION", "us-east-1")

    model = BedrockModel(
        model_id=model_id,
        region_name=region,
    )

    agent = Agent(
        model=model,
        system_prompt=RAG_SYSTEM_PROMPT,
        tools=[retrieve_from_kb],
        callback_handler=None,
    )

    return agent


# AgentCore expects a module-level agent or factory
agent = build_agent()


def handler(event: dict) -> dict:
    """Handle incoming requests from AgentCore Runtime.

    AgentCore invokes this handler for each request. The event contains
    the user's message, and we return the agent's response.

    Args:
        event: Request payload with 'input' or 'messages' field.

    Returns:
        Response dict with 'output' field containing the agent's answer.
    """
    # Extract input from event
    user_input = event.get("input") or event.get("prompt", "")

    if not user_input:
        messages = event.get("messages", [])
        if messages:
            user_input = messages[-1].get("content", "")

    if not user_input:
        return {
            "output": "Please provide a question about NovaTech products.",
            "status": "error",
            "error": "No input provided",
        }

    try:
        response = agent(user_input)
        return {
            "output": str(response),
            "status": "success",
        }
    except Exception as e:
        return {
            "output": f"I encountered an error processing your request: {str(e)}",
            "status": "error",
            "error": str(e),
        }
