#!/usr/bin/env bash
# Walk through every module against a running `just dev`.
set -euo pipefail
api="${1:-http://127.0.0.1:8080}"
ca="$HOME/.aws/emulator/${EMULATOR_PORT:-9297}/ca.pem"
email="demo-$(date +%s)@example.com"
pw='Demo-Passw0rd'
json() { python3 -c "import json,sys; print(json.load(sys.stdin)$1)"; }
step() { printf '\n== %s\n' "$*"; }

step "register $email"
curl -sf -X POST "$api/api/v1/auth/register" -H 'content-type: application/json' \
  -d "{\"email\":\"$email\",\"password\":\"$pw\",\"name\":\"Demo User\"}"
step "login"
token=$(curl -sf -X POST "$api/api/v1/auth/login" -H 'content-type: application/json' \
  -d "{\"email\":\"$email\",\"password\":\"$pw\"}" | json "['access_token']")
auth=(-H "authorization: Bearer $token")
echo "access token: ${token:0:24}..."
step "me (identity module, SQL repository)"
curl -sf "$api/api/v1/auth/me" "${auth[@]}"
step "profile created by the UserRegistered event (profiles module, DynamoDB)"
curl -sf "$api/api/v1/profiles/me" "${auth[@]}"
step "avatar upload through a presigned S3 URL"
put_url=$(curl -sf -X POST "$api/api/v1/profiles/me/avatar" "${auth[@]}" \
  -H 'content-type: application/json' -d '{"content_type":"image/png"}' | json "['url']")
printf '\x89PNG demo' | curl -sf --cacert "$ca" -X PUT -H 'content-type: image/png' \
  --data-binary @- "$put_url" -o /dev/null -w 'S3 PUT %{http_code}\n'
get_url=$(curl -sf "$api/api/v1/profiles/me/avatar" "${auth[@]}" | json "['url']")
curl -sf --cacert "$ca" "$get_url" | head -c 9; echo
step "word-count job (jobs module, Lambda)"
curl -sf -X POST "$api/api/v1/jobs/word-count" "${auth[@]}" -H 'content-type: application/json' \
  -d '{"text":"strangler fig strangler monolith"}'
step "orders: still owned by legacy, proxied by the strangler facade"
curl -sf -D - "$api/api/v1/orders" "${auth[@]}" | grep -iE '^x-strangler-route|served_by' || true
step "no token -> 401"
curl -s -o /dev/null -w '%{http_code}\n' "$api/api/v1/auth/me"
step "admin-only route as a regular user -> 403"
curl -s -o /dev/null -w '%{http_code}\n' "$api/api/v1/auth/users" "${auth[@]}"
