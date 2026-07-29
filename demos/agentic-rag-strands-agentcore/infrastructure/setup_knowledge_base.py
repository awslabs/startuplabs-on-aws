"""Set up Amazon Bedrock Knowledge Base with Titan Embeddings and OpenSearch Serverless.

This script provisions:
1. An S3 bucket for storing documents
2. An OpenSearch Serverless collection for vector storage
3. A Bedrock Knowledge Base configured with Titan Embeddings V2

Prerequisites:
- AWS credentials configured with appropriate permissions
- Bedrock model access enabled for amazon.titan-embed-text-v2:0

Usage:
    python infrastructure/setup_knowledge_base.py
"""

import os
import json
import time
import boto3
from dotenv import load_dotenv

load_dotenv()

REGION = os.getenv("AWS_REGION", "us-east-1")
EMBEDDING_MODEL_ID = os.getenv("EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0")
KB_NAME = "novatech-knowledge-base"
COLLECTION_NAME = "novatech-vectors"
INDEX_NAME = "novatech-docs-index"


def get_account_id():
    """Get the current AWS account ID."""
    sts = boto3.client("sts", region_name=REGION)
    return sts.get_caller_identity()["Account"]


def resolve_bucket_name(account_id):
    """Resolve the S3 bucket name.

    S3 bucket names are globally unique, so a fixed default would collide across
    accounts. Prefer an explicit S3_BUCKET_NAME; otherwise derive a
    per-account-unique name by suffixing the account ID.
    """
    explicit = os.getenv("S3_BUCKET_NAME")
    if explicit:
        return explicit
    return f"agentic-rag-demo-docs-{account_id}"


def create_s3_bucket(s3_client, bucket_name):
    """Create the S3 bucket for document storage."""
    print(f"Creating S3 bucket: {bucket_name}")
    try:
        if REGION == "us-east-1":
            s3_client.create_bucket(Bucket=bucket_name)
        else:
            s3_client.create_bucket(
                Bucket=bucket_name,
                CreateBucketConfiguration={"LocationConstraint": REGION},
            )
        print(f"  ✓ Bucket created: {bucket_name}")
    except s3_client.exceptions.BucketAlreadyOwnedByYou:
        print(f"  ✓ Bucket already exists: {bucket_name}")
    except Exception as e:
        print(f"  ✗ Error creating bucket: {e}")
        raise


def create_kb_execution_role(iam_client, account_id, bucket_name, collection_id):
    """Create IAM role for the Knowledge Base."""
    role_name = "AgenticRAGKnowledgeBaseRole"
    print(f"Creating IAM role: {role_name}")

    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": account_id},
                },
            }
        ],
    }

    try:
        response = iam_client.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description="Role for Agentic RAG Knowledge Base",
        )
        role_arn = response["Role"]["Arn"]
        print(f"  ✓ Role created: {role_arn}")
    except iam_client.exceptions.EntityAlreadyExistsException:
        role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
        print(f"  ✓ Role already exists: {role_arn}")

    # Attach policies for S3 access and Bedrock model invocation
    inline_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:ListBucket"],
                "Resource": [
                    f"arn:aws:s3:::{bucket_name}",
                    f"arn:aws:s3:::{bucket_name}/*",
                ],
            },
            {
                "Effect": "Allow",
                "Action": ["bedrock:InvokeModel"],
                "Resource": [
                    f"arn:aws:bedrock:{REGION}::foundation-model/{EMBEDDING_MODEL_ID}"
                ],
            },
            {
                "Effect": "Allow",
                "Action": ["aoss:APIAccessAll"],
                # Scoped to this specific collection (by ARN), not all collections.
                "Resource": [
                    f"arn:aws:aoss:{REGION}:{account_id}:collection/{collection_id}"
                ],
            },
        ],
    }

    iam_client.put_role_policy(
        RoleName=role_name,
        PolicyName="KnowledgeBasePolicy",
        PolicyDocument=json.dumps(inline_policy),
    )
    print("  ✓ Policies attached")

    # Wait for role propagation
    time.sleep(10)
    return role_arn


