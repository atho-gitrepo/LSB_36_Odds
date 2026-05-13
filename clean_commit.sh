#!/bin/bash
set -euo pipefail
cd "/workspaces/LSB_36_Odds"

if [ -d ".git.lsb_backup" ]; then
  echo "Removing .git.lsb_backup"
  rm -rf ".git.lsb_backup"
fi

if [ -f ".vscode/tasks.json" ]; then
  echo "Removing temporary task file"
  rm -f ".vscode/tasks.json"
fi

if [ -f "clone_new_3680_apr2026.sh" ]; then
  echo "Removing temporary clone script"
  rm -f "clone_new_3680_apr2026.sh"
fi

git add -A
if git diff --cached --quiet; then
  echo "No changes to commit."
else
  git commit -m "Clean up and commit"
fi

rm -f "$0"
