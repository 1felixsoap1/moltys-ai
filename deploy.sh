#!/usr/bin/env bash
# Deploy Moltys.AI: scan picks, check resolutions, export, push.
# Each stage is isolated — a failure in one doesn't block the rest.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

POLY_DIR="$(cd "$SCRIPT_DIR/../Polymarket" && pwd)"

echo "==> Scanning for fresh picks..."
if python3 "$POLY_DIR/VelocityCompounder.py" scan; then
    echo "    Scan OK."
else
    echo "    WARNING: Scan failed (exit $?). Using stale picks."
fi

echo "==> Checking for new resolutions..."
if python3 "$POLY_DIR/VelocityMonitor.py" --status; then
    echo "    Resolution check OK."
else
    echo "    WARNING: Resolution check failed (exit $?). Continuing."
fi

echo "==> Exporting data..."
if ! python3 export_data.py; then
    echo "    ERROR: Export failed. Aborting deploy."
    exit 1
fi

echo "==> Committing..."
git add data/
git diff --cached --quiet && { echo "No changes to commit."; exit 0; }
git commit -m "Update picks $(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo "==> Pushing..."
if ! git push; then
    echo "    ERROR: Push failed. Data committed locally but not deployed."
    exit 1
fi

echo "==> Done. Vercel will auto-deploy."
