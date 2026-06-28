#!/usr/bin/env bash
# =============================================================================
# IDIA Server — AWS Cluster Deploy
# =============================================================================
#
# Deploys the IDIA Server on AWS using the Ray Cluster Launcher (cluster.yaml).
#
# Workflow (decision 2026-06-28 — pre-render approach):
#   1. Load .env and validate required vars
#   2. Pre-render serve_config.yaml (resolve ${VAR} placeholders locally)
#   3. Launch the Ray cluster on EC2 via ray up
#   4. Deploy the LLM application via ray exec
#   5. Print connection info for the dashboard tunnel
#
# Why pre-render instead of env vars on the head node?
#   serve_config.yaml has ${VAR} placeholders filled by render_config.py
#   (Phase 2 design). The Ray Cluster Launcher's file_mounts mechanism
#   copies static files — it doesn't substitute env vars. Pre-rendering
#   locally is simpler and doesn't hardcode secrets in cluster.yaml.
#
# Prerequisites:
#   - ray[default] installed (pip install "ray[default]")
#   - AWS credentials configured (aws configure)
#   - .env file with required variables
#
# Usage:
#   ./scripts/deploy_cluster.sh            # full deploy
#   ./scripts/deploy_cluster.sh --dry-run  # validate .env + pre-render only
#   ./scripts/deploy_cluster.sh --help     # this message
#
# See also:
#   docs/ARCHITECTURE.md §7.3 — Ray Cluster Launcher
#   docs/ARCHITECTURE.md §9 — Security hardening
# =============================================================================

set -euo pipefail

# ── Paths ──────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
CLUSTER_FILE="$REPO_DIR/cluster.yaml"
RENDERED_FILE="$REPO_DIR/rendered_config.yaml"
ENV_FILE="$REPO_DIR/.env"

# ── Help ────────────────────────────────────────────────────────────────────

if [ "${1:-}" = "--help" ]; then
    sed -n 's/^# //p; s/^#$//p' "$0"
    exit 0
fi

# ── Pre-flight checks ───────────────────────────────────────────────────────

if ! command -v ray &> /dev/null; then
    echo "ERROR: 'ray' CLI not found."
    echo "Install with:  pip install 'ray[default]'"
    exit 1
fi

if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: .env file not found at $ENV_FILE"
    echo "Copy .env.example to .env and fill in your values."
    exit 1
fi

# ── Load .env ───────────────────────────────────────────────────────────────

# Using set -a to auto-export all variables sourced from .env
set -a
# shellcheck source=/dev/null
source "$ENV_FILE"
set +a

REQUIRED_VARS=("HF_TOKEN" "LITELLM_MASTER_KEY" "MODEL_ID" "MODEL_SOURCE")
for var in "${REQUIRED_VARS[@]}"; do
    if [ -z "${!var:-}" ]; then
        echo "ERROR: Required variable '$var' is not set in .env"
        exit 1
    fi
done

echo "[OK] .env loaded successfully"
echo "     MODEL_ID=$MODEL_ID"
echo "     MODEL_SOURCE=$MODEL_SOURCE"

# ── Pre-render config ──────────────────────────────────────────────────────

echo ""
echo "[1/4] Pre-rendering serve_config.yaml..."

if [ ! -f "$SCRIPT_DIR/render_config.py" ]; then
    echo "ERROR: render_config.py not found at $SCRIPT_DIR/render_config.py"
    exit 1
fi

python3 "$SCRIPT_DIR/render_config.py" --dry-run > "$RENDERED_FILE"
echo "  ✓ Created $RENDERED_FILE"

# ── Dry-run mode ────────────────────────────────────────────────────────────

if [ "${1:-}" = "--dry-run" ]; then
    echo ""
    echo "=== Dry-run mode — cluster launch skipped ==="
    echo "Rendered config would be uploaded to /app/rendered_config.yaml:"
    echo "--------------------------------------------------------------"
    cat "$RENDERED_FILE"
    echo "--------------------------------------------------------------"
    echo "To launch:  ./scripts/deploy_cluster.sh"
    exit 0
fi

# ── Launch cluster ─────────────────────────────────────────────────────────

echo ""
echo "[2/4] Launching Ray cluster (this takes ~5-10 minutes)..."
echo "      Head node: m5.large (CPU)"
echo "      Worker:    g5.xlarge (GPU) × 0-4 (autoscaled)"

ray up -y "$CLUSTER_FILE"

echo "  ✓ Cluster launched"

# ── Deploy LLM app ──────────────────────────────────────────────────────────

echo ""
echo "[3/4] Deploying LLM app..."

# Wait briefly for Ray to be ready after ray up
sleep 5

ray exec "$CLUSTER_FILE" "serve run /app/rendered_config.yaml"

echo "  ✓ LLM app deployed"

# ── Done ─────────────────────────────────────────────────────────────────────

echo ""
echo "[4/4] Done!"
echo ""
echo "=========================================="
echo " IDIA Server deployed on AWS"
echo "=========================================="
echo ""
echo "Dashboard (SSH tunnel):"
echo "  ray dashboard $CLUSTER_FILE"
echo ""
echo "API endpoint (via head node public IP):"
echo "  curl -X POST http://<head-public-ip>:4000/chat/completions \\"
echo "    -H \"Authorization: Bearer \$LITELLM_MASTER_KEY\" \\"
echo "    -H \"Content-Type: application/json\" \\"
echo '    -d '\''{"model":"'"$MODEL_ID"'","messages":[{"role":"user","content":"ping"}]}'\'''
echo ""
echo "To scale down to zero workers:"
echo "  ray down -y $CLUSTER_FILE"
echo ""
echo "=========================================="
