"""gitsync.sync_db with a fake git (no repo, no network)."""
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src import gitsync, store
from src.models import Observation

DB = gitsync.REPO_ROOT / "data" / "prices.db"
NOW = datetime(2026, 10, 23, 12, 0, 5, tzinfo=timezone.utc)


class FakeGit:
    """Answers git commands from scripted results: {verb: [rc, rc, ...]} where
    verb is the first git argument ('diff', 'pull', 'push', ...). The last rc
    repeats; unscripted verbs succeed. ``ahead`` is rev-list's stdout."""

    def __init__(self, ahead="1", **script):
        self.script = {k: list(v) for k, v in script.items()}
        self.ahead = ahead
        self.calls: list[list[str]] = []

    def __call__(self, cmd):
        self.calls.append(cmd)
        verb = cmd[1]
        rcs = self.script.get(verb, [0])
        rc = rcs.pop(0) if len(rcs) > 1 else rcs[0]
        out = self.ahead if verb == "rev-list" else ""
        return subprocess.CompletedProcess(cmd, rc, out, "boom" if rc else "")

    def verbs(self):
        return [c[1] for c in self.calls]


def _sync(git, **kw):
    sleeps = []
    ok = gitsync.sync_db(DB, runner=git, now=NOW, sleep=sleeps.append, **kw)
    return ok, sleeps


def test_changed_db_is_committed_rebased_and_pushed():
    git = FakeGit(diff=[1])                       # staged changes present
    ok, sleeps = _sync(git)
    assert ok and sleeps == []
    assert git.verbs() == ["add", "diff", "commit", "pull", "rev-list", "push"]
    assert git.calls[0] == ["git", "add", "--", "data/prices.db"]
    assert git.calls[2] == ["git", "commit", "-m", "data: price snapshot 2026-10-23T12:00:05Z",
                            "--", "data/prices.db"]
    assert git.calls[3] == ["git", "pull", "--rebase", "--autostash"]   # never -X theirs


def test_unchanged_db_skips_the_commit_but_still_pulls():
    # pulling picks up coupon_checks.yaml edits pushed while the watcher runs
    git = FakeGit(diff=[0], ahead="0")
    ok, _ = _sync(git)
    assert ok
    assert git.verbs() == ["add", "diff", "pull", "rev-list"]   # nothing to push


def test_unpushed_earlier_commit_is_pushed_even_without_changes():
    git = FakeGit(diff=[0], ahead="2")
    ok, _ = _sync(git)
    assert ok and git.verbs()[-1] == "push"


def test_pull_failure_aborts_the_rebase_and_retries():
    git = FakeGit(diff=[1], pull=[1, 1, 0])
    ok, sleeps = _sync(git)
    assert ok
    assert git.verbs().count("pull") == 3
    assert git.verbs().count("rebase") == 2       # git rebase --abort after each failure
    assert git.verbs()[-1] == "push" and sleeps == [5, 10]


def test_push_failure_retries_then_gives_up():
    git = FakeGit(diff=[1], push=[1])
    ok, sleeps = _sync(git)
    assert ok is False
    assert git.verbs().count("pull") == 3 and git.verbs().count("push") == 3
    assert git.verbs().count("commit") == 1       # committed once, pushed later
    assert sleeps == [5, 10]


def test_push_race_recovers_on_the_next_pull():
    git = FakeGit(diff=[1], push=[1, 0])
    ok, _ = _sync(git)
    assert ok and git.verbs().count("push") == 2


def test_commit_failure_stops():
    git = FakeGit(diff=[1], commit=[1])
    ok, _ = _sync(git)
    assert ok is False and "push" not in git.verbs()


def test_db_outside_the_repo_is_skipped(tmp_path):
    git = FakeGit()
    assert gitsync.sync_db(tmp_path / "x.db", runner=git) is False
    assert git.calls == []


def test_default_runner_never_raises():
    cp = gitsync.default_runner(["definitely-not-a-real-binary-xyz"])
    assert cp.returncode != 0


