# adb-mcp

An MCP server that gives coding agents (Codex, Claude Code, ...) a solid set of tools for
Android devices and emulators over `adb`: inspect devices, install and launch apps, read and
drive the UI, pull logs and crashes, move files, and forward ports.

It runs locally as a stdio process next to your `adb` binary; the agent starts it on demand.

## Install

Requirements: `adb` (Android platform-tools), plus either [`uv`](https://docs.astral.sh/uv/)
or Python 3.10+.

```bash
scripts/install.sh            # build .venv and register with Codex as "adb"
scripts/install.sh --claude   # ...and also with Claude Code
```

The script builds a virtualenv in `.venv/`, locates `adb`, and runs
`codex mcp add adb --env ADB_PATH=... -- <this dir>/.venv/bin/adb-mcp`. Re-running it is safe.
Then start a new Codex session; `codex mcp list` should show `adb`.

Other scripts:

| Script | Purpose |
| --- | --- |
| `scripts/check.sh` | Show the adb binary in use, tool count, and connected devices |
| `scripts/uninstall.sh [--purge]` | Unregister from Codex/Claude; `--purge` also deletes `.venv` |

### Manual registration

The install script writes the equivalent of this to `~/.codex/config.toml`:

```toml
[mcp_servers.adb]
command = "/absolute/path/to/adb-mcp/.venv/bin/adb-mcp"
env = { ADB_PATH = "/absolute/path/to/platform-tools/adb" }
# Optional: Codex kills tool calls after 60s by default. Raise it if you record long
# videos or install large APKs.
# tool_timeout_sec = 300
```

The package is installed in editable mode, so edits under `src/` take effect the next time the
agent starts the server. If you move this folder, re-run `scripts/install.sh`.

## Configuration

| Env var | Effect |
| --- | --- |
| `ADB_PATH` | Full path to `adb`. Otherwise looked up on `PATH`, `$ANDROID_HOME`, and common SDK locations. |
| `ANDROID_SERIAL` | Default device when a tool call gives no `serial`. |

Every device tool takes an optional `serial`. With exactly one device connected it is picked
automatically; with several, the error lists the serials to choose from.

## Tools

**Devices** — `list_devices`, `device_info`, `connect_device`, `disconnect_device`,
`pair_device`, `enable_tcpip`, `reboot`

**Apps** — `list_packages`, `package_info`, `install_apk`, `uninstall_app`, `start_app`,
`stop_app`, `clear_app_data`, `set_permission`, `current_activity`, `send_intent`

**Screen & UI** — `ui_dump` (on-screen elements with tap coordinates), `tap_element` (tap by
text / resource id / content description), `wait_for_element`, `tap`, `swipe`, `scroll`,
`input_text`, `press_key`, `screenshot`, `screen_record`

**Logs** — `logcat` (filter by package, tag, priority, regex, or the crash buffer),
`clear_logcat`

**Files** — `push_file`, `pull_file`, `list_files`

**Ports** — `forward_port` (forward and reverse), `list_forwards`, `remove_forward`

**Escape hatches** — `shell` (any device shell command), `adb_command` (any host adb command)

Tools carry read-only / destructive annotations so clients can decide what to prompt for.
Large outputs are truncated to keep the agent's context usable.

## Notes and limits

- `input_text` is ASCII-only; that is a limitation of Android's `input text`.
- `ui_dump` relies on `uiautomator`, which cannot see inside games, most WebViews, or Flutter
  views without semantics, and fails while the screen is animating. Use `screenshot` there.
- `screenshot` downscales to 1024px on the long side by default and reports the scale;
  coordinates for `tap`/`swipe` are always device pixels.
- `shell` and `adb_command` can do anything `adb` can, including destructive things. Rely on
  your agent's approval settings accordingly.

## Debugging the server

```bash
scripts/check.sh
npx @modelcontextprotocol/inspector .venv/bin/adb-mcp   # interactive tool browser
```
