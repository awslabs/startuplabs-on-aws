"""Custom tools for the Agentic RAG agent."""

import os
import boto3
from strands import tool


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
        return "Error: KNOWLEDGE_BASE_ID environment variable not set. Please configure the knowledge base."

    region = os.getenv("KNOWLEDGE_BASE_REGION", os.getenv("AWS_REGION", "us-east-1"))
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
                    # matching. This surfaces sections containing literal query terms
                    # (e.g. "webhook events", "Enterprise plan") even when a document's
                    # overview chunk scores higher on pure vector similarity.
                    "overrideSearchType": "HYBRID",
                }
            },
        )

        results = response.get("retrievalResults", [])

        if not results:
            return f"No relevant documents found for query: '{query}'. Try rephrasing your question."

        formatted_results = []
        for i, result in enumerate(results, 1):
            content = result.get("content", {}).get("text", "No content available")
            score = result.get("score", 0.0)
            location = result.get("location", {})

            # Extract source info
            source = "Unknown source"
            if location.get("type") == "S3":
                s3_uri = location.get("s3Location", {}).get("uri", "")
                source = s3_uri.split("/")[-1] if s3_uri else "S3 document"

            formatted_results.append(
                f"### Result {i} (Score: {score:.3f})\n"
                f"**Source:** {source}\n\n"
                f"{content}\n"
            )

        header = f"## Retrieved {len(results)} relevant passages for: \"{query}\"\n\n"
        return header + "\n---\n\n".join(formatted_results)

    except Exception as e:
        return f"Error retrieving from knowledge base: {str(e)}"