# --- review round 2: DB conflicts merge rows instead of dropping a side (C0) -----

class RoutedGit:
    """Fake git answering by command prefix: {("pull",): [rc, ...], ...}; the
    last rc repeats. ``outputs`` maps a prefix to stdout."""

    def __init__(self, rcs=None, outputs=None):
        self.rcs = {k: list(v) for k, v in (rcs or {}).items()}
        self.outputs = outputs or {}
        self.calls: list[list[str]] = []

    def _match(self, table, cmd):
        for prefix in sorted(table, key=len, reverse=True):
            if tuple(cmd[1:1 + len(prefix)]) == prefix:
                return prefix
        return None

    def __call__(self, cmd):
        self.calls.append(cmd)
        key = self._match(self.rcs, cmd)
        rc = 0
        if key is not None:
            seq = self.rcs[key]
            rc = seq.pop(0) if len(seq) > 1 else seq[0]
        out_key = self._match(self.outputs, cmd)
        out = self.outputs[out_key] if out_key is not None else ""
        return subprocess.CompletedProcess(cmd, rc, out, "boom" if rc else "")

    def verbs(self):
        return [c[1] for c in self.calls]


def _conflict_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(gitsync, "REPO_ROOT", tmp_path)
    db = tmp_path / "data" / "prices.db"
    db.parent.mkdir()
    db.write_bytes(b"ours")
    git = RoutedGit(
        rcs={("diff", "--cached"): [1], ("pull",): [1, 0]},
        outputs={("diff", "--name-only"): "data/prices.db\n",
                 ("rev-parse",): "origin/main\n", ("rev-list",): "1"})
    return db, git


def _export(spec, dest):
    Path(dest).write_bytes(b"upstream")
    return True


def _merge(src, dst):            # our rows into the copy of upstream's DB
    Path(dst).write_bytes(Path(dst).read_bytes() + b"+" + Path(src).read_bytes())
    return {"observations": 3}


def test_db_conflict_is_merged_onto_upstream(tmp_path, monkeypatch):
    db, git = _conflict_setup(tmp_path, monkeypatch)
    ok = gitsync.sync_db(db, runner=git, now=NOW, sleep=lambda s: None,
                         merge_fn=_merge, export_fn=_export)
    assert ok
    assert db.read_bytes() == b"upstream+ours"        # nobody's rows dropped
    assert ["git", "pull", "--rebase", "--autostash"] in git.calls
    assert not any("theirs" in c for c in git.calls)  # no side silently wins
    assert ["git", "reset", "--keep", "origin/main"] in git.calls
    commits = [c for c in git.calls if c[1] == "commit"]
    assert len(commits) == 2 and "merged" in commits[1][3]
    assert git.verbs()[-1] == "push"


def test_failed_merge_moves_nothing(tmp_path, monkeypatch):   # G2
    db, git = _conflict_setup(tmp_path, monkeypatch)

    def corrupt(src, dst):
        raise sqlite3.DatabaseError("file is not a database")

    assert gitsync.sync_db(db, runner=git, now=NOW, merge_fn=corrupt,
                           export_fn=_export) is False
    assert not any(c[1] == "reset" for c in git.calls)   # branch never moved
    assert db.read_bytes() == b"ours"                     # snapshot intact
    unreadable = RoutedGit(rcs={("diff", "--cached"): [1], ("pull",): [1]},
                           outputs={("diff", "--name-only"): "data/prices.db",
                                    ("rev-parse",): "origin/main"})
    assert gitsync.sync_db(db, runner=unreadable, merge_fn=_merge,
                           export_fn=lambda spec, dest: False) is False
    assert not any(c[1] == "reset" for c in unreadable.calls)


def test_sync_never_raises():   # G2
    def broken(cmd):
        raise OSError("git vanished")
    assert gitsync.sync_db(DB, runner=broken, now=NOW) is False


