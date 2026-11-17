#!/usr/bin/env bash
# Build the server's virtualenv and register it with Codex (and optionally Claude Code).
#
#   scripts/install.sh              build + register with Codex as "adb"
#   scripts/install.sh --claude     also register with Claude Code (user scope)
#   scripts/install.sh --no-codex   build only (combine with --claude if wanted)
#   scripts/install.sh --name foo   register under a different server name
#
# Safe to re-run: it replaces any existing registration of the same name.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME="adb"
DO_CODEX=1
DO_CLAUDE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="${2:?--name needs a value}"; shift 2 ;;
    --claude) DO_CLAUDE=1; shift ;;
    --no-codex) DO_CODEX=0; shift ;;
    -h|--help) sed -n '2,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

echo "==> Building virtualenv in $DIR/.venv"
if command -v uv >/dev/null 2>&1; then
  (cd "$DIR" && uv sync --quiet)
elif command -v python3 >/dev/null 2>&1; then
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
    || { echo "Python 3.10+ is required (or install uv: https://docs.astral.sh/uv/)." >&2; exit 1; }
  python3 -m venv "$DIR/.venv"
  "$DIR/.venv/bin/pip" install --quiet --upgrade pip
  "$DIR/.venv/bin/pip" install --quiet -e "$DIR"
else
  echo "Need either uv or python3 (3.10+) on PATH." >&2
  exit 1
fi
BIN="$DIR/.venv/bin/adb-mcp"

# Codex starts MCP servers with a trimmed environment, so pin adb's location
# rather than relying on PATH at launch time.
ADB="${ADB_PATH:-}"
if [[ -z "$ADB" ]]; then
  ADB="$(command -v adb || true)"
fi
if [[ -z "$ADB" ]]; then
  for c in "${ANDROID_HOME:-}" "${ANDROID_SDK_ROOT:-}" "$HOME/Android/Sdk" "$HOME/android-sdk" "$HOME/Library/Android/sdk"; do
    if [[ -n "$c" && -x "$c/platform-tools/adb" ]]; then ADB="$c/platform-tools/adb"; break; fi
  done
fi
ENV_ARGS=()
if [[ -n "$ADB" ]]; then
  ENV_ARGS=(--env "ADB_PATH=$ADB")
else
  echo "!!  adb not found. Install Android platform-tools, then re-run (or: ADB_PATH=/path/to/adb $0)." >&2
fi

echo "==> Self-check"
ADB_PATH="$ADB" "$BIN" --check || true

if [[ $DO_CODEX -eq 1 ]]; then
  if command -v codex >/dev/null 2>&1; then
    echo "==> Registering '$NAME' with Codex"
    codex mcp remove "$NAME" >/dev/null 2>&1 || true
    codex mcp add "$NAME" ${ENV_ARGS[@]+"${ENV_ARGS[@]}"} -- "$BIN"
  else
    echo "==> codex CLI not found. Add this to ~/.codex/config.toml yourself:"
    echo
    echo "[mcp_servers.$NAME]"
    echo "command = \"$BIN\""
    [[ -n "$ADB" ]] && echo "env = { ADB_PATH = \"$ADB\" }"
    echo
  fi
fi

if [[ $DO_CLAUDE -eq 1 ]]; then
  if command -v claude >/dev/null 2>&1; then
    echo "==> Registering '$NAME' with Claude Code (user scope)"
    claude mcp remove --scope user "$NAME" >/dev/null 2>&1 || true
    claude mcp add --scope user "$NAME" ${ENV_ARGS[@]+"${ENV_ARGS[@]}"} -- "$BIN"
  else
    echo "!!  claude CLI not found; skipped." >&2
  fi
fi

echo
if [[ $DO_CODEX -eq 1 ]] && command -v codex >/dev/null 2>&1; then
  echo "Done. Start a new Codex session and the '$NAME' tools will be available."
  echo "Verify with: codex mcp list"
else
  echo "Done. Server binary: $BIN"
fi
