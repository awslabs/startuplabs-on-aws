#!/bin/bash
# Enable invasive data collection for a cluster
#
# NOTE: If your db-secret-arn contains '!' (e.g. RDS managed secrets), wrap it in single quotes:
#   ./enable-invasive-collection.sh cluster-id db-host db-user 'arn:...rds!cluster-...' [db-name] ...

if [ $# -lt 4 ]; then
  echo "Usage: $0 <cluster-id> <db-host> <db-user> <db-secret-arn> [db-name] [pgsnapper-min-days] [pgsnapper-interval] [skip-pg-stat-statements]"
  echo "  db-name: database name to connect to (default: postgres)"
  echo "  pgsnapper-min-days: minimum days of snapshots required (default: 1, use 0.1 for ~2.4 hours)"
  echo "  pgsnapper-interval: snapshot interval in minutes (default: 60)"
  echo "  skip-pg-stat-statements: true to skip pg_stat_statements prerequisite check (default: false)"
  echo "  NOTE: wrap db-secret-arn in single quotes if it contains '!' (RDS managed secrets)"
  echo "  Example:"
  echo "    $0 my-cluster db.host.com dbuser 'arn:aws:secretsmanager:us-east-1:123456789012:secret:rds!cluster-xxxxx' mydb 1 60 true"
  exit 1
fi

CLUSTER_ID="$1"
DB_HOST="$2"
DB_USER="$3"
DB_SECRET_ARN="$4"
DB_NAME="${5:-postgres}"
PGSNAPPER_MIN_DAYS="${6:-1}"
PGSNAPPER_INTERVAL="${7:-60}"
SKIP_PG_STAT_STATEMENTS="${8:-false}"

# Verify network connectivity to the database endpoint before writing the flag
echo "Checking connectivity to $DB_HOST:5432..."
if command -v pg_isready &>/dev/null; then
    if ! pg_isready -h "$DB_HOST" -p 5432 -t 5 &>/dev/null; then
        echo "WARNING: Cannot reach $DB_HOST:5432 from this instance."
        echo "   Possible causes:"
        echo "   - Security group does not allow outbound TCP/5432 from this EC2"
        echo "   - DB security group does not allow inbound from this EC2"
        echo "   - Network ACL blocking traffic"
        echo "   - VPC peering / routing not configured"
        echo ""
        echo "   The flag file will still be created, but collect-and-share.sh will fail"
        echo "   until connectivity is resolved."
        echo ""
    else
        echo "OK - database endpoint is reachable."
    fi
else
    echo "INFO: pg_isready not found, skipping connectivity check."
fi

# Load config for DATA_DIR
REAL_SCRIPT_PATH="$(readlink -f "$0" 2>/dev/null || echo "$0")"
CONFIG_FILE="$(dirname "$REAL_SCRIPT_PATH")/collection.conf"
if [ -f "$CONFIG_FILE" ]; then
  source "$CONFIG_FILE"
fi
DATA_DIR="${DATA_DIR:-/home/ec2-user/wal-db-stats-collection/data}"

FLAGS_DIR="$DATA_DIR/flags"
mkdir -p "$FLAGS_DIR"

cat > "$FLAGS_DIR/$CLUSTER_ID.flag" << FLAG
CLUSTER_ID="$CLUSTER_ID"
DB_HOST="$DB_HOST"
DB_USER="$DB_USER"
DB_SECRET_ARN='$DB_SECRET_ARN'
DB_NAME="$DB_NAME"
PGSNAPPER_MIN_DAYS="$PGSNAPPER_MIN_DAYS"
PGSNAPPER_INTERVAL="$PGSNAPPER_INTERVAL"
SKIP_PG_STAT_STATEMENTS="$SKIP_PG_STAT_STATEMENTS"
FLAG

echo "✅ Invasive collection enabled for cluster: $CLUSTER_ID"
echo "   Flag: $FLAGS_DIR/$CLUSTER_ID.flag"
echo "Run ./collect-and-share.sh to start data collection with PGPerfStatsSnapper"
