#!/bin/bash
# Customer Data Collection and Sharing Script
#
# Run this script TWICE when using invasive collection (PGPerfStatsSnapper):
#   1st run: sets up the PGSnapper cron job (no data collection)
#   Wait:    allow snapshots to accumulate (pgsnapper-min-days worth of data)
#   2nd run: collects ALL data (non-invasive + invasive) with aligned timestamps

set -e

# Parse arguments
NO_REDACT=""
SKIP_SECURITY=""
GENERATE_REPORT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-redact) NO_REDACT="--no-redact"; shift ;;
    --skip-security) SKIP_SECURITY="--skip-security"; shift ;;
    --generate-report) GENERATE_REPORT="true"; shift ;;
    *) echo "Unknown option: $1"; echo "Usage: $0 [--no-redact] [--skip-security] [--generate-report]"; exit 1 ;;
  esac
done

# Load config (written by UserData during deployment)
REAL_SCRIPT_PATH="$(readlink -f "$0" 2>/dev/null || echo "$0")"
CONFIG_FILE="$(dirname "$REAL_SCRIPT_PATH")/collection.conf"
if [ -f "$CONFIG_FILE" ]; then
  source "$CONFIG_FILE"
fi

REGION="${AWS_REGION:-us-east-1}"
DATA_BUCKET="${CUSTOMER_DATA_BUCKET:-}"
DATA_DIR="${DATA_DIR:-/home/ec2-user/wal-db-stats-collection/data}"
# Resolve project root: follow symlink to real path, then go up from deployment/
REAL_SCRIPT="$(readlink -f "$0" 2>/dev/null || echo "$0")"
PROJECT_DIR="$(cd "$(dirname "$REAL_SCRIPT")/.." && pwd)"
SCRIPTS_DIR="$PROJECT_DIR/scripts"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)

echo "📊 Customer Data Collection - Fleet Discovery and Metrics Collection"
echo "=================================================================="

mkdir -p "$DATA_DIR"

# ── Determine if this is Run 1 (setup) or Run 2 (collect) ──
FLAGS_DIR="$DATA_DIR/flags"
NEEDS_SETUP=false
HAS_INVASIVE=false

