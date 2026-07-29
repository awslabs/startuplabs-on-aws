import os
from strands.models.bedrock import BedrockModel


def load_model() -> BedrockModel:
    """Get Bedrock model client using IAM credentials.

    Uses Mistral Large 3 as the inference model for the RAG agent.
    Inference runs in the runtime's own region (AWS_REGION), where the model
    is available. Knowledge Base retrieval targets KNOWLEDGE_BASE_REGION separately.
    """
    model_id = os.getenv("INFERENCE_MODEL_ID", "mistral.mistral-large-3-675b-instruct")
    return BedrockModel(model_id=model_id)
