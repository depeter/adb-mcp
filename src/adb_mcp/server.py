"""adb MCP server: tools for inspecting and driving Android devices."""

from __future__ import annotations

import asyncio
import io
import os
import re
import shlex
import sys
import time
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP, Image
from mcp.types import ToolAnnotations
from PIL import Image as PILImage
from pydantic import Field

from . import adb, ui
from .adb import AdbError

mcp = FastMCP(
    "adb",
    instructions=(
        "Tools for Android devices over adb. Every device tool takes an optional `serial`; omit it "
        "when one device is connected, otherwise get serials from list_devices.\n"
        "To drive the UI: call ui_dump to see on-screen elements with their tap coordinates, then "
        "tap_element / tap / input_text / press_key / scroll. Prefer ui_dump over screenshot for "
        "finding things to tap; use screenshot when you need to see visuals. All coordinates are "
        "device pixels.\n"
        "To debug an app: logcat(package=...) for its logs, logcat(buffer='crash') for crashes, "
        "package_info and current_activity for state. `shell` runs anything not covered by a "
        "dedicated tool."
    ),
)

READ = ToolAnnotations(readOnlyHint=True)
ACT = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

Serial = Annotated[
    str | None,
    Field(description="Device serial from list_devices. Omit when exactly one device is connected."),
]

MAX_OUTPUT = 30_000
q = shlex.quote


def clip(text: str, limit: int = MAX_OUTPUT, keep: Literal["both", "tail"] = "both") -> str:
    """Bound output size so one noisy command can't flood the model's context."""
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    if keep == "tail":
        return f"[... {omitted} earlier characters omitted ...]\n" + text[-limit:]
    half = limit // 2
    return text[:half] + f"\n[... {omitted} characters omitted ...]\n" + text[-half:]


async def sh(command: str, serial: str | None, timeout: float = 30.0, check: bool = True) -> str:
    """Run a device shell command on the resolved device and return its output."""
    s = await adb.resolve_serial(serial)
    r = await adb.run(["shell", command], serial=s, timeout=timeout, check=check)
    return r.text


