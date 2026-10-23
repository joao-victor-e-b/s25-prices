"""scripts/*.ps1, the PC watcher's wrapper and its Task Scheduler installer:
they parse, stay ASCII (Windows PowerShell 5.1 reads a BOM-less script as
ANSI, where a UTF-8 dash can turn into a quote), never prompt, and the
wrapper really runs the loop as the local role, logging to the day's file
(UTF-8 with a BOM), steps aside while a watcher holds the clone's lock or
another process holds the day's log, and takes python down with it when it
is killed. The installer is only ever read here: nothing gets registered."""
import codecs
import ctypes
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WRAPPER = ROOT / "scripts" / "local_watcher.ps1"
INSTALLER = ROOT / "scripts" / "install_local_watcher.ps1"
SCRIPTS = [WRAPPER, INSTALLER]
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
needs_powershell = pytest.mark.skipif(POWERSHELL is None, reason="needs PowerShell")
LOOP = "--loop --minutes 345 --interval 240 --coupon-every 1800 --git-sync --role local"


@needs_powershell
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_parses(script):
    cmd = ("$tokens = $null; $errors = $null; "
           "[void][System.Management.Automation.Language.Parser]::ParseFile("
           f"'{script}', [ref]$tokens, [ref]$errors); "
           "$errors | ForEach-Object { $_.ToString() }; exit $errors.Count")
    cp = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", cmd],
                        capture_output=True, text=True, timeout=120)
    assert cp.returncode == 0, cp.stdout + cp.stderr


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_is_ascii_and_never_prompts(script):
    text = script.read_bytes().decode("ascii")       # raises on any non-ASCII byte
    for prompt in ("Read-Host", "Get-Credential", "PromptForChoice", "Out-GridView",
                   "ShouldContinue"):
        assert prompt.lower() not in text.lower(), prompt
    assert not re.search(r"^\s*pause\b", text, re.I | re.M)
    assert "$ErrorActionPreference = 'Stop'" in text


def test_wrapper_contract():
    text = WRAPPER.read_text(encoding="ascii")
    assert f"'-m src.run {LOOP}'" in text
    assert "$env:S25_ROLE = 'local'" in text
    assert "'pull --rebase --autostash'" in text
    assert "'watcher-{0}.log' -f (Get-Date -Format 'yyyyMMdd')" in text
    assert "$env:GIT_TERMINAL_PROMPT = '0'" in text and "$env:GCM_INTERACTIVE = 'Never'" in text
    assert "exit $code" in text
    # one loop per clone: the lock run.py takes, checked before any git
    assert r"$LockFile = Join-Path $RepoDir 'data\prices.db.lock'" in text
    assert text.index("if (Test-WatcherRunning)") < text.index("'pull --rebase --autostash'")
    # the job object: python ends with the wrapper (Task Scheduler stop, End)
    assert "KillOnJobClose = 0x2000" in text and "AssignProcessToJobObject" in text
    assert text.index("        Enter-KillOnCloseJob\n") < text.index("$code = Invoke-Logged $Python")
    # logs shared with readers, UTF-8 with a BOM, own file when the day's is held
    assert "[System.IO.FileShare]::ReadWrite" in text
    assert "New-Object System.Text.UTF8Encoding($true)" in text
    assert "'watcher-{0}.log' -f (Get-Date -Format 'yyyyMMdd-HHmmss')" in text


def test_installer_task_definition():
    text = INSTALLER.read_text(encoding="ascii")
    for needle in (
            "$TaskName = 'S25 Amazon watcher'",
            "New-ScheduledTaskTrigger -AtLogOn -User $user",
            "$logon.Delay = 'PT2M'",
            "RunOnlyIfNetworkAvailable  = $true",
            "-Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 30)",
            "MultipleInstances          = 'IgnoreNew'",
            "StartWhenAvailable         = $true",
            "AllowStartIfOnBatteries    = $true",
            "DontStopIfGoingOnBatteries = $true",
            "ExecutionTimeLimit         = (New-TimeSpan -Hours 6)",
            "-LogonType Interactive",
            "-Execute 'powershell.exe'",
            "'-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File \"{0}\" -Python \"{1}\"'",
            "Register-ScheduledTask @taskArgs",
            "Force       = $true",
            "[switch]$Uninstall",
            "Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false"):
        assert needle in text, needle
    code = "\n".join(x for x in text.splitlines() if not x.lstrip().startswith("#"))
    assert "RepetitionDuration" not in code          # none = forever; MaxValue breaks on Win10+
    # the wrapper path handed to the task is the one the installer checked
    assert "$Wrapper = Join-Path $RepoDir 'scripts\\local_watcher.ps1'" in text


def test_logs_and_the_lock_are_ignored_by_git():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
    assert "logs/" in ignored and "data/*.lock" in ignored


