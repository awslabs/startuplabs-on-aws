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

# ── Pre-flight: check pre-existing Interface Endpoints with PrivateDnsEnabled=true
#    are reachable from the chosen subnet.
#
#    A VPC Interface Endpoint with PrivateDnsEnabled=true overrides public DNS for the
#    service hostname (e.g. rds.us-east-1.amazonaws.com) VPC-wide. The endpoint ENI
#    only exists in its configured subnets. If the chosen subnet is NOT in the endpoint's
#    subnet list, DNS resolves to the endpoint ENI's private IP which is unreachable
#    from the instance — AWS API calls silently time out. This affects ALL modes (1, 2, 3).
#    Being in the same AZ is NOT sufficient — the chosen subnet must be explicitly listed
#    in the endpoint's subnet configuration.
#
#    Fix: redeploy using one of the subnets the endpoint is deployed in.
# ─────────────────────────────────────────────────────────────────────────────────────────
echo "🔍 Checking for pre-existing VPC Interface Endpoints that may affect API routing..."
SERVICES_TO_CHECK=(ssm ssmmessages ec2messages rds monitoring pi cloudformation secretsmanager)
ENDPOINT_ERRORS=()
if [ -n "$SUBNET_ID" ]; then
    for SVC in "${SERVICES_TO_CHECK[@]}"; do
        EP_INFO=$(aws ec2 describe-vpc-endpoints \
            --region "$REGION" \
            --filters "Name=service-name,Values=com.amazonaws.${REGION}.${SVC}" \
                      "Name=vpc-id,Values=${VPC_ID}" \
                      "Name=vpc-endpoint-state,Values=available,pending" \
            --query 'VpcEndpoints[0].{Id:VpcEndpointId,Dns:PrivateDnsEnabled,Subnets:SubnetIds}' \
            --output json 2>/dev/null || echo "null")
        EP_ID=$(echo "$EP_INFO" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('Id','') or '')" 2>/dev/null || true)
        EP_DNS=$(echo "$EP_INFO" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('Dns','') or '')" 2>/dev/null || true)
        EP_SUBNETS=$(echo "$EP_INFO" | python3 -c "import sys,json; d=json.load(sys.stdin); print(' '.join(d.get('Subnets') or []))" 2>/dev/null || true)
        [ -z "$EP_ID" ] && continue
        [ "$EP_DNS" != "True" ] && continue
        # Chosen subnet is in the endpoint — ENI is directly reachable.
        # Also check the endpoint's SG allows port 443 from the instance SG (or all sources).
        # If not, the TCP connection will be silently dropped even though DNS resolves correctly.
        if echo "$EP_SUBNETS" | grep -qw "$SUBNET_ID"; then
            echo "   ✅ $SVC ($EP_ID): chosen subnet is in endpoint subnet list — reachable"
            # Get the endpoint's SG
            EP_SG=$(aws ec2 describe-vpc-endpoints \
                --vpc-endpoint-ids "$EP_ID" --region "$REGION" \
                --query 'VpcEndpoints[0].Groups[0].GroupId' --output text 2>/dev/null || true)
            if [ -n "$EP_SG" ] && [ "$EP_SG" != "None" ]; then
                # Check if port 443 is open to 0.0.0.0/0, the instance SG, or the subnet CIDR
                HAS_443=$(aws ec2 describe-security-group-rules \
                    --filters "Name=group-id,Values=$EP_SG" \
                    --region "$REGION" \
                    --query "SecurityGroupRules[?!IsEgress && FromPort<=\`443\` && ToPort>=\`443\`].{Cidr:CidrIpv4,SG:ReferencedGroupInfo.GroupId}" \
                    --output json 2>/dev/null || echo "[]")
                ALLOWS_ALL=$(echo "$HAS_443" | python3 -c "import sys,json; rules=json.load(sys.stdin); print('yes' if any(r.get('Cidr')=='0.0.0.0/0' for r in rules) else 'no')" 2>/dev/null)
                # INSTANCE_SG is not yet known at pre-flight time (CFN creates it), so we record
                # the endpoint SG for post-deploy rule injection instead.
                if [ "$ALLOWS_ALL" != "yes" ]; then
                    echo "      ⚠️  Endpoint SG $EP_SG does not open TCP/443 to all sources."
                    echo "         This is a pre-existing shared security group in your account."
                    echo "         After stack creation, the deploy script will automatically add"
                    echo "         an inbound TCP/443 rule from the new instance SG to $EP_SG"
                    echo "         so the collection instance can reach AWS APIs via this endpoint."
                    echo "         The rule will be scoped to only this stack's instance SG (least privilege)."
                    # Record for post-deploy injection (keyed by endpoint SG, deduplicated)
                    PRE_EXISTING_EP_SGS="${PRE_EXISTING_EP_SGS:-} $EP_SG"
                fi
            fi
            continue
        fi
        # Chosen subnet is NOT in the endpoint's subnet list.
        # The endpoint ENI's private IP is only reachable from its own subnets.
        EP_SUBNET_LIST="$(echo $EP_SUBNETS | xargs)"
        ENDPOINT_ERRORS+=("$SVC|$EP_ID|$EP_SUBNET_LIST")
        echo "   ❌ $SVC ($EP_ID): PrivateDnsEnabled=true but chosen subnet $SUBNET_ID is not in endpoint"
        echo "      Endpoint subnets: $EP_SUBNET_LIST"
    done
