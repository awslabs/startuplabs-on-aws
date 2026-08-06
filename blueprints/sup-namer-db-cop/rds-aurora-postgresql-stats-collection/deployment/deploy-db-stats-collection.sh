#!/bin/bash

# Deploy customer data collection CloudFormation stack for GenAI WAL Review

set -e

# Default values
STACK_NAME="wal-db-stats-collection"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_FILE="$SCRIPT_DIR/../cfn/db-stats-collection.yaml"
REGION="us-east-1"
INSTANCE_TYPE="t3.medium"
ENABLE_SCHEDULED="true"
SCHEDULE="0 6 * * *"
CODE_KEY="wal-db-stats-collection.zip"

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --stack-name)
            STACK_NAME="$2"
            shift 2
            ;;
        --region)
            REGION="$2"
            shift 2
            ;;
        --key-pair)
            KEY_PAIR="$2"
            shift 2
            ;;
        --vpc-id)
            VPC_ID="$2"
            shift 2
            ;;
        --subnet-id)
            SUBNET_ID="$2"
            shift 2
            ;;
        --instance-type)
            INSTANCE_TYPE="$2"
            shift 2
            ;;
        --allowed-cidr)
            ALLOWED_CIDR="$2"
            shift 2
            ;;
        --db-port)
            DB_PORT="$2"
            shift 2
            ;;
        --no-public-ip)
            ASSIGN_PUBLIC_IP="false"
            shift
            ;;
        --create-ssm-endpoints)
            CREATE_SSM_ENDPOINTS="true"
            shift
            ;;
        --airgapped)
            AIRGAPPED="true"
            shift
            ;;
        --route-table-ids)
            ROUTE_TABLE_IDS="$2"
            shift 2
            ;;
        --sa-data-bucket)
            SA_DATA_BUCKET="$2"
            shift 2
            ;;
        --enable-scheduled)
            ENABLE_SCHEDULED="$2"
            shift 2
            ;;
        --schedule)
            SCHEDULE="$2"
            shift 2
            ;;
        --help)
            echo "Usage: $0 [OPTIONS]"
            echo "Deploy customer data collection environment for GenAI WAL Review"
            echo ""
            echo "Options:"
            echo "  --stack-name STACK_NAME         CloudFormation stack name (default: wal-db-stats-collection)"
            echo "  --region REGION                 AWS region (default: us-east-1)"
            echo "  --key-pair KEY_PAIR             EC2 Key Pair name (required)"
            echo "  --vpc-id VPC_ID                 VPC ID (required)"
            echo "  --subnet-id SUBNET_ID           Subnet ID (public subnet recommended)"
            echo "  --instance-type TYPE            Instance type (default: t3.medium)"
            echo "  --allowed-cidr CIDR             Allowed CIDR for SSH (required when using public IP; e.g. \$(curl -s ifconfig.me)/32 — 0.0.0.0/0 is rejected)
  --no-public-ip                    Deploy without public IP (private subnet + SSM access; --allowed-cidr not required)
  --create-ssm-endpoints           Create VPC Interface Endpoints for SSM/SSMMessages/EC2Messages (~$0.03/hr).
                                   Use when your VPC does not already have these endpoints.
                                   If your VPC already has them, omit this flag — the deploy script reuses them.
  --db-port PORT                  PostgreSQL port on target RDS/Aurora endpoint for invasive collection (default: 5432)"
            echo "  --sa-data-bucket BUCKET         S3 bucket name for SA data sharing (optional)"
            echo "  --enable-scheduled true/false   Enable scheduled data collection (default: true)"
            echo "  --schedule 'CRON'               Cron schedule for data collection (default: '0 6 * * *')"
            echo "  --help                          Show this help message"
            echo ""
            echo "Customer Data Collection Workflow:"
            echo "  1. Customer deploys data collection environment"
            echo "  2. Customer runs fleet discovery and data collection"
            echo "  3. Customer shares data with SA via S3 bucket"
            echo "  4. SA runs GenAI analysis in separate environment"
            echo "  5. SA provides comprehensive reports back to customer"
            echo ""
            echo "Prerequisites:"
            echo "  - AWS CLI configured with RDS/CloudWatch/PI permissions"
            echo "  - VPC with public subnet for EC2 instance"
            echo "  - EC2 Key Pair for SSH access"
            echo "  - PostgreSQL databases in AWS account"
            echo ""
            echo "Example:"
            echo "  $0 --key-pair my-keypair --vpc-id vpc-12345 --subnet-id subnet-67890 --allowed-cidr \$(curl -s ifconfig.me)/32"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Validate required parameters