def test_readme_watcher_section():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    section = text.split("## Watcher no PC", 1)[1].split("\n## ", 1)[0]
    # first rollout: the cloud run in progress has no standby until it ends
    assert "gh run cancel <id>" in section and "**First rollout only.**" in section
    assert "first rollout: only once the cloud runs a job started" in section
    # a PC clock running behind can't be detected: it must sync itself
    assert "**Definir horário automaticamente** on" in section
    # the live log: the newest file (a run keeps the day it started), as UTF-8
    assert (r"Get-ChildItem C:\Users\<user>\s25-watcher\logs\watcher-*.log | "
            "Sort-Object LastWriteTime | Select-Object -Last 1 | "
            "Get-Content -Encoding UTF8 -Wait -Tail 40") in section
    assert "one file\nper day" not in section
    assert "named for\nthe day a run *started*" in section


_STAND_IN = '''\
import os, sys
print("argv=" + " ".join(sys.argv[1:]))
print("role=%s db=%s" % (os.environ.get("S25_ROLE"), os.environ.get("S25_DB_PATH", "unset")))
print("cwd=" + os.getcwd())
print("prompts git=%s gcm=%s" % (os.environ.get("GIT_TERMINAL_PROMPT"),
                                 os.environ.get("GCM_INTERACTIVE")))
print("acentua\\u00e7\\u00e3o \\U0001F50D")
print("to stderr", file=sys.stderr)
sys.exit(3)
'''


@needs_powershell
@pytest.mark.skipif(sys.platform != "win32", reason="the wrapper logs through cmd.exe")
@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_wrapper_runs_the_local_loop_and_logs_it(tmp_path):
    repo = tmp_path / "s25 watcher"                  # a space, as in C:\Users\Jo Silva
    (repo / "src").mkdir(parents=True)
    (repo / "scripts").mkdir()
    shutil.copy(WRAPPER, repo / "scripts" / WRAPPER.name)
    (repo / "src" / "__init__.py").write_text("")
    (repo / "src" / "run.py").write_text(_STAND_IN)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@e.x"]
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(git + ["add", "."], cwd=repo, check=True)
    subprocess.run(git + ["commit", "-qm", "base"], cwd=repo, check=True)
    logs = repo / "logs"
    logs.mkdir()
    stale = logs / "watcher-20000101.log"            # pruned: older than 30 days
    stale.write_text("old")
    os.utime(stale, (time.time() - 40 * 86400,) * 2)

    def wrapper():
        return subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(repo / "scripts" / WRAPPER.name), "-Python", sys.executable],
            capture_output=True, timeout=120, stdin=subprocess.DEVNULL,
            env={**os.environ, "S25_DB_PATH": str(tmp_path / "elsewhere.db"),
                 "S25_ROLE": "cloud"})

    cp = wrapper()
    assert cp.returncode == 3, cp.stdout + cp.stderr      # python's exit code
    assert not stale.exists()
    (log,) = logs.glob("watcher-*.log")
    assert re.fullmatch(r"watcher-\d{8}\.log", log.name)
    assert log.read_bytes().startswith(codecs.BOM_UTF8)   # PS 5.1's Get-Content: UTF-8
    text = log.read_text(encoding="utf-8-sig")
    assert "no job object" not in text                    # python runs in the job
    assert f"argv={LOOP}\n" in text.replace("\r\n", "\n")
    assert "role=local db=unset" in text                  # role forced, clone's own DB
    assert f"cwd={repo}" in text
    assert "prompts git=0 gcm=Never" in text
    assert "acentua\u00e7\u00e3o \U0001F50D" in text       # UTF-8 survives the redirect
    assert "to stderr" in text
    assert "[wrapper] git pull failed (exit 1)" in text   # no upstream: logged, not fatal
    assert "[wrapper] watcher exited with code 3" in text

    cp = wrapper()                                        # a second run appends
    assert cp.returncode == 3
    assert log.read_text(encoding="utf-8-sig").count("argv=") == 2
    assert log.read_bytes().count(codecs.BOM_UTF8) == 1

    # rows a killed run left in the DB: no autostash pull, the watcher merges them
    (repo / "data").mkdir()
    (repo / "data" / "prices.db").write_bytes(b"v1")
    subprocess.run(git + ["add", "."], cwd=repo, check=True)
    subprocess.run(git + ["commit", "-qm", "db"], cwd=repo, check=True)
    (repo / "data" / "prices.db").write_bytes(b"v1 + rows")
    assert wrapper().returncode == 3
    text = log.read_text(encoding="utf-8-sig")
    assert "never synced: no pull here" in text
    assert text.count("git pull failed") == 2             # this run skipped the pull
    assert (repo / "data" / "prices.db").read_bytes() == b"v1 + rows"


# --- review round: one loop per clone, a held log, a killed wrapper ------------------

windows_wrapper = pytest.mark.skipif(
    POWERSHELL is None or sys.platform != "win32" or shutil.which("git") is None,
    reason="needs Windows PowerShell, cmd.exe and git")


