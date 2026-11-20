#!/usr/bin/env bash
# `source scripts/emulator-env.sh` to point the AWS CLI / any SDK in this shell at
# the emulator (the app itself does not need this: its client factory reads the
# same files directly).
port="${EMULATOR_PORT:-9297}"
dir="$HOME/.aws/emulator/$port"
export AWS_ENDPOINT_URL="https://127.0.0.1:$port"
export AWS_SHARED_CREDENTIALS_FILE="$dir/emulator_credentials"
export AWS_CONFIG_FILE="$dir/config"
export AWS_CA_BUNDLE="$dir/ca.pem"
export NODE_EXTRA_CA_CERTS="$dir/ca.pem"
export AWS_EC2_METADATA_DISABLED=true
export AWS_DISABLE_HOST_PREFIX_INJECTION=true
export AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_PROFILE
echo "AWS CLI/SDKs in this shell now target $AWS_ENDPOINT_URL (account 000000000000)"