def test_conflict_merge_still_pushes_in_final_mode(tmp_path, monkeypatch):
    db, git = _conflict_setup(tmp_path, monkeypatch)
    assert gitsync.sync_db(db, runner=git, final=True, merge_fn=_merge, export_fn=_export)
    assert git.verbs()[-1] == "push"


def test_budget_sync_retries_until_its_time_is_up():   # G1
    t = {"now": 0.0, "pulls": 0}

    def slow_pull(cmd):
        if cmd[1] == "pull":
            t["now"] += 70                    # each pull eats 70 s and fails
            t["pulls"] += 1
            return subprocess.CompletedProcess(cmd, 1, "", "timeout")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    ok = gitsync.sync_db(DB, runner=slow_pull, now=NOW, sleep=lambda s: None,
                         budget_s=100, clock=lambda: t["now"])
    assert ok is False
    assert t["pulls"] == 2                    # a 3rd attempt would start past 100 s
    t.update(now=0.0, pulls=0)
    gitsync.sync_db(DB, runner=slow_pull, now=NOW, sleep=lambda s: None,
                    clock=lambda: t["now"])
    assert t["pulls"] == 3                    # no budget: all retries


def test_final_sync_is_one_fast_attempt():
    git = FakeGit(diff=[1], push=[1])
    sleeps = []
    assert gitsync.sync_db(DB, runner=git, now=NOW, sleep=sleeps.append, final=True) is False
    assert git.verbs().count("pull") == 1 and git.verbs().count("push") == 1
    assert sleeps == []


def test_pull_latest():
    git = FakeGit()
    assert gitsync.pull_latest(runner=git, db_path=DB) is True
    assert git.calls == [["git", "diff", "--quiet", "HEAD", "--", "data/prices.db"],
                         ["git", "pull", "--rebase", "--autostash"]]
    git = FakeGit(pull=[1], ahead="0")                # nothing local to merge
    assert gitsync.pull_latest(runner=git, db_path=DB) is False
    assert git.verbs() == ["diff", "pull", "rebase", "rev-list"]   # aborted, never -X theirs


# --- the PC watcher's clone outlives its runs (local role, Oct 2026) ------------

def test_pull_latest_syncs_rows_an_interrupted_run_left_uncommitted():
    git, synced = FakeGit(diff=[1]), []               # the DB differs from HEAD
    ok = gitsync.pull_latest(runner=git, db_path=DB,
                             sync_fn=lambda db, runner=None: synced.append(db) or True)
    assert ok is True and synced == [DB]
    assert git.verbs() == ["diff"]                    # no autostash pull at all


def test_pull_latest_syncs_an_unpushed_snapshot_when_the_pull_fails():
    git, synced = FakeGit(pull=[1], ahead="1"), []
    ok = gitsync.pull_latest(runner=git, db_path=DB,
                             sync_fn=lambda db, runner=None: synced.append(db) or True)
    assert ok is True and synced == [DB]
    assert git.verbs() == ["diff", "pull", "rebase", "rev-list"]


def _repo_pair(tmp_path):
    """remote + clone ``cloud`` (pushed a row after ``pc`` last synced) +
    clone ``pc``; both DBs start with row X."""
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    cloud, pc = tmp_path / "cloud", tmp_path / "pc"
    _clone(remote, cloud)
    _git(cloud, "checkout", "-b", "main")
    (cloud / ".gitattributes").write_text("*.db binary\n")
    (cloud / "data").mkdir()
    _add_row(cloud / "data" / "prices.db", "X", "2026-10-23T05:00:00+00:00")
    _git(cloud, "add", ".")
    _git(cloud, "commit", "-m", "base")
    _git(cloud, "push", "-u", "origin", "main")
    _clone(remote, pc)
    _add_row(cloud / "data" / "prices.db", "CLOUD-night", "2026-10-23T06:00:00+00:00")
    _git(cloud, "commit", "-am", "cloud snapshot")
    _git(cloud, "push")
    return remote, pc