def create_opensearch_collection(aoss_client, account_id):
    """Create an OpenSearch Serverless collection for vector storage."""
    print(f"Creating OpenSearch Serverless collection: {COLLECTION_NAME}")

    # Create encryption policy
    encryption_policy = json.dumps(
        {
            "Rules": [{"ResourceType": "collection", "Resource": [f"collection/{COLLECTION_NAME}"]}],
            "AWSOwnedKey": True,
        }
    )

    try:
        aoss_client.create_security_policy(
            name=f"{COLLECTION_NAME}-enc",
            type="encryption",
            policy=encryption_policy,
        )
    except Exception as e:
        print(f"  Note: encryption policy not created (may already exist): {e}")

    # Network policy.
    #
    # SECURITY: By default this uses a VPC-scoped policy (AllowFromPublic=False).
    # For production, provide VPC endpoint ID(s) via OPENSEARCH_VPC_ENDPOINTS so the
    # collection is reachable only from within your VPC. Public access is available
    # only as an explicit opt-in for quick local experimentation and is NOT
    # recommended — set OPENSEARCH_ALLOW_PUBLIC=true to enable it knowingly.
    allow_public = os.getenv("OPENSEARCH_ALLOW_PUBLIC", "false").lower() == "true"
    vpc_endpoints = [
        e.strip()
        for e in os.getenv("OPENSEARCH_VPC_ENDPOINTS", "").split(",")
        if e.strip()
    ]

    network_rule = {
        "Rules": [
            {"ResourceType": "collection", "Resource": [f"collection/{COLLECTION_NAME}"]},
            {"ResourceType": "dashboard", "Resource": [f"collection/{COLLECTION_NAME}"]},
        ],
    }
    if allow_public:
        print("  ⚠️  WARNING: creating a PUBLIC network policy for the collection. "
              "Do not use this for production.")
        network_rule["AllowFromPublic"] = True
    else:
        network_rule["AllowFromPublic"] = False
        if vpc_endpoints:
            network_rule["SourceVPCEs"] = vpc_endpoints
        else:
            print("  Note: network policy is VPC-scoped (non-public) but no VPC "
                  "endpoints were provided via OPENSEARCH_VPC_ENDPOINTS. Set that "
                  "env var (or OPENSEARCH_ALLOW_PUBLIC=true for local testing) so "
                  "the collection is reachable.")

    network_policy = json.dumps([network_rule])

    try:
        aoss_client.create_security_policy(
            name=f"{COLLECTION_NAME}-net",
            type="network",
            policy=network_policy,
        )
    except Exception as e:
        print(f"  Note: network policy not created (may already exist): {e}")

    # Create data access policy
    data_access_policy = json.dumps(
        [
            {
                "Rules": [
                    {
                        "ResourceType": "collection",
                        "Resource": [f"collection/{COLLECTION_NAME}"],
                        "Permission": [
                            "aoss:CreateCollectionItems",
                            "aoss:DeleteCollectionItems",
                            "aoss:UpdateCollectionItems",
                            "aoss:DescribeCollectionItems",
                        ],
                    },
                    {
                        "ResourceType": "index",
                        "Resource": [f"index/{COLLECTION_NAME}/*"],
                        "Permission": [
                            "aoss:CreateIndex",
                            "aoss:DeleteIndex",
                            "aoss:UpdateIndex",
                            "aoss:DescribeIndex",
                            "aoss:ReadDocument",
                            "aoss:WriteDocument",
                        ],
                    },
                ],
                "Principal": [
                    f"arn:aws:iam::{account_id}:root",
                    f"arn:aws:iam::{account_id}:role/AgenticRAGKnowledgeBaseRole",
                ],
            }
        ]
    )

    try:
        aoss_client.create_access_policy(
            name=f"{COLLECTION_NAME}-access",
            type="data",
            policy=data_access_policy,
        )
    except Exception as e:
        print(f"  Note: data access policy not created (may already exist): {e}")

    # Create the collection
    try:
        response = aoss_client.create_collection(
            name=COLLECTION_NAME,
            type="VECTORSEARCH",
            description="Vector store for NovaTech knowledge base",
        )
        collection_id = response["createCollectionDetail"]["id"]
        print(f"  ✓ Collection created: {collection_id}")

        # Wait for collection to become active
        print("  ⏳ Waiting for collection to become ACTIVE...")
        while True:
            status = aoss_client.batch_get_collection(ids=[collection_id])
            state = status["collectionDetails"][0]["status"]
            if state == "ACTIVE":
                break
            time.sleep(15)
            print(f"    Status: {state}...")

        endpoint = status["collectionDetails"][0]["collectionEndpoint"]
        print(f"  ✓ Collection active: {endpoint}")
        return collection_id, endpoint

    except Exception as e:
        if "ConflictException" in str(type(e).__name__) or "already exists" in str(e):
            # Collection exists, get its details
            collections = aoss_client.list_collections(
                collectionFilters={"name": COLLECTION_NAME}
            )
            if collections["collectionSummaries"]:
                cid = collections["collectionSummaries"][0]["id"]
                details = aoss_client.batch_get_collection(ids=[cid])
                endpoint = details["collectionDetails"][0]["collectionEndpoint"]
                print(f"  ✓ Collection already exists: {endpoint}")
                return cid, endpoint
        raise


