<#
.SYNOPSIS
    Runs one S25 watcher loop on this PC (role 'local') and logs it.

.DESCRIPTION
    Started by the "S25 Amazon watcher" scheduled task (install_local_watcher.ps1)
    from a dedicated clone of the repo. From a home IP Amazon serves the
    "all offers" panel that GitHub's IPs never get, so this watcher names every
    seller; while it polls, the cloud watcher stands by (it sends nothing).

    1. exits at once (code 0) when a watcher python from an earlier start
       still holds data\prices.db.lock (one loop per clone; run.py takes it);
    2. updates the clone: git pull --rebase --autostash (a failure is logged,
       never fatal; skipped when the DB holds rows a killed run never synced,
       which the watcher itself merges before its first poll);
    3. runs the cloud's loop as the local role:
       python -m src.run --loop --minutes 345 --interval 240 --coupon-every 1800 --git-sync --role local
       appending its stdout + stderr to logs\watcher-<yyyyMMdd>.log (the day
       the run started; UTF-8 with a BOM, so Windows PowerShell reads it right;
       watcher-<yyyyMMdd-HHmmss>.log when a leftover process holds that one);
    4. exits with python's exit code.

    This process puts itself in a kill-on-close job object first: when it is
    stopped (Task Scheduler's 6 h limit, End in Task Scheduler), cmd.exe and
    python.exe go with it instead of polling on as orphans.

    Never prompts: git's terminal and credential-manager prompts are off (a
    hidden task would hang on one; a failed push is logged and retried).

.PARAMETER RepoDir
    The clone to run in (default: the repo this script lives in).

.PARAMETER Python
    The python.exe to run (default: 'python' from PATH).