# --key-pair is only required when using a public IP (SSH access).
# When --no-public-ip is set, SSM Session Manager is used instead — no key pair needed.
if [[ -z "$KEY_PAIR" ]] && [[ "${ASSIGN_PUBLIC_IP:-true}" == "true" ]]; then
    echo "❌ Error: --key-pair is required when using a public IP"
    echo "   Use --no-public-ip to deploy without a key pair (SSM Session Manager access)"
    exit 1
fi

if [[ -z "$VPC_ID" ]]; then
    echo "❌ Error: --vpc-id is required"
    exit 1
fi

if [[ -z "$SUBNET_ID" ]]; then
    echo "❌ Error: --subnet-id is required"
    exit 1
fi

if [[ -z "$ALLOWED_CIDR" ]]; then
    # CIDR is only required when assigning a public IP (SSH access)
    if [[ "${ASSIGN_PUBLIC_IP:-true}" == "true" ]]; then
        echo "Error: --allowed-cidr is required when using a public IP"
        echo "   Specify your IP in CIDR notation, e.g. --allowed-cidr \$(curl -s ifconfig.me)/32"
        echo "   Or use --no-public-ip for private subnet + SSM access (no SSH needed)"
        exit 1
    else
        # No public IP — use a placeholder that creates no ingress rule
        ALLOWED_CIDR="127.0.0.1/32"
    fi
fi

if [[ "$ALLOWED_CIDR" == "0.0.0.0/0" ]]; then
    echo "❌ Error: --allowed-cidr 0.0.0.0/0 is not allowed — open SSH access is a security risk."
    echo "   Specify your IP in CIDR notation, e.g. --allowed-cidr \$(curl -s ifconfig.me)/32"
    exit 1
fi
SA_DATA_BUCKET=${SA_DATA_BUCKET:-""}

# ── Pre-flight check: --no-public-ip requires NAT Gateway or VPC endpoints ──
if [[ "${ASSIGN_PUBLIC_IP:-true}" == "false" ]]; then
    echo "🔍 Checking network prerequisites for --no-public-ip mode..."

    if [[ "${AIRGAPPED:-false}" == "true" ]]; then
        # Air-gapped mode: VPC endpoints are created by CFN (NeedAirgappedEndpoints condition)
        # No pre-flight endpoint checks needed here — CFN manages them
        echo "   🔒 Air-gapped mode: VPC endpoints will be created by CloudFormation"
        echo "      (s3, ssm, ssmmessages, ec2messages, rds, monitoring, pi, cloudformation, secretsmanager)"
        # Auto-discover route table for S3 Gateway endpoint if not provided
        if [ -z "$ROUTE_TABLE_IDS" ]; then
            ROUTE_TABLE_IDS=$(aws ec2 describe-route-tables \
                --filters "Name=association.subnet-id,Values=$SUBNET_ID" \
                --region "$REGION" \
                --query 'RouteTables[*].RouteTableId' --output text 2>/dev/null | tr '\t' ',')
            echo "   Auto-detected route table(s): $ROUTE_TABLE_IDS"
        fi
    else
        HAS_NAT=$(aws ec2 describe-route-tables \
            --filters "Name=association.subnet-id,Values=$SUBNET_ID" \
            --region "$REGION" \
            --query 'RouteTables[0].Routes[?NatGatewayId!=null].NatGatewayId' \
            --output text 2>/dev/null)
        if [ -n "$HAS_NAT" ] && [ "$HAS_NAT" != "None" ]; then
            echo "   ✅ NAT Gateway found on subnet route table ($HAS_NAT)"
            echo "      Bootstrap will route outbound traffic through NAT."
        else
            echo ""
            echo "❌ Error: --no-public-ip requires a subnet with a NAT Gateway."
            echo ""
            echo "   The EC2 instance bootstrap needs outbound internet access to:"
            echo "     - Install packages (dnf)"
            echo "     - Download application code (git / S3 fallback)"
            echo "     - Install Python dependencies (pip)"
            echo "     - Signal CloudFormation on completion (cfn-signal)"
            echo ""
            echo "   Subnet $SUBNET_ID has no NAT Gateway on its route table."
            echo "   Without outbound internet, the bootstrap will hang and the stack will fail."
            echo ""
            echo "   Options:"
            echo "     - Use a private subnet with a NAT Gateway (--no-public-ip)"
            echo "     - Use --airgapped for fully private deployment (requires VPC endpoints)"
            echo "     - Use a public subnet (remove --no-public-ip)"
            echo ""
            exit 1
        fi
    fi
