#Requires -Version 5.1
<#
.SYNOPSIS
    Wrapper that loads the certinext-zabbix env file and runs
    certinext-zabbix-push.exe, for use as a Scheduled Task action.

.NOTES
    UNTESTED. Written as a starting reference, not run against a real
    Windows host or verified by a Windows admin. Review and test in a
    non-production environment before relying on it.

.DESCRIPTION
    Task Scheduler has no equivalent of systemd's EnvironmentFile= or
    cron's inline KEY=VALUE lines, so this script fills that gap: it reads
    a KEY=VALUE env file into the current process's environment, then
    invokes certinext-zabbix-push.exe with any extra arguments passed
    through (e.g. -PushArgs '--expiry-days','14' for the daily run).

    Exits with the pusher's own exit code (0 ok, 1 errors, 130
    interrupted) so Task Scheduler's "Last Run Result" reflects it.

.PARAMETER ExePath
    Path to certinext-zabbix-push.exe inside the installed venv.

.PARAMETER EnvFile
    Path to the KEY=VALUE environment file (see
    certinext-zabbix.env.example in this directory).

.PARAMETER PushArgs
    Extra arguments forwarded to certinext-zabbix-push.exe, e.g.
    @('--expiry-days', '14') for the daily run.
#>
[CmdletBinding()]
param(
    [string]$ExePath = "C:\certinext-zabbix\venv\Scripts\certinext-zabbix-push.exe",
    [string]$EnvFile = "C:\ProgramData\certinext-zabbix\certinext-zabbix.env",
    [string[]]$PushArgs = @()
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $ExePath)) {
    Write-Error "certinext-zabbix-push.exe not found at $ExePath"
    exit 1
}
if (-not (Test-Path -LiteralPath $EnvFile)) {
    Write-Error "Environment file not found at $EnvFile"
    exit 1
}

Get-Content -LiteralPath $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -eq "" -or $line.StartsWith("#")) { return }
    $name, $value = $line -split "=", 2
    if ($null -ne $value) {
        [System.Environment]::SetEnvironmentVariable($name.Trim(), $value.Trim(), "Process")
    }
}

& $ExePath @PushArgs
exit $LASTEXITCODE
