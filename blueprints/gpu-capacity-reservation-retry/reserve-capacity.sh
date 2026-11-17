#!/usr/bin/env bash
# reserve-capacity.sh
# Keeps trying to create On-Demand Capacity Reservations (ODCRs) in ONE
# Availability Zone until a named request holds COUNT instances, or MAX_HOURS
# passes.
#
# BILLING: a reservation is charged at the On-Demand rate from the moment it
# exists, whether or not an instance uses it, until you cancel it.
#
# Each reservation is tagged with the request NAME you choose:
#   - Run again with the same NAME: it continues that request. It counts what
#     the request already holds and asks only for the rest.
#   - Use a new NAME: it starts a new request, separate from any other.
#   - --cancel: cancels only the reservations of that NAME.
#
# Usage (example values):
#   ACCOUNT_ID=111122223333 NAME=team-a-training ZONE=us-east-1b INSTANCE_TYPE=g7e.48xlarge COUNT=3 ./reserve-capacity.sh
#   ACCOUNT_ID=111122223333 NAME=team-a-training ./reserve-capacity.sh --cancel
#   DEBUG=1 ...                                 # print every command it runs
#
# Or set the values in SETTINGS below and run ./reserve-capacity.sh.
# Run only one copy per NAME at a time.

# ----------------------------- SETTINGS ------------------------------------
# Required: no default, because a wrong guess would reserve the wrong thing.
ACCOUNT_ID="${ACCOUNT_ID:-}"               # AWS account ID to reserve in.
NAME="${NAME:-}"                           # Name of this request, e.g. team-a-training.
ZONE="${ZONE:-}"                           # Zone name (us-east-1b) or zone ID (use1-az2). Not needed for --cancel.
INSTANCE_TYPE="${INSTANCE_TYPE:-}"         # Instance type, e.g. g7e.48xlarge. Not needed for --cancel.
COUNT="${COUNT:-}"                         # Total instances this request should hold, e.g. 3. Not needed for --cancel.
# Optional.
AWS_REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null)}"  # Default: your AWS CLI region.
RETRY_SECONDS="${RETRY_SECONDS:-120}"      # Wait between rounds. Each failed round leaves 1-2 "failed" reservation records.
MAX_HOURS="${MAX_HOURS:-8}"                # Stop after this long. Raise it for long searches (e.g. 72 on an EC2 instance).
# ---------------------------------------------------------------------------

set -uo pipefail
[[ "${DEBUG:-0}" == "1" ]] && set -x
TAG_KEY="reserve-capacity-request"
MODE="reserve"
[[ "${1:-}" == "--cancel" ]] && MODE="cancel"

fail() { echo "ERROR: $*"; exit 1; }

# ---- Check settings ----
[[ -n "$ACCOUNT_ID" ]] || fail "Set ACCOUNT_ID at the top of the script."
[[ "$NAME" =~ ^[A-Za-z0-9._-]{1,64}$ ]] \
  || fail "Set NAME to a name for this request: letters, digits, '.', '_' or '-', up to 64 characters."
[[ -n "$AWS_REGION" ]] || fail "Set AWS_REGION, or set a default region with 'aws configure set region <region>'."
export AWS_REGION
if [[ "$MODE" == "reserve" ]]; then
  [[ "$ZONE" =~ ^[a-z0-9-]+$ ]] || fail "Set ZONE, e.g. ZONE=us-east-1b or ZONE=use1-az2."
  [[ -n "$INSTANCE_TYPE" ]]     || fail "Set INSTANCE_TYPE, e.g. INSTANCE_TYPE=g7e.48xlarge."
  [[ "$COUNT" =~ ^[1-9][0-9]*$ ]] || fail "Set COUNT to the number of instances you want, e.g. COUNT=3."
  [[ "$RETRY_SECONDS" =~ ^[0-9]+$ ]] || fail "RETRY_SECONDS must be a whole number of seconds."
  [[ "$MAX_HOURS" =~ ^[0-9]+$ ]]     || fail "MAX_HOURS must be a whole number of hours."
fi

