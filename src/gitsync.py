"""Commit + push the price DB from inside the long-running watcher.

Why: the watch job polls every ~4 minutes for ~6 hours per GitHub Actions run.
Committing only at the end would lose hours of history whenever a run is
killed, and would leave the heartbeat job (which reads the committed DB) hours
behind. So ``run.loop --git-sync`` pulls before its first poll, snapshots
data/prices.db every 30 minutes, and once more on exit.

How a sync goes:
- only the DB is staged and committed ("data: price snapshot <UTC ts>", the
  message the old per-run commit step used); nothing changed -> no commit;
- ``git pull --rebase --autostash`` always runs, even with nothing to commit,
  so changes pushed meanwhile reach the running job — notably a new
  coupon_checks.yaml entry, which every poll re-reads;
- if the rebase stops on a DB conflict (two watchers' snapshots: the previous
  run's final push landed after this one started), neither side's binary file
  may win — that silently dropped the other watcher's last rows and its dedup
  state. Instead: abort, merge our snapshot's rows into a copy of upstream's
  DB (``git show @{u}:<db>``; ``store.merge_db_rows``: append-only tables
  union, upserted rows newer-wins), and only once that worked move onto
  upstream with ``reset --keep`` (local edits survive) and commit the merged
  DB on top. A failed merge moves nothing; a failure after the move restores
  the saved HEAD;
- pull + push are retried up to 3 times (a racing push makes ours fail);
- the loop's budget exit syncs normally but within ``budget_s`` (a few
  minutes); the exit on a stop signal (``final=True``) is one fast attempt
  with short git timeouts: the runner kills the job ~10 s after asking it to
  stop;
- nothing raises and every git call has a timeout: a failed sync is logged and
  simply retried at the next one while the watcher keeps polling.

Every git command goes through an injectable ``runner`` so tests need no repo.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import config, store

REPO_ROOT = Path(__file__).resolve().parent.parent
GIT_TIMEOUT_S = 120
FINAL_GIT_TIMEOUT_S = 6   # the exit sync must fit in the runner's kill window
RETRIES = 3

Runner = Callable[[list[str]], subprocess.CompletedProcess]


def default_runner(cmd: list[str], timeout: float = GIT_TIMEOUT_S) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 1, "", f"{type(e).__name__}: {e}")


def _repo_relative(path: Path) -> str | None:
    try:
        return Path(path).resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return None  # S25_DB_PATH points outside the repo (local experiments)


def _why(cp: subprocess.CompletedProcess) -> str:
    return " ".join((cp.stderr or cp.stdout or "").split())[:200]


def _ahead(run: Runner) -> bool:
    """Local commits the remote doesn't have? (unknown upstream -> try)"""
    cp = run(["git", "rev-list", "--count", "@{u}..HEAD"])
    if cp.returncode != 0:
        return True
    try:
        return int((cp.stdout or "0").strip() or 0) > 0
    except ValueError:
        return True


def _timestamp(now: datetime | None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _commit_db(run: Runner, rel: str, message: str) -> bool | None:
    """Stage + commit the DB. True committed, None nothing to commit, False
    failed."""
    run(["git", "add", "--", rel])
    if run(["git", "diff", "--cached", "--quiet", "--", rel]).returncode == 0:
        return None
    cp = run(["git", "commit", "-m", message, "--", rel])
    if cp.returncode != 0:
        print(f"[git-sync] commit failed: {_why(cp)}")
        return False
    return True


def default_export(spec: str, dest: Path) -> bool:
    """Write the blob ``<rev>:<path>`` to ``dest`` (binary-safe: the runner's
    text capture would mangle a SQLite file)."""
    try:
        with open(dest, "wb") as f:
            cp = subprocess.run(["git", "show", spec], cwd=REPO_ROOT, stdout=f,
                                stderr=subprocess.PIPE, timeout=GIT_TIMEOUT_S)
        return cp.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _db_conflict(run: Runner, rel: str) -> bool:
    cp = run(["git", "diff", "--name-only", "--diff-filter=U"])
    return rel in (cp.stdout or "").split()


def _merge_onto_upstream(run: Runner, rel: str, db: Path, now: datetime | None,
                         merge_fn: Callable[[Path, Path], dict],
                         export_fn: Callable[[str, Path], bool]) -> bool:
    """Resolve a DB conflict by row merge: our snapshot's rows go into a copy of
    upstream's DB first; only when that worked does the branch move onto
    upstream and the merged DB get committed on top. Our commit only ever
    touched the DB, so nothing else is lost by leaving it."""
    run(["git", "rebase", "--abort"])
    cp = run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    upstream = (cp.stdout or "").strip()
    head = (run(["git", "rev-parse", "HEAD"]).stdout or "").strip()
    if cp.returncode != 0 or not upstream or not head:
        print(f"[git-sync] DB CONFLICT and no upstream to merge onto: {_why(cp)}")
        return False
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix="s25-upstream-")
    os.close(fd)
    try:
        if not export_fn(f"{upstream}:{rel}", Path(tmp)):
            print(f"[git-sync] DB CONFLICT: could not read {upstream}:{rel}; "
                  "local snapshot kept, retried next sync")
            return False
        try:
            added = merge_fn(db, Path(tmp))   # our rows -> a copy of theirs
        except Exception as e:
            print(f"[git-sync] DB CONFLICT: merge failed ({type(e).__name__}: {e}); "
                  "nothing moved, local snapshot kept")
            return False
        cp = run(["git", "reset", "--keep", upstream])
        if cp.returncode != 0:
            print(f"[git-sync] DB CONFLICT: could not move onto {upstream} "
                  f"(local snapshot kept, retried next sync): {_why(cp)}")
            return False
        try:
            shutil.copyfile(tmp, db)
        except BaseException:
            if run(["git", "reset", "--keep", head]).returncode != 0:
                print(f"[git-sync] DB CONFLICT: restore failed; our snapshot is commit "
                      f"{head} (git reset --hard {head})")
            raise
        print(f"[git-sync] DB conflict with {upstream}: merged our rows into it "
              f"({sum(added.values())} added/updated)")
        return _commit_db(run, rel, f"data: price snapshot {_timestamp(now)} "
                                    "(merged with a concurrent snapshot)") is not False
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def sync_db(db_path: Path | None = None, runner: Runner | None = None,
            retries: int = RETRIES, now: datetime | None = None,
            sleep: Callable[[float], None] = time.sleep, final: bool = False,
            merge_fn: Callable[[Path, Path], dict] | None = None,
            export_fn: Callable[[str, Path], bool] | None = None,
            budget_s: float | None = None,
            clock: Callable[[], float] = time.monotonic) -> bool:
    """Commit the DB if it changed, pull --rebase (merging rows on a DB
    conflict), push. True when the remote ends up with everything local (or
    there was nothing to push). ``final``: one fast attempt for a stop signal;
    ``budget_s``: the normal retrying sync, but done within that many seconds.
    Never raises."""
    try:
        return _sync_db(db_path, runner, retries, now, sleep, final, merge_fn,
                        export_fn, budget_s, clock)
    except Exception as e:
        print(f"[git-sync] sync failed: {type(e).__name__}: {e}")
        return False


