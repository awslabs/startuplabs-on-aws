#!/usr/bin/env bash
# Wait up to 60 s for the emulator health check.
set -euo pipefail
port="${1:-9297}"
ca="$HOME/.aws/emulator/$port/ca.pem"
for _ in $(seq 60); do
  if [[ -f "$ca" ]] && curl -sf --cacert "$ca" "https://127.0.0.1:$port/__admin/health" >/dev/null; then
    echo "aws-emulator ready on https://127.0.0.1:$port"
    exit 0
  fi
  sleep 1
done
echo "aws-emulator did not become healthy; see: just emulator-logs" >&2
exit 1
