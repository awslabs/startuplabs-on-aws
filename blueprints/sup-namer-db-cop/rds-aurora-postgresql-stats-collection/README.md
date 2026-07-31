# RDS/Aurora PostgreSQL Database Statistics Collection

A blueprint for collecting database metrics, configuration, and performance data from **Aurora PostgreSQL**, **RDS for PostgreSQL**, and **RDS Multi-AZ DB Clusters** for Well-Architected Review analysis.

Deploys a lightweight EC2 instance that discovers all PostgreSQL databases in your account, collects CloudWatch metrics, Performance Insights, and optionally deep database statistics via [PGPerfStatsSnapper](https://github.com/aws-samples/aurora-and-database-migration-labs/tree/master/Code/PGPerfStatsSnapper).

## Three Deployment Modes

| Mode | Subnet | Outbound | Use when |
|------|--------|----------|----------|
| **1 — Public (IGW)** | Public subnet + IGW | Full internet | Default, no restrictions |
| **2 — Private + NAT** | Private subnet + NAT GW | AWS APIs via NAT | Security requires no public IP |
| **3 — Air-gapped** | Private subnet, no NAT | VPC endpoints only | Fully restricted egress (no internet) |

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

# Mode 3: Air-gapped (no NAT, no IGW — fully private)
bash deployment/deploy-db-stats-collection.sh \
  --no-public-ip \
  --airgapped \
  --vpc-id <your-vpc-id> \
  --subnet-id <private-subnet-no-nat-id> \
  --region <region>
```

> **Mode 3 — Air-gapped prerequisites**: The deploy script automatically detects and creates missing VPC endpoints for: `s3` (Gateway), `ssm`, `ssmmessages`, `ec2messages`, `rds`, `monitoring`, `secretsmanager` (Interface). No manual endpoint setup required.

Wait ~10 minutes for the instance to finish setup after the stack completes.

## Step 2: Access the instance

```bash
# Mode 1: SSH
ssh -i <key-pair>.pem ec2-user@<public-ip>

# Mode 2 & 3: SSM Session Manager (no SSH key needed)
aws ssm start-session --target <instance-id> --region <region>
# The deploy script prints the exact command in the stack outputs.
```

## Step 3: Run collection

```bash
cd /home/ec2-user/wal-db-stats-collection

# Non-invasive (CloudWatch + Performance Insights — no DB credentials needed)
./collect-and-share.sh

# Enable deep database statistics (optional — requires Secrets Manager ARN)
./enable-invasive-collection.sh \
  <cluster-id> <db-host> <db-user> '<db-secret-arn>'

# Run 1: installs PGSnapper cron, verifies connectivity
./collect-and-share.sh

# Wait pgsnapper-min-days (default: 1 day) for snapshots to accumulate...

# Run 2: collects all data + uploads to S3
./collect-and-share.sh --generate-report
```

## Step 4: Share data with SA

```bash
aws s3 sync s3://<customer-data-bucket>/db-stats/ ./db-stats-export/
```

Share the downloaded data via your support case.

## Architecture

```
                    ┌─────────────────────────────────────────────┐
                    │  Customer AWS Account                       │
                    │                                             │
  ┌──────────────┐  │  ┌──────────────┐    ┌──────────────────┐  │
  │ SA machine   │  │  │ EC2 (t3.med) │───►│ RDS/Aurora       │  │
  │ (analysis)   │  │  │              │    │ PostgreSQL        │  │
  └──────┬───────┘  │  │ Collects:    │    └──────────────────┘  │
         │          │  │ - CloudWatch │                           │
         │◄─────────┼──│ - PI metrics │    ┌──────────────────┐  │
      S3 download   │  │ - DB stats   │───►│ S3 (data bucket) │  │
                    │  └──────────────┘    └──────────────────┘  │
                    └─────────────────────────────────────────────┘
```

## Cost Estimate (us-east-1, 1-day run)

| Resource | Cost |
|----------|------|
| EC2 t3.medium | ~$1.00 |
| S3 storage | <$0.01 |
| Performance Insights API | <$0.01 |
| **SSM VPC endpoints** (Mode 3, if created) | ~$0.72/day |

## Cleanup

```bash
# 1. Delete stack
aws cloudformation delete-stack --stack-name <stack-name> --region <region>
aws cloudformation wait stack-delete-complete --stack-name <stack-name> --region <region>

# 2. Delete S3 data bucket (after SA review is complete)
aws s3api delete-objects \
  --bucket <customer-data-bucket> --region <region> \
  --delete "$(aws s3api list-object-versions \
    --bucket <customer-data-bucket> --region <region> \
    --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}' --output json)"
aws s3 rb s3://<customer-data-bucket> --region <region>
```

> **Note**: VPC endpoints created by `--airgapped` are managed by the CFN stack and deleted automatically with the stack.

## License

Apache-2.0 — see [LICENSE](LICENSE).