fi

# Upload code to S3 as fallback (CFN tries git clone first, falls back to S3)
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --region "$REGION")
CODE_BUCKET="wal-db-stats-code-${ACCOUNT_ID}"
ZIP_NAME="wal-db-stats-collection.zip"
REPO_ROOT="$SCRIPT_DIR/.."
ZIP_PATH="$REPO_ROOT/$ZIP_NAME"

    # Always recreate zip to ensure latest code
    [ -f "$ZIP_PATH" ] && rm -f "$ZIP_PATH"
    echo "📦 Creating code package from repo contents..."
    REQUIRED_FILES="
        deployment/collect-and-share.sh
        deployment/enable-invasive-collection.sh
        scripts/non_invasive_collector.py
        scripts/invasive_collector.py
        scripts/pg_health_queries.py
        utils/fleet_discovery.py
        utils/pii_redactor.py
        requirements.txt
        README.md
    "
    MISSING=""
    for f in $REQUIRED_FILES; do
        if [ ! -f "$REPO_ROOT/$f" ]; then
            MISSING="$MISSING  $f\n"
        fi
    done
    if [ -n "$MISSING" ]; then
        echo "❌ Cannot create deployment package — missing required files:"
        echo -e "$MISSING"
        echo "   Ensure you have the complete repo before deploying."
        exit 1
    fi
    (cd "$REPO_ROOT" && zip -r "$ZIP_NAME" \
        deployment/collect-and-share.sh \
        deployment/enable-invasive-collection.sh \
        deployment/deploy-db-stats-collection.sh \
        cfn/db-stats-collection.yaml \
        scripts/non_invasive_collector.py \
        scripts/invasive_collector.py \
        scripts/pg_health_queries.py \
        scripts/generate_report.py \
        scripts/pgsnapper_sql_fixes/ \
        viewer/report-template.html \
        utils/fleet_discovery.py \
        utils/pii_redactor.py \
        requirements.txt \
        README.md \
        -x "*.DS_Store*" -q)
    echo "   Created $ZIP_NAME"

echo "📦 Uploading code to S3..."
aws s3 mb "s3://$CODE_BUCKET" --region "$REGION" 2>/dev/null || true

# Harden the temporary code bucket — public access block + SSE encryption.
# This bucket is ephemeral (deleted after stack creation) but must be secure
# during the ~15-minute bootstrap window.
aws s3api put-public-access-block \
    --bucket "$CODE_BUCKET" --region "$REGION" \
    --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" \
    2>/dev/null || true
aws s3api put-bucket-encryption \
    --bucket "$CODE_BUCKET" --region "$REGION" \
    --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}' \
    2>/dev/null || true

