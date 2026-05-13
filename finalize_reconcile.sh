#!/bin/bash
set -euo pipefail
cd "/workspaces/LSB_36_Odds"

echo "Cleaning up diagnostic scripts..."
rm -f diagnose_divergence.sh reconcile_branches.sh
rm -f .vscode/tasks.json

echo "Committing reconciliation..."
git add -A
git commit -m "Reconcile divergent branches: main now tracks upstream/main (new_3680_apr2026)"

echo "✅ Complete!"
git log --oneline -3

rm -f "$0"
