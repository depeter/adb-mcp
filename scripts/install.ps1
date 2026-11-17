# Build the server's virtualenv and register it with Codex (and optionally Claude Code).
#
#   scripts\install.ps1              build + register with Codex as "adb"
#   scripts\install.ps1 -Claude      also register with Claude Code (user scope)
#   scripts\install.ps1 -NoCodex     build only (combine with -Claude if wanted)
#   scripts\install.ps1 -Name foo    register under a different server name
#
# If script execution is blocked, run it as:
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#
# Safe to re-run: it replaces any existing registration of the same name.
[CmdletBinding()]
param(
    [string]$Name = 'adb',
    [switch]$Claude,
    [switch]$NoCodex
)

$ErrorActionPreference = 'Stop'
$Dir = Split-Path -Parent $PSScriptRoot
$Scripts = Join-Path $Dir '.venv\Scripts'
$Bin = Join-Path $Scripts 'adb-mcp.exe'

# Resolve a real executable (.exe/.cmd), never a .ps1 shim: npm's PowerShell shims swallow the
# "--" separator that `mcp add` needs.
function Find-App([string]$App) {
    $cmd = Get-Command $App -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { return $cmd.Source }
    return $null
}

# Run a native command, discarding its output and ignoring failure.
function Invoke-Quiet([string]$Exe, [string[]]$Arguments) {
    $ErrorActionPreference = 'SilentlyContinue'
    & $Exe @Arguments *> $null
}

function Test-Python([string]$Exe, [string[]]$Prefix) {
    # The Microsoft Store "python" alias exists on PATH but only prints an install hint.
    $ErrorActionPreference = 'SilentlyContinue'
    & $Exe @Prefix -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' *> $null
    return ($LASTEXITCODE -eq 0)
}

Write-Host "==> Building virtualenv in $Dir\.venv"
$uv = Find-App 'uv'
if ($uv) {
    Push-Location $Dir
    try { & $uv sync --quiet } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed (exit $LASTEXITCODE)." }
}
else {
    $py = $null; $pyArgs = @()
    $launcher = Find-App 'py'
    $python = Find-App 'python'
    if ($launcher -and (Test-Python $launcher @('-3'))) { $py = $launcher; $pyArgs = @('-3') }
    elseif ($python -and (Test-Python $python @())) { $py = $python }
    if (-not $py) { throw 'Need either uv (https://docs.astral.sh/uv/) or Python 3.10+ on PATH.' }
    & $py @pyArgs -m venv (Join-Path $Dir '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the virtualenv.' }
    & (Join-Path $Scripts 'python.exe') -m pip install --quiet --upgrade pip
    & (Join-Path $Scripts 'python.exe') -m pip install --quiet -e $Dir
    if ($LASTEXITCODE -ne 0) { throw 'pip install failed.' }
}
if (-not (Test-Path $Bin)) { throw "Build did not produce $Bin; not registering anything." }

# Agents start MCP servers with a trimmed environment, so pin adb's location rather than
# relying on PATH at launch time.
$Adb = $env:ADB_PATH
if (-not $Adb) { $Adb = Find-App 'adb' }
if (-not $Adb) {
    $roots = @($env:ANDROID_HOME, $env:ANDROID_SDK_ROOT, (Join-Path $env:LOCALAPPDATA 'Android\Sdk'))
    foreach ($r in $roots) {
        if ($r -and (Test-Path (Join-Path $r 'platform-tools\adb.exe'))) {
            $Adb = Join-Path $r 'platform-tools\adb.exe'
            break
        }
    }
}
$EnvArgs = @()
if ($Adb) { $EnvArgs = @('--env', "ADB_PATH=$Adb") }
else { Write-Warning 'adb not found. Install Android platform-tools (or set ADB_PATH) and re-run.' }

Write-Host '==> Self-check'
$saved = $env:ADB_PATH
if ($Adb) { $env:ADB_PATH = $Adb }
& $Bin --check
$env:ADB_PATH = $saved

function Register-Server([string]$Cli, [string[]]$ScopeArgs) {
    Invoke-Quiet $Cli (@('mcp', 'remove') + $ScopeArgs + @($Name))
    $addArgs = @('mcp', 'add') + $ScopeArgs + @($Name) + $EnvArgs + @('--', $Bin)
    & $Cli @addArgs
    if ($LASTEXITCODE -ne 0) { Write-Warning "Registering '$Name' failed (exit $LASTEXITCODE)." }
}

if (-not $NoCodex) {
    $codex = Find-App 'codex'
    if ($codex) {
        Write-Host "==> Registering '$Name' with Codex"
        Register-Server $codex @()
    }
    else {
        Write-Host '==> codex CLI not found. Add this to ~\.codex\config.toml yourself:'
        Write-Host ''
        Write-Host "[mcp_servers.$Name]"
        Write-Host "command = '$Bin'"
        if ($Adb) { Write-Host "env = { ADB_PATH = '$Adb' }" }
        Write-Host ''
    }
}

if ($Claude) {
    $claudeCli = Find-App 'claude'
    if ($claudeCli) {
        Write-Host "==> Registering '$Name' with Claude Code (user scope)"
        Register-Server $claudeCli @('--scope', 'user')
    }
    else { Write-Warning 'claude CLI not found; skipped.' }
}

Write-Host ''
Write-Host "Done. Start a new agent session and the '$Name' tools will be available."