def host_path(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


# --------------------------------------------------------------------------- devices


@mcp.tool(annotations=READ)
async def list_devices() -> str:
    """List connected Android devices and emulators with serial, state and model."""
    devs = await adb.devices()
    if not devs:
        return "No devices connected. Use connect_device for a network device, or plug one in with USB debugging enabled."
    lines = []
    for d in devs:
        extra = " ".join(f"{k}={v}" for k, v in d.details.items() if k in ("model", "device", "product"))
        lines.append(f"{d.serial}  state={d.state}  {extra}".rstrip())
    return "\n".join(lines)


@mcp.tool(annotations=READ)
async def device_info(serial: Serial = None) -> str:
    """Summarise a device: model, Android version/SDK, ABI, screen size and density, battery,
    whether the screen is awake/locked, Wi-Fi IP, free storage."""
    script = r"""
for p in ro.product.manufacturer ro.product.model ro.product.device ro.build.version.release \
         ro.build.version.sdk ro.product.cpu.abi ro.build.type ro.build.fingerprint; do
  echo "$p: $(getprop $p)"
done
echo "screen: $(wm size | tr '\n' ' ')$(wm density | tr '\n' ' ')"
echo "current display: $(dumpsys window displays | grep -o 'cur=[0-9]*x[0-9]*' | head -1)"
echo "wakefulness: $(dumpsys power | grep -o 'mWakefulness=[A-Za-z]*' | head -1 | cut -d= -f2)"
echo "keyguard showing: $(dumpsys window | grep -o 'mDreamingLockscreen=[a-z]*' | head -1 | cut -d= -f2)"
echo "battery: $(dumpsys battery | grep -E '^ *(level|status|temperature|AC powered|USB powered):' | tr -s ' ' | tr '\n' ';')"
echo "wifi ip: $(ip -f inet addr show wlan0 2>/dev/null | grep -o 'inet [0-9./]*' | cut -d' ' -f2)"
echo "data storage: $(df -h /data 2>/dev/null | tail -1)"
echo "shell user: $(id -un 2>/dev/null)"
"""
    return await sh(script, serial)


@mcp.tool(annotations=ACT)
async def connect_device(
    host: Annotated[str, Field(description="IP or hostname, optionally with :port (default 5555).")],
) -> str:
    """Connect to a device over TCP/IP (wireless debugging or an emulator). On Android 11+ with
    wireless debugging, run pair_device once first, and use the *connect* port shown on the
    device, not the pairing port."""
    target = host if ":" in host else f"{host}:5555"
    r = await adb.run(["connect", target], timeout=20, check=False)
    out = r.text
    if r.code != 0 or re.search(r"failed|cannot|unable|refused", out, re.I):
        raise AdbError(out or f"could not connect to {target}")
    return out


@mcp.tool(annotations=ACT)
async def disconnect_device(
    host: Annotated[str | None, Field(description="host[:port] to drop; omit to drop all TCP/IP devices.")] = None,
) -> str:
    """Disconnect a TCP/IP device (USB devices are unaffected)."""
    r = await adb.run(["disconnect"] + ([host] if host else []), timeout=15)
    return r.text or "disconnected"


@mcp.tool(annotations=ACT)
async def pair_device(
    host: Annotated[str, Field(description="IP:port shown under 'Pair device with pairing code'.")],
    code: Annotated[str, Field(description="The 6-digit pairing code shown on the device.")],
) -> str:
    """Pair with an Android 11+ device for wireless debugging. Follow with connect_device."""
    r = await adb.run(["pair", host, code], timeout=30, check=False)
    if r.code != 0 or "Successfully" not in r.text:
        raise AdbError(r.text or "pairing failed")
    return r.text


@mcp.tool(annotations=ACT)
async def enable_tcpip(port: int = 5555, serial: Serial = None) -> str:
    """Switch a USB-connected device to also listen for adb over TCP/IP on `port`, and report
    its Wi-Fi address so you can connect_device to it and unplug the cable."""
    s = await adb.resolve_serial(serial)
    ip = (await adb.run(
        ["shell", "ip -f inet addr show wlan0 2>/dev/null | grep -o 'inet [0-9.]*' | cut -d' ' -f2"],
        serial=s, check=False,
    )).stdout.strip()
    r = await adb.run(["tcpip", str(port)], serial=s, timeout=20)
    where = f"connect_device('{ip}:{port}')" if ip else "the device's IP (Wi-Fi address not detected)"
    return f"{r.text}\nNext: {where}"


@mcp.tool(annotations=DESTRUCTIVE)
async def reboot(
    mode: Annotated[
        Literal["system", "bootloader", "recovery", "sideload"],
        Field(description="'system' is a normal reboot."),
    ] = "system",
    serial: Serial = None,
) -> str:
    """Reboot the device. It drops off adb until it finishes booting."""
    s = await adb.resolve_serial(serial)
    await adb.run(["reboot"] + ([] if mode == "system" else [mode]), serial=s, timeout=30)
    return f"Rebooting {s} ({mode}). Poll list_devices until its state is 'device' again."


# --------------------------------------------------------------------------- shell


@mcp.tool()
async def shell(
    command: Annotated[str, Field(description="Command line for the device's sh; pipes and redirects work.")],
    timeout: Annotated[float, Field(description="Seconds before the command is killed.")] = 30,
    serial: Serial = None,
) -> str:
    """Run a shell command on the device (as the `shell` user) and return stdout, stderr and a
    non-zero exit code if any. Use for anything without a dedicated tool: dumpsys, getprop,
    settings, am/pm/cmd, run-as <package> for app-private files. Not for interactive or
    never-ending commands (use logcat for logs)."""
    s = await adb.resolve_serial(serial)
    r = await adb.run(["shell", command], serial=s, timeout=timeout, check=False)
    out = clip(r.text) or "(no output)"
    return out if r.code == 0 else f"{out}\n[exit code {r.code}]"


@mcp.tool()
async def adb_command(
    args: Annotated[list[str], Field(description="Arguments after `adb`, e.g. ['root'] or ['emu', 'kill'].")],
    timeout: float = 60,
    serial: Annotated[
        str | None, Field(description="Passed as `adb -s`. Unlike other tools it is not auto-selected.")
    ] = None,
) -> str:
    """Escape hatch: run a raw host-side adb command (root, unroot, remount, kill-server,
    start-server, bugreport, emu ..., wait-for-device, sideload). Prefer the dedicated tools."""
    r = await adb.run(args, serial=serial, timeout=timeout, check=False)
    out = clip(r.text) or "(no output)"
    return out if r.code == 0 else f"{out}\n[exit code {r.code}]"


# --------------------------------------------------------------------------- apps


@mcp.tool(annotations=READ)
async def list_packages(
    filter: Annotated[str | None, Field(description="Only package names containing this substring.")] = None,
    scope: Annotated[
        Literal["third_party", "system", "all"], Field(description="third_party = user-installed apps.")
    ] = "third_party",
    serial: Serial = None,
) -> str:
    """List installed package names."""
    flag = {"third_party": "-3", "system": "-s", "all": ""}[scope]
    out = await sh(f"pm list packages {flag}", serial)
    names = sorted(line.removeprefix("package:").strip() for line in out.splitlines() if line.startswith("package:"))
    if filter:
        names = [n for n in names if filter.lower() in n.lower()]
    return clip("\n".join(names)) if names else "No matching packages."


_PKG_FIELDS = (
    "versionName", "versionCode", "minSdk", "targetSdk", "firstInstallTime", "lastUpdateTime",
    "installerPackageName", "dataDir",
)


@mcp.tool(annotations=READ)
async def package_info(package: str, serial: Serial = None) -> str:
    """Details of an installed app: version, SDK levels, install times, APK paths, launcher
    activity, whether it is running, and granted runtime permissions."""
    s = await adb.resolve_serial(serial)
    p = q(package)
    dump = (await adb.run(["shell", f"dumpsys package {p}"], serial=s, timeout=30)).stdout
    if f"Package [{package}]" not in dump:
        raise AdbError(f"Package '{package}' is not installed. Use list_packages to find the right name.")
    section = dump[dump.index(f"Package [{package}]"):]
    nxt = section.find("\n  Package [", 1)
    if nxt > 0:
        section = section[:nxt]
    lines = [f"package: {package}"]
    for field in _PKG_FIELDS:
        m = re.search(rf"\b{field}=(\S+)", section)
        if m:
            lines.append(f"{field}: {m.group(1)}")
    rest = (await adb.run(
        ["shell", f"pm path {p}; echo ---; cmd package resolve-activity --brief -c android.intent.category.LAUNCHER {p} | tail -1; echo ---; pidof {p}"],
        serial=s, check=False,
    )).stdout.split("---")
    rest += [""] * (3 - len(rest))
    apks = [l.removeprefix("package:") for l in rest[0].split()]
    lines.append("apk paths: " + ", ".join(apks))
    launcher = rest[1].strip()
    lines.append("launcher activity: " + (launcher if "/" in launcher else "none"))
    lines.append("running pids: " + (rest[2].strip() or "not running"))
    # Install-time permissions are always granted and run to hundreds for system apps;
    # the runtime ones are what callers can change and care about.
    runtime = section.split("runtime permissions:", 1)[1] if "runtime permissions:" in section else ""
    granted = sorted(set(re.findall(r"^\s+([\w.]+): granted=true", runtime, re.M)))
    lines.append("granted runtime permissions: " + (", ".join(granted) if granted else "none"))
    return "\n".join(lines)


@mcp.tool(annotations=ACT)
async def install_apk(
    paths: Annotated[
        list[str],
        Field(description="Host path(s) to .apk files. Several paths are installed together as split APKs of one app."),
    ],
    grant_permissions: Annotated[bool, Field(description="Grant all runtime permissions at install (-g).")] = False,
    allow_downgrade: bool = False,
    allow_test_apk: Annotated[bool, Field(description="Allow testOnly APKs (-t).")] = False,
    serial: Serial = None,
) -> str:
    """Install an APK from the host machine, replacing an existing install and keeping its data."""
    s = await adb.resolve_serial(serial)
    files = [host_path(p) for p in paths]
    for f in files:
        if not os.path.isfile(f):
            raise AdbError(f"APK not found on host: {f}")
    if not files:
        raise AdbError("No APK paths given.")
    args = ["install" if len(files) == 1 else "install-multiple", "-r"]
    if grant_permissions:
        args.append("-g")
    if allow_downgrade:
        args.append("-d")
    if allow_test_apk:
        args.append("-t")
    r = await adb.run(args + files, serial=s, timeout=600, check=False)
    if r.code != 0 or "Success" not in r.text:
        raise AdbError(f"Install failed: {r.text}")
    return r.text


@mcp.tool(annotations=DESTRUCTIVE)
async def uninstall_app(
    package: str,
    keep_data: Annotated[bool, Field(description="Keep the app's data and cache directories (-k).")] = False,
    serial: Serial = None,
) -> str:
    """Uninstall an app."""
    s = await adb.resolve_serial(serial)
    r = await adb.run(["uninstall"] + (["-k"] if keep_data else []) + [package], serial=s, timeout=120, check=False)
    if "Success" not in r.text:
        raise AdbError(f"Uninstall failed: {r.text}")
    return r.text


@mcp.tool(annotations=ACT)
async def start_app(
    package: str,
    activity: Annotated[
        str | None,
        Field(description="Activity class, e.g. '.MainActivity'. Omit to start the launcher activity."),
    ] = None,
    stop_first: Annotated[bool, Field(description="Force-stop the app first for a cold start.")] = False,
    serial: Serial = None,
) -> str:
    """Launch an app and wait until its activity is drawn. Reports launch state and timing."""
    s = await adb.resolve_serial(serial)
    if activity:
        component = activity if "/" in activity else f"{package}/{activity}"
    else:
        out = (await adb.run(
            ["shell", f"cmd package resolve-activity --brief -c android.intent.category.LAUNCHER {q(package)} | tail -1"],
            serial=s, check=False,
        )).stdout.strip()
        if "/" not in out:
            raise AdbError(
                f"No launcher activity found for '{package}'. Is it installed (list_packages)? "
                "If it has no launcher icon, pass `activity` explicitly."
            )
        component = out
    flag = "-S " if stop_first else ""
    r = await adb.run(["shell", f"am start -W {flag}-n {q(component)}"], serial=s, timeout=60, check=False)
    if r.code != 0 or re.search(r"^Error", r.text, re.M):
        raise AdbError(r.text)
    return r.text


@mcp.tool(annotations=ACT)
async def stop_app(package: str, serial: Serial = None) -> str:
    """Force-stop an app (kills all its processes; data is kept)."""
    await sh(f"am force-stop {q(package)}", serial)
    return f"Stopped {package}."


@mcp.tool(annotations=DESTRUCTIVE)
async def clear_app_data(package: str, serial: Serial = None) -> str:
    """Wipe an app's data and cache, returning it to a freshly-installed state. Irreversible."""
    out = await sh(f"pm clear {q(package)}", serial)
    if "Success" not in out:
        raise AdbError(f"pm clear failed: {out}")
    return f"Cleared data for {package}."


@mcp.tool(annotations=ACT)
async def set_permission(
    package: str,
    permission: Annotated[str, Field(description="e.g. 'CAMERA' or 'android.permission.CAMERA'.")],
    grant: Annotated[bool, Field(description="True to grant, False to revoke.")] = True,
    serial: Serial = None,
) -> str:
    """Grant or revoke a runtime permission for an app."""
    perm = permission if "." in permission else f"android.permission.{permission}"
    verb = "grant" if grant else "revoke"
    out = await sh(f"pm {verb} {q(package)} {q(perm)}", serial)
    if out.strip():
        raise AdbError(out)
    return f"{verb}ed {perm} for {package}".replace("revokeed", "revoked")


@mcp.tool(annotations=READ)
async def current_activity(serial: Serial = None) -> str:
    """Show which app/activity is in the foreground and which window has input focus."""
    out = await sh(
        "dumpsys activity activities | grep -E 'topResumedActivity|mResumedActivity|ResumedActivity:' | head -3;"
        " dumpsys window | grep -E 'mCurrentFocus|mFocusedApp' | head -2",
        serial, check=False,
    )
    lines = list(dict.fromkeys(l.strip() for l in out.splitlines() if l.strip()))
    return "\n".join(lines) or "Could not determine the foreground activity."


@mcp.tool(annotations=ACT)
async def send_intent(
    kind: Annotated[
        Literal["activity", "broadcast", "service"],
        Field(description="activity = am start, broadcast = am broadcast, service = am start-foreground-service."),
    ] = "activity",
    action: Annotated[str | None, Field(description="e.g. 'android.intent.action.VIEW'.")] = None,
    data: Annotated[str | None, Field(description="Data URI, e.g. a URL or deep link.")] = None,
    component: Annotated[str | None, Field(description="'package/.Activity' to target explicitly.")] = None,
    package: Annotated[str | None, Field(description="Restrict resolution to this package.")] = None,
    category: str | None = None,
    mime_type: str | None = None,
    extras: Annotated[
        dict[str, str | bool | int | float] | None,
        Field(description="Intent extras; the JSON type picks string/boolean/int/float."),
    ] = None,
    serial: Serial = None,
) -> str:
    """Start an activity, send a broadcast, or start a service with an arbitrary intent. Use it
    to open URLs and deep links (action VIEW + data), trigger receivers, or launch with extras."""
    cmd = {"activity": "am start -W", "broadcast": "am broadcast", "service": "am start-foreground-service"}[kind]
    parts = [cmd]
    for flag, value in (("-a", action), ("-d", data), ("-t", mime_type), ("-c", category), ("-n", component)):
        if value:
            parts += [flag, q(value)]
    for key, value in (extras or {}).items():
        if isinstance(value, bool):
            parts += ["--ez", q(key), "true" if value else "false"]
        elif isinstance(value, int):
            parts += ["--ei", q(key), str(value)]
        elif isinstance(value, float):
            parts += ["--ef", q(key), str(value)]
        else:
            parts += ["--es", q(key), q(str(value))]
    if package:
        parts.append(q(package))
    if len(parts) == 1:
        raise AdbError("Give at least one of action, data, component or package.")
    out = await sh(" ".join(parts), serial, timeout=60, check=False)
    if re.search(r"^Error|unable to resolve Intent|SecurityException", out, re.M | re.I):
        raise AdbError(out)
    return clip(out)


# --------------------------------------------------------------------------- screen & UI


@mcp.tool(annotations=READ)
async def screenshot(
    max_dim: Annotated[
        int,
        Field(description="Downscale so the longer side is at most this many pixels. 0 = full-resolution PNG."),
    ] = 1024,
    save_path: Annotated[
        str | None, Field(description="Also save the full-resolution PNG to this host path.")
    ] = None,
    serial: Serial = None,
):
    """Capture the device screen and return it as an image. The accompanying text gives the real
    screen size and the scale of the returned image, which you need to convert positions in the
    image into device-pixel coordinates for tap/swipe. To locate UI elements, ui_dump is more
    precise and cheaper."""
    s = await adb.resolve_serial(serial)
    r = await adb.run(["exec-out", "screencap", "-p"], serial=s, timeout=30, binary=True, check=False)
    png = r.raw
    if not png.startswith(b"\x89PNG"):
        detail = (png[:300].decode("utf-8", "replace") + " " + r.stderr).strip()
        raise AdbError(f"screencap returned no image. {detail}")
    img = PILImage.open(io.BytesIO(png))
    w, h = img.size
    notes = [f"Screen is {w}x{h} device pixels."]
    if save_path:
        dest = host_path(save_path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(png)
        notes.append(f"Full-resolution PNG saved to {dest}.")

    if max_dim and max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        small = img.convert("RGB").resize((round(w * scale), round(h * scale)), PILImage.LANCZOS)
        buf = io.BytesIO()
        small.save(buf, format="JPEG", quality=80)
        data, fmt = buf.getvalue(), "jpeg"
        notes.append(
            f"Image below is {small.width}x{small.height} (scale {scale:.4f}). "
            f"Multiply image coordinates by {1 / scale:.4f} to get device pixels for tap/swipe."
        )
    else:
        data, fmt = png, "png"
        notes.append("Image below is full resolution; its pixel coordinates are device coordinates.")
    return [" ".join(notes), Image(data=data, format=fmt)]


async def _dump_elements(serial: str) -> list[ui.Element]:
    """Dump the view hierarchy. Tries stdout first, then a temp file for devices where
    /dev/tty dumping is unsupported."""
    r = await adb.run(["exec-out", "uiautomator", "dump", "/dev/tty"], serial=serial, timeout=30, check=False)
    xml = ui.extract_xml(r.stdout)
    if xml is None:
        tmp = "/data/local/tmp/adb_mcp_ui.xml"
        r = await adb.run(
            ["shell", f"uiautomator dump {tmp} >/dev/null 2>&1; cat {tmp} 2>/dev/null; rm -f {tmp}"],
            serial=serial, timeout=30, check=False,
        )
        xml = ui.extract_xml(r.stdout)
    if xml is None:
        raise AdbError(
            "uiautomator could not dump the UI. This happens while the screen is animating or playing "
            "video, when the device is locked/asleep, or on secure screens. Retry, or use screenshot. "
            f"Output: {r.text[:300]}"
        )
    try:
        return ui.parse(xml)
    except Exception as e:  # malformed XML from the device
        raise AdbError(f"Could not parse the UI hierarchy: {e}") from None


@mcp.tool(annotations=READ)
async def ui_dump(
    interactive_only: Annotated[
        bool, Field(description="Only elements you can act on (clickable, editable, scrollable, checkable).")
    ] = False,
    filter: Annotated[
        str | None, Field(description="Only elements whose text, description or id contains this (case-insensitive).")
    ] = None,
    serial: Serial = None,
) -> str:
    """List the elements currently on screen, one per line:
    `[index] Class "text" desc="content description" id=resource-id @(x,y) flags`
    where @(x,y) is the element's centre in device pixels, ready for tap. This is the best way
    to read the screen and find what to tap. Elements inside WebViews, games and some custom
    views (Flutter, Compose without semantics) may be missing; fall back to screenshot there."""
    s = await adb.resolve_serial(serial)
    elements = await _dump_elements(s)
    package = next((e.package for e in elements if e.package), "?")
    shown = elements
    if interactive_only:
        shown = [e for e in shown if e.interactive]
    if filter:
        f = filter.casefold()
        shown = [e for e in shown if any(f in v.casefold() for v in (e.text, e.desc, e.rid, e.label))]
    if not shown:
        return f"Foreground package: {package}. No matching elements ({len(elements)} on screen in total)."
    header = f"Foreground package: {package}. {len(shown)} elements:"
    return clip(header + "\n" + "\n".join(e.line() for e in shown))


def _describe_matches(matches: list[ui.Element]) -> str:
    return "\n".join(e.line() for e in matches[:15])


async def _find_one(
    serial: str, text: str | None, resource_id: str | None, content_desc: str | None, match_index: int | None
) -> ui.Element:
    if text is None and resource_id is None and content_desc is None:
        raise AdbError("Give at least one of text, resource_id or content_desc.")
    matches = ui.find(await _dump_elements(serial), text, resource_id, content_desc)
    if not matches:
        raise AdbError("No element matches. Call ui_dump to see what is on screen (it may need scrolling into view).")
    if match_index is not None:
        if not 0 <= match_index < len(matches):
            raise AdbError(f"match_index {match_index} out of range; {len(matches)} matches:\n{_describe_matches(matches)}")
        return matches[match_index]
    if len(matches) > 1:
        # A label inside a clickable row matches twice at the same spot; that is not ambiguous.
        if len({e.center for e in matches}) > 1:
            raise AdbError(
                f"{len(matches)} elements match; narrow the criteria or pass match_index (0-based, in this order):\n"
                + _describe_matches(matches)
            )
    return matches[0]


@mcp.tool(annotations=ACT)
async def tap_element(
    text: Annotated[str | None, Field(description="Visible text. Exact match is preferred, then substring; case-insensitive.")] = None,
    resource_id: Annotated[str | None, Field(description="Resource id, short ('login_button') or full ('com.app:id/login_button').")] = None,
    content_desc: Annotated[str | None, Field(description="Accessibility content description (used by icon buttons).")] = None,
    match_index: Annotated[int | None, Field(description="Which match to use when several elements match (0-based).")] = None,
    long_press: bool = False,
    serial: Serial = None,
) -> str:
    """Find an on-screen element by text, resource id and/or content description and tap its
    centre. More reliable than tapping raw coordinates. Fails, listing candidates, if the match
    is ambiguous."""
    s = await adb.resolve_serial(serial)
    e = await _find_one(s, text, resource_id, content_desc, match_index)
    x, y = e.center
    cmd = f"input swipe {x} {y} {x} {y} 800" if long_press else f"input tap {x} {y}"
    await adb.run(["shell", cmd], serial=s)
    return f"{'Long-pressed' if long_press else 'Tapped'} {e.line()}"


@mcp.tool(annotations=READ)
async def wait_for_element(
    text: str | None = None,
    resource_id: str | None = None,
    content_desc: str | None = None,
    timeout: Annotated[float, Field(description="Seconds to keep polling.", le=50)] = 15,
    gone: Annotated[bool, Field(description="Wait for the element to disappear instead of appear.")] = False,
    serial: Serial = None,
) -> str:
    """Poll the UI until an element matching the criteria appears (or disappears with gone=True).
    Use after actions that load a new screen instead of guessing a delay."""
    if text is None and resource_id is None and content_desc is None:
        raise AdbError("Give at least one of text, resource_id or content_desc.")
    s = await adb.resolve_serial(serial)
    deadline = time.monotonic() + timeout
    while True:
        try:
            matches = ui.find(await _dump_elements(s), text, resource_id, content_desc)
        except AdbError:
            matches = None  # dump fails mid-animation; keep polling
        if matches is not None and bool(matches) != gone:
            return "Element is gone." if gone else "Found:\n" + _describe_matches(matches)
        if time.monotonic() >= deadline:
            state = "still present" if gone else "did not appear"
            raise AdbError(f"Timed out after {timeout:g}s: element {state}. Call ui_dump to see the screen.")
        await asyncio.sleep(0.5)


@mcp.tool(annotations=ACT)
async def tap(
    x: int,
    y: int,
    long_press: bool = False,
    serial: Serial = None,
) -> str:
    """Tap (or long-press) at a position in device pixels. Get coordinates from ui_dump; if you
    take them from a downscaled screenshot, convert them with the scale it reported."""
    cmd = f"input swipe {x} {y} {x} {y} 800" if long_press else f"input tap {x} {y}"
    await sh(cmd, serial)
    return f"{'Long-pressed' if long_press else 'Tapped'} ({x},{y})."


@mcp.tool(annotations=ACT)
async def swipe(
    x1: int, y1: int, x2: int, y2: int,
    duration_ms: Annotated[int, Field(description="Slower swipes drag; fast ones fling.")] = 300,
    serial: Serial = None,
) -> str:
    """Swipe/drag from one point to another, in device pixels. For plain scrolling use scroll."""
    await sh(f"input swipe {x1} {y1} {x2} {y2} {duration_ms}", serial)
    return f"Swiped ({x1},{y1}) -> ({x2},{y2}) in {duration_ms}ms."


async def _display_size(serial: str) -> tuple[int, int]:
    """Current (rotation-aware) display size in pixels."""
    out = (await adb.run(
        ["shell", "dumpsys window displays | grep -o 'cur=[0-9]*x[0-9]*' | head -1; wm size"], serial=serial
    )).stdout
    m = re.search(r"cur=(\d+)x(\d+)", out) or re.search(r"Override size: (\d+)x(\d+)", out) \
        or re.search(r"Physical size: (\d+)x(\d+)", out)
    if not m:
        raise AdbError(f"Could not determine the screen size: {out.strip()}")
    return int(m.group(1)), int(m.group(2))


@mcp.tool(annotations=ACT)
async def scroll(
    direction: Annotated[
        Literal["down", "up", "left", "right"],
        Field(description="Direction to move through the content: 'down' reveals what is further below."),
    ] = "down",
    amount: Annotated[float, Field(description="Fraction of the screen to scroll.", gt=0, le=0.9)] = 0.5,
    serial: Serial = None,
) -> str:
    """Scroll the screen content by swiping across the middle of the display."""
    s = await adb.resolve_serial(serial)
    w, h = await _display_size(s)
    cx, cy = w // 2, h // 2
    dx, dy = int(w * amount / 2), int(h * amount / 2)
    x1, y1, x2, y2 = {
        "down": (cx, cy + dy, cx, cy - dy),
        "up": (cx, cy - dy, cx, cy + dy),
        "right": (cx + dx, cy, cx - dx, cy),
        "left": (cx - dx, cy, cx + dx, cy),
    }[direction]
    await adb.run(["shell", f"input swipe {x1} {y1} {x2} {y2} 400"], serial=s)
    return f"Scrolled {direction}."


@mcp.tool(annotations=ACT)
async def input_text(
    text: Annotated[str, Field(description="Text to type into the focused field. ASCII only.")],
    clear_first: Annotated[bool, Field(description="Delete the field's existing content first.")] = False,
    press_enter: Annotated[bool, Field(description="Press Enter afterwards (submits most forms/searches).")] = False,
    serial: Serial = None,
) -> str:
    """Type text into the currently focused input field. Tap the field first to focus it.
    Android's `input text` cannot type non-ASCII characters (accents, emoji, CJK)."""
    if not text.isascii():
        raise AdbError(
            "input_text only supports ASCII: Android's `input text` drops or crashes on other characters. "
            "Type the ASCII parts, or set the value another way (e.g. paste via the app, or a deep link)."
        )
    s = await adb.resolve_serial(serial)
    if clear_first:
        await adb.run(["shell", "input keyevent KEYCODE_MOVE_END " + "KEYCODE_DEL " * 200], serial=s, timeout=60)
    # `input text` treats %s as a space and cannot take a literal one; send line by line.
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line:
            await adb.run(["shell", "input text " + q(line.replace(" ", "%s"))], serial=s, timeout=60)
        if i < len(lines) - 1:
            await adb.run(["shell", "input keyevent KEYCODE_ENTER"], serial=s)
    if press_enter:
        await adb.run(["shell", "input keyevent KEYCODE_ENTER"], serial=s)
    return f"Typed {len(text)} characters."


@mcp.tool(annotations=ACT)
async def press_key(
    key: Annotated[
        str,
        Field(description=(
            "Key name without the KEYCODE_ prefix, or a numeric keycode. Common: HOME, BACK, ENTER, "
            "APP_SWITCH, MENU, POWER, WAKEUP, SLEEP, VOLUME_UP, VOLUME_DOWN, DEL, TAB, ESCAPE, "
            "DPAD_UP/DOWN/LEFT/RIGHT/CENTER, MEDIA_PLAY_PAUSE, SEARCH, NOTIFICATION."
        )),
    ],
    repeat: Annotated[int, Field(ge=1, le=50)] = 1,
    long_press: bool = False,
    serial: Serial = None,
) -> str:
    """Press a hardware or navigation key. WAKEUP turns the screen on without toggling it off
    again; MENU after WAKEUP dismisses a swipe-only lock screen."""
    k = key.strip().upper().replace(" ", "_")
    if not k.isdigit() and not k.startswith("KEYCODE_"):
        k = "KEYCODE_" + k
    if not re.fullmatch(r"\d+|KEYCODE_[A-Z0-9_]+", k):
        raise AdbError(f"Invalid key name: {key!r}")
    cmd = "input keyevent " + ("--longpress " if long_press else "") + " ".join([k] * repeat)
    out = await sh(cmd, serial)
    if "Invalid" in out or "Error" in out:
        raise AdbError(out)
    return f"Pressed {k}" + (f" x{repeat}" if repeat > 1 else "") + "."


@mcp.tool(annotations=ACT)
async def screen_record(
    save_path: Annotated[str, Field(description="Host path for the .mp4.")],
    duration: Annotated[int, Field(description="Seconds to record.", ge=1, le=180)] = 10,
    serial: Serial = None,
) -> str:
    """Record the screen for a fixed duration and save the video on the host. Blocks while
    recording, so trigger the behaviour you want to capture beforehand or keep it short."""
    s = await adb.resolve_serial(serial)
    dest = host_path(save_path)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = "/data/local/tmp/adb_mcp_record.mp4"
    try:
        await adb.run(["shell", f"screenrecord --time-limit {duration} {tmp}"], serial=s, timeout=duration + 30)
        await adb.run(["pull", tmp, dest], serial=s, timeout=120)
    finally:
        await adb.run(["shell", f"rm -f {tmp}"], serial=s, check=False)
    return f"Saved {duration}s recording to {dest} ({os.path.getsize(dest) // 1024} KB)."


# --------------------------------------------------------------------------- logs


@mcp.tool(annotations=READ)
async def logcat(
    lines: Annotated[int, Field(description="How many of the most recent matching lines to return.", ge=1, le=5000)] = 200,
    package: Annotated[str | None, Field(description="Only logs from this app's running process.")] = None,
    tag: Annotated[str | None, Field(description="Only this log tag.")] = None,
    min_priority: Annotated[
        Literal["V", "D", "I", "W", "E", "F"] | None, Field(description="Minimum level, e.g. 'W' for warnings and worse.")
    ] = None,
    grep: Annotated[str | None, Field(description="Only lines matching this regex (case-insensitive).")] = None,
    buffer: Annotated[
        Literal["default", "main", "system", "crash", "events", "radio", "all"],
        Field(description="'crash' holds stack traces of recent app crashes, even for dead processes."),
    ] = "default",
    serial: Serial = None,
) -> str:
    """Return recent device logs (a snapshot, not a stream). Typical uses: logcat(package=...)
    after reproducing a bug; logcat(buffer='crash') to get a crash stack trace;
    logcat(min_priority='E') for errors. Call clear_logcat before reproducing to isolate
    fresh output."""
    s = await adb.resolve_serial(serial)
    # Regex filtering happens host-side, so over-fetch to still end up with `lines` matches.
    fetch = min(max(lines * 25, 3000), 20000) if grep else lines
    args = ["logcat", "-d", "-v", "threadtime", "-t", str(fetch)]
    if buffer != "default":
        args += ["-b", buffer]
    if package:
        pid = (await adb.run(["shell", f"pidof {q(package)}"], serial=s, check=False)).stdout.split()
        if not pid:
            raise AdbError(
                f"'{package}' is not running, so its logs cannot be selected by process. If it crashed, use "
                f"logcat(buffer='crash') or logcat(grep='{package}')."
            )
        args.append(f"--pid={pid[0]}")
    if tag:
        args += ["-s", f"{tag}:{min_priority or 'V'}"]
    elif min_priority:
        args.append(f"*:{min_priority}")
    out = (await adb.run(args, serial=s, timeout=60)).stdout
    result = [l for l in out.splitlines() if l.strip() and not l.startswith("--------- ")]
    if grep:
        try:
            pattern = re.compile(grep, re.I)
        except re.error as e:
            raise AdbError(f"Invalid grep regex: {e}") from None
        result = [l for l in result if pattern.search(l)][-lines:]
    if not result:
        return "No matching log lines."
    return clip("\n".join(result), keep="tail")


@mcp.tool(annotations=ACT)
async def clear_logcat(serial: Serial = None) -> str:
    """Clear the device log buffers, so the next logcat only shows what happens from now on."""
    s = await adb.resolve_serial(serial)
    await adb.run(["logcat", "-c"], serial=s, timeout=30)
    return "Log buffers cleared."


# --------------------------------------------------------------------------- files


@mcp.tool(annotations=ACT)
async def push_file(
    local_path: Annotated[str, Field(description="File or directory on the host.")],
    device_path: Annotated[str, Field(description="Destination on the device, e.g. /sdcard/Download/.")],
    serial: Serial = None,
) -> str:
    """Copy a file or directory from the host to the device, overwriting existing files."""
    s = await adb.resolve_serial(serial)
    src = host_path(local_path)
    if not os.path.exists(src):
        raise AdbError(f"Not found on host: {src}")
    r = await adb.run(["push", src, device_path], serial=s, timeout=600)
    return r.text.splitlines()[-1] if r.text else "pushed"


@mcp.tool(annotations=ACT)
async def pull_file(
    device_path: Annotated[str, Field(description="File or directory on the device.")],
    local_path: Annotated[str, Field(description="Destination on the host.")],
    serial: Serial = None,
) -> str:
    """Copy a file or directory from the device to the host. App-private files under
    /data/data are not readable this way; for debuggable apps use shell with
    `run-as <package> cat <file>`."""
    s = await adb.resolve_serial(serial)
    dest = host_path(local_path)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    r = await adb.run(["pull", device_path, dest], serial=s, timeout=600)
    return (r.text.splitlines()[-1] if r.text else "pulled") + f"\nSaved to {dest}"


@mcp.tool(annotations=READ)
async def list_files(
    path: Annotated[str, Field(description="Directory on the device.")] = "/sdcard",
    serial: Serial = None,
) -> str:
    """List a directory on the device (ls -la)."""
    # Trailing slash so a symlinked directory such as /sdcard lists its contents, not the link.
    return clip(await sh(f"ls -la {q(path.rstrip('/') + '/')}", serial))


# --------------------------------------------------------------------------- port forwarding


@mcp.tool(annotations=ACT)
async def forward_port(
    local: Annotated[str, Field(description="Host-side spec, e.g. 'tcp:8080'. A bare number means tcp.")],
    remote: Annotated[str, Field(description="Device-side spec, e.g. 'tcp:8080' or 'localabstract:chrome_devtools_remote'.")],
    reverse: Annotated[
        bool,
        Field(description="False: host port -> device (reach a server on the device). True: device port -> host (let the device reach a dev server on this machine)."),
    ] = False,
    serial: Serial = None,
) -> str:
    """Forward a socket between host and device. With reverse=True, `remote` is the device-side
    port the app connects to and `local` is the host port it lands on."""
    s = await adb.resolve_serial(serial)
    spec = lambda v: f"tcp:{v}" if v.isdigit() else v
    lo, re_ = spec(local), spec(remote)
    if reverse:
        await adb.run(["reverse", re_, lo], serial=s)
        return f"device {re_} -> host {lo}"
    await adb.run(["forward", lo, re_], serial=s)
    return f"host {lo} -> device {re_}"


@mcp.tool(annotations=READ)
async def list_forwards(serial: Serial = None) -> str:
    """List active port forwards and reverse forwards for a device."""
    s = await adb.resolve_serial(serial)
    fwd = (await adb.run(["forward", "--list"], serial=s)).stdout
    mine = [l for l in fwd.splitlines() if l.startswith(s + " ")]
    rev = (await adb.run(["reverse", "--list"], serial=s, check=False)).stdout.strip()
    return f"forward (host -> device):\n{chr(10).join(mine) or '(none)'}\nreverse (device -> host):\n{rev or '(none)'}"


@mcp.tool(annotations=ACT)
async def remove_forward(
    spec: Annotated[
        str | None,
        Field(description="The host-side spec for a forward, or the device-side spec for a reverse (e.g. 'tcp:8080'). Omit together with remove_all=True."),
    ] = None,
    reverse: bool = False,
    remove_all: Annotated[bool, Field(description="Remove every forward (or every reverse) for this device.")] = False,
    serial: Serial = None,
) -> str:
    """Remove a port forward or reverse forward."""
    s = await adb.resolve_serial(serial)
    verb = "reverse" if reverse else "forward"
    if remove_all:
        await adb.run([verb, "--remove-all"], serial=s)
        return f"Removed all {verb}s."
    if not spec:
        raise AdbError("Give `spec`, or set remove_all=True.")
    await adb.run([verb, "--remove", f"tcp:{spec}" if spec.isdigit() else spec], serial=s)
    return f"Removed {verb} {spec}."


# --------------------------------------------------------------------------- entry point


def check() -> int:
    """Self-test for install scripts: verify adb is reachable and show what the server sees."""
    try:
        path = adb.find_adb()
        version = asyncio.run(adb.run(["version"])).stdout.splitlines()[0]
        devs = asyncio.run(adb.devices())
    except AdbError as e:
        print(f"FAIL: {e}")
        return 1
    tools = asyncio.run(mcp.list_tools())
    print(f"adb:     {path}\n         {version}")
    print(f"tools:   {len(tools)} registered")
    print("devices: " + (", ".join(f"{d.serial} ({d.state})" for d in devs) or "none connected"))
    return 0


def main() -> None:
    if "--check" in sys.argv[1:]:
        sys.exit(check())
    mcp.run()


if __name__ == "__main__":
    main()
