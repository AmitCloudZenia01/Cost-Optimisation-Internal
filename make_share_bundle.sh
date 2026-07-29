#!/usr/bin/env bash
# Build a distributable copy of the tool with no secrets and no client data.
#
# The project folder is NOT a git repository, so .gitignore protects nothing
# when you copy or zip it. This script is the supported way to hand the tool
# to someone else: it copies only source, then verifies the result is clean.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$HOME/Desktop/cost-optimisation-share}"
rm -rf "$OUT" && mkdir -p "$OUT"

rsync -a \
  --exclude='.vantage_token'      --exclude='*client_secret*'  \
  --exclude='*credentials*.json'  --exclude='token.json'       \
  --exclude='snapshots/'          --exclude='*.xlsx'           \
  --exclude='tmp*.json'           --exclude='__pycache__/'     \
  --exclude='*.pyc'               --exclude='.venv/'           \
  --exclude='.DS_Store'           --exclude='.git/'            \
  "$SRC/" "$OUT/"

# Verify rather than trust the exclude list.
FOUND=$(grep -rIlE '[0-9]{12}|AKIA[0-9A-Z]{16}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' \
        "$OUT" 2>/dev/null | grep -v '\.md$' || true)
if [ -n "$FOUND" ]; then
  echo "REFUSING TO SHIP — possible account id / key / token in:"; echo "$FOUND"; exit 1
fi

echo "clean bundle: $OUT"
find "$OUT" -type f | wc -l | xargs echo "files:"
