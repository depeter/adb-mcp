# Unregister the server from Codex and Claude Code. Pass -Purge to also delete .venv.
#
#   scripts\uninstall.ps1 [-Name adb] [-Purge]
[CmdletBinding()]
param(
    [string]$Name = 'adb',
    [switch]$Purge
)

$Dir = Split-Path -Parent $PSScriptRoot

function Find-App([string]$App) {
    $cmd = Get-Command $App -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { return $cmd.Source }
    return $null
}

function Remove-Server([string]$Cli, [string[]]$Arguments) {
    $ErrorActionPreference = 'SilentlyContinue'
    & $Cli @Arguments *> $null
    return ($LASTEXITCODE -eq 0)
}

$codex = Find-App 'codex'
$claudeCli = Find-App 'claude'
if ($codex -and (Remove-Server $codex @('mcp', 'remove', $Name))) { Write-Host "Removed '$Name' from Codex." }
if ($claudeCli -and (Remove-Server $claudeCli @('mcp', 'remove', '--scope', 'user', $Name))) { Write-Host "Removed '$Name' from Claude Code." }
if ($Purge) {
    $venv = Join-Path $Dir '.venv'
    if (Test-Path $venv) {
        Remove-Item -Recurse -Force $venv
        Write-Host "Deleted $venv"
    }
}