def create_knowledge_base(bedrock_agent_client, role_arn, collection_arn):
    """Create the Bedrock Knowledge Base."""
    print(f"Creating Knowledge Base: {KB_NAME}")

    try:
        response = bedrock_agent_client.create_knowledge_base(
            name=KB_NAME,
            description="NovaTech Solutions documentation knowledge base for RAG demo",
            roleArn=role_arn,
            knowledgeBaseConfiguration={
                "type": "VECTOR",
                "vectorKnowledgeBaseConfiguration": {
                    "embeddingModelArn": f"arn:aws:bedrock:{REGION}::foundation-model/{EMBEDDING_MODEL_ID}",
                    "embeddingModelConfiguration": {
                        "bedrockEmbeddingModelConfiguration": {"dimensions": 1024}
                    },
                },
            },
            storageConfiguration={
                "type": "OPENSEARCH_SERVERLESS",
                "opensearchServerlessConfiguration": {
                    "collectionArn": collection_arn,
                    "vectorIndexName": INDEX_NAME,
                    "fieldMapping": {
                        "vectorField": "embedding",
                        "textField": "text",
                        "metadataField": "metadata",
                    },
                },
            },
        )

        kb_id = response["knowledgeBase"]["knowledgeBaseId"]
        print(f"  ✓ Knowledge Base created: {kb_id}")
        return kb_id

    except Exception as e:
        if "already exists" in str(e).lower() or "ConflictException" in str(type(e).__name__):
            # Try to find existing KB
            kbs = bedrock_agent_client.list_knowledge_bases()
            for kb in kbs.get("knowledgeBaseSummaries", []):
                if kb["name"] == KB_NAME:
                    kb_id = kb["knowledgeBaseId"]
                    print(f"  ✓ Knowledge Base already exists: {kb_id}")
                    return kb_id
        raise


def create_data_source(bedrock_agent_client, kb_id, bucket_name):
    """Create an S3 data source for the Knowledge Base."""
    print("Creating data source...")

    try:
        response = bedrock_agent_client.create_data_source(
            knowledgeBaseId=kb_id,
            name="novatech-docs-s3",
            description="NovaTech markdown documentation from S3",
            dataSourceConfiguration={
                "type": "S3",
                "s3Configuration": {
                    "bucketArn": f"arn:aws:s3:::{bucket_name}",
                    "inclusionPrefixes": ["knowledge_docs/"],
                },
            },
            vectorIngestionConfiguration={
                "chunkingConfiguration": {
                    "chunkingStrategy": "FIXED_SIZE",
                    "fixedSizeChunkingConfiguration": {
                        "maxTokens": 512,
                        "overlapPercentage": 20,
                    },
                }
            },
        )
        ds_id = response["dataSource"]["dataSourceId"]
        print(f"  ✓ Data source created: {ds_id}")
        return ds_id
    except Exception as e:
        print(f"  ✗ Error: {e}")
        raise


def main():
    """Run the complete Knowledge Base setup."""
    print("\n" + "=" * 60)
    print("  AGENTIC RAG - KNOWLEDGE BASE SETUP")
    print("=" * 60 + "\n")

    account_id = get_account_id()
    bucket_name = resolve_bucket_name(account_id)
    print(f"Account ID: {account_id}")
    print(f"Region: {REGION}")
    print(f"S3 Bucket: {bucket_name}\n")

    # Initialize clients
    s3_client = boto3.client("s3", region_name=REGION)
    iam_client = boto3.client("iam", region_name=REGION)
    aoss_client = boto3.client("opensearchserverless", region_name=REGION)
    bedrock_agent_client = boto3.client("bedrock-agent", region_name=REGION)

    # Step 1: Create S3 bucket
    create_s3_bucket(s3_client, bucket_name)

    # Step 2: Create OpenSearch Serverless collection first, so the IAM role can
    # be scoped to this specific collection's ARN.
    collection_id, endpoint = create_opensearch_collection(aoss_client, account_id)
    collection_arn = f"arn:aws:aoss:{REGION}:{account_id}:collection/{collection_id}"

    # Step 3: Create IAM role scoped to the actual bucket and collection
    role_arn = create_kb_execution_role(iam_client, account_id, bucket_name, collection_id)

    # Step 4: Create Knowledge Base
    kb_id = create_knowledge_base(bedrock_agent_client, role_arn, collection_arn)

    # Step 5: Create data source
    ds_id = create_data_source(bedrock_agent_client, kb_id, bucket_name)

    # Output configuration
    print("\n" + "=" * 60)
    print("  SETUP COMPLETE!")
    print("=" * 60)
    print(f"\n  Knowledge Base ID: {kb_id}")
    print(f"  Data Source ID:    {ds_id}")
    print(f"  S3 Bucket:         {bucket_name}")
    print(f"  Collection:        {endpoint}")
    print(f"\n  Update your .env file:")
    print(f"    KNOWLEDGE_BASE_ID={kb_id}")
    print(f"    S3_BUCKET_NAME={bucket_name}")
    print("\n  Next step: Run 'python infrastructure/upload_data.py' to upload documents")


if __name__ == "__main__":
    main()
