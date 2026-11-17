#!/usr/bin/env bash
# Unregister the server from Codex and Claude Code. Pass --purge to also delete .venv.
#
#   scripts/uninstall.sh [--name adb] [--purge]
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME="adb"
PURGE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="${2:?--name needs a value}"; shift 2 ;;
    --purge) PURGE=1; shift ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

if command -v codex >/dev/null 2>&1 && codex mcp remove "$NAME" >/dev/null 2>&1; then
  echo "Removed '$NAME' from Codex."
fi
if command -v claude >/dev/null 2>&1 && claude mcp remove --scope user "$NAME" >/dev/null 2>&1; then
  echo "Removed '$NAME' from Claude Code."
fi
if [[ $PURGE -eq 1 ]]; then
  rm -rf "$DIR/.venv"
  echo "Deleted $DIR/.venv"
fi