def _rows_on_remote(remote, tmp_path):
    check = tmp_path / "check"
    _clone(remote, check)
    conn = store.connect(check / "data" / "prices.db")
    try:
        return ({r[0] for r in conn.execute("SELECT listing_id FROM observations")},
                {r[0] for r in conn.execute("SELECT listing_id FROM alerts_sent")})
    finally:
        conn.close()


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_pc_shut_down_mid_loop_rows_are_merged_not_stashed(tmp_path, monkeypatch):
    """The PC's run was killed after polling (rows in the DB, no commit) while
    the cloud pushed: a plain autostash pull would exit 0 with those rows in
    a stash and the DB unmerged. pull_latest merges and pushes them."""
    remote, pc = _repo_pair(tmp_path)
    _add_row(pc / "data" / "prices.db", "PC-last", "2026-10-23T05:50:00+00:00")
    monkeypatch.setattr(gitsync, "REPO_ROOT", pc)
    assert gitsync.pull_latest(db_path=pc / "data" / "prices.db") is True
    assert _git(pc, "status", "--porcelain").stdout == ""
    assert _git(pc, "stash", "list").stdout == ""
    assert _rows_on_remote(remote, tmp_path) == ({"X", "CLOUD-night", "PC-last"},) * 2


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_pc_snapshot_that_never_pushed_is_merged(tmp_path, monkeypatch):
    remote, pc = _repo_pair(tmp_path)
    _add_row(pc / "data" / "prices.db", "PC-last", "2026-10-23T05:50:00+00:00")
    _git(pc, "commit", "-am", "data: price snapshot (push failed)")
    monkeypatch.setattr(gitsync, "REPO_ROOT", pc)
    assert gitsync.pull_latest(db_path=pc / "data" / "prices.db") is True
    assert _rows_on_remote(remote, tmp_path) == ({"X", "CLOUD-night", "PC-last"},) * 2


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True)


def _clone(remote, path):
    _git(path.parent, "clone", str(remote), str(path))
    _git(path, "config", "user.name", "t")
    _git(path, "config", "user.email", "t@e.x")
    _git(path, "config", "commit.gpgsign", "false")


def _add_row(db, listing_id, ts):
    conn = store.connect(db)
    store.init_db(conn)
    store.insert_observation(conn, Observation(
        source_id="amazon_seminovos", seller="S", venue="amazon", listing_id=listing_id,
        url="u", ts=ts, list_price=4000.0, in_stock=True))
    store.record_alert(conn, listing_id, "aggressive", 3790.0, ts)
    conn.commit()
    conn.close()


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_handover_keeps_both_watchers_rows_with_real_git(tmp_path, monkeypatch):
    """Watcher A pushes its final snapshot after watcher B started from an older
    commit; B's sync must keep A's rows (they used to be dropped by -X theirs)."""
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    _clone(remote, a)
    _git(a, "checkout", "-b", "main")
    (a / ".gitattributes").write_text("*.db binary\n")
    (a / "data").mkdir()
    _add_row(a / "data" / "prices.db", "X", "2026-10-23T05:00:00+00:00")
    _git(a, "add", ".")
    _git(a, "commit", "-m", "base")
    _git(a, "push", "-u", "origin", "main")
    _clone(remote, b)                                  # B starts here
    _add_row(a / "data" / "prices.db", "A-final", "2026-10-23T05:48:00+00:00")
    _git(a, "commit", "-am", "A final snapshot")
    _git(a, "push")
    _add_row(b / "data" / "prices.db", "B-1", "2026-10-23T06:10:00+00:00")

    monkeypatch.setattr(gitsync, "REPO_ROOT", b)
    assert gitsync.sync_db(b / "data" / "prices.db", sleep=lambda s: None)

    _clone(remote, c)
    conn = store.connect(c / "data" / "prices.db")
    obs = {r[0] for r in conn.execute("SELECT listing_id FROM observations")}
    alerts = {r[0] for r in conn.execute("SELECT listing_id FROM alerts_sent")}
    conn.close()
    assert obs == alerts == {"X", "A-final", "B-1"}
