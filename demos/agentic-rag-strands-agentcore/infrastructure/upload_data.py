"""Upload markdown documents to S3 and trigger Knowledge Base sync.

Uploads all markdown files from data/knowledge_docs/ to the configured
S3 bucket and starts a data ingestion job on the Knowledge Base.

Usage:
    python infrastructure/upload_data.py
"""

import os
import glob
import time
import boto3
from dotenv import load_dotenv

load_dotenv()

REGION = os.getenv("AWS_REGION", "us-east-1")
BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "agentic-rag-demo-docs")
KNOWLEDGE_BASE_ID = os.getenv("KNOWLEDGE_BASE_ID")
DOCS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "knowledge_docs")


def upload_documents():
    """Upload all markdown files to S3."""
    s3_client = boto3.client("s3", region_name=REGION)

    md_files = glob.glob(os.path.join(DOCS_DIR, "*.md"))

    if not md_files:
        print(f"No markdown files found in {DOCS_DIR}")
        return []

    print(f"Uploading {len(md_files)} documents to s3://{BUCKET_NAME}/knowledge_docs/\n")

    uploaded = []
    for filepath in sorted(md_files):
        filename = os.path.basename(filepath)
        s3_key = f"knowledge_docs/{filename}"

        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()

        s3_client.put_object(
            Bucket=BUCKET_NAME,
            Key=s3_key,
            Body=content.encode("utf-8"),
            ContentType="text/markdown",
        )

        file_size = len(content)
        print(f"  ✓ {filename} ({file_size:,} bytes)")
        uploaded.append(s3_key)

    print(f"\n  Total: {len(uploaded)} files uploaded")
    return uploaded


def start_ingestion():
    """Start the Knowledge Base ingestion job."""
    if not KNOWLEDGE_BASE_ID:
        print("\n⚠️  KNOWLEDGE_BASE_ID not set. Skipping ingestion.")
        print("   Set it in .env and re-run to trigger sync.")
        return

    bedrock_agent_client = boto3.client("bedrock-agent", region_name=REGION)

    # Find the data source
    data_sources = bedrock_agent_client.list_data_sources(
        knowledgeBaseId=KNOWLEDGE_BASE_ID
    )

    if not data_sources.get("dataSourceSummaries"):
        print("\n⚠️  No data sources found for the Knowledge Base.")
        return

    ds_id = data_sources["dataSourceSummaries"][0]["dataSourceId"]

    print(f"\nStarting ingestion job for data source: {ds_id}")

    response = bedrock_agent_client.start_ingestion_job(
        knowledgeBaseId=KNOWLEDGE_BASE_ID,
        dataSourceId=ds_id,
    )

    job_id = response["ingestionJob"]["ingestionJobId"]
    print(f"  Ingestion job started: {job_id}")
    print("  ⏳ Waiting for ingestion to complete...")

    # Poll for completion
    while True:
        status_response = bedrock_agent_client.get_ingestion_job(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            dataSourceId=ds_id,
            ingestionJobId=job_id,
        )

        status = status_response["ingestionJob"]["status"]
        if status == "COMPLETE":
            stats = status_response["ingestionJob"].get("statistics", {})
            print(f"\n  ✓ Ingestion complete!")
            print(f"    Documents scanned: {stats.get('numberOfDocumentsScanned', 'N/A')}")
            print(f"    Documents indexed: {stats.get('numberOfNewDocumentsIndexed', 'N/A')}")
            print(f"    Documents modified: {stats.get('numberOfModifiedDocumentsIndexed', 'N/A')}")
            break
        elif status == "FAILED":
            failure = status_response["ingestionJob"].get("failureReasons", ["Unknown"])
            print(f"\n  ✗ Ingestion failed: {failure}")
            break
        else:
            print(f"    Status: {status}...")
            time.sleep(10)


def main():
    """Upload documents and trigger ingestion."""
    print("\n" + "=" * 60)
    print("  AGENTIC RAG - DOCUMENT UPLOAD & INGESTION")
    print("=" * 60 + "\n")

    # Upload documents
    uploaded = upload_documents()

    if uploaded:
        # Start ingestion
        start_ingestion()

    print("\n" + "=" * 60)
    print("  DONE! Your knowledge base is ready for queries.")
    print("=" * 60)


if __name__ == "__main__":
    main()
