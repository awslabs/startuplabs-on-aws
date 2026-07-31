#!/bin/bash
# Offline install script for air-gapped EC2 (no internet access)
set -e
VENDOR_DIR="$(dirname "$0")"
echo "=== Offline install from $VENDOR_DIR ==="

# Install Python packages from pre-downloaded wheels
python3.11 -m pip install \
    --no-index \
    --find-links "$VENDOR_DIR/wheels" \
    boto3 botocore psycopg2-binary PyGreSQL
echo "Python packages installed"

# Install PyGreSQL for system Python (used by PGSnapper loader)
python3 -m pip install \
    --no-index \
    --find-links "$VENDOR_DIR/wheels" \
    boto3 psycopg2-binary PyGreSQL 2>/dev/null || true

# Copy PGSnapper
mkdir -p /home/ec2-user/pgperfstats/Code
cp -r "$VENDOR_DIR/pgsnapper/PGPerfStatsSnapper" /home/ec2-user/pgperfstats/Code/
chown -R ec2-user:ec2-user /home/ec2-user/pgperfstats
echo "PGSnapper installed to /home/ec2-user/pgperfstats/Code/PGPerfStatsSnapper"
echo "=== Offline install complete ==="