aws s3 cp "$ZIP_PATH" "s3://$CODE_BUCKET/$CODE_KEY" --region "$REGION"

# Air-gapped mode: build vendor.zip on-the-fly from vendor/ directory and upload
if [[ "${AIRGAPPED:-false}" == "true" ]]; then
    VENDOR_DIR="$REPO_ROOT/vendor"
    if [ ! -d "$VENDOR_DIR/wheels" ] || [ ! -d "$VENDOR_DIR/pgsnapper" ]; then
        echo "❌ vendor/wheels/ or vendor/pgsnapper/ not found. Required for --airgapped mode."
        echo "   Expected at: $VENDOR_DIR"
        exit 1
    fi
    VENDOR_ZIP="/tmp/vendor-$STACK_NAME.zip"
    echo "📦 Building vendor bundle from vendor/ directory..."
    (cd "$REPO_ROOT" && zip -r "$VENDOR_ZIP" \
        vendor/wheels \
        vendor/pgsnapper \
        vendor/install.sh \
        vendor/requirements.txt \
        -q)
    echo "📦 Uploading vendor bundle (offline packages + PGSnapper)..."
    aws s3 cp "$VENDOR_ZIP" "s3://$CODE_BUCKET/vendor.zip" --region "$REGION"
    echo "   Uploaded vendor.zip ($(du -sh "$VENDOR_ZIP" | cut -f1))"
    rm -f "$VENDOR_ZIP"
fi

# Determine the data bucket name.
# IMPORTANT: On stack updates, do NOT set SADataBucket if the bucket is owned by this stack
# (created via the NeedToCreateBucket condition). Setting it would flip the condition to false
# and cause CFN to DELETE the stack-owned bucket — losing all collected data.
CUSTOMER_DATA_BUCKET_NAME="${STACK_NAME}-${ACCOUNT_ID}"
if [ -n "$SA_DATA_BUCKET" ]; then
    # User explicitly provided an external bucket — use it
    RESOLVED_DATA_BUCKET="$SA_DATA_BUCKET"
else
    # Let CFN manage the bucket (create on first deploy, keep on updates)
    # SA_DATA_BUCKET stays empty so NeedToCreateBucket remains true
    SA_DATA_BUCKET=""
    RESOLVED_DATA_BUCKET="$CUSTOMER_DATA_BUCKET_NAME"
fi

echo "🚀 Deploying Customer Data Collection for GenAI WAL Review"
echo "========================================================="
echo "Stack Name: $STACK_NAME"
echo "Region: $REGION"
echo "Key Pair: $KEY_PAIR"
echo "VPC ID: $VPC_ID"
echo "Subnet ID: $SUBNET_ID"
echo "Instance Type: $INSTANCE_TYPE"
echo "Allowed CIDR: $ALLOWED_CIDR"
echo "SA Data Bucket: ${SA_DATA_BUCKET:-'Will be auto-created'}"
echo "Scheduled Collection: $ENABLE_SCHEDULED"
echo "Schedule: $SCHEDULE"
echo ""

# Check if stack exists
if aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" >/dev/null 2>&1; then
    echo "📝 Stack exists, updating..."
    OPERATION="update-stack"
else
    echo "🆕 Creating new stack..."
    OPERATION="create-stack"
fi