fi
if [ ${#ENDPOINT_ERRORS[@]} -gt 0 ]; then
    echo ""
    echo "❌ Deployment blocked: pre-existing VPC Interface Endpoint(s) with PrivateDnsEnabled=true"
    echo "   do not include the chosen subnet ($SUBNET_ID)."
    echo ""
    echo "   Because Private DNS is enabled, all instances in this VPC resolve AWS service"
    echo "   hostnames to the endpoint's private IP. That IP is only reachable from the"
    echo "   subnet(s) the endpoint is deployed in — not from $SUBNET_ID."
    echo "   AWS API calls (rds:Describe*, cloudwatch:GetMetrics, etc.) would silently time out."
    echo ""
    echo "   Affected endpoints:"
    for ERR in "${ENDPOINT_ERRORS[@]}"; do
        SVC="${ERR%%|*}"; REST="${ERR#*|}"; EP_ID="${REST%%|*}"; EP_SUBNETS_ERR="${REST##*|}"
        echo "     $SVC ($EP_ID) — endpoint subnet(s): $EP_SUBNETS_ERR"
    done
    echo "   This would cause AWS API calls (rds:Describe*, cloudwatch:GetMetrics, etc.) to silently"
    echo "   fail — DNS resolves the service hostname to a private IP unreachable from subnet $SUBNET_ID."
    echo ""
    echo "   Affected endpoints:"
    for ERR in "${ENDPOINT_ERRORS[@]}"; do
        SVC="${ERR%%|*}"; REST="${ERR#*|}"; EP_ID="${REST%%|*}"; EP_SUBNETS_ERR="${REST##*|}"
        echo "     $SVC ($EP_ID) — endpoint subnet(s): $EP_SUBNETS_ERR"
    done
    echo ""
    echo "   Fix: use --subnet-id with one of the subnet IDs listed above for the affected endpoint(s)."
    exit 1
fi

# ── Pre-flight check: --no-public-ip requires NAT Gateway or VPC endpoints ──
if [[ "${ASSIGN_PUBLIC_IP:-true}" == "false" ]]; then
    echo "🔍 Checking network prerequisites for --no-public-ip mode..."

    if [[ "${AIRGAPPED:-false}" == "true" ]]; then
        # Air-gapped mode: check which of the 9 required endpoints already exist in the VPC.
        # Only pass 'true' for endpoints that are MISSING — CFN will create only those.
        # Endpoints that already exist are left alone (skipped by CFN), avoiding the
        # 'private DNS conflict' error caused by trying to create a duplicate endpoint.
        echo "   🔒 Air-gapped mode: checking which VPC endpoints need to be created..."

        AIRGAPPED_SERVICES=(ssm ssmmessages ec2messages rds monitoring pi cloudformation secretsmanager)
        # Map service name -> CFN parameter name
        declare -A SVC_PARAM=(
            [ssm]="CreateSSMEndpoint"
            [ssmmessages]="CreateSSMMessagesEndpoint"
            [ec2messages]="CreateEC2MessagesEndpoint"
            [rds]="CreateRDSEndpoint"
            [monitoring]="CreateMonitoringEndpoint"
            [pi]="CreatePIEndpoint"
            [cloudformation]="CreateCloudFormationEndpoint"
            [secretsmanager]="CreateSecretsManagerEndpoint"  # pragma: allowlist secret
        )

        # Build per-endpoint parameter values
        declare -A EP_CREATE
        declare -A EP_EXISTING_ID
        for SVC in "${AIRGAPPED_SERVICES[@]}"; do
            EXISTING=$(aws ec2 describe-vpc-endpoints \
                --filters "Name=service-name,Values=com.amazonaws.${REGION}.${SVC}" \
                          "Name=vpc-id,Values=${VPC_ID}" \
                          "Name=vpc-endpoint-state,Values=available,pending" \
                --query 'VpcEndpoints[0].VpcEndpointId' \
                --output text --region "$REGION" 2>/dev/null || true)
            if [ -z "$EXISTING" ] || [ "$EXISTING" = "None" ]; then
                EP_CREATE[$SVC]="true"
                echo "      ➕ $SVC: will be created by CFN"
            else
                # Check if this endpoint is owned by THIS stack (CFN manages it)
                # If so, pass 'true' so CFN keeps managing it on update.
                # If owned by another stack or created manually, pass 'false' to skip.
                OWNER_STACK=$(aws ec2 describe-tags --region "$REGION" \
                    --filters "Name=resource-id,Values=${EXISTING}" \
                              "Name=key,Values=aws:cloudformation:stack-name" \
                    --query 'Tags[0].Value' --output text 2>/dev/null || true)
                if [ "$OWNER_STACK" = "$STACK_NAME" ]; then
                    EP_CREATE[$SVC]="true"
                    echo "      ♻️  $SVC: owned by this stack ($EXISTING) — CFN will keep managing it"
                else
                    EP_CREATE[$SVC]="false"
                    EP_EXISTING_ID[$SVC]="$EXISTING"
                    if [ -z "$OWNER_STACK" ] || [ "$OWNER_STACK" = "None" ]; then
                        echo "      ✅ $SVC: pre-existing ($EXISTING, not CFN-managed) — CFN will skip"
                    else
                        echo "      ✅ $SVC: owned by stack '$OWNER_STACK' ($EXISTING) — CFN will skip"
                    fi
                fi
            fi
        done

        # S3 is a Gateway endpoint — same logic
        S3_EXISTING=$(aws ec2 describe-vpc-endpoints \
            --filters "Name=service-name,Values=com.amazonaws.${REGION}.s3" \
                      "Name=vpc-id,Values=${VPC_ID}" \
                      "Name=vpc-endpoint-type,Values=Gateway" \
                      "Name=vpc-endpoint-state,Values=available,pending" \
            --query 'VpcEndpoints[0].VpcEndpointId' \
            --output text --region "$REGION" 2>/dev/null || true)
        if [ -z "$S3_EXISTING" ] || [ "$S3_EXISTING" = "None" ]; then
            EP_CREATE[s3]="true"
            echo "      ➕ s3 (Gateway): will be created by CFN"
        else
            S3_OWNER=$(aws ec2 describe-tags --region "$REGION" \
                --filters "Name=resource-id,Values=${S3_EXISTING}" \
                          "Name=key,Values=aws:cloudformation:stack-name" \
                --query 'Tags[0].Value' --output text 2>/dev/null || true)
            if [ "$S3_OWNER" = "$STACK_NAME" ]; then
                EP_CREATE[s3]="true"
                echo "      ♻️  s3 (Gateway): owned by this stack ($S3_EXISTING) — CFN will keep managing it"
            else
                EP_CREATE[s3]="false"
                EP_EXISTING_ID[s3]="$S3_EXISTING"
                echo "      ✅ s3 (Gateway): pre-existing ($S3_EXISTING) — CFN will skip"
            fi
        fi

        # Auto-discover route table for S3 Gateway endpoint if not provided
        if [ -z "$ROUTE_TABLE_IDS" ]; then
            ROUTE_TABLE_IDS=$(aws ec2 describe-route-tables \
                --filters "Name=association.subnet-id,Values=$SUBNET_ID" \
                --region "$REGION" \
                --query 'RouteTables[*].RouteTableId' --output text 2>/dev/null | tr '\t' ',')
            # If no explicit association, fall back to the VPC main route table
            if [ -z "$ROUTE_TABLE_IDS" ] || [ "$ROUTE_TABLE_IDS" = "None" ]; then
                ROUTE_TABLE_IDS=$(aws ec2 describe-route-tables \
                    --filters "Name=vpc-id,Values=$VPC_ID" "Name=association.main,Values=true" \
                    --region "$REGION" \
                    --query 'RouteTables[0].RouteTableId' --output text 2>/dev/null)
                echo "   Auto-detected main route table (subnet has no explicit association): $ROUTE_TABLE_IDS"
            else
                echo "   Auto-detected route table(s): $ROUTE_TABLE_IDS"
            fi
        fi
        # Detect the SG on any pre-existing SSM/SSMMessages/EC2Messages endpoints so CFN
        # can add an inbound rule for the instance SG — required for bootstrap to succeed.
        EXISTING_ENDPOINT_SG=""
        for SVC in ssm ssmmessages ec2messages; do
            if [ "${EP_CREATE[$SVC]:-true}" = "false" ] && [ -n "${EP_EXISTING_ID[$SVC]:-}" ]; then
                DETECTED_SG=$(aws ec2 describe-vpc-endpoints \
                    --vpc-endpoint-ids "${EP_EXISTING_ID[$SVC]}" \
                    --region "$REGION" \
                    --query 'VpcEndpoints[0].Groups[0].GroupId' --output text 2>/dev/null || true)
                if [ -n "$DETECTED_SG" ] && [ "$DETECTED_SG" != "None" ]; then
                    EXISTING_ENDPOINT_SG="$DETECTED_SG"
                    echo "   🔐 Pre-existing $SVC endpoint SG: $EXISTING_ENDPOINT_SG (will add instance ingress rule)"
                    break
                fi
            fi
        done
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
    # Download python3.11 RPMs for offline install on the air-gapped EC2 instance.
    # python3.11 is NOT pre-installed on the AL2023 AMI — it must be bundled here.
    # RPMs are downloaded from the AL2023 repo on this machine (which has internet)
    # and included in the vendor bundle for offline dnf localinstall on the instance.
    RPM_DIR="$VENDOR_DIR/rpms"
    mkdir -p "$RPM_DIR"
    if ! ls "$RPM_DIR"/python3.11-*.rpm &>/dev/null 2>&1; then
        echo "📦 Downloading python3.11 RPMs for air-gapped install..."
        if command -v dnf &>/dev/null; then
            dnf download --destdir="$RPM_DIR" --resolve python3.11 python3.11-pip 2>/dev/null \
                && echo "   Downloaded $(ls "$RPM_DIR"/*.rpm 2>/dev/null | wc -l) RPMs" \
                || echo "   ⚠️  dnf download failed — python3.11 RPMs not available on this machine"
        else
            echo "   ⚠️  dnf not available on this machine — python3.11 RPMs not bundled"
            echo "      Air-gapped instance will fall back to system python3 + pip"
        fi
    else
        echo "📦 Using cached python3.11 RPMs ($(ls "$RPM_DIR"/*.rpm | wc -l) files)"
    fi
    VENDOR_ZIP="/tmp/vendor-$STACK_NAME.zip"
    echo "📦 Building vendor bundle from vendor/ directory..."
    (cd "$REPO_ROOT" && zip -r "$VENDOR_ZIP" \
        vendor/wheels \
        vendor/rpms \
        vendor/pgsnapper \
        vendor/install.sh \
        vendor/requirements.txt \
        vendor/global-bundle.pem \
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

# Check if stack exists and is in a live (non-deleted) state
STACK_STATUS=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "DOES_NOT_EXIST")
if [[ "$STACK_STATUS" != "DOES_NOT_EXIST" && "$STACK_STATUS" != "DELETE_COMPLETE" && "$STACK_STATUS" != "None" ]]; then
    echo "📝 Stack exists ($STACK_STATUS), updating..."
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
        $(if [[ "${AIRGAPPED:-false}" == "true" ]]; then
            echo "ParameterKey=CreateSSMEndpoint,ParameterValue=${EP_CREATE[ssm]:-false}"
            echo "ParameterKey=CreateSSMMessagesEndpoint,ParameterValue=${EP_CREATE[ssmmessages]:-false}"
            echo "ParameterKey=CreateEC2MessagesEndpoint,ParameterValue=${EP_CREATE[ec2messages]:-false}"
            echo "ParameterKey=CreateS3Endpoint,ParameterValue=${EP_CREATE[s3]:-false}"
            echo "ParameterKey=CreateRDSEndpoint,ParameterValue=${EP_CREATE[rds]:-false}"
            echo "ParameterKey=CreateMonitoringEndpoint,ParameterValue=${EP_CREATE[monitoring]:-false}"
            echo "ParameterKey=CreatePIEndpoint,ParameterValue=${EP_CREATE[pi]:-false}"
            echo "ParameterKey=CreateCloudFormationEndpoint,ParameterValue=${EP_CREATE[cloudformation]:-false}"
            echo "ParameterKey=CreateSecretsManagerEndpoint,ParameterValue=${EP_CREATE[secretsmanager]:-false}"
            echo "ParameterKey=ExistingSSMEndpointSecurityGroupId,ParameterValue=${EXISTING_ENDPOINT_SG:-}"
        fi) \
    --capabilities CAPABILITY_NAMED_IAM \
    --region "$REGION"

echo "⏳ Waiting for stack operation to complete..."
# Use python3 polling instead of aws cloudformation wait to avoid the AWS CLI waiter bug
# where it resolves the stack name to a stale DELETE_COMPLETE ARN and immediately fails.
python3 - <<PYEOF
import boto3, time, sys
cf = boto3.client('cloudformation', region_name='${REGION}')
terminal_create = {'CREATE_COMPLETE','CREATE_FAILED','ROLLBACK_COMPLETE','ROLLBACK_FAILED'}
terminal_update = {'UPDATE_COMPLETE','UPDATE_FAILED','UPDATE_ROLLBACK_COMPLETE','UPDATE_ROLLBACK_FAILED'}
terminal = terminal_create | terminal_update
i = 0
while True:
    try:
        r = cf.describe_stacks(StackName='${STACK_NAME}')
        status = r['Stacks'][0]['StackStatus']
        print(f'[{i*15}s] {status}', flush=True)
        if status in terminal:
            if 'COMPLETE' in status and 'ROLLBACK' not in status and 'FAILED' not in status:
                sys.exit(0)
            else:
                sys.exit(1)
    except cf.exceptions.ClientError as e:
        if 'does not exist' in str(e):
            print(f'[{i*15}s] waiting for stack to appear...', flush=True)
        else:
            print(f'Error: {e}', flush=True)
            sys.exit(1)
    time.sleep(15)
    i += 1
PYEOF
WAIT_EXIT=$?
if [ $WAIT_EXIT -ne 0 ]; then
    echo "❌ Stack operation failed."
    exit 1
fi

# ── Post-deploy: add instance SG inbound rule to pre-existing airgapped endpoints ──
# For any airgapped endpoints that already existed (skipped by CFN), add an inbound
# TCP/443 rule on the endpoint's SG from the instance SG so the instance can reach them.
# CFN-created endpoints already have the instance SG attached at creation time.
if [[ "${AIRGAPPED:-false}" == "true" ]]; then
    INSTANCE_SG=$(aws cloudformation describe-stacks \
        --stack-name "$STACK_NAME" --region "$REGION" \
        --query 'Stacks[0].Outputs[?OutputKey==`InstanceSecurityGroup`].OutputValue' \
        --output text 2>/dev/null)

    if [ -n "$INSTANCE_SG" ] && [ "$INSTANCE_SG" != "None" ]; then
        # Track which endpoint SGs we've already patched to avoid duplicate-rule errors
        # when multiple pre-existing endpoints share the same SG.
        declare -A PATCHED_SGS
        for SVC in "${!EP_EXISTING_ID[@]}"; do
            EP_ID="${EP_EXISTING_ID[$SVC]}"
            [ -z "$EP_ID" ] && continue

            # S3 Gateway endpoints have no SG — skip
            EP_TYPE=$(aws ec2 describe-vpc-endpoints \
                --vpc-endpoint-ids "$EP_ID" --region "$REGION" \
                --query 'VpcEndpoints[0].VpcEndpointType' --output text 2>/dev/null || true)
            [ "$EP_TYPE" = "Gateway" ] && continue

            # Get the endpoint's attached SG
            ENDPOINT_SG=$(aws ec2 describe-vpc-endpoints \
                --vpc-endpoint-ids "$EP_ID" --region "$REGION" \
                --query 'VpcEndpoints[0].Groups[0].GroupId' --output text 2>/dev/null || true)
            [ -z "$ENDPOINT_SG" ] || [ "$ENDPOINT_SG" = "None" ] && continue

            # If we already patched this SG for a previous endpoint, reuse result
            if [ "${PATCHED_SGS[$ENDPOINT_SG]+_}" ]; then
                echo "   ✅ $SVC ($EP_ID): shares endpoint SG $ENDPOINT_SG — rule already handled"
                continue
            fi

            # Check if rule already exists (query by group-id only, then filter in jq/python)
            RULE_EXISTS=$(aws ec2 describe-security-group-rules \
                --filters "Name=group-id,Values=${ENDPOINT_SG}" \
                --region "$REGION" \
                --query "SecurityGroupRules[?ReferencedGroupInfo.GroupId=='${INSTANCE_SG}' && FromPort==\`443\` && !IsEgress].SecurityGroupRuleId" \
                --output text 2>/dev/null || true)
            if [ -n "$RULE_EXISTS" ] && [ "$RULE_EXISTS" != "None" ]; then
                echo "   ✅ $SVC ($EP_ID): inbound rule already exists on endpoint SG $ENDPOINT_SG"
                PATCHED_SGS[$ENDPOINT_SG]="existing"
            else
                aws ec2 authorize-security-group-ingress \
                    --group-id "$ENDPOINT_SG" \
                    --protocol tcp --port 443 \
                    --source-group "$INSTANCE_SG" \
                    --region "$REGION" --output text > /dev/null 2>&1 \
                    && echo "   ✅ $SVC ($EP_ID): added inbound TCP/443 from $INSTANCE_SG to endpoint SG $ENDPOINT_SG" \
                    && PATCHED_SGS[$ENDPOINT_SG]="added" \
                    || echo "   ⚠️  $SVC ($EP_ID): could not add inbound rule (check permissions)"
            fi
        done
    fi
fi

# ── Post-deploy: configure pre-existing SSM endpoints for --no-public-ip ────
# Only applies to Mode 2 (--no-public-ip without --airgapped).
# In airgapped mode, SSM endpoints are either CFN-managed or already patched above.
if [[ "${ASSIGN_PUBLIC_IP:-true}" == "false" ]] && [[ "${CREATE_SSM_ENDPOINTS:-false}" == "false" ]] && [[ "${AIRGAPPED:-false}" == "false" ]]; then
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
                    --region "$REGION" \
                    --query "SecurityGroupRules[?ReferencedGroupInfo.GroupId=='${INSTANCE_SG}' && FromPort==\`443\` && !IsEgress].SecurityGroupRuleId" \
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

# ── Post-deploy: inject instance SG into pre-existing endpoint SGs detected at pre-flight ──
# When a pre-existing VPC Interface Endpoint (rds, monitoring, pi, etc.) has PrivateDnsEnabled=true
# and the chosen subnet is in its subnet list, the endpoint routes traffic for that service VPC-wide.
# The endpoint's SG must allow TCP/443 from the instance SG, or API calls will be silently dropped
# even though the subnet check passes. We add the rule here (post-deploy) because the instance SG
# is only known after CFN creates it.
if [ -n "${PRE_EXISTING_EP_SGS:-}" ]; then
    INSTANCE_SG=$(aws cloudformation describe-stacks \
        --stack-name "$STACK_NAME" --region "$REGION" \
        --query 'Stacks[0].Outputs[?OutputKey==`InstanceSecurityGroup`].OutputValue' \
        --output text 2>/dev/null)
    if [ -n "$INSTANCE_SG" ] && [ "$INSTANCE_SG" != "None" ]; then
        echo ""
        echo "🔗 Adding instance SG to pre-existing endpoint SG(s) for data collection access..."
        PATCHED_PRE_SGS=""
        for EP_SG in $PRE_EXISTING_EP_SGS; do
            [ -z "$EP_SG" ] && continue
            echo "$PATCHED_PRE_SGS" | grep -qw "$EP_SG" && continue
            PATCHED_PRE_SGS="$PATCHED_PRE_SGS $EP_SG"
            RULE_EXISTS=$(aws ec2 describe-security-group-rules \
                --filters "Name=group-id,Values=${EP_SG}" --region "$REGION" \
                --query "SecurityGroupRules[?ReferencedGroupInfo.GroupId=='${INSTANCE_SG}' && FromPort==\`443\` && !IsEgress].SecurityGroupRuleId" \
                --output text 2>/dev/null || true)
            if [ -n "$RULE_EXISTS" ] && [ "$RULE_EXISTS" != "None" ]; then
                echo "   ✅ Endpoint SG $EP_SG: inbound TCP/443 rule for $INSTANCE_SG already exists"
            else
                aws ec2 authorize-security-group-ingress \
                    --group-id "$EP_SG" --protocol tcp --port 443 \
                    --source-group "$INSTANCE_SG" --region "$REGION" \
                    --output text > /dev/null 2>&1 \
                    && echo "   ✅ Endpoint SG $EP_SG: added inbound TCP/443 from $INSTANCE_SG" \
                    || echo "   ⚠️  Endpoint SG $EP_SG: could not add rule (check permissions)"
            fi
        done
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
