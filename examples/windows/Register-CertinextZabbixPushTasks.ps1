#Requires -Version 5.1
#Requires -Modules ScheduledTasks
<#
.SYNOPSIS
    Registers the two certinext-zabbix-push Scheduled Tasks (frequent +
    daily expiry), the Windows equivalent of the systemd timer pairs or
    cron lines in ../systemd/ and ../cron/.

.NOTES
    UNTESTED. Written as a starting reference, not run against a real
    Windows host or verified by a Windows admin. Review every default —
    account/logon-type choice, task settings, ACLs — and test in a
    non-production environment before relying on it.

.DESCRIPTION
    Creates/updates:
      - "CertiNext Zabbix Push"          — every 15 minutes, indefinitely
      - "CertiNext Zabbix Push - Expiry" — once daily, with --expiry-days 14

    Both tasks run Invoke-CertinextZabbixPush.ps1 (in this directory),
    which loads the env file and calls certinext-zabbix-push.exe. Both
    MUST run as the same service account so their file locks
    (%TEMP%\certinext_zabbix_push_<env>_<job>.lock, one per job) land in
    the same temp directory — this is the Windows analogue of the
    systemd-side PrivateTmp warning. Re-run this script to update an
    existing registration (it unregisters before re-registering).

    Idempotent, but NOT a substitute for a real Ansible/DSC/Group Policy
    rollout in a managed fleet — treat it as a reference a Windows admin
    adapts, not a drop-in production script. Run elevated (Administrator).

.PARAMETER ServiceAccount
    The account both tasks run as, e.g. 'DOMAIN\svc-certinext-zabbix' or
    a group-managed service account 'DOMAIN\svc-certinext-zabbix$'.

.PARAMETER Credential
    Credential for ServiceAccount. Omit when ServiceAccount is a gMSA
    (LogonType Password with no password works for gMSA accounts) — pass
    -AsGmsa in that case instead.

.PARAMETER AsGmsa
    Register ServiceAccount as a group-managed service account (no
    password needed/stored).

.PARAMETER ScriptDir
    Directory containing Invoke-CertinextZabbixPush.ps1 and the env file
    it defaults to reading. Defaults to this script's own directory.

.EXAMPLE
    # Regular service account, prompts for its password
    .\Register-CertinextZabbixPushTasks.ps1 -ServiceAccount 'DOMAIN\svc-certinext-zabbix'

.EXAMPLE
    # gMSA — no password to manage
    .\Register-CertinextZabbixPushTasks.ps1 -ServiceAccount 'DOMAIN\svc-certinext-zabbix$' -AsGmsa
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)]
    [string]$ServiceAccount,

    [System.Management.Automation.PSCredential]$Credential,

    [switch]$AsGmsa,

    [string]$ScriptDir = $PSScriptRoot
)

$ErrorActionPreference = "Stop"

$wrapperPath = Join-Path $ScriptDir "Invoke-CertinextZabbixPush.ps1"
if (-not (Test-Path -LiteralPath $wrapperPath)) {
    throw "Invoke-CertinextZabbixPush.ps1 not found next to this script ($ScriptDir)"
}

function Register-PushTask {
    param(
        [string]$TaskName,
        [string]$Description,
        [Microsoft.Management.Infrastructure.CimInstance]$Trigger,
        [string[]]$PushArgs
    )

    $argList = "-NoProfile -ExecutionPolicy Bypass -File `"$wrapperPath`""
    if ($PushArgs.Count -gt 0) {
        $quoted = $PushArgs | ForEach-Object { "'$_'" }
        $argList += " -PushArgs @($($quoted -join ','))"
    }

    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argList
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -DontStopOnIdleEnd `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
        -RestartCount 0

    $principal = New-ScheduledTaskPrincipal -UserId $ServiceAccount -LogonType Password -RunLevel Limited

    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        if ($PSCmdlet.ShouldProcess($TaskName, "Unregister existing task")) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        }
    }

    $task = New-ScheduledTask -Action $action -Trigger $Trigger -Principal $principal -Settings $settings -Description $Description

    if ($PSCmdlet.ShouldProcess($TaskName, "Register scheduled task")) {
        if ($AsGmsa) {
            Register-ScheduledTask -TaskName $TaskName -InputObject $task | Out-Null
        }
        else {
            if (-not $Credential) {
                $Credential = Get-Credential -UserName $ServiceAccount -Message "Password for $ServiceAccount"
            }
            Register-ScheduledTask -TaskName $TaskName -InputObject $task `
                -User $ServiceAccount -Password $Credential.GetNetworkCredential().Password | Out-Null
        }
    }
}

# Frequent run: every 15 minutes, indefinitely, starting in one minute.
$frequentTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration ([TimeSpan]::MaxValue)
Register-PushTask -TaskName "CertiNext Zabbix Push" `
    -Description "Push CertiNext DCV health metrics to Zabbix (frequent)" `
    -Trigger $frequentTrigger -PushArgs @()

# Daily expiry run.
$dailyTrigger = New-ScheduledTaskTrigger -Daily -At "03:12"
Register-PushTask -TaskName "CertiNext Zabbix Push - Expiry" `
    -Description "Push CertiNext DCV expiry metrics to Zabbix (daily)" `
    -Trigger $dailyTrigger -PushArgs @("--expiry-days", "14")

Write-Host "Registered 'CertiNext Zabbix Push' and 'CertiNext Zabbix Push - Expiry' as $ServiceAccount."
Write-Host "Verify with: Get-ScheduledTaskInfo -TaskName 'CertiNext Zabbix Push'"