# Deploy stack
echo "⚡ Deploying CloudFormation stack..."
aws cloudformation "$OPERATION" \
    --stack-name "$STACK_NAME" \
    --template-body "file://$TEMPLATE_FILE" \
    --parameters \
        "ParameterKey=KeyPairName,ParameterValue=${KEY_PAIR:-}" \
        "ParameterKey=VpcId,ParameterValue=$VPC_ID" \
        "ParameterKey=SubnetId,ParameterValue=$SUBNET_ID" \
        "ParameterKey=InstanceType,ParameterValue=$INSTANCE_TYPE" \
        "ParameterKey=AllowedCIDR,ParameterValue=$ALLOWED_CIDR" \
        "ParameterKey=AssignPublicIP,ParameterValue=${ASSIGN_PUBLIC_IP:-true}" \
        "ParameterKey=CreateSSMEndpoints,ParameterValue=${CREATE_SSM_ENDPOINTS:-false}" \
        "ParameterKey=DBPort,ParameterValue=${DB_PORT:-5432}" \
        "ParameterKey=InstallMode,ParameterValue=$([ "${AIRGAPPED:-false}" = "true" ] && echo airgapped || echo online)" \
        "ParameterKey=RouteTableIds,ParameterValue=${ROUTE_TABLE_IDS:-}" \
        "ParameterKey=SADataBucket,ParameterValue=$SA_DATA_BUCKET" \
        "ParameterKey=ResolvedDataBucketName,ParameterValue=$RESOLVED_DATA_BUCKET" \
        "ParameterKey=EnableScheduledCollection,ParameterValue=$ENABLE_SCHEDULED" \
        "ParameterKey=CollectionSchedule,ParameterValue=$SCHEDULE" \
        "ParameterKey=CodeSourceBucket,ParameterValue=$CODE_BUCKET" \
        "ParameterKey=CodeSourceKey,ParameterValue=$CODE_KEY" \
    --capabilities CAPABILITY_NAMED_IAM \
    --region "$REGION"

echo "⏳ Waiting for stack operation to complete..."
aws cloudformation wait "stack-${OPERATION%-stack}-complete" \
    --stack-name "$STACK_NAME" \
    --region "$REGION"

# ── Post-deploy: configure pre-existing SSM endpoints for --no-public-ip ────
# If the VPC already has SSM endpoints (CreateSSMEndpoints=false / default),
# the deploy script ensures they include this stack's subnet and instance SG.
# This makes SSM Session Manager work without creating duplicate endpoints.
if [[ "${ASSIGN_PUBLIC_IP:-true}" == "false" ]] && [[ "${CREATE_SSM_ENDPOINTS:-false}" == "false" ]]; then
    echo ""
    echo "🔗 Configuring pre-existing SSM endpoints for this deployment..."

    # Get the instance security group from CFN outputs
    INSTANCE_SG=$(aws cloudformation describe-stacks \
        --stack-name "$STACK_NAME" --region "$REGION" \
        --query 'Stacks[0].Outputs[?OutputKey==`InstanceSecurityGroup`].OutputValue' \
        --output text 2>/dev/null)

    if [ -n "$INSTANCE_SG" ] && [ "$INSTANCE_SG" != "None" ]; then
        for SVC in ssm ssmmessages ec2messages; do
            # Find the endpoint for this service in this VPC
            ENDPOINT_ID=$(aws ec2 describe-vpc-endpoints \
                --region "$REGION" \
                --filters "Name=service-name,Values=com.amazonaws.${REGION}.${SVC}" \
                          "Name=vpc-id,Values=${VPC_ID}" \
                          "Name=vpc-endpoint-state,Values=available" \
                --query 'VpcEndpoints[0].VpcEndpointId' \
                --output text 2>/dev/null || true)

            if [ -z "$ENDPOINT_ID" ] || [ "$ENDPOINT_ID" == "None" ]; then
                echo "   ⚠️  No existing $SVC endpoint found in $VPC_ID."
                echo "      Re-run with --create-ssm-endpoints to create it, or create it manually."
                continue
            fi

            # Add subnet if not already present
            EXISTING_SUBNETS=$(aws ec2 describe-vpc-endpoints \
                --vpc-endpoint-ids "$ENDPOINT_ID" --region "$REGION" \
                --query 'VpcEndpoints[0].SubnetIds' --output text 2>/dev/null || true)
            if echo "$EXISTING_SUBNETS" | grep -qw "$SUBNET_ID"; then
                echo "   ✅ $SVC ($ENDPOINT_ID): subnet already present"
            else
                aws ec2 modify-vpc-endpoint \
                    --vpc-endpoint-id "$ENDPOINT_ID" \
                    --add-subnet-ids "$SUBNET_ID" \
                    --region "$REGION" --output text > /dev/null 2>&1 \
                    && echo "   ✅ $SVC ($ENDPOINT_ID): added subnet $SUBNET_ID" \
                    || echo "   ⚠️  $SVC: could not add subnet (check permissions)"
            fi

            # Add instance SG to endpoint's inbound rules if not already present
            ENDPOINT_SG=$(aws ec2 describe-vpc-endpoints \
                --vpc-endpoint-ids "$ENDPOINT_ID" --region "$REGION" \
                --query 'VpcEndpoints[0].Groups[0].GroupId' --output text 2>/dev/null || true)
            if [ -n "$ENDPOINT_SG" ] && [ "$ENDPOINT_SG" != "None" ]; then
                # Check if rule already exists
                RULE_EXISTS=$(aws ec2 describe-security-group-rules \
                    --filters "Name=group-id,Values=${ENDPOINT_SG}" \
                              "Name=referenced-group-id,Values=${INSTANCE_SG}" \
                    --region "$REGION" \
                    --query 'SecurityGroupRules[?FromPort==`443`].SecurityGroupRuleId' \
                    --output text 2>/dev/null || true)
                if [ -n "$RULE_EXISTS" ] && [ "$RULE_EXISTS" != "None" ]; then
                    echo "   ✅ $SVC endpoint SG: inbound rule already exists"
                else
                    aws ec2 authorize-security-group-ingress \
                        --group-id "$ENDPOINT_SG" \
                        --protocol tcp --port 443 \
                        --source-group "$INSTANCE_SG" \
                        --region "$REGION" --output text > /dev/null 2>&1 \
                        && echo "   ✅ $SVC endpoint SG: added inbound TCP/443 from $INSTANCE_SG" \
                        || echo "   ⚠️  $SVC: could not add SG rule (may already exist)"
                fi
            fi
        done
        echo "   SSM endpoint configuration complete."
    else
        echo "   ⚠️  Could not retrieve instance security group — SSM endpoint configuration skipped."
        echo "      You may need to manually add this stack's SG to existing SSM VPC endpoint SGs."
    fi
