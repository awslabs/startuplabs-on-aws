#!/bin/bash
# Offline install script for air-gapped EC2 (no internet access)
#
# Called by the CFN bootstrap after vendor.zip is extracted to /opt/vendor/.
# Installs Python packages and PGSnapper dependencies entirely from the
# vendor bundle — no internet or dnf repo access required.
set -e
VENDOR_DIR="$(dirname "$0")"
echo "=== Offline install from $VENDOR_DIR ==="

# ── Application Python (python3.11) ─────────────────────────────────────────
# python3.11 is installed from vendor/rpms/ before this script runs (see CFN UserData).
# The cp311 wheels in vendor/wheels/ are built for python3.11.
if command -v python3.11 &>/dev/null; then
    PY="python3.11"
    echo "Using Python: $PY ($(python3.11 --version))"
else
    PY="python3"
    echo "[WARN] python3.11 not found, falling back to $(python3 --version)"
fi

$PY -m pip install \
    --no-index \
    --find-links "$VENDOR_DIR/wheels" \
    "boto3==1.43.61" "botocore==1.43.61" psycopg2-binary
echo "Application Python packages installed ($PY)"

# ── System Python (python3.9) — used by PGSnapper ───────────────────────────
# PGSnapper scripts use #!/usr/bin/python3 (system python3.9 on AL2023).
# They require: boto3, and PyGreSQL (provides pgdb module).
#
# The pre-built PyGreSQL wheel (cp39 linux_x86_64) is included in vendor/wheels/
# and was built using Docker on an AL2023 x86_64 container with postgresql15-devel.
# No compilation required on the air-gapped instance.
echo "Installing system Python packages for PGSnapper..."

# Bootstrap pip for system python3 if not present (AL2023 AMI doesn't ship python3-pip)
if ! python3 -m pip --version &>/dev/null; then
    python3 -m ensurepip --upgrade 2>/dev/null \
        && echo "  pip bootstrapped for python3" \
        || { echo "[ERROR] Cannot bootstrap pip for python3"; exit 1; }
fi

# Install boto3 and PyGreSQL from pre-built wheels — no compilation needed
# Note: boto3>=1.35 requires Python 3.10+, so we bundle two boto3 versions:
#   boto3-1.43.61 → for python3.11 (application, installed above)
#   boto3-1.34.162 → for system python3.9 (PGSnapper, installed here)
# The explicit version pin ensures pip picks the correct one for each interpreter.
python3 -m pip install \
    --no-index \
    --find-links "$VENDOR_DIR/wheels" \
    "boto3==1.34.162" pygresql \
    && echo "  boto3 and PyGreSQL installed for python3 (PGSnapper ready)" \
    || echo "[WARN] System Python package install failed — PGSnapper may not work"

# ── PGSnapper ────────────────────────────────────────────────────────────────
mkdir -p /home/ec2-user/pgperfstats/Code
cp -r "$VENDOR_DIR/pgsnapper/PGPerfStatsSnapper" /home/ec2-user/pgperfstats/Code/
chown -R ec2-user:ec2-user /home/ec2-user/pgperfstats
echo "PGSnapper installed to /home/ec2-user/pgperfstats/Code/PGPerfStatsSnapper"

# ── RDS SSL certificate ───────────────────────────────────────────────────────
if [ -f "$VENDOR_DIR/global-bundle.pem" ]; then
    mkdir -p /certs
    cp "$VENDOR_DIR/global-bundle.pem" /certs/global-bundle.pem
    chmod 644 /certs/global-bundle.pem
    echo "SSL cert installed from vendor bundle"
fi

echo "=== Offline install complete ==="