ACTUAL=$(aws sts get-caller-identity --query Account --output text) \
  || fail "AWS credentials are not working. Log in and retry."
[[ "$ACTUAL" == "$ACCOUNT_ID" ]] \
  || fail "Credentials are for account $ACTUAL, but ACCOUNT_ID is $ACCOUNT_ID."

# Every active or pending reservation of this request, in any zone:
# one line per reservation: zone  zone-id  instance-type  id  instances
NAME_FILTERS=("Name=tag:$TAG_KEY,Values=$NAME" "Name=state,Values=active,pending")
request_reservations() {
  aws ec2 describe-capacity-reservations --filters "${NAME_FILTERS[@]}" \
    --query 'CapacityReservations[].[AvailabilityZone,AvailabilityZoneId,InstanceType,CapacityReservationId,TotalInstanceCount]' \
    --output text
}

# ---- --cancel: cancel every reservation of this request ----
if [[ "$MODE" == "cancel" ]]; then
  LIST=$(request_reservations)
  [[ -n "$LIST" ]] || { echo "Request '$NAME' has no active reservations in $AWS_REGION. Nothing to cancel."; exit 0; }
  while read -r zone zid itype id n; do
    if aws ec2 cancel-capacity-reservation --capacity-reservation-id "$id" --output text >/dev/null; then
      echo "cancelled $id ($n x $itype in $zone ($zid))"
    else
      echo "could not cancel $id"
    fi
  done <<<"$LIST"
  exit 0
fi

# ---- Resolve ZONE: zone names differ per account, zone IDs do not ----
read -r ZONE_NAME ZONE_ID < <(aws ec2 describe-availability-zones \
  --query "AvailabilityZones[?ZoneName=='$ZONE' || ZoneId=='$ZONE'] | [0].[ZoneName,ZoneId]" --output text)
[[ -n "${ZONE_ID:-}" && "$ZONE_ID" != "None" ]] \
  || fail "$ZONE is not a zone name or zone ID in $AWS_REGION for this account."
ZONE_LABEL="$ZONE_NAME ($ZONE_ID)"

# ---- Stop if this request already has reservations somewhere else ----
OTHER=$(request_reservations | awk -v z="$ZONE_NAME" -v t="$INSTANCE_TYPE" '$1 != z || $3 != t')
if [[ -n "$OTHER" ]]; then
  echo "ERROR: request '$NAME' already has reservations for a different zone or instance type:"
  echo "$OTHER" | awk '{print "  " $4 ": " $5 " x " $3 " in " $1 " (" $2 ")"}'
  echo "Use the same ZONE and INSTANCE_TYPE to continue it, a new NAME to start a separate request,"
  echo "or 'NAME=$NAME ./reserve-capacity.sh --cancel' to cancel it."
  exit 1
fi

# ---- Fail fast if the instance type is not offered in the zone ----
OFFERED=$(aws ec2 describe-instance-type-offerings --location-type availability-zone \
  --filters "Name=location,Values=$ZONE_NAME" "Name=instance-type,Values=$INSTANCE_TYPE" \
  --query 'length(InstanceTypeOfferings)' --output text) \
  || fail "Could not check instance type offerings in $AWS_REGION."
[[ "$OFFERED" != "0" ]] || fail "$INSTANCE_TYPE is not offered in $ZONE_LABEL. Check ZONE and INSTANCE_TYPE."

# Reservations this run created that AWS does not list yet, as "id count".
# DescribeCapacityReservations can take a moment to show a new reservation, so
# the script keeps counting its own new ones until AWS lists them. Without this,
# a re-count straight after a success could come up short and book extra.
CREATED=()

