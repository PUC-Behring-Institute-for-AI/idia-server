#!/usr/bin/env bash
# =============================================================================
# IDIA Server — Post-Deploy Smoke Test
# =============================================================================
#
# Verifies that the IDIA Server LLM endpoint responds correctly after deploy.
# Tests each configured model with a minimal chat completion request.
#
# Usage:
#   ./scripts/smoke_test.sh              # test against localhost
#   ./scripts/smoke_test.sh <base-url>   # test against remote host
#
# Examples:
#   # Local deployment (Docker Compose)
#   ./scripts/smoke_test.sh
#
#   # Remote deployment (AWS)
#   ./scripts/smoke_test.sh http://54.123.45.67:4000
#
# Prerequisites:
#   - curl and jq installed
#   - LITELLM_MASTER_KEY set in .env or environment
#   - Server must be running and accepting requests
#
# Returns exit code 0 only if ALL configured models respond correctly.
# =============================================================================

set -euo pipefail

# ── Paths ──────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$REPO_DIR/.env"

# ── Load .env if present ───────────────────────────────────────────────────

if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck source=/dev/null
    source "$ENV_FILE"
    set +a
fi

# ── Configuration ──────────────────────────────────────────────────────────

BASE_URL="${1:-http://localhost:4000}"
MASTER_KEY="${LITELLM_MASTER_KEY:-}"
TIMEOUT_SEC=60  # Max wait for first response (cold start)

if [ -z "$MASTER_KEY" ]; then
    echo "ERROR: LITELLM_MASTER_KEY is not set."
    echo "Set it in .env or export LITELLM_MASTER_KEY=sk-..."
    exit 1
fi

# ── Collect models ─────────────────────────────────────────────────────────

MODELS=()
MODELS_COUNT="${MODELS_COUNT:-0}"
if [ "$MODELS_COUNT" -gt 0 ] 2>/dev/null; then
    for n in $(seq 1 "$MODELS_COUNT"); do
        id_var="MODEL_${n}_ID"
        mid="${!id_var:-}"
        [ -n "$mid" ] && MODELS+=("$mid")
    done
else
    MODELS=("${MODEL_ID:-}")
fi

if [ ${#MODELS[@]} -eq 0 ] || [ -z "${MODELS[0]:-}" ]; then
    echo "ERROR: No model IDs found. Set MODEL_ID or MODELS_COUNT in .env."
    exit 1
fi

# ── Smoke test helpers ─────────────────────────────────────────────────────

pass=0
fail=0

test_model() {
    local model="$1"
    echo "  Testing model: $model ..."

    local response
    response=$(curl -s -o /dev/null -w "%{http_code}" \
        --max-time "$TIMEOUT_SEC" \
        -X POST "${BASE_URL}/chat/completions" \
        -H "Authorization: Bearer ${MASTER_KEY}" \
        -H "Content-Type: application/json" \
        -d "$(cat <<EOF
{
    "model": "$model",
    "messages": [{"role": "user", "content": "Responda apenas: OK"}],
    "max_tokens": 10
}
EOF
)" 2>&1) || true

    if [ "$response" = "200" ]; then
        echo "  ✓ $model — HTTP 200"
        pass=$((pass + 1))
    else
        echo "  ✗ $model — HTTP $response (expected 200)"
        fail=$((fail + 1))
    fi
}

# ── Run tests ──────────────────────────────────────────────────────────────

echo ""
echo "=========================================="
echo " Smoke Test — $BASE_URL"
echo "=========================================="
echo ""

for model in "${MODELS[@]}"; do
    [ -z "$model" ] && continue
    test_model "$model"
    echo ""
done

# ── Summary ────────────────────────────────────────────────────────────────

echo "=========================================="
echo " Results: ${pass} passed, ${fail} failed"
echo "=========================================="

if [ "$fail" -gt 0 ]; then
    exit 1
fi