def _sync_db(db_path, runner, retries, now, sleep, final, merge_fn, export_fn,
             budget_s, clock) -> bool:
    start = clock()

    def left() -> float | None:
        return None if budget_s is None else budget_s - (clock() - start)

    if final:
        retries = 1
        if runner is None:
            runner = lambda cmd: default_runner(cmd, timeout=FINAL_GIT_TIMEOUT_S)  # noqa: E731
    elif budget_s is not None and runner is None:
        runner = lambda cmd: default_runner(  # noqa: E731
            cmd, timeout=max(5.0, min(GIT_TIMEOUT_S, left() or 0)))
    run = runner or default_runner
    merge_fn = merge_fn or store.merge_db_rows
    export_fn = export_fn or default_export
    db = Path(db_path or config.DB_PATH)
    rel = _repo_relative(db)
    if rel is None:
        print(f"[git-sync] {db} is outside the repo; skipped")
        return False

    ts = _timestamp(now)
    committed = _commit_db(run, rel, f"data: price snapshot {ts}")
    if committed is False:
        return False
    if committed:
        print(f"[git-sync] committed snapshot {ts}")

    attempt, merged = 0, False
    while attempt < retries:
        if budget_s is not None and attempt and (left() or 0) <= 0:
            print("[git-sync] out of time for this sync")
            break
        attempt += 1
        cp = run(["git", "pull", "--rebase", "--autostash"])
        if cp.returncode != 0:
            if not merged and _db_conflict(run, rel):
                if not _merge_onto_upstream(run, rel, db, now, merge_fn, export_fn):
                    return False
                merged = True
                attempt -= 1   # the merge doesn't use up an attempt
                continue       # pull again (now a fast-forward), then push
            run(["git", "rebase", "--abort"])  # leave a clean tree for the retry
            print(f"[git-sync] pull failed ({attempt}/{retries}): {_why(cp)}")
        elif not _ahead(run):
            return True
        else:
            cp = run(["git", "push"])
            if cp.returncode == 0:
                print("[git-sync] pushed")
                return True
            print(f"[git-sync] push failed ({attempt}/{retries}): {_why(cp)}")
        if attempt < retries:
            pause = 5 * attempt
            if budget_s is not None:
                pause = min(pause, max(0.0, left() or 0))
            sleep(pause)
    if final and _ahead(run):
        print("[git-sync] exit sync incomplete: local snapshot not pushed")
    return False


def pull_latest(runner: Runner | None = None) -> bool:
    """Bring the checkout up to date before the first poll: a queued watcher's
    checkout predates the previous watcher's final snapshots, and polling on
    that stale DB would re-send alerts it already sent. Never raises."""
    run = runner or default_runner
    cp = run(["git", "pull", "--rebase", "--autostash"])
    if cp.returncode == 0:
        print("[git-sync] up to date before the first poll")
        return True
    run(["git", "rebase", "--abort"])
    print(f"[git-sync] WARNING: pull before the first poll failed; polling on the "
          f"checked-out DB: {_why(cp)}")
    return False
