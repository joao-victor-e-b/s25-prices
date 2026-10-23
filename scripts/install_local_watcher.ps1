<#
.SYNOPSIS
    Registers (or updates) the "S25 Amazon watcher" scheduled task, or removes it.

.DESCRIPTION
    The task runs scripts\local_watcher.ps1 from this clone for the current
    user: 2 minutes after log on and every 30 minutes from now on, one run at
    a time (a start while a ~5h45 loop runs is ignored), only while the user
    is logged on and a network is up, on battery too, and never longer than 6
    hours (python included: the wrapper's job object). Run it from the
    dedicated watcher clone (README, "Watcher no PC"). No admin rights needed.

.PARAMETER Python
    Full path of the python.exe that has requirements.txt installed (default:
    the first python on PATH, resolved now, so the task does not depend on
    the PATH it starts with).

.PARAMETER RepoDir
    The clone the task runs in (default: the repo this script lives in).

.PARAMETER Uninstall
    Remove the task instead.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_local_watcher.ps1 -Python C:\Users\me\anaconda3\python.exe

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_local_watcher.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string]$Python = '',
    [string]$RepoDir = '',
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$TaskName = 'S25 Amazon watcher'

if ($Uninstall) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Host "No scheduled task '$TaskName': nothing to remove."
        exit 0
    }
    $running = $task.State -eq 'Running'
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'."
    if ($running) {
        Write-Host 'A watcher run was in progress: if its python.exe keeps running, it stops by'
        Write-Host 'itself when its loop ends (at most 5h45), after a last DB sync. The cloud'
        Write-Host 'watcher takes over 45 min after the last poll the PC pushed.'
    }
    exit 0
}

if (-not $RepoDir) { $RepoDir = Split-Path -Parent $PSScriptRoot }
$RepoDir = (Resolve-Path -LiteralPath $RepoDir).Path
$Wrapper = Join-Path $RepoDir 'scripts\local_watcher.ps1'
if (-not (Test-Path -LiteralPath (Join-Path $RepoDir 'src\run.py')) -or
    -not (Test-Path -LiteralPath $Wrapper)) {
    throw "$RepoDir is not an s25-prices clone (no src\run.py or scripts\local_watcher.ps1)"
}

if (-not $Python) {
    $found = Get-Command python -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $found) { throw 'python is not on PATH: pass -Python C:\path\to\python.exe' }
    $Python = $found.Source
}
$Python = (Resolve-Path -LiteralPath $Python).Path
if ($Python -like '*\WindowsApps\*') {
    Write-Warning "$Python is the Microsoft Store alias, not an install: pass -Python with your python.exe"
}
& $Python -c 'import requests, yaml, bs4, lxml'
if ($LASTEXITCODE -ne 0) {
    Write-Warning "$Python lacks the watcher's packages: & '$Python' -m pip install -r requirements.txt"
}
if ($null -eq (Get-Command git -CommandType Application -ErrorAction SilentlyContinue)) {
    Write-Warning 'git is not on PATH: the watcher could not pull or push its DB snapshots'
}
if (-not (Test-Path -LiteralPath (Join-Path $RepoDir '.env'))) {
    Write-Warning "no .env in $RepoDir (copy yours there, or no alert can be sent)"
}

$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -Python "{1}"' -f $Wrapper, $Python
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments -WorkingDirectory $RepoDir
$logon = New-ScheduledTaskTrigger -AtLogOn -User $user
$logon.Delay = 'PT2M'    # Wi-Fi connects after the desktop shows (the loop retries its pull too)
$triggers = @(
    $logon,
    # no -RepetitionDuration: the repetition never ends
    (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 30))
)
$settingsArgs = @{
    MultipleInstances          = 'IgnoreNew'                # a start while a loop runs is skipped
    StartWhenAvailable         = $true                      # a start missed while off runs when back
    RunOnlyIfNetworkAvailable  = $true                      # no start offline (the next tick retries)
    AllowStartIfOnBatteries    = $true
    DontStopIfGoingOnBatteries = $true
    ExecutionTimeLimit         = (New-TimeSpan -Hours 6)    # the loop stops itself at 5h45
}
$settings = New-ScheduledTaskSettingsSet @settingsArgs
# Interactive = "run only when the user is logged on"; no stored password, no elevation
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

$existed = $null -ne (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)
$taskArgs = @{
    TaskName    = $TaskName
    Description = 'S25 Ultra Amazon price watcher from this PC (home IP: full offers panel). ' +
                  'The cloud watcher stands by while it polls. See README, "Watcher no PC".'
    Action      = $action
    Trigger     = $triggers
    Settings    = $settings
    Principal   = $principal
    Force       = $true                                       # replaces the task when it exists
}
Register-ScheduledTask @taskArgs | Out-Null

$verb = if ($existed) { 'Updated' } else { 'Registered' }
Write-Host "$verb scheduled task '$TaskName' for $user"
Write-Host "  runs:  powershell.exe $arguments"
Write-Host "  in:    $RepoDir"
Write-Host '  when:  2 min after log on, and every 30 min from now on; one run at a time'
Write-Host '         (a run lasts ~5h45 and is stopped at 6h); only while you are logged'
Write-Host '         on and a network is up, on battery too'
Write-Host "  logs:  $(Join-Path $RepoDir 'logs')\watcher-<yyyyMMdd>.log (the day a run started)"
Write-Host "Start it now:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Remove it:     powershell -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Uninstall"
