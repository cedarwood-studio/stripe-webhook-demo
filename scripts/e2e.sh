#!/usr/bin/env bash
# End-to-end run against AWS Emulator: start it, apply the Terraform, send
# signed events through the HTTP API, destroy the stack, stop the emulator.
#
# Needs on PATH: aws-emulator (or AWS_EMULATOR_BIN), terraform, and the Python
# environment from requirements-dev.txt. No AWS account and no Stripe account.
set -euo pipefail
cd "$(dirname "$0")/.."

EMULATOR_BIN="${AWS_EMULATOR_BIN:-aws-emulator}"
export EMULATOR_PORT="${EMULATOR_PORT:-9297}"
export EMULATOR_HOME="${EMULATOR_HOME:-$PWD/.emulator}"
PYTHON="${PYTHON:-python3}"
SECRET="whsec_test_not_a_real_secret" # the same made-up value tests/events.py signs with

mkdir -p "$EMULATOR_HOME"
"$EMULATOR_BIN" --version | head -1
# Telemetry is on by default in the emulator. This demo always turns it off.
HOME="$EMULATOR_HOME" "$EMULATOR_BIN" --telemetry off --port "$EMULATOR_PORT" \
  >"$EMULATOR_HOME/emulator.log" 2>&1 &
EMULATOR_PID=$!

cleanup() {
  status=$?
  if [ -n "${APPLIED:-}" ] && [ -z "${KEEP_STACK:-}" ]; then
    terraform -chdir=infra destroy -auto-approve -input=false -no-color \
      -var "stripe_webhook_secret=$SECRET" >"$EMULATOR_HOME/destroy.log" 2>&1 ||
      echo "terraform destroy failed (see $EMULATOR_HOME/destroy.log); state dies with the emulator"
  fi
  kill "$EMULATOR_PID" 2>/dev/null || true
  rm -f infra/terraform.tfstate infra/terraform.tfstate.backup
  exit "$status"
}
trap cleanup EXIT

. scripts/emulator-env.sh
for _ in $(seq 100); do
  curl -sf --cacert "$AWS_CA_BUNDLE" "$AWS_ENDPOINT_URL/__admin/health" >/dev/null 2>&1 && break
  sleep 0.1
done
curl -sf --cacert "$AWS_CA_BUNDLE" "$AWS_ENDPOINT_URL/__admin/health" >/dev/null ||
  { echo "emulator did not start; see $EMULATOR_HOME/emulator.log"; exit 1; }

terraform -chdir=infra init -input=false -no-color >/dev/null
APPLIED=1
# A 10 second visibility timeout keeps the dead-letter test short: three
# failed receives take about 30 seconds instead of three minutes.
time terraform -chdir=infra apply -auto-approve -input=false -no-color \
  -var "stripe_webhook_secret=$SECRET" -var visibility_timeout_seconds=10 | tail -n 12
terraform -chdir=infra output -json >"$EMULATOR_HOME/outputs.json"

E2E_OUTPUTS="$EMULATOR_HOME/outputs.json" "$PYTHON" -m pytest tests/e2e -v -s -W ignore::DeprecationWarning
