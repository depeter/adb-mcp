# Show what the server sees: adb location/version, tool count, connected devices.
$Dir = Split-Path -Parent $PSScriptRoot
$Bin = Join-Path $Dir '.venv\Scripts\adb-mcp.exe'
if (-not (Test-Path $Bin)) {
    Write-Error 'Not installed yet. Run scripts\install.ps1 first.'
    exit 1
}
& $Bin --check