if compgen -G "$FLAGS_DIR/*.flag" > /dev/null 2>&1; then
  HAS_INVASIVE=true
  for FLAG_FILE in "$FLAGS_DIR"/*.flag; do
    unset CLUSTER_ID
    source "$FLAG_FILE"
    STATUS_FILE="$FLAGS_DIR/${CLUSTER_ID}_pgsnapper_status.json"
    if [ ! -f "$STATUS_FILE" ]; then
      # No status file at all — need setup
      NEEDS_SETUP=true
    elif python3.11 -c "
import json, sys
d = json.load(open('$STATUS_FILE'))
# Setup is needed only if status file doesn't exist yet.
# Once cron is installed (status='collecting'), we proceed to Run 2
# which will check if enough snapshots exist and analyze them.
status = d.get('status', '')
if status in ('analyzed', 'collecting'):
    sys.exit(0)  # Ready for Run 2
sys.exit(1)  # Unknown status — need setup
" 2>/dev/null; then
      : # Status OK — proceed to Run 2
    else
      NEEDS_SETUP=true
    fi
  done
fi

if [ "$HAS_INVASIVE" = true ] && [ "$NEEDS_SETUP" = true ]; then
  # ── Run 1: Setup only — no data collection ──
  echo "📊 Setting up invasive data collection for $(ls "$FLAGS_DIR"/*.flag | wc -l) cluster(s)..."
  echo "   This run installs the PGSnapper snapshot cron job."
  echo "   NO data collection happens on this run."
  for FLAG_FILE in "$FLAGS_DIR"/*.flag; do
    unset CLUSTER_ID DB_HOST DB_USER DB_SECRET_ARN DB_NAME PGSNAPPER_MIN_DAYS PGSNAPPER_INTERVAL SKIP_PG_STAT_STATEMENTS
    source "$FLAG_FILE"
    if [ -n "$DB_HOST" ] && [ -n "$DB_USER" ] && [ -n "$DB_SECRET_ARN" ]; then
      echo "  → $CLUSTER_ID (setup only)"
      SKIP_FLAG=""
      [ "${SKIP_PG_STAT_STATEMENTS:-false}" = "true" ] && SKIP_FLAG="--skip-pg-stat-statements"
      python3.11 "$SCRIPTS_DIR/invasive_collector.py" \
        --cluster-id "$CLUSTER_ID" \
        --region "$REGION" \
        --db-host "$DB_HOST" \
        --db-user "$DB_USER" \
        --db-name "${DB_NAME:-postgres}" \
        --db-secret-arn "$DB_SECRET_ARN" \
        --pgsnapper-min-days "${PGSNAPPER_MIN_DAYS:-1}" \
        --pgsnapper-interval "${PGSNAPPER_INTERVAL:-60}" \
        --output-dir "$DATA_DIR" \
        --status-file "$FLAGS_DIR/${CLUSTER_ID}_pgsnapper_status.json" \
        --setup-only \
        $SKIP_FLAG $NO_REDACT $SKIP_SECURITY
    else
      echo "⚠️  Skipping $FLAG_FILE — missing DB_HOST, DB_USER, or DB_SECRET_ARN"
    fi
  done
  echo ""
  # Show actual wait time from the last flag file's PGSNAPPER_MIN_DAYS
  WAIT_DAYS="${PGSNAPPER_MIN_DAYS:-1}"
  echo "✅ Setup complete. Wait for $WAIT_DAYS day(s) worth of snapshots,"
  echo "   then run ./collect-and-share.sh again to collect all data."

elif [ "$HAS_INVASIVE" = true ]; then
  # ── Run 2: Collect everything (aligned timestamps) ──
  echo "📊 Collecting all data (non-invasive + invasive)..."

  # Step 1: Non-invasive for ALL DBs (fleet)
  echo "🔍 Discovering PostgreSQL fleet and collecting non-invasive data..."
  python3.11 "$SCRIPTS_DIR/non_invasive_collector.py" --fleet --region "$REGION" --output-dir "$DATA_DIR" $NO_REDACT

  # Step 2: Invasive for flagged DBs (skip internal non-invasive — already done above)
  echo "📊 Running invasive data collection..."
  for FLAG_FILE in "$FLAGS_DIR"/*.flag; do
    unset CLUSTER_ID DB_HOST DB_USER DB_SECRET_ARN DB_NAME PGSNAPPER_MIN_DAYS PGSNAPPER_INTERVAL SKIP_PG_STAT_STATEMENTS
    source "$FLAG_FILE"
    if [ -n "$DB_HOST" ] && [ -n "$DB_USER" ] && [ -n "$DB_SECRET_ARN" ]; then
      echo "  → $CLUSTER_ID"
      SKIP_FLAG=""
      [ "${SKIP_PG_STAT_STATEMENTS:-false}" = "true" ] && SKIP_FLAG="--skip-pg-stat-statements"
      python3.11 "$SCRIPTS_DIR/invasive_collector.py" \
        --cluster-id "$CLUSTER_ID" \
        --region "$REGION" \
        --db-host "$DB_HOST" \
        --db-user "$DB_USER" \
        --db-name "${DB_NAME:-postgres}" \
        --db-secret-arn "$DB_SECRET_ARN" \
        --pgsnapper-min-days "${PGSNAPPER_MIN_DAYS:-1}" \
        --pgsnapper-interval "${PGSNAPPER_INTERVAL:-60}" \
        --output-dir "$DATA_DIR" \
        --status-file "$FLAGS_DIR/${CLUSTER_ID}_pgsnapper_status.json" \
        --skip-non-invasive \
        $SKIP_FLAG $NO_REDACT $SKIP_SECURITY
    else
      echo "⚠️  Skipping $FLAG_FILE — missing DB_HOST, DB_USER, or DB_SECRET_ARN"
    fi
  done

else
  # ── No invasive flags: non-invasive only ──
  echo "🔍 Discovering PostgreSQL fleet..."
  python3.11 "$SCRIPTS_DIR/non_invasive_collector.py" --fleet --region "$REGION" --output-dir "$DATA_DIR" $NO_REDACT
fi

# ── Package and upload ──
echo "📦 Creating data package for SA analysis..."
PACKAGE_DIR="$DATA_DIR/sa-package-$TIMESTAMP"
mkdir -p "$PACKAGE_DIR"

cp "$DATA_DIR"/*.json "$PACKAGE_DIR/" 2>/dev/null || echo "No JSON files to copy"
cp "$DATA_DIR"/fleet_* "$PACKAGE_DIR/" 2>/dev/null || echo "No fleet files to copy"

# ── Generate HTML reports (if --generate-report flag is set) ──
if [ "$GENERATE_REPORT" = "true" ]; then
  REPORT_SCRIPT="$PROJECT_DIR/scripts/generate_report.py"
  if [ -f "$REPORT_SCRIPT" ]; then
    echo "📊 Generating interactive HTML reports..."
    for JSON_FILE in "$PACKAGE_DIR"/*_invasive_data.json "$PACKAGE_DIR"/*_non_invasive_data.json; do
      [ -f "$JSON_FILE" ] || continue
      python3.11 "$REPORT_SCRIPT" "$JSON_FILE" 2>/dev/null && echo "  ✓ $(basename "${JSON_FILE%_data.json}_report.html")" || echo "  ⚠️ Failed: $(basename "$JSON_FILE")"
    done
  else
    echo "⚠️ Report generator not found at $REPORT_SCRIPT — skipping HTML report generation"
  fi
fi

cat > "$PACKAGE_DIR/collection-metadata.json" << METADATA
{
  "collection_timestamp": "$TIMESTAMP",
  "customer_account": "$(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo 'unknown')",
  "region": "$REGION",
  "collection_type": "fleet_data_collection",
  "data_files": $(ls "$PACKAGE_DIR"/*.json 2>/dev/null | wc -l)
}
METADATA

if [ -n "$DATA_BUCKET" ]; then
  echo "📤 Uploading data package to S3 for SA analysis..."
  aws s3 sync "$PACKAGE_DIR" "s3://$DATA_BUCKET/db-stats/$TIMESTAMP/"

  # Determine next steps
  NEXT_STEPS=""
  if ! compgen -G "$FLAGS_DIR/*.flag" > /dev/null 2>&1; then
    NEXT_STEPS="Next Steps:
1. Share collected data with your SA
2. Download the collected data from S3 and share it with your SA via a support case:
   aws s3 sync s3://$DATA_BUCKET/db-stats/ ./db-stats-export/"
  elif [ "$NEEDS_SETUP" = true ]; then
    NEXT_STEPS="Next Steps:
1. Wait for $WAIT_DAYS day(s) worth of snapshots
2. Re-run ./collect-and-share.sh
3. Download the collected data from S3 and share it with your SA via a support case:
   aws s3 sync s3://$DATA_BUCKET/db-stats/ ./db-stats-export/"
  else
    NEXT_STEPS="Next Steps:
1. Share collected data with your SA
2. Download the collected data from S3 and share it with your SA via a support case:
   aws s3 sync s3://$DATA_BUCKET/db-stats/ ./db-stats-export/"
  fi

  cat > "$DATA_DIR/sa-sharing-instructions.txt" << INSTRUCTIONS
Customer Data Collection Completed
=================================
Collection Timestamp: $TIMESTAMP
S3 Location: s3://$DATA_BUCKET/db-stats/$TIMESTAMP/
$NEXT_STEPS
Data Location: $PACKAGE_DIR
INSTRUCTIONS

  echo "✅ Data collection and sharing completed!"
  echo "📁 Data package: $PACKAGE_DIR"
  echo "☁️  S3 location: s3://$DATA_BUCKET/db-stats/$TIMESTAMP/"
  cat "$DATA_DIR/sa-sharing-instructions.txt"
else
  echo "⚠️  No S3 bucket configured for sharing"
  echo "📁 Data package available locally: $PACKAGE_DIR"
fi