# Sets HAVE (instances this request holds) and PENDING (reservations still in
# the pending state). Not a $(...) function, because it updates CREATED.
count_held() {
  local listed id c state entry keep=()
  HAVE=0; PENDING=0
  listed=$(aws ec2 describe-capacity-reservations --filters "${NAME_FILTERS[@]}" \
    --query 'CapacityReservations[].[CapacityReservationId,TotalInstanceCount,State]' --output text) || listed=""
  while read -r id c state; do
    [[ -n "$id" ]] || continue
    HAVE=$(( HAVE + c ))
    [[ "$state" == "pending" ]] && PENDING=$(( PENDING + 1 ))
  done <<<"$listed"
  for entry in ${CREATED[@]+"${CREATED[@]}"}; do
    id=${entry% *}; c=${entry#* }
    grep -qw -- "$id" <<<"$listed" && continue        # AWS lists it now, already counted
    state=$(aws ec2 describe-capacity-reservations --capacity-reservation-ids "$id" \
      --query 'CapacityReservations[0].State' --output text 2>/dev/null) || state="not-listed-yet"
    case "$state" in
      failed|cancelled|expired)
        echo "$(date '+%H:%M:%S') reservation $id is $state, so it does not count"; continue ;;
    esac
    HAVE=$(( HAVE + c )); PENDING=$(( PENDING + 1 )); keep+=("$entry")
  done
  CREATED=(${keep[@]+"${keep[@]}"})
}

# Try to reserve $1 instances. Returns 0 on success, 1 on no capacity, exits on any other error.
reserve() {
  local out token
  # Same token on the AWS CLI's own retries, so a request that timed out but
  # succeeded is not created twice. Max 64 characters.
  token="${NAME:0:40}-$(date +%s)-$RANDOM"
  out=$(aws ec2 create-capacity-reservation \
    --availability-zone "$ZONE_NAME" --instance-type "$INSTANCE_TYPE" \
    --instance-platform Linux/UNIX --instance-count "$1" \
    --instance-match-criteria open --end-date-type unlimited \
    --client-token "$token" \
    --tag-specifications "ResourceType=capacity-reservation,Tags=[{Key=$TAG_KEY,Value=$NAME},{Key=Name,Value=$NAME}]" \
    --query CapacityReservation.CapacityReservationId --output text 2>&1)
  if [[ "$out" == cr-* ]]; then
    CREATED+=("$out $1")
    echo "$(date '+%H:%M:%S') reserved $1 ($out) - billing has started"; return 0
  fi
  if [[ "$out" == *InsufficientInstanceCapacity* ]]; then
    echo "$(date '+%H:%M:%S') no capacity for $1"; return 1
  fi
  fail "AWS returned an error that retrying will not fix:
$out"
}

count_held
echo "Request '$NAME': $COUNT x $INSTANCE_TYPE in $ZONE_LABEL (account $ACCOUNT_ID). Already holding $HAVE."
if (( HAVE > COUNT )); then
  echo "It already holds more than COUNT. The script never cancels on its own; use --cancel to remove reservations."
fi

DEADLINE=$(( $(date +%s) + MAX_HOURS * 3600 ))
while true; do
  count_held
  NEED=$(( COUNT - HAVE ))
  if (( NEED <= 0 )); then
    # A pending reservation can still fail, so wait until none are pending.
    (( PENDING == 0 )) && break
    (( $(date +%s) >= DEADLINE )) && { echo "Reached MAX_HOURS with $PENDING reservation(s) still pending."; break; }
    echo "$(date '+%H:%M:%S') waiting for $PENDING new reservation(s) to become active"
    sleep 10; continue
  fi
  echo "Holding $HAVE of $COUNT"
  # Try for everything still needed; if that fails, try for one.
  reserve "$NEED" && continue
  (( NEED > 1 )) && reserve 1 && continue
  (( $(date +%s) >= DEADLINE )) && { echo "Reached MAX_HOURS ($MAX_HOURS). What it holds stays reserved."; break; }
  sleep "$RETRY_SECONDS"
done

count_held
echo
echo "Request '$NAME' holds $HAVE of $COUNT x $INSTANCE_TYPE in $ZONE_LABEL:"
request_reservations | awk '{print "  " $4 ": " $5}'
echo
echo "Next: start your instances in $ZONE_LABEL. Matching instances there use these reservations automatically."
echo "To stop billing: ACCOUNT_ID=$ACCOUNT_ID AWS_REGION=$AWS_REGION NAME=$NAME ./reserve-capacity.sh --cancel"
