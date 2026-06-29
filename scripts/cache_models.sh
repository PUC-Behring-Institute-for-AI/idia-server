#!/usr/bin/env bash
# =============================================================================
# IDIA Server — Model Cache (S3)
# =============================================================================
#
# Downloads model weights from HuggingFace and uploads them to S3 so that
# AWS GPU workers can sync them locally instead of re-downloading from HF.
# Reduces cold start from ~15 min to ~2 min (S3 sync).
#
# Usage:
#   ./scripts/cache_models.sh [--dry-run] [MODEL_1_SOURCE MODEL_2_SOURCE ...]
#
# If no MODEL_*_SOURCE arguments are given, reads from .env (both single
# and multi-model modes). Run before ./scripts/deploy_cluster.sh.
#
# Prerequisites:
#   - huggingface-cli installed  (pip install huggingface_hub)
#   - AWS CLI configured          (aws configure)
#   - HF_TOKEN set in .env       (for gated models)
#
# Environment:
#   S3_BUCKET           S3 bucket name (default: idia-models-cache-<aws-account>)
#   AWS_DEFAULT_REGION  AWS region (default: us-east-1)
#
# S3 bucket layout:
#   s3://<bucket>/hf-cache/<model_source>/
#
# See docs/ARCHITECTURE.md §7.3 for the cache workflow.
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

S3_BUCKET="${S3_BUCKET:-}"
if [ -z "$S3_BUCKET" ]; then
    ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo "unknown")
    S3_BUCKET="idia-models-cache-${ACCOUNT_ID}"
fi

CACHE_DIR="${HOME}/.cache/huggingface"
S3_PREFIX="hf-cache"

# ── Dry-run flag ───────────────────────────────────────────────────────────

DRY_RUN=false
if [ "${1:-}" = "--dry-run" ]; then
    DRY_RUN=true
    shift
fi

# ── Collect model sources ──────────────────────────────────────────────────

MODEL_SOURCES=()

if [ $# -gt 0 ]; then
    # From command line arguments
    MODEL_SOURCES=("$@")
else
    # From .env — handle both single and multi-model modes
    MODELS_COUNT="${MODELS_COUNT:-0}"
    if [ "$MODELS_COUNT" -gt 0 ]; 2>/dev/null; then
        for n in $(seq 1 "$MODELS_COUNT"); do
            src_var="MODEL_${n}_SOURCE"
            src="${!src_var:-}"
            if [ -n "$src" ]; then
                MODEL_SOURCES+=("$src")
            fi
        done
    else
        MODEL_SOURCES=("${MODEL_SOURCE:-}")
    fi
fi

if [ ${#MODEL_SOURCES[@]} -eq 0 ] || [ -z "${MODEL_SOURCES[0]:-}" ]; then
    echo "ERROR: No model sources found. Set MODEL_SOURCE or MODELS_COUNT in .env,"
    echo "       or pass model sources as arguments."
    exit 1
fi

# ── Ensure bucket exists ───────────────────────────────────────────────────

if [ "$DRY_RUN" = false ]; then
    if ! aws s3 ls "s3://${S3_BUCKET}" &> /dev/null; then
        echo "Creating S3 bucket: s3://${S3_BUCKET}"
        aws s3 mb "s3://${S3_BUCKET}" --region "${AWS_DEFAULT_REGION:-us-east-1}"
    else
        echo "Bucket s3://${S3_BUCKET} already exists."
    fi
fi

# ── Cache each model ───────────────────────────────────────────────────────

for SOURCE in "${MODEL_SOURCES[@]}"; do
    [ -z "$SOURCE" ] && continue
    echo ""
    echo "=========================================="
    echo " Model: $SOURCE"
    echo "=========================================="

    # Download from HF
    if command -v huggingface-cli &> /dev/null; then
        echo "  [1/2] Downloading from HuggingFace..."
        if [ "$DRY_RUN" = false ]; then
            huggingface-cli download "$SOURCE" \
                --local-dir "$CACHE_DIR/hub" \
                --resume-download \
                --quiet 2>&1 || {
                echo "  WARN: Download failed for $SOURCE — skipping"
                continue
            }
        fi
    else
        echo "  WARN: huggingface-cli not found — install with: pip install huggingface_hub"
        echo "  Skipping download for $SOURCE"
        continue
    fi

    # Upload to S3
    local_path="$CACHE_DIR"
    s3_path="s3://${S3_BUCKET}/${S3_PREFIX}/"
    echo "  [2/2] Uploading to S3..."

    if [ "$DRY_RUN" = false ]; then
        aws s3 sync "$local_path" "$s3_path" \
            --quiet \
            --no-progress
        echo "  ✓ Cached: $s3_path"
    else
        echo "  [DRY-RUN] Would sync: $local_path → $s3_path"
    fi
done

# ── Output ─────────────────────────────────────────────────────────────────

echo ""
echo "=========================================="
echo " Cache complete"
echo "=========================================="
echo ""
echo "Models cached: ${#MODEL_SOURCES[@]}"
echo "Bucket: s3://${S3_BUCKET}/${S3_PREFIX}/"
echo ""
echo "To sync on GPU worker startup, add to cluster.yaml:"
echo "  head_setup_commands:"
echo "    - aws s3 sync s3://${S3_BUCKET}/${S3_PREFIX}/ /root/.cache/huggingface/ --quiet"
echo "  worker_setup_commands:"
echo "    - aws s3 sync s3://${S3_BUCKET}/${S3_PREFIX}/ /root/.cache/huggingface/ --quiet"
echo ""