#>
[CmdletBinding()]
param(
    [string]$RepoDir = '',
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'

if (-not $RepoDir) { $RepoDir = Split-Path -Parent $PSScriptRoot }
$RepoDir = (Resolve-Path -LiteralPath $RepoDir).Path
Set-Location -LiteralPath $RepoDir

$LogDir = Join-Path $RepoDir 'logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir ('watcher-{0}.log' -f (Get-Date -Format 'yyyyMMdd'))
try {
    # A leftover run's cmd.exe still appending to the day's log lets nobody
    # else write to it (its >> redirect shares read access only): this run
    # then logs to a file of its own.
    (New-Object System.IO.FileStream($LogFile, [System.IO.FileMode]::Append,
        [System.IO.FileAccess]::Write, [System.IO.FileShare]::ReadWrite)).Dispose()
} catch {
    $LogFile = Join-Path $LogDir ('watcher-{0}.log' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
}
$LockFile = Join-Path $RepoDir 'data\prices.db.lock'
$Utf8 = New-Object System.Text.UTF8Encoding($true)

function Write-Log([string]$Message) {
    # Open for append letting readers (Get-Content -Wait) and other writers
    # in, and start a new file with the UTF-8 BOM (PowerShell 5.1 reads a
    # BOM-less file as ANSI).
    $line = '{0} [wrapper] {1}{2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message, [Environment]::NewLine
    $fs = New-Object System.IO.FileStream($LogFile, [System.IO.FileMode]::Append,
        [System.IO.FileAccess]::Write, [System.IO.FileShare]::ReadWrite)
    try {
        $bytes = $Utf8.GetBytes($line)
        if ($fs.Length -eq 0) {
            $bom = $Utf8.GetPreamble()
            $fs.Write($bom, 0, $bom.Length)
        }
        $fs.Write($bytes, 0, $bytes.Length)
    } finally {
        $fs.Dispose()
    }
}

function Invoke-Logged([string]$Exe, [string]$Arguments) {
    # cmd.exe appends the program's own bytes (UTF-8) to the log, stderr
    # included: PowerShell 5.1 would decode native output with the console
    # code page and wrap every stderr line in an error record.
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $env:ComSpec
    $psi.Arguments = '/d /s /c ""{0}" {1} >> "{2}" 2>&1"' -f $Exe, $Arguments, $LogFile
    $psi.WorkingDirectory = $RepoDir
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $proc = [System.Diagnostics.Process]::Start($psi)
    $proc.WaitForExit()
    return $proc.ExitCode
}

function Test-WatcherRunning {
    # True while a watcher python holds the clone's lock (run.py takes a
    # byte-range lock on data\prices.db.lock for its whole loop).
    if (-not (Test-Path -LiteralPath $LockFile)) { return $false }
    try {
        $fs = [System.IO.File]::Open($LockFile, [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::ReadWrite)
    } catch {
        return $false    # can't tell: python takes (or refuses) the lock itself
    }
    try {
        $fs.Lock(0, 1)
        $fs.Unlock(0, 1)
        return $false
    } catch [System.IO.IOException] {
        return $true
    } finally {
        $fs.Dispose()
    }
}

function Enter-KillOnCloseJob {
    # Puts this process (and so every process it starts from now on) in a job
    # object whose only handle this process holds: when it ends, however it
    # ends, Windows ends cmd.exe and python.exe too.
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

public static class S25WatcherJob {
    [StructLayout(LayoutKind.Sequential)]
    struct BasicLimits {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct IoCounters {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct ExtendedLimits {
        public BasicLimits Basic;
        public IoCounters Io;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    const int ExtendedLimitInformation = 9;
    const uint KillOnJobClose = 0x2000;

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern IntPtr CreateJobObject(IntPtr attributes, string name);

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool SetInformationJobObject(IntPtr job, int infoClass,
                                               ref ExtendedLimits info, uint size);

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    [DllImport("kernel32.dll")]
    static extern IntPtr GetCurrentProcess();

    // Never closed: the job lives exactly as long as this process.
    public static IntPtr Job = IntPtr.Zero;

    public static void Enter() {
        IntPtr job = CreateJobObject(IntPtr.Zero, null);
        if (job == IntPtr.Zero) throw new Win32Exception();
        ExtendedLimits info = new ExtendedLimits();
        info.Basic.LimitFlags = KillOnJobClose;
        if (!SetInformationJobObject(job, ExtendedLimitInformation, ref info,
                                     (uint)Marshal.SizeOf(typeof(ExtendedLimits))))
            throw new Win32Exception();
        if (!AssignProcessToJobObject(job, GetCurrentProcess()))
            throw new Win32Exception();
        Job = job;
    }
}
'@
    [S25WatcherJob]::Enter()
}

try {
    Write-Log ('start: {0} in {1}' -f $Python, $RepoDir)
    Get-ChildItem -LiteralPath $LogDir -Filter 'watcher-*.log' |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
        Remove-Item -Force -ErrorAction SilentlyContinue

    if (Test-WatcherRunning) {
        Write-Log 'a watcher from an earlier start still holds data\prices.db.lock: nothing to do'
        exit 0
    }

    try {
        Enter-KillOnCloseJob
    } catch {
        Write-Log "no job object ($_): a stopped task may leave python running until its loop ends"
    }

    $env:GIT_TERMINAL_PROMPT = '0'
    $env:GCM_INTERACTIVE = 'Never'

    # A run killed mid-loop (shutdown, logoff) leaves DB rows uncommitted.
    # Pull's autostash would park them in a stash and leave the DB unmerged,
    # so they are left to the watcher, whose first sync merges them row by row.
    $diff = Invoke-Logged 'git' 'diff --quiet HEAD -- data/prices.db'
    if ($diff -eq 1) {
        Write-Log 'data/prices.db holds rows a killed run never synced: no pull here, the watcher merges them before its first poll'
    } elseif ($diff -ne 0) {
        Write-Log "git diff failed (exit $diff): no pull, running the checked-out code"
    } else {
        $pull = Invoke-Logged 'git' 'pull --rebase --autostash'
        if ($pull -ne 0) {
            $gitDir = Join-Path $RepoDir '.git'
            if ((Test-Path -LiteralPath (Join-Path $gitDir 'rebase-merge')) -or
                (Test-Path -LiteralPath (Join-Path $gitDir 'rebase-apply'))) {
                Invoke-Logged 'git' 'rebase --abort' | Out-Null
            }
            Write-Log "git pull failed (exit $pull): running the checked-out code"
        }
    }

    $env:S25_ROLE = 'local'
    $env:PYTHONUNBUFFERED = '1'    # live log lines
    $env:PYTHONUTF8 = '1'          # UTF-8 stdout and stderr in the log
    # git sync only commits the clone's own data\prices.db
    Remove-Item -Path Env:S25_DB_PATH -ErrorAction SilentlyContinue

    $code = Invoke-Logged $Python '-m src.run --loop --minutes 345 --interval 240 --coupon-every 1800 --git-sync --role local'
    Write-Log "watcher exited with code $code"
    exit $code
} catch {
    try { Write-Log "wrapper error: $_" } catch { }
    exit 1
}