fi

# Get outputs
echo ""
echo "✅ Stack deployment completed!"
echo ""
echo "📋 Stack Outputs:"
aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' \
    --output table

# Get specific outputs for next steps
PUBLIC_IP=$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --query 'Stacks[0].Outputs[?OutputKey==`PublicIP`].OutputValue' \
    --output text)

CUSTOMER_BUCKET=$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --query 'Stacks[0].Outputs[?OutputKey==`CustomerDataBucket`].OutputValue' \
    --output text)

echo ""
echo "🔗 Customer Data Collection Workflow:"
echo "===================================="
echo "1. SSH to the data collection instance:"
echo "   ssh -i $KEY_PAIR.pem ec2-user@$PUBLIC_IP"
echo ""
echo "2. Run data collection and sharing:"
echo "   cd /home/ec2-user/wal-db-stats-collection"
echo "   ./collect-and-share.sh"
echo ""
echo "3. (Optional) Enable invasive collection (requires DB credentials in Secrets Manager):"
echo "   NOTE: Wrap db-secret-arn in single quotes if it contains '!' (RDS managed secrets)"
echo "   ./enable-invasive-collection.sh \\"
echo "     <cluster-id> \\"
echo "     <db-host> \\"
echo "     <db-user> \\"
echo "     '<db-secret-arn>' \\"
echo "     [db-name] \\"
echo "     [pgsnapper-min-days] \\"
echo "     [pgsnapper-interval]"
echo ""
echo "   Invasive collection requires 2 runs of ./collect-and-share.sh:"
echo "     1st run: installs PGSnapper cron job to start taking snapshots"
echo "     Wait:    allow snapshots to accumulate (pgsnapper-min-days worth of data)"
echo "     2nd run: analyzes snapshots and uploads the full dataset"
echo ""
echo "4. Verify data collection:"
echo "   ls -la data/"
echo "   aws s3 ls s3://$CUSTOMER_BUCKET/db-stats/"
echo ""
echo "5. Share collected data with your SA:"
echo "   Download the data package from S3 and share via a support case."
echo "   aws s3 sync s3://$CUSTOMER_BUCKET/db-stats/ ./db-stats-export/"
echo ""
echo "6. SA runs analysis in separate environment using shared data"
echo ""
echo "📊 Metrics Collection Features:"
echo "=========================="
echo "- Fleet discovery across all PostgreSQL databases"
echo "- CloudWatch metrics collection (7 days)"
echo "- Performance Insights metrics collection"
echo "- Database configuration and metadata"
echo "- pg_stat_statements and PGPerfStatsSnapper support for in-depth database statistics, query performance, and metrics collection"
echo "- Automated S3 upload for SA sharing"
echo ""
echo "📋 Collection Types:"
echo "=================="
echo "- Non-invasive: CloudWatch + Performance Insights only"
echo "- Invasive: Includes DB slow queries + pg_stat_statements + PGPerfStatsSnapper"
echo "- To enable invasive: ./enable-invasive-collection.sh <cluster-id> <host> <user> <db-secret-arn> [pgsnapper-min-days] [pgsnapper-interval]"
echo ""
if [[ "$ENABLE_SCHEDULED" == "true" ]]; then
    echo "⏰ Scheduled Collection: Enabled ($SCHEDULE)"
    echo "   Data collection will run automatically and upload to S3"
