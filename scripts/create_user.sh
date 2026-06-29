#!/usr/bin/env bash
# =============================================================================
# IDIA Server — Create LiteLLM User
# =============================================================================
#
# Creates a virtual key for a new LiteLLM user, scoped to a rate-limit team.
#
# Usage:
#   ./scripts/create_user.sh <user-name> <team-alias>
#
# Arguments:
#   user-name   Unique identifier for the user (e.g. "alice", "bob")
#   team-alias  Rate-limit tier: hard, regular, or light
#                 hard    = 15 RPM / 50000 TPM (researchers, batch)
#                 regular =  4 RPM / 15000 TPM (masters students)
#                 light   =  1 RPM /  5000 TPM (undergrads)
#
# Prerequisites:
#   - LITELLM_MASTER_KEY must be set in .env or environment
#   - LiteLLM proxy must be running
#
# Environment:
#   LITELLM_BASE_URL   Proxy URL (default: http://localhost:4000)
#   LITELLM_MASTER_KEY Admin key for authentication
#
# Output:
#   JSON with the created key details — save this for the user.
#
# See docs/ARCHITECTURE.md §4.3 for rate-limit tier definitions.
# =============================================================================

set -euo pipefail

# ── Paths ──────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$REPO_DIR/.env"

# ── Help ────────────────────────────────────────────────────────────────────

if [ "${1:-}" = "--help" ] || [ $# -lt 2 ]; then
    sed -n 's/^# //p; s/^#$//p' "$0"
    exit 0
fi

USER_NAME="$1"
TEAM_ALIAS="$2"

# ── Validate team alias ─────────────────────────────────────────────────────

VALID_TEAMS=("hard" "regular" "light")
match=0
for t in "${VALID_TEAMS[@]}"; do
    if [ "$TEAM_ALIAS" = "$t" ]; then
        match=1
        break
    fi
done
if [ "$match" -eq 0 ]; then
    echo "ERROR: Invalid team alias '$TEAM_ALIAS'. Must be one of: ${VALID_TEAMS[*]}"
    exit 1
fi

# ── Load .env ───────────────────────────────────────────────────────────────

if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck source=/dev/null
    source "$ENV_FILE"
    set +a
fi

LITELLM_BASE_URL="${LITELLM_BASE_URL:-http://localhost:4000}"
LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY:-}"

if [ -z "$LITELLM_MASTER_KEY" ]; then
    echo "ERROR: LITELLM_MASTER_KEY is not set."
    echo "Set it in .env or export LITELLM_MASTER_KEY=sk-..."
    exit 1
fi

# ── Create key via LiteLLM API ─────────────────────────────────────────────

echo "Creating virtual key for user='$USER_NAME' team='$TEAM_ALIAS'..."

RESPONSE=$(curl -s -X POST "$LITELLM_BASE_URL/key/generate" \
    -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
    -H "Content-Type: application/json" \
    -d "$(cat <<EOF
{
    "user_id": "$USER_NAME",
    "team_id": "$TEAM_ALIAS",
    "metadata": {
        "created_by": "idia-server/scripts/create_user.sh",
        "user": "$USER_NAME",
        "tier": "$TEAM_ALIAS"
    }
}
EOF
)" 2>&1)

# ── Check response ─────────────────────────────────────────────────────────

if echo "$RESPONSE" | python3 -c "import sys,json; data=json.load(sys.stdin); sys.exit(0 if data.get('key') else 1)" 2>/dev/null; then
    echo ""
    echo "=========================================="
    echo " Key created successfully"
    echo "=========================================="
    echo "$RESPONSE" | python3 -m json.tool
    echo ""
    echo "Share with $USER_NAME:"
    echo "  export OPENAI_API_KEY=\$(echo \"$RESPONSE\" | python3 -c \"import sys,json; print(json.load(sys.stdin)['key'])\")"
    echo "  export OPENAI_BASE_URL=http://<idia-server-ip>:4000"
else
    echo "ERROR: Failed to create key."
    echo "Response:"
    echo "$RESPONSE"
    exit 1
fi
