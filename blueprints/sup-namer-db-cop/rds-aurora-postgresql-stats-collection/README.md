# RDS/Aurora PostgreSQL Database Statistics Collection

> **Disclaimer**: This tool collects database metrics and configuration data from your AWS environment for Well-Architected review purposes. Review the README carefully and test against your staging/QA environment first to understand the scripts and data collected. Review the data collected before sharing and ensure it complies with your organization's data sharing policies. Please issue a support case to share any data collected to AWS. If any concerns, please reach back to your account SA and proceed with ad-hoc data collection using the issued support case.

![DB Metrics Report Demo](demo/demo-metrics.gif)

## Table of Contents

**Background**
- [What this does](#what-this-does)
- [Three Deployment Modes](#three-deployment-modes)
- [Architecture](#architecture)
- [Data collected](#data-collected)
- [Security and privacy](#security-and-privacy)

**Getting started**
- [Prerequisites](#prerequisites)
- [Step 1: Deploy](#step-1-deploy)
- [Step 2: Access the instance](#step-2-access-the-instance)
- [Step 3: Run collection](#step-3-run-database-statistics-and-metrics-collection)
- [Step 4: Share data with SA](#step-4-share-data-with-sa)

**Reference**
- [Cost estimate](#cost-estimate-us-east-1-1-day-run)
- [Cleanup](#cleanup)
- [Troubleshooting](#troubleshooting)

## What this does

Deploys a lightweight EC2 instance in your AWS account that:

- Discovers all PostgreSQL databases (Aurora PostgreSQL, RDS for PostgreSQL, and RDS Multi-AZ DB Clusters for PostgreSQL) in your account/region
- Collects [CloudWatch metrics](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/Aurora.AuroraMonitoring.Metrics.html), [Performance Insights](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/USER_DatabaseInsights.html) data, and database configuration
- Collects deeper database statistics — query performance (`pg_stat_statements`), table/index bloat, health insights, and workload trends via [PGPerfStatsSnapper](https://github.com/aws-samples/aurora-and-database-migration-labs/tree/master/Code/PGPerfStatsSnapper) (requires [AWS Secrets Manager](https://docs.aws.amazon.com/secretsmanager/latest/userguide/intro.html) to access DB from your account)
- Generates interactive HTML reports for visual exploration of collected metrics
- Uploads collected data to an S3 bucket in your account for review

## Three Deployment Modes

| Mode | Subnet | Outbound connectivity | Access |
|------|--------|-----------------------|--------|
| **1 — Public subnet** | Public subnet with Internet Gateway | AWS APIs, S3, package repos via IGW | SSH |
| **2 — Private + NAT** | Private subnet with NAT Gateway | AWS APIs, S3, package repos via NAT | SSM Session Manager |
| **3 — Air-gapped** | Private subnet, no NAT, no IGW | AWS APIs and S3 via VPC endpoints only — no internet access | SSM Session Manager |

> **Mode 3 note**: All required VPC endpoints (`s3`, `ssm`, `ssmmessages`, `ec2messages`, `rds`, `monitoring`, `pi`, `cloudformation`, `secretsmanager`) are created automatically by CloudFormation. No manual setup required. Mode 3 uses the pre-bundled offline packages in the [`vendor/`](vendor/) directory (Python wheels, PGPerfStatsSnapper source, and RDS SSL certificate) — no internet access is needed during bootstrap. Note that these vendored packages may become outdated over time; review `vendor/requirements.txt` and rebuild the vendor bundle periodically.

## Architecture

```
                    ┌──────────────────────────────────────────────────────────────┐
                    │  Customer AWS Account                                        │
                    │                                                              │
  ┌──────────────┐  │  ┌──────────────────────────────┐   ┌──────────────────────┐ │
  │ SA           │  │  │ EC2 (t3.medium)              │──►│ RDS/Aurora           │ │
  │ (analysis)   │  │  │                              │   │ PostgreSQL           │ │
  └──────┬───────┘  │  │ Installed:                   │   │                      │ │
         │          │  │  - Python 3.11               │   │  - pg_stat_          │ │
         │          │  │  - PostgreSQL 15 client      │   │    statements        │ │
         │◄─────────┼──│  - PGPerfStatsSnapper        │   │  - Database Insights │ │
          share     │  │  - Local PostgreSQL DB       │   └──────────────────────┘ │
                    │  │    (PGSnapper analysis)      │                            │
                    │  │                              │   ┌──────────────────────┐ │
                    │  │ Collects:                    │──►│ S3 (data bucket)     │ │
                    │  │  - CloudWatch metrics        │   └──────────────────────┘ │
                    │  │  - Database Insights         │                            │
                    │  │  - DB statistics             │                            │
                    │  └──────────────────────────────┘                            │
                    └──────────────────────────────────────────────────────────────┘
```

> **Note**: The EC2 instance runs a local PostgreSQL instance used exclusively by PGPerfStatsSnapper to load and analyze the periodic performance statistics it collects from the RDS/Aurora database. It is used as a temporary analysis engine and all results are written to the S3 data bucket.

## Prerequisites

- AWS CLI configured with your account credentials
- A VPC with a subnet (public or private — see deployment modes above)
- An EC2 Key Pair — required only for **Mode 1** (SSH access)
- IAM permissions: EC2, CloudFormation, S3, RDS, CloudWatch, Performance Insights, Secrets Manager. See the permissions defined in [`cfn/deploy-iam-policy.json`](cfn/deploy-iam-policy.json). Replace `<your-region>` in the policy file with your deployment region (e.g. `us-east-1`).

### Network ACL requirements

Only relevant if the subnet has a **custom NACL** (non-default). The default NACL allows all traffic — no changes needed if you are using the default NACL. Custom NACLs that deny ephemeral port return traffic are the most common cause of silent connectivity failures (instance appears to deploy successfully but `dnf`, AWS API calls, and S3 uploads hang or time out).

**Mode 1 (public subnet):**

| Direction | Port | Protocol | Purpose |
|-----------|------|----------|---------|
| Outbound | 443 | TCP | HTTPS — AWS APIs, S3, package repos |
| Outbound | 5432 (or `--db-port` value) | TCP | PostgreSQL — invasive collection only |
| Outbound | 1024–65535 | TCP | Ephemeral return traffic |
| Inbound | 22 | TCP | SSH access |
| Inbound | 1024–65535 | TCP | Ephemeral return traffic |

**Mode 2 (private + NAT):** Same as Mode 1 except **no port 22 inbound** (SSH not used — SSM via NAT).

**Mode 3 (air-gapped):** All traffic stays within the VPC. No internet access, no port 22.

| Direction | Port | Protocol | Purpose |
|-----------|------|----------|---------|
| Outbound | 443 | TCP | HTTPS — VPC endpoints (AWS APIs) |
| Outbound | 5432 (or `--db-port` value) | TCP | PostgreSQL — invasive collection (VPC-internal to RDS) |
| Inbound | 1024–65535 | TCP | Ephemeral return traffic |

## Step 1: Deploy

```bash
# Mode 1: Public subnet (SSH access)
bash deployment/deploy-db-stats-collection.sh \
  --vpc-id <your-vpc-id> \
  --subnet-id <public-subnet-id> \
  --key-pair <key-pair-name> \
  --allowed-cidr $(curl -s ifconfig.me)/32 \
  --region <region>

# Mode 2: Private subnet + NAT (SSM access, no SSH key needed)
bash deployment/deploy-db-stats-collection.sh \
  --no-public-ip \
  --vpc-id <your-vpc-id> \
  --subnet-id <private-subnet-with-nat-id> \
  --region <region>

# Mode 3: Air-gapped (no NAT, no IGW — fully private, VPC endpoints only)
bash deployment/deploy-db-stats-collection.sh \
  --no-public-ip \
  --airgapped \
  --vpc-id <your-vpc-id> \
  --subnet-id <private-subnet-no-nat-id> \
  --region <region>
```

The script will:

1. Package the directory contents and upload to S3 (used as the bootstrap source for the EC2 instance)
2. Deploy a CloudFormation stack (`wal-db-stats-collection`) with a `t3.medium` EC2 instance
3. The EC2 instance bootstraps from the S3 package on first boot
4. Print the instance ID, SSM connect command, and data S3 bucket name on completion

Wait ~10 minutes for the instance to finish setup after the stack completes.

### Parameters

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `--key-pair` | Mode 1 only | — | EC2 Key Pair name for SSH access. Required when deploying into a **public subnet** (Mode 1). Not required with `--no-public-ip`. |
| `--vpc-id` | Yes | — | VPC ID where the EC2 instance will be deployed. **Must be the same VPC as your RDS/Aurora cluster** so the instance can reach the database endpoint. |
| `--subnet-id` | Yes | — | Subnet ID within the VPC above. |
| `--allowed-cidr` | Mode 1 only | — | CIDR allowed for SSH inbound on port 22. **Must be your specific IP** (e.g. `x.x.x.x/32`). `0.0.0.0/0` is rejected. Find your IP with `curl -s ifconfig.me`. Required for Mode 1 only. |
| `--no-public-ip` | No | — | Deploy without a public IP address. Use when deploying into a private subnet (Mode 2 or 3). SSH is not available; connect via [AWS Systems Manager Session Manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/what-is-systems-manager.html). |
| `--create-ssm-endpoints` | No | — | Use with `--no-public-ip` when the VPC has **no existing SSM VPC endpoints**. Creates three Interface VPC Endpoints (`ssm`, `ssmmessages`, `ec2messages`) so SSM traffic stays off the public internet. A NAT Gateway is still required for bootstrap and S3 uploads. |
| `--airgapped` | No | — | Fully private mode — no NAT, no internet. CFN creates all required VPC endpoints automatically (Mode 3). |
| `--db-port` | No | `5432` | PostgreSQL port for invasive collection. Only needed if your database uses a non-standard port. |
| `--region` | No | `us-east-1` | AWS region to deploy into. |
| `--instance-type` | No | `t3.medium` | EC2 instance type. |
| `--sa-data-bucket` | No | auto-created | Existing S3 bucket name for data sharing with your SA. If omitted, a bucket named `wal-db-stats-collection-<account-id>` is created automatically. |
| `--stack-name` | No | `wal-db-stats-collection` | CloudFormation stack name. |
| `--enable-scheduled` | No | `true` | Enable daily automatic collection. |
| `--schedule` | No | `0 6 * * *` | Cron schedule (default: daily at 6 AM UTC). |

## Step 2: Access the instance

```bash
# Mode 1: SSH
ssh -i <key-pair>.pem ec2-user@<public-ip>

# Mode 2 & 3: SSM Session Manager (no SSH key needed)
aws ssm start-session --target <instance-id> --region <region>
# The deploy script prints the exact SSM command in the stack outputs.
```

**AWS Console (Mode 2 & 3):**

1. Open the [EC2 console](https://console.aws.amazon.com/ec2/) → Instances
2. Select the instance → Connect → Session Manager → Connect

> **Note**: The EC2 instance role already includes the `AmazonSSMManagedInstanceCore` policy, so SSM is enabled automatically.

> **Note**: In Mode 2 (private subnet with a NAT Gateway), SSM works automatically via NAT — no VPC endpoints are required. In Mode 3, SSM connectivity is provided by the VPC endpoints created by `--airgapped`.

## Step 3: Run database statistics and metrics collection

Database statistics and metrics collection gathers CloudWatch metrics (7 days), Performance Insights, and RDS/Aurora configuration for all PostgreSQL databases discovered in your account. Additionally, it collects database statistics and query performance data using [pg_stat_statements](https://www.postgresql.org/docs/current/pgstatstatements.html) and [PGPerfStatsSnapper](https://github.com/aws-samples/aurora-and-database-migration-labs/blob/master/Code/PGPerfStatsSnapper/README.md) for performance and workload analysis. This requires database credentials stored in AWS Secrets Manager.

> **Note**: Database statistics collection runs read-only queries against your database. No data is modified. Queries are lightweight and designed to have minimal performance impact. Test with your QA/test environment to understand the metrics collected before running against production. If you have concerns about direct database access, see [(Optional) Collect CloudWatch metrics only](#optional-collect-cloudwatch-metrics-only) — however, skipping in-depth database statistics limits the SA's ability to identify slow queries, table bloat, unused indexes, checkpoint pressure, and historical workload trends.

### Step 3.1: Enable database statistics collection

Run `enable-invasive-collection.sh` once per cluster. Each call registers that cluster for deep collection — you can enable as many clusters as needed before running `collect-and-share.sh`.

```bash
cd /home/ec2-user/wal-db-stats-collection
./enable-invasive-collection.sh \
  <cluster-id> \
  <db-host> \
  <db-user> \
  '<db-secret-arn>' \
  [db-name] \
  [pgsnapper-min-days] \
  [pgsnapper-interval] \
  [skip-pg-stat-statements]
```

| Argument | Required | Description |
|----------|----------|-------------|
| `cluster-id` | Yes | RDS cluster or instance identifier. For RDS Multi-AZ DB Clusters, use the cluster identifier (e.g. `my-maz-cluster`). |
| `db-host` | Yes | Database endpoint hostname. For Aurora and RDS Multi-AZ DB Clusters, use the **writer endpoint** to capture write workload, `pg_stat_statements`, and PGSnapper snapshots from the primary. |
| `db-user` | Yes | Database username. |
| `db-secret-arn` | Yes | Secrets Manager ARN containing the DB password. |
| `db-name` | No | Database name to connect to (default: `postgres`). |
| `pgsnapper-min-days` | No | Minimum days of snapshots required (default: `1`; use `0.1` for ~2.4 hours). |
| `pgsnapper-interval` | No | Snapshot interval in minutes (default: `60`). |
| `skip-pg-stat-statements` | No | Set to `true` to skip the `pg_stat_statements` prerequisite check (default: `false`). Use when the extension is not installed on the cluster. |

> **Note**: If your `db-secret-arn` contains `!` (e.g. RDS managed secrets like `rds!cluster-...`), wrap it in **single quotes** to prevent bash history expansion.

Example:

```bash
./enable-invasive-collection.sh \
  my-aurora-cluster \
  my-cluster.cluster-xyz.us-east-1.rds.amazonaws.com \
  postgres \
  'arn:aws:secretsmanager:us-east-1:123456789012:secret:rds!cluster-xxxxx' \
  my_db \
  1 \
  60 \
  false   # set to true if pg_stat_statements is not installed
```

With the example above (`pgsnapper-min-days=1`, `pgsnapper-interval=60`), wait at least 1 day between runs. For a quick test, use `pgsnapper-min-days=0.01` (~15 minutes) and `pgsnapper-interval=1` (1-minute interval).

### Step 3.2: Run collection and generate reports

Database statistics collection requires **two runs** of `./collect-and-share.sh`:

1. **Run 1 (setup only)** — installs the PGSnapper cron job and runs an initial snapshot to verify connectivity. **No data collection happens on this run** — no CloudWatch metrics, no database statistics, no schema or query performance data. This keeps Run 1 fast and avoids collecting data that would be stale by Run 2.
2. **Wait** — allow snapshots to accumulate for at least `pgsnapper-min-days` worth of data.
3. **Run 2 (collect everything)** — collects **all** data with aligned timestamps: CloudWatch metrics (7 days), Performance Insights, configuration for the entire fleet, plus database statistics (schema, query performance, PGSnapper analysis) for flagged clusters. Both non-invasive and invasive data share the same time window.

```bash
# Run 1 — setup only: installs cron, verifies connectivity (no data collection)
./collect-and-share.sh

# Wait for pgsnapper-min-days worth of snapshots...

# Run 2 — collects all data + generates interactive HTML reports
./collect-and-share.sh --generate-report --skip-security
```

> **Note**: If the initial snapshot fails during Run 1 (bad credentials, network issue, etc.), the cron job will **not** be installed. Fix the underlying issue and re-run `./collect-and-share.sh` — it will detect that setup is still needed and retry.

> **Note**: `--skip-security` excludes security-related queries (user roles, privileges, SSL, passwords, RLS, audit config) from the collection. Remove the flag if you want security data collected.

> **Note**: `--generate-report` produces a self-contained interactive HTML report for each database (7 tabs: Configuration, CloudWatch Metrics, Performance Insights, Security, Database Health, Workload Trends, Schema Explorer). Open the `*_report.html` file in any browser — no internet, server, or additional software required.

Collected data and reports are uploaded to S3:

```
s3://wal-db-stats-collection-<account-id>/db-stats/<timestamp>/
├── database-1_invasive_data.json           (raw data for SA)
├── database-1_invasive_report.html         (interactive visual report)
├── database-1_non_invasive_data.json
├── database-1_non_invasive_report.html
└── ...
```

### (Optional) Collect CloudWatch metrics only

If you have concerns about running database statistics collection, you can run non-invasive collection only: CloudWatch metrics (7 days), Performance Insights, and RDS/Aurora configuration for all PostgreSQL databases discovered in your account.

If you previously ran `enable-invasive-collection.sh` for one or more clusters but have decided not to proceed with deep collection, remove the flag file(s) before running `collect-and-share.sh`.

```bash
cd /home/ec2-user/wal-db-stats-collection

# Remove a specific cluster's flag
rm data/flags/<cluster-id>.flag

# Or remove all registered clusters at once
rm -f data/flags/*.flag

# Then run non-invasive collection only
./collect-and-share.sh
```

> **Note**: Removing the flag file only prevents invasive collection — it does not affect any PGSnapper cron job already installed. The cron job is **automatically removed** after a successful Run 2 collection. To remove it manually:
>
> ```bash
> crontab -l | grep -v 'pg_perf_stat_snapper' | crontab -
> ```

## Step 4: Share data with SA

Download the collected data from S3 and share it with your SA via a support case:

```bash
# Download the data package locally
aws s3 sync s3://wal-db-stats-collection-<account-id>/db-stats/ ./db-stats-export/

# Then attach the data to your support case or share via your preferred secure channel
```

Your SA will use this data to perform the Well-Architected Review and provide you with a comprehensive report.

## Data collected

**Non-invasive** (no DB credentials needed):

- RDS/Aurora cluster and instance configuration
- CloudWatch metrics (CPU, memory, IOPS, connections, replication lag — 7 days)
- Performance Insights top SQL and wait events
- Parameter group settings
- Subnet, VPC, and security group configuration

**Invasive** (requires DB credentials via Secrets Manager):

- All of the above, plus:
- `pg_stat_statements` — top queries by execution time and call count *(skipped if extension not installed; pass `skip-pg-stat-statements=true` to `enable-invasive-collection.sh`)*
- `pg_stat_user_tables` — table bloat, sequential scans, DML activity
- `pg_stat_user_indexes` — unused and duplicate indexes
- `pg_stat_bgwriter` — checkpoint and buffer statistics
- PostgreSQL health insights — comprehensive assessment across 9 areas: database overview, configuration health, connection activity, replication status, data footprint, query/IO performance, maintenance health, optimization opportunities, and security audit *(security queries can be excluded with `--skip-security`)*
- PGPerfStatsSnapper workload snapshots (historical query performance trends, session activity, CPU-heavy queries, checkpoint/temp file trends)

## Security and privacy

- Database credentials are retrieved from Secrets Manager — never stored in plaintext
- All data is encrypted in transit (HTTPS/TLS) and at rest (S3 SSE)
- The S3 bucket is private with public access blocked
- Data is automatically deleted from S3 after 30 days
- You retain full control of the S3 bucket — data stays in your account

### PII handling

PII redaction runs **automatically** before any data is written to disk or uploaded to S3. The following fields are redacted by default:

- **Database endpoints** (`endpoint`, `reader_endpoint`) → masked to `<masked-endpoint>`
- **Client IP addresses** in connection activity data → SHA-256 hash (first 8 chars)
- **KMS key ARNs** → trimmed to key ID only (no account ID or region)
- **Password hashes** in database health data → replaced with `<redacted>`
- **Query text** — a `query_hash` field is added alongside each query for cross-referencing. The query text itself is **not removed** because `pg_stat_statements` stores only the parameterized form (e.g. `UPDATE t SET col = $1 WHERE id = $2`) which contains no customer data.

**What is NOT redacted**:

- Table, column, and schema names
- All numeric metric values
- Parameter names and settings
- Database passwords are never written to any output file — retrieved from Secrets Manager at runtime only

To **skip redaction** (e.g. for internal analysis where you need raw endpoints):

```bash
./collect-and-share.sh --no-redact
```

## Cost Estimate (us-east-1, 1-day run)

| Resource | Rate | Typical usage (1 day) | Estimated cost |
|----------|------|----------------------|----------------|
| EC2 t3.medium | $0.0416/hr | 24 hrs | ~$1.00 |
| S3 storage | $0.023/GB-month | <100 MB of metrics data | <$0.01 |
| S3 requests | $0.005/1K PUT | ~50 files uploaded | <$0.01 |
| CloudWatch API | $0.01/1K metrics | ~1,000 metric queries | ~$0.01 |
| Performance Insights — 7-day retention | Free | Included with RDS/Aurora | $0 |
| Performance Insights — API calls | $0.01/1K calls | ~400 API calls per run | <$0.01 |
| **SSM VPC endpoints** (Mode 3 only) | $0.01/hr × 9 endpoints | 24 hrs | ~$2.16 |
| **NAT Gateway** (if created for this deployment) | $0.045/hr + $0.045/GB | 24 hrs + ~1 GB data | ~$1.13 |

**Estimated total cost per day:**

| Deployment mode | Cost/day |
|---|---|
| Mode 1 — public subnet | ~$1.00–$1.05 |
| Mode 2 — private subnet + NAT (NAT pre-existing) | ~$1.00–$1.05 |
| Mode 2 — private subnet + NAT (NAT created for this) | ~$2.13–$2.18 |
| Mode 3 — air-gapped (VPC endpoints, no NAT) | ~$3.16–$3.21 |

> **Note**: To minimize cost, delete the stack as soon as the SA has confirmed receipt of the data. The S3 data bucket has a 30-day lifecycle expiry — objects are deleted automatically.

## Cleanup

```bash
# Step 0: Pre-deletion prerequisites

# If you deployed with --no-public-ip or --airgapped, the deploy script may have added
# inbound TCP/443 rules to existing SGs (SSM endpoint SGs or pre-existing airgapped
# endpoint SGs) referencing the stack's instance security group.
# CFN cannot remove these external references automatically — revoke them first or
# the stack deletion will fail with DELETE_FAILED on the instance security group.
INSTANCE_SG=$(aws cloudformation describe-stacks \
  --stack-name wal-db-stats-collection --region <region> \
  --query 'Stacks[0].Outputs[?OutputKey==`InstanceSecurityGroup`].OutputValue' \
  --output text)

REFERENCING_SGS=$(aws ec2 describe-security-groups --region <region> \
  --filters "Name=vpc-id,Values=<your-vpc-id>" \
  --query "SecurityGroups[?IpPermissions[?UserIdGroupPairs[?GroupId=='${INSTANCE_SG}']]].GroupId" \
  --output text)

if [ -z "$REFERENCING_SGS" ] || [ "$REFERENCING_SGS" = "None" ]; then
  echo "  No external SG references found — safe to delete stack"
else
  for SG_ID in $REFERENCING_SGS; do
    aws ec2 revoke-security-group-ingress \
      --group-id "$SG_ID" --protocol tcp --port 443 \
      --source-group "$INSTANCE_SG" --region <region> > /dev/null \
      && echo "  Revoked rule in $SG_ID" || echo "  $SG_ID: could not revoke"
  done
fi

# The S3 data bucket contains collected metrics.
# Stack deletion will fail if the bucket is non-empty. Empty it first:
OBJECTS=$(aws s3api list-object-versions \
  --bucket wal-db-stats-collection-<account-id> --region <region> \
  --output json \
  --query '{Objects: [Versions,DeleteMarkers][][].{Key:Key,VersionId:VersionId}}')
echo "$OBJECTS" | grep -q '"Key"' && \
  aws s3api delete-objects \
    --bucket wal-db-stats-collection-<account-id> --region <region> \
    --delete "$OBJECTS" || echo "Bucket already empty"

# Step 1: Delete the CloudFormation stack
aws cloudformation delete-stack \
  --stack-name wal-db-stats-collection \
  --region <region>

# Wait for deletion to complete
aws cloudformation wait stack-delete-complete \
  --stack-name wal-db-stats-collection \
  --region <region>
```

> **Note**: VPC endpoints created by `--airgapped` are managed by the CFN stack and deleted automatically with the stack. However, any inbound rules added to **pre-existing** endpoint SGs (endpoints that already existed in your VPC before deploying) are external references that CFN cannot clean up — Step 0 above handles those.

> **Note**: The temporary code bucket (`wal-db-stats-code-<account-id>`) is automatically deleted by the deploy script after stack creation completes. If it was not cleaned up automatically, delete it manually: `aws s3 rb s3://wal-db-stats-code-<account-id> --force --region <region>`

> **Note**: If you created a NAT Gateway, EIP, private subnet, or route table outside of CloudFormation for this deployment, those resources must be deleted manually — they are not managed by the stack.

## Troubleshooting

**Instance setup not complete after 10 minutes**

Check UserData logs:

```bash
# Mode 1: SSH
ssh -i <keypair>.pem ec2-user@<ip>
sudo tail -f /var/log/user-data.log

# Mode 2 & 3: SSM
aws ssm start-session --target <instance-id> --region <region>
sudo tail -f /var/log/user-data.log
```

**UserData hangs at `dnf update` or AWS API calls**

This is the most common symptom of a NACL blocking return traffic (ephemeral ports). The instance security group allows outbound TCP/443, but NACLs are stateless — return packets on ports 1024–65535 must be explicitly permitted inbound.

- Check the NACL associated with your subnet in the VPC console
- Ensure inbound ephemeral ports 1024–65535 TCP are allowed from 0.0.0.0/0
- See the [Network ACL requirements](#network-acl-requirements) section above for the full rule set

**Collection script not found**

The instance may still be setting up. Wait a few more minutes and check `/var/log/user-data.log`.

**SSM Session Manager not connecting (Mode 2)**

Ensure the subnet has a NAT Gateway with a route to the internet. SSM requires outbound connectivity to `ssm`, `ssmmessages`, and `ec2messages` — this works via NAT without VPC endpoints.

**SSM not connecting (Mode 3)**

Verify all VPC endpoints are in `available` state:

```bash
aws ec2 describe-vpc-endpoints \
  --filters "Name=vpc-id,Values=<vpc-id>" \
  --query 'VpcEndpoints[*].{SVC:ServiceName,State:VpcEndpointState}' \
  --output table --region <region>
```

**CloudWatch or Performance Insights returns no data**

- Ensure Performance Insights is enabled on your RDS/Aurora cluster
- Verify that permissions `pi:GetResourceMetrics` and `cloudwatch:GetMetricStatistics` are not explicitly denied

**Collection fails with SSL error (Mode 3)**

The RDS SSL certificate is included in the vendor bundle for Mode 3 and installed automatically. For Modes 1 & 2, it is downloaded from the internet during bootstrap.

**Invasive collection fails**

- Confirm the Secrets Manager ARN is correct and the secret contains a `password` key
- Ensure the EC2 instance security group can reach the database endpoint on its port (default: 5432)
- If the error mentions `track_functions`: set `track_functions = all` in the DB parameter group (RDS for PostgreSQL) or cluster parameter group (Aurora PostgreSQL). This is a dynamic parameter — no reboot required.
- If the output mentions `[Optional] track_activity_query_size`: this is a recommended improvement, not a blocking error — collection will still run. To capture full text of very long SQL statements, set `track_activity_query_size = 102400` in the parameter group, then reboot the DB instance [REBOOT REQUIRED].

## License

Apache-2.0 — see [LICENSE](LICENSE).

## DISCLAIMER OF WARRANTIES AND LIABILITY

This code is provided solely for prototyping and proof-of-concept purposes. By accessing, downloading, or using this code, you acknowledge and agree to the following terms:

**NO WARRANTY** This code is provided "as-is," without warranty of any kind, express or implied, including but not limited to warranties of merchantability, fitness for a particular purpose, or non-infringement. Amazon Web Services, Inc. and its affiliates ("AWS") make no representations or warranties regarding the accuracy, reliability, completeness, or suitability of this code for any purpose.

**LIMITATION OF LIABILITY** AWS and its affiliates shall not be liable for any direct, indirect, incidental, special, consequential, or exemplary damages arising out of or in connection with the use, misuse, or inability to use this code, even if advised of the possibility of such damages.

**NO SUPPORT** AWS and its affiliates do not provide technical support, maintenance, updates, or bug fixes for this code. Use of this code is entirely at the discretion and risk of the end user.

**CUSTOMER RESPONSIBILITY** It is the sole responsibility of the customer to evaluate, test, and validate this code in non-production (lower) environments prior to any deployment in production systems. Deployment of this code in any environment, including production, is undertaken entirely at the customer's own risk.

**INDEPENDENT USE** This code does not constitute professional advice, and customers are encouraged to engage qualified technical personnel to assess its suitability for their specific use case.

By using this code, you confirm that you have read, understood, and agreed to the terms set forth in this disclaimer.
