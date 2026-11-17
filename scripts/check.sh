#!/usr/bin/env bash
# Show what the server sees: adb location/version, tool count, connected devices.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -x "$DIR/.venv/bin/adb-mcp" ]] || { echo "Not installed yet. Run scripts/install.sh first." >&2; exit 1; }
exec "$DIR/.venv/bin/adb-mcp" --check
