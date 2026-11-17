"""Thin async wrapper around the adb binary."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


class AdbError(Exception):
    """An adb failure with a message meant to be read by the calling model."""


@dataclass
class Result:
    stdout: str
    stderr: str
    code: int
    raw: bytes = b""

    @property
    def text(self) -> str:
        """stdout and stderr merged, for showing to the caller."""
        parts = [p.strip("\n") for p in (self.stdout, self.stderr) if p.strip()]
        return "\n".join(parts)


def find_adb() -> str:
    explicit = os.environ.get("ADB_PATH")
    if explicit:
        if os.path.isfile(explicit) and os.access(explicit, os.X_OK):
            return explicit
        raise AdbError(f"ADB_PATH is set to '{explicit}' but that is not an executable file.")

    exe = "adb.exe" if sys.platform == "win32" else "adb"
    candidates: list[str | Path | None] = [shutil.which("adb")]
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(var):
            candidates.append(Path(os.environ[var]) / "platform-tools" / exe)
    home = Path.home()
    if os.environ.get("LOCALAPPDATA"):  # Android Studio's default SDK location on Windows
        candidates.append(Path(os.environ["LOCALAPPDATA"]) / "Android" / "Sdk" / "platform-tools" / exe)
    candidates += [
        home / "Android" / "Sdk" / "platform-tools" / exe,
        home / "android-sdk" / "platform-tools" / exe,
        home / "Library" / "Android" / "sdk" / "platform-tools" / exe,
        "/opt/android-sdk/platform-tools/adb",
        "/usr/lib/android-sdk/platform-tools/adb",
    ]
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return str(c)
    raise AdbError(
        "adb binary not found. Install Android platform-tools, or set the ADB_PATH "
        "environment variable for this MCP server to the full path of adb."
    )


async def run(
    args: list[str],
    *,
    serial: str | None = None,
    timeout: float = 30.0,
    check: bool = True,
    binary: bool = False,
) -> Result:
    """Run `adb [-s serial] args...`.

    stdin is always /dev/null: the server's own stdin is the MCP transport and
    `adb shell` would otherwise swallow protocol bytes from it.
    """
    cmd = [find_adb()]
    if serial:
        cmd += ["-s", serial]
    cmd += args
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise AdbError(f"Timed out after {timeout:g}s running: adb {' '.join(args)}") from None
    except asyncio.CancelledError:
        proc.kill()
        raise

    result = Result(
        stdout="" if binary else out.decode("utf-8", "replace").replace("\r\n", "\n"),
        stderr=err.decode("utf-8", "replace").replace("\r\n", "\n"),
        code=proc.returncode or 0,
        raw=out if binary else b"",
    )
    if check and result.code != 0:
        detail = result.stderr.strip() or (out.decode("utf-8", "replace").strip()[:2000])
        raise AdbError(f"adb {' '.join(args)} failed (exit {result.code}): {detail}")
    return result


@dataclass
class Device:
    serial: str
    state: str
    details: dict[str, str]


async def devices() -> list[Device]:
    r = await run(["devices", "-l"], timeout=15)
    found = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith(("List of devices", "*")):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        details = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        found.append(Device(parts[0], parts[1], details))
    return found


_STATE_HINTS = {
    "unauthorized": "unlock the device and accept the 'Allow USB debugging' prompt",
    "offline": "replug the cable or reconnect; `adb_command(['kill-server'])` can help",
    "no permissions": "udev rules are missing for this device on the host",
}


async def resolve_serial(serial: str | None) -> str:
    """Pick the device to talk to, failing with an actionable message."""
    devs = await devices()
    wanted = serial or os.environ.get("ANDROID_SERIAL")
    if wanted:
        for d in devs:
            if d.serial == wanted:
                if d.state != "device":
                    hint = _STATE_HINTS.get(d.state, "")
                    raise AdbError(f"Device {wanted} is '{d.state}'" + (f": {hint}." if hint else "."))
                return wanted
        listing = ", ".join(d.serial for d in devs) or "none"
        raise AdbError(f"Device '{wanted}' is not connected. Connected devices: {listing}.")

    ready = [d for d in devs if d.state == "device"]
    if len(ready) == 1:
        return ready[0].serial
    if not ready:
        if devs:
            lines = "; ".join(
                f"{d.serial} is '{d.state}'" + (f" ({_STATE_HINTS[d.state]})" if d.state in _STATE_HINTS else "")
                for d in devs
            )
            raise AdbError(f"No usable device. {lines}.")
        raise AdbError(
            "No Android devices connected. Plug one in with USB debugging enabled, start an "
            "emulator, or use connect_device for a device on the network."
        )
    listing = ", ".join(f"{d.serial} ({d.details.get('model', '?')})" for d in ready)
    raise AdbError(f"Multiple devices connected; pass `serial`. Options: {listing}.")