fi
echo ""
echo "🔒 Security & Privacy:"
echo "====================="
echo "- Read-only queries for database statistics, query performance, and metrics collection"
echo "- Customer retains full control of data"
echo "- SA receives the metrics data shared without any database access"
echo ""
echo "📞 Next Steps:"
echo "============="
echo "Refer to README.md for detailed next steps"
echo "1. (Optional)Enable in-depth database statistics and metrics collection (DB slow queries + pg_stat_statements + PGPerfStatsSnapper): ./enable-invasive-collection.sh <cluster-id> <host> <user> <db-secret-arn> [pgsnapper-min-days] [pgsnapper-interval]"
echo "2. Run database statistics and metrics collection: ./collect-and-share.sh"
echo "3. Share collected data with your SA"
echo "4. SA will process your data and perform analysis"
echo "5. SA will provide comprehensive Well Architected Review reports and recommendations"
echo ""

# ── Clean up temporary code bucket ───────────────────────────────────────────
# The code bucket is only needed during EC2 bootstrap (UserData downloads the
# zip once). Remove the zip and attempt to delete the bucket to avoid leaving
# a persistent resource in the customer account.
echo "🧹 Cleaning up bootstrap code bucket..."

# Remove only the zip we uploaded — never touch other objects
aws s3 rm "s3://$CODE_BUCKET/$CODE_KEY" --region "$REGION" 2>/dev/null || true

# Delete bucket only if empty (safe — fails silently if other objects are present)
if aws s3api delete-bucket --bucket "$CODE_BUCKET" --region "$REGION" 2>/dev/null; then
    echo "   ✅ Temporary code bucket deleted: s3://$CODE_BUCKET"
else
    # Bucket still exists — likely contains other objects not uploaded by this script.
    # Re-apply security settings so it stays hardened regardless.
    aws s3api put-public-access-block \
        --bucket "$CODE_BUCKET" --region "$REGION" \
        --public-access-block-configuration \
        "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" \
        2>/dev/null || true
    echo "   ⚠️  Code bucket s3://$CODE_BUCKET could not be deleted (it may contain"
    echo "      other objects). The bootstrap zip has been removed. The bucket remains"
    echo "      encrypted and with public access blocked."
    echo "      You can safely delete it manually when no longer needed:"
    echo "      aws s3 rb s3://$CODE_BUCKET --force --region $REGION"
fi