def _clone(tmp_path, stand_in):
    """A minimal git clone for the wrapper: scripts/, a stand-in src/run.py."""
    repo = tmp_path / "s25 watcher"
    (repo / "src").mkdir(parents=True)
    (repo / "scripts").mkdir()
    (repo / "data").mkdir()
    shutil.copy(WRAPPER, repo / "scripts" / WRAPPER.name)
    (repo / "src" / "__init__.py").write_text("")
    (repo / "src" / "run.py").write_text(stand_in)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@e.x"]
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(git + ["add", "."], cwd=repo, check=True)
    subprocess.run(git + ["commit", "-qm", "base"], cwd=repo, check=True)
    return repo


def _wrapper_cmd(repo):
    return [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(repo / "scripts" / WRAPPER.name), "-Python", sys.executable]


def _logs(repo):
    return {p.name: p.read_text(encoding="utf-8-sig") for p in (repo / "logs").glob("*.log")}


@windows_wrapper
def test_wrapper_steps_aside_while_a_watcher_holds_the_clone(tmp_path):
    from src import run
    repo = _clone(tmp_path, _STAND_IN)
    lock = run.acquire_lock(repo / "data" / "prices.db.lock")   # a running loop's lock
    assert lock
    try:
        cp = subprocess.run(_wrapper_cmd(repo), capture_output=True, timeout=120,
                            stdin=subprocess.DEVNULL)
    finally:
        run.release_lock(lock)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    (text,) = _logs(repo).values()
    assert "a watcher from an earlier start still holds data\\prices.db.lock" in text
    assert "argv=" not in text and "git pull" not in text      # no git, no python
    cp = subprocess.run(_wrapper_cmd(repo), capture_output=True, timeout=120,
                        stdin=subprocess.DEVNULL)              # released: it runs
    assert cp.returncode == 3 and "argv=" in "".join(_logs(repo).values())


def _hold_for_writing(path):
    """Open ``path`` the way cmd.exe's >> redirect does: write access, sharing
    read only (nobody else may write). Returns the handle to close."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = ctypes.c_void_p
    handle = k32.CreateFileW(str(path), 0x40000000, 0x1, None, 4, 0x80, None)
    assert handle not in (None, ctypes.c_void_p(-1).value), ctypes.get_last_error()
    return k32, handle


@windows_wrapper
def test_wrapper_logs_to_its_own_file_while_the_days_log_is_held(tmp_path):
    repo = _clone(tmp_path, _STAND_IN)
    (repo / "logs").mkdir()
    day = repo / "logs" / f"watcher-{time.strftime('%Y%m%d')}.log"
    k32, handle = _hold_for_writing(day)                       # a leftover run's cmd.exe
    try:
        cp = subprocess.run(_wrapper_cmd(repo), capture_output=True, timeout=120,
                            stdin=subprocess.DEVNULL)
    finally:
        k32.CloseHandle(ctypes.c_void_p(handle))
    assert cp.returncode == 3, cp.stdout + cp.stderr           # it ran, and said so
    logs = _logs(repo)
    (own,) = [n for n in logs if n != day.name]
    assert re.fullmatch(r"watcher-\d{8}-\d{6}\.log", own)
    assert f"argv={LOOP}" in logs[own] and "[wrapper] watcher exited with code 3" in logs[own]


_SLEEPER = '''\
import os, time
with open(os.environ["S25_TEST_PIDFILE"], "w") as f:
    f.write(str(os.getpid()))
time.sleep(120)
'''


def _exits_within(pid, seconds):
    """Has process ``pid`` ended (or does it not exist) within ``seconds``?"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    handle = k32.OpenProcess(0x00100000, False, pid)           # SYNCHRONIZE
    if not handle:
        return True
    try:
        return k32.WaitForSingleObject(ctypes.c_void_p(handle), int(seconds * 1000)) == 0
    finally:
        k32.CloseHandle(ctypes.c_void_p(handle))


@windows_wrapper
def test_killing_the_wrapper_ends_python_too(tmp_path):
    # Task Scheduler's 6 h limit (or End) terminates powershell.exe only: the
    # job object must take cmd.exe and python.exe with it
    repo = _clone(tmp_path, _SLEEPER)
    pidfile = tmp_path / "python.pid"
    proc = subprocess.Popen(_wrapper_cmd(repo), stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env={**os.environ, "S25_TEST_PIDFILE": str(pidfile)})
    pid = None
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and not (pidfile.exists() and pidfile.read_text()):
            assert proc.poll() is None, "the wrapper ended before starting python"
            time.sleep(0.2)
        pid = int(pidfile.read_text())
        assert not _exits_within(pid, 0.5)                     # python is up
        proc.kill()                                            # TerminateProcess
        proc.wait(timeout=30)
        assert _exits_within(pid, 15), "python outlived its wrapper"
    finally:
        if proc.poll() is None:
            proc.kill()
        if pid is not None and not _exits_within(pid, 0):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    assert "no job object" not in "".join(_logs(repo).values())
