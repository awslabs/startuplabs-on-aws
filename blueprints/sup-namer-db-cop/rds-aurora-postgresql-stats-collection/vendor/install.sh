#!/bin/bash
# Offline install script for air-gapped EC2 (no internet access)
set -e
VENDOR_DIR="$(dirname "$0")"
echo "=== Offline install from $VENDOR_DIR ==="

# Use python3.11 if available, fall back to python3
if command -v python3.11 &>/dev/null; then
    PY="python3.11"
else
    PY="python3"
    echo "[WARN] python3.11 not found, using $(python3 --version)"
fi
echo "Using Python: $PY"

# Install Python packages from pre-downloaded wheels for application python
$PY -m pip install \
    --no-index \
    --find-links "$VENDOR_DIR/wheels" \
    boto3 botocore psycopg2-binary
echo "Application Python packages installed ($PY)"

# Install for system Python (python3.9) — used by PGSnapper loader
python3 -m pip install \
    --no-index \
    --find-links "$VENDOR_DIR/wheels" \
    boto3 psycopg2-binary PyGreSQL 2>/dev/null || true
echo "System Python packages installed (python3)"

# Copy PGSnapper
mkdir -p /home/ec2-user/pgperfstats/Code
cp -r "$VENDOR_DIR/pgsnapper/PGPerfStatsSnapper" /home/ec2-user/pgperfstats/Code/
chown -R ec2-user:ec2-user /home/ec2-user/pgperfstats
echo "PGSnapper installed to /home/ec2-user/pgperfstats/Code/PGPerfStatsSnapper"
# Copy RDS SSL certificate (required for invasive collection with sslmode=verify-full)
if [ -f "$VENDOR_DIR/global-bundle.pem" ]; then
    mkdir -p /certs
    cp "$VENDOR_DIR/global-bundle.pem" /certs/global-bundle.pem
    chmod 644 /certs/global-bundle.pem
    echo "SSL cert installed from vendor bundle"
fi
echo "=== Offline install complete ==="
