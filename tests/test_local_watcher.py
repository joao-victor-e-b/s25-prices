"""The PC watcher next to the cloud one (Oct 2026): the role (S25_ROLE /
--role) and run_log.host/ok, the cloud's standby while a healthy local poll
is fresh, the loop's announce and refresh syncs, resumes from a suspend, the
PC's pull retries and held sends, clock skew, one loop per clone, host and ok
through migration and merges, and the heartbeat's per-host counts. No
network."""
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from src import config, coupons, notify, promos as promos_mod, run, store
from src.coupons import CashbackStatus, CouponCode
from src.models import Promo
from tests.test_loop import Clock
from tests.test_run import (ASIN, COUPON_LINE, NOW, PIX_LINE, Env, FakeSource, _hb_conn,
                            _line, _offer)

ROOT = Path(__file__).resolve().parent.parent
COMMITTED_DB = ROOT / "data" / "prices.db"


@pytest.fixture
def env(monkeypatch, tmp_path):
    return Env(monkeypatch, tmp_path / "prices.db")   # polls at 12:00, 12:04, 12:08...


def _logged(db, ts, host):
    """A run_log row as a watcher's poll writes it."""
    conn = store.connect(db)
    try:
        store.init_db(conn)
        store.insert_run_log(conn, ts, 1, 0, 0, 0, "seed", host=host)
        conn.commit()
    finally:
        conn.close()


# --- the role -------------------------------------------------------------------

def test_role_is_cloud_unless_s25_role_or_the_flag_say_local(monkeypatch):
    assert config.role() == "cloud"                   # conftest: S25_ROLE empty
    monkeypatch.setenv("S25_ROLE", " Local ")
    assert config.role() == "local"
    assert config.role("cloud") == "cloud"            # the flag wins
    monkeypatch.setenv("S25_ROLE", "lcoal")
    with pytest.raises(ValueError, match="cloud, local"):
        config.role()                                 # a typo is refused, not guessed
    with pytest.raises(ValueError):
        config.role("pc")


def test_cli_role_reaches_the_loop_and_the_poll(monkeypatch, capsys):
    monkeypatch.setenv("NTFY_TOPIC", "s25-test")        # the PC needs a channel
    got = {}
    monkeypatch.setattr(run, "loop", lambda **kw: got.update(kw) or {"polls": 0})
    monkeypatch.setattr(run, "poll", lambda **kw: got.update(kw) or {})
    run.main(["--loop", "--git-sync", "--role", "local"])
    assert got["role"] == "local"
    run.main(["--poll", "--dry-run"])
    assert got["role"] == "cloud"
    monkeypatch.setenv("S25_ROLE", "local")
    run.main(["--poll"])
    assert got["role"] == "local"
    run.main(["--poll", "--role", "cloud"])
    assert got["role"] == "cloud"
    with pytest.raises(SystemExit) as e:
        run.main(["--poll", "--role", "pc"])
    assert e.value.code == 2
    monkeypatch.setenv("S25_ROLE", "laptop")
    with pytest.raises(SystemExit) as e:
        run.main(["--loop"])
    assert e.value.code == 2 and "S25_ROLE" in capsys.readouterr().err
    monkeypatch.setattr(run, "heartbeat",             # the heartbeat doesn't care
                        lambda: {"wa_ok": True, "ntfy_ok": False, "message": "x"})
    assert run.main(["--heartbeat"]) == 0


def test_every_poll_logs_its_host(env):
    env.source = FakeSource([_offer()])
    env.poll()
    env.poll(role="local")
    assert [r["host"] for r in env.rows("SELECT host FROM run_log ORDER BY ts")] == [
        "cloud", "local"]


# --- cloud standby ----------------------------------------------------------------

LAST_LOCAL = "2026-10-23T11:50:00+00:00"


def _hot_offer():
    """3.700 (🟠) with a new coupon message whose terms an alert would fetch."""
    return _offer(list_price=3700.0, pix_discount=185.0, promos=[PIX_LINE, COUPON_LINE],
                  promo_ids={COUPON_LINE: "PROMO123"})


@pytest.fixture
def senders(env, monkeypatch):
    """Every way out besides Env's stubs (price, 🔍, promo messages): coupon/
    cashback and DDG pings, promotion-terms lookups — with all of them fed
    something worth sending, and the raw channels rigged to fail the test."""
    out = {"coupons": [], "ddg": [], "terms": []}

    def report(items):
        return {"sent": len(items), "wa_ok": True, "ntfy_ok": True,
                "delivered": list(range(len(items)))}

    def no_channel(*a, **kw):
        raise AssertionError("a raw channel was called")

    monkeypatch.setattr(notify, "notify_coupons", lambda b: out["coupons"].extend(b) or report(b))
    monkeypatch.setattr(notify, "notify_promos", lambda p: out["ddg"].extend(p) or report(p))
    monkeypatch.setattr(notify, "send_whatsapp", no_channel)
    monkeypatch.setattr(notify, "send_ntfy", no_channel)
    monkeypatch.setattr(run, "_terms_session", lambda: None)
    monkeypatch.setattr(run, "amazon_promo_terms",
                        lambda session, pid: out["terms"].append(pid) or "Termos")
    monkeypatch.setattr(coupons, "scan_codes", lambda session=None: ([CouponCode(
        source="cuponomia,promobit", code="CUPOM150", abs_off=150.0, until="2099-12-31")], []))
    monkeypatch.setattr(coupons, "scan_cashback", lambda session=None: ([CashbackStatus(
        portal="meliuz", rate_pct=5.0, active=True, url="https://meliuz")], []))
    monkeypatch.setattr(promos_mod, "scan", lambda: [Promo(
        source="pelando.com.br", title="S25 Ultra 256GB", url="https://pelando/x", price=3400.0)])
    monkeypatch.setattr(promos_mod, "is_active", lambda url: True)
    env.source = FakeSource([_hot_offer()], summary=_line(n=4, price=3600.0))
    return out


def _nothing_sent(env, senders):
    assert env.alerts == env.unseen == env.promo_blocks == []
    assert senders == {"coupons": [], "ddg": [], "terms": []}


def test_cloud_standby_stores_everything_and_sends_nothing(env, senders, capsys):
    _logged(env.db, LAST_LOCAL, "local")
    _logged(env.db, "2026-10-23T11:58:00+00:00", "cloud")   # newer, but not the PC
    r = env.poll(scan_promos=True)                          # + coupon sweep + DDG
    out = capsys.readouterr().out
    assert out.count("[standby]") == 1
    assert f"[standby] local watcher active (last poll {LAST_LOCAL})\n" in out
    _nothing_sent(env, senders)
    assert env.rows("SELECT * FROM alerts_sent") == []
    assert (r["alerts"], r["unseen_alerts"]) == (1, 1)      # evaluated, held
    assert r["standby"] is True and r["standby_left_s"] == 35 * 60
    for table, n in (("observations", 1), ("offer_summary", 1), ("offer_promos", 2),
                     ("coupon_codes", 1), ("cashback_status", 1), ("promotions", 1)):
        assert len(env.rows(f"SELECT * FROM {table}")) == n, table
    log = env.rows("SELECT host, detail FROM run_log ORDER BY ts DESC")[0]
    assert log["host"] == "cloud"
    assert f"standby: local poll {LAST_LOCAL}" in log["detail"]


def test_standby_dry_run_does_not_claim_anything_would_send(env, senders, capsys):
    _logged(env.db, LAST_LOCAL, "local")
    env.poll(scan_promos=True, dry_run=True)
    out = capsys.readouterr().out
    assert "[standby] local watcher active" in out and "would send" not in out
    _nothing_sent(env, senders)


def test_held_alerts_go_out_once_the_local_poll_is_over_45_min_old(env, senders, capsys):
    _logged(env.db, "2026-10-23T11:20:00+00:00", "local")
    assert env.poll(scan_promos=True)["standby"]            # 12:00: 40 min old
    assert env.poll(scan_promos=True)["standby_left_s"] == 60   # 12:04: 44 min old
    _nothing_sent(env, senders)
    capsys.readouterr()
    r = env.poll(scan_promos=True)                          # 12:08: 48 min old
    assert "[standby]" not in capsys.readouterr().out
    assert r["standby"] is False and r["standby_left_s"] is None
    assert len(env.alerts) == len(env.unseen) == len(env.promo_blocks) == 1
    assert len(senders["coupons"]) == 2 and len(senders["ddg"]) == 1   # code + cashback
    assert senders["terms"] == ["PROMO123"]
    assert {x["trigger"] for x in env.rows("SELECT trigger FROM alerts_sent")} == {
        "aggressive", "unseen_offer", "offer_promo", "coupon", "cashback", "promo"}


@pytest.mark.parametrize("ts, host", [
    ("2026-10-23T11:14:59+00:00", "local"),     # 45 min 1 s before the poll
    ("2026-10-23T11:59:00+00:00", "cloud"),     # fresh, but a cloud poll
    ("2026-10-23T11:59:00+00:00", None),        # a row from before the host column
])
def test_no_standby_without_a_fresh_local_poll(env, senders, capsys, ts, host):
    _logged(env.db, ts, host)
    r = env.poll()
    assert r["standby"] is False and "[standby]" not in capsys.readouterr().out
    assert len(env.alerts) == len(env.unseen) == len(env.promo_blocks) == 1
    assert len(env.rows("SELECT * FROM alerts_sent")) == 3


def test_the_pc_watcher_never_stands_by(env, senders, capsys):
    _logged(env.db, "2026-10-23T11:58:00+00:00", "local")   # its own previous poll
    r = env.poll(role="local")
    assert r["standby"] is False and "[standby]" not in capsys.readouterr().out
    assert len(env.alerts) == len(env.unseen) == len(env.promo_blocks) == 1
    assert env.rows("SELECT host FROM run_log ORDER BY ts DESC")[0]["host"] == "local"


def test_local_watcher_freshness_boundary(tmp_path):
    conn = store.connect(tmp_path / "x.db")
    store.init_db(conn)
    assert run._local_watcher(conn, NOW) is None
    store.insert_run_log(conn, "2026-10-23T11:15:00+00:00", 1, 0, 0, 0, host="local")
    assert run._local_watcher(conn, NOW) == ("2026-10-23T11:15:00+00:00", 0)   # 45 min sharp
    assert run._local_watcher(conn, "2026-10-23T12:00:01+00:00") is None
    store.insert_run_log(conn, "2026-10-23T11:30:00+00:00", 1, 0, 0, 0, host="local")
    assert run._local_watcher(conn, NOW) == ("2026-10-23T11:30:00+00:00", 15 * 60)  # latest


# --- the loop: the PC announces itself, the cloud looks before its standby lapses -----

def _scripted(clock, results):
    """poll/sync stand-ins logging ("poll", t, role) / ("sync", t, kwargs);
    poll n returns results[n-1] (the last one repeats) and takes 10 s."""
    events = []

    def poll_fn(**kw):
        events.append(("poll", clock.t, kw["role"]))
        clock.t += 10
        n = sum(1 for e in events if e[0] == "poll")
        return results[min(n, len(results)) - 1]

    def sync_fn(**kw):
        events.append(("sync", clock.t, kw))
        return True

    return events, poll_fn, sync_fn


def _loop(clock, poll_fn, sync_fn, **kw):
    return run.loop(poll_fn=poll_fn, sync_fn=sync_fn, pull_fn=lambda: True,
                    cdi_fn=lambda: 0.01, clock=clock, sleep=clock.sleep,
                    uniform=lambda a, b: 1.0, minutes=60, interval=240, git_sync=True, **kw)


def _syncs(events):
    return [t for kind, t, _ in events if kind == "sync"]


def test_the_pc_watcher_syncs_right_after_its_first_poll():
    clock = Clock()
    events, poll_fn, sync_fn = _scripted(clock, [{}])
    _loop(clock, poll_fn, sync_fn, role="local")
    assert {role for kind, _, role in events if kind == "poll"} == {"local"}
    assert _syncs(events) == [10, 2000, 3510]       # announce, every 30 min, exit
    clock = Clock()
    events, poll_fn, sync_fn = _scripted(clock, [{}])
    _loop(clock, poll_fn, sync_fn)                   # the cloud keeps its 30-min beat
    assert {role for kind, _, role in events if kind == "poll"} == {"cloud"}
    assert _syncs(events) == [2000, 3510]


def test_the_cloud_syncs_before_the_poll_its_standby_would_lapse_on():
    # the standby runs out at t=600 by what the checkout knows: the poll due at
    # t=750 is preceded by a sync (the PC may have pushed newer polls since)
    clock = Clock()
    events, poll_fn, sync_fn = _scripted(clock, [
        {"standby_left_s": 600}, {"standby_left_s": 350}, {"standby_left_s": 100}, {}])
    _loop(clock, poll_fn, sync_fn)
    assert events[:5] == [("poll", 0, "cloud"), ("poll", 250, "cloud"),
                          ("poll", 500, "cloud"), ("sync", 750, {}), ("poll", 750, "cloud")]
    assert _syncs(events) == [750, 2750, 3510]       # the 30-min beat restarts from it


def test_a_due_sync_and_a_lapse_sync_once():
    # poll 8 (t=1750) has 250 s of standby left: at t=2000 the standby lapses
    # just as the 30-min sync falls due — one sync, before that poll
    clock = Clock()
    events, poll_fn, sync_fn = _scripted(clock, [{}] * 7 + [{"standby_left_s": 250}, {}])
    _loop(clock, poll_fn, sync_fn)
    assert _syncs(events) == [2000, 3510]
    assert events[events.index(("sync", 2000, {})) + 1] == ("poll", 2000, "cloud")


def test_a_failed_lapse_sync_never_stops_the_watcher():
    clock = Clock()
    events, poll_fn, _ = _scripted(clock, [{"standby_left_s": 100}, {}])
    calls = []

    def flaky(**kw):
        calls.append(clock.t)
        if len(calls) == 1:
            raise RuntimeError("push rejected")
        return True
    r = _loop(clock, poll_fn, flaky)
    assert calls[0] == 250 and r["polls"] == 15      # polled on to the budget
    assert ("poll", 250, "cloud") in events


# --- host through migrations and merges ------------------------------------------

def test_host_column_migration_on_a_copy_of_the_committed_db(tmp_path):
    if not COMMITTED_DB.exists():
        pytest.skip("no committed DB")
    db = tmp_path / "prices.db"
    shutil.copyfile(COMMITTED_DB, db)                 # read only: the copy is migrated
    conn = sqlite3.connect(db)
    added = [c for c in ("host", "ok")
             if c in {r[1] for r in conn.execute("PRAGMA table_info(run_log)")}]
    if added:
        # the watcher already migrated it: undo that, to migrate what predates it
        if sqlite3.sqlite_version_info < (3, 35):
            pytest.skip("needs SQLite >= 3.35 for DROP COLUMN")
        for c in added:
            conn.execute(f"ALTER TABLE run_log DROP COLUMN {c}")
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    old = [tuple(r) for r in conn.execute("SELECT * FROM run_log ORDER BY ts")]
    conn.commit()
    conn.close()
    assert old, "the committed DB has polls"

    conn = store.connect(db)
    try:
        store.init_db(conn)
        store.init_db(conn)                           # idempotent
        cols = [r[1] for r in conn.execute("PRAGMA table_info(run_log)")]
        assert cols == ["ts", "polled", "failures", "partial", "coupon_errors", "detail",
                        "host", "ok"]
        assert {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in tables} == counts            # nothing lost anywhere
        rows = [tuple(r) for r in conn.execute("SELECT * FROM run_log ORDER BY ts")]
        assert [r[:-2] for r in rows] == old and {r[-2:] for r in rows} == {(None, None)}
        latest = old[-1][0]
        assert run._local_watcher(conn, latest) is None   # history alone: no standby
        tot = store.run_log_totals(conn, latest)
        assert tot["local_runs"] == 0 and tot["cloud_runs"] == tot["runs"] > 0
        store.insert_run_log(conn, "2099-01-01T00:00:00+00:00", 6, 0, 0, 0, "x", host="local")
        assert store.last_poll(conn, "local") == "2099-01-01T00:00:00+00:00"
        assert run._local_watcher(conn, "2099-01-01T00:10:00+00:00") == (
            "2099-01-01T00:00:00+00:00", 35 * 60)
        # a failed local poll is the latest, but not the one the standby rests on
        store.insert_run_log(conn, "2099-01-01T00:05:00+00:00", 6, 6, 0, 0, "x",
                             host="local", ok=False)
        assert store.last_poll(conn, "local") == "2099-01-01T00:05:00+00:00"
        assert store.last_poll(conn, "local", healthy=True) == "2099-01-01T00:00:00+00:00"
        assert run._local_watcher(conn, "2099-01-01T00:10:00+00:00")[0] == (
            "2099-01-01T00:00:00+00:00")
    finally:
        conn.close()


_OLD_RUN_LOG = """
CREATE TABLE run_log (ts TIMESTAMP PRIMARY KEY, polled INTEGER, failures INTEGER,
  partial INTEGER, coupon_errors INTEGER, detail TEXT);
"""


def _log_db(path, rows, old_schema=False):
    if old_schema:                                    # a watcher from before the column
        conn = sqlite3.connect(path)
        conn.executescript(_OLD_RUN_LOG)
        for ts, _ in rows:
            conn.execute("INSERT INTO run_log VALUES (?, 1, 0, 0, 0, 'd')", (ts,))
    else:
        conn = store.connect(path)
        store.init_db(conn)
        for ts, host in rows:
            store.insert_run_log(conn, ts, 1, 0, 0, 0, "d", host=host)
    conn.commit()
    conn.close()
    return path


def _hosts(path):
    conn = store.connect(path)
    try:
        return {r["ts"]: r["host"] for r in conn.execute("SELECT ts, host FROM run_log")}
    finally:
        conn.close()


T1, T2, T3, T4 = (f"2026-10-23T1{i}:00:00+00:00" for i in range(4))


def test_merge_carries_run_log_host(tmp_path):
    upstream = _log_db(tmp_path / "up.db", [(T1, "cloud")])
    pc = _log_db(tmp_path / "pc.db", [(T1, "cloud"), (T2, "local")])
    assert store.merge_db_rows(pc, upstream)["run_log"] == 1
    assert _hosts(upstream) == {T1: "cloud", T2: "local"}
    # an upstream still on the old schema gains the column, and the PC's hosts
    old = _log_db(tmp_path / "old.db", [(T3, None)], old_schema=True)
    store.merge_db_rows(pc, old)
    assert _hosts(old) == {T1: "cloud", T2: "local", T3: None}
    # an old watcher's rows merge in without a host
    older = _log_db(tmp_path / "older.db", [(T4, None)], old_schema=True)
    store.merge_db_rows(older, upstream)
    assert _hosts(upstream) == {T1: "cloud", T2: "local", T4: None}


def test_merge_carries_run_log_ok(tmp_path):
    def db(path, rows):
        conn = store.connect(path)
        store.init_db(conn)
        for ts, ok in rows:
            store.insert_run_log(conn, ts, 1, 0, 0, 0, "d", host="local", ok=ok)
        conn.commit()
        conn.close()
        return path
    upstream = db(tmp_path / "up.db", [(T1, True)])
    pc = db(tmp_path / "pc.db", [(T2, False), (T3, True)])
    store.merge_db_rows(pc, upstream)
    conn = store.connect(upstream)
    try:
        assert {r["ts"]: r["ok"] for r in conn.execute("SELECT ts, ok FROM run_log")} == {
            T1: 1, T2: 0, T3: 1}
    finally:
        conn.close()


# --- the heartbeat's 24h line: PC ativo / só nuvem -------------------------------------

def test_heartbeat_counts_polls_per_host(tmp_path):
    conn = _hb_conn(tmp_path)
    assert "\n24h: 0 polls · 0 falhas" in run._heartbeat_message(conn, NOW)
    for ts, host in (("2026-10-22T11:00:00+00:00", "local"),     # over 24h ago
                     ("2026-10-22T13:00:00+00:00", None),        # before the column
                     ("2026-10-23T08:00:00+00:00", "cloud"),
                     ("2026-10-23T09:00:00+00:00", "cloud")):
        store.insert_run_log(conn, ts, 1, 0, 0, 0, host=host)
    assert "\n24h: 3 polls (só nuvem) · 0 falhas" in run._heartbeat_message(conn, NOW)
    for ts in ("2026-10-23T10:00:00+00:00", "2026-10-23T11:00:00+00:00"):
        store.insert_run_log(conn, ts, 1, 0, 0, 0, host="local")
    tot = store.run_log_totals(conn, NOW)
    assert (tot["runs"], tot["local_runs"], tot["cloud_runs"]) == (5, 2, 3)
    assert run._heartbeat_message(conn, NOW).split("\n")[-1] == (
        "24h: 5 polls (PC ativo 2, nuvem 3) · 0 falhas · 0 parciais · 0 erros cupom"
        " · 6 fontes")


def test_heartbeat_without_host_counts_keeps_its_old_line():
    tot = {"runs": 3, "failures": 0, "partial": 0, "coupon_errors": 0}
    assert notify.format_heartbeat(None, None, 6, tot).endswith(
        "\n24h: 3 polls · 0 falhas · 0 parciais · 0 erros cupom · 6 fontes")


# --- review round: only a healthy PC holds the cloud ----------------------------------

def test_poll_health_goes_to_run_log(env, senders):
    r = env.poll(role="local")                              # 12:00: saw it, sent it
    assert (r["ok"], r["undelivered"]) == (True, 0)
    env.source.error = RuntimeError("amazon: captcha / bot wall")
    assert env.poll(role="local")["ok"] is False            # 12:04: every fetch raised
    env.source.error = None
    env.source.degraded = ["dp: captcha"]                   # a blocked product page
    assert env.poll(role="local")["ok"] is False            # 12:08: hard block
    env.source.degraded = []
    env.delivered = False                                   # no channel takes it
    env.source.offers = [_hot_offer()]
    env.source.offers[0].list_price = 3600.0                # R$ 100 lower: news
    r = env.poll(role="local")                              # 12:12
    assert (r["ok"], r["undelivered"]) == (False, 1)
    rows = env.rows("SELECT ok, detail FROM run_log ORDER BY ts")
    assert [x["ok"] for x in rows] == [1, 0, 0, 0]
    assert "undelivered: 1 alert item(s)" in rows[-1]["detail"]


@pytest.mark.parametrize("trouble", ["blind", "mute"])
def test_a_blind_or_mute_pc_does_not_hold_the_cloud(env, senders, capsys, trouble):
    # the PC keeps logging fresh polls, but can't see the offer (a captcha on
    # the home IP) or can't deliver it (a stale .env): the cloud must send
    if trouble == "blind":
        env.source.error = RuntimeError("amazon: captcha / bot wall")
    else:
        env.delivered = False
    assert [env.poll(role="local")["ok"] for _ in range(3)] == [False] * 3
    env.source.error, env.delivered = None, True
    capsys.readouterr()
    r = env.poll()                                          # 12:12: the cloud
    assert r["standby"] is False and "[standby]" not in capsys.readouterr().out
    assert {x["trigger"] for x in env.rows("SELECT trigger FROM alerts_sent")} == {
        "aggressive", "unseen_offer", "offer_promo"}


def test_standby_rests_on_the_last_healthy_local_poll(tmp_path, capsys):
    conn = store.connect(tmp_path / "x.db")
    store.init_db(conn)

    def local(hhmm, ok):
        store.insert_run_log(conn, f"2026-10-23T{hhmm}:00+00:00", 6, 0 if ok else 6,
                             0, 0, host="local", ok=ok)

    local("11:20", True)
    local("11:50", False)                                   # not the one to go by
    assert run._local_watcher(conn, NOW) == ("2026-10-23T11:20:00+00:00", 5 * 60)
    local("11:54", False)
    assert run._local_watcher(conn, NOW)[0] == "2026-10-23T11:20:00+00:00"
    assert "[standby] off" not in capsys.readouterr().out
    local("11:58", False)                                   # 3 failed in a row: off now
    assert run._local_watcher(conn, NOW) is None
    assert ("[standby] off: the PC's last 3 polls failed (blind, or its alerts "
            "undelivered; latest 2026-10-23T11:58:00+00:00): sending") in capsys.readouterr().out
    local("11:59", True)                                    # it recovered
    assert run._local_watcher(conn, NOW) == ("2026-10-23T11:59:00+00:00", 44 * 60)
    # only failed polls lately, the last good one too old: nothing to stand by for
    conn.execute("DELETE FROM run_log")
    local("11:00", True)
    local("11:59", False)
    assert run._local_watcher(conn, NOW) is None
    # rows from before the ok column count as healthy
    conn.execute("UPDATE run_log SET ok=NULL")
    assert run._local_watcher(conn, NOW) == ("2026-10-23T11:59:00+00:00", 44 * 60)


def test_the_pc_watcher_refuses_to_run_without_a_channel(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(run, "loop", lambda **kw: called.append(kw) or {"polls": 0})
    monkeypatch.setattr(run, "poll", lambda **kw: called.append(kw) or {})
    assert run.main(["--loop", "--git-sync", "--role", "local"]) == 2   # conftest: none set
    monkeypatch.setenv("S25_ROLE", "local")
    assert run.main(["--poll"]) == 2
    assert called == []
    assert "no alert channel configured" in capsys.readouterr().err
    monkeypatch.setenv("CALLMEBOT_PHONE", "5511999999999")     # half a channel
    assert run.main(["--loop"]) == 2
    assert run.main(["--poll", "--dry-run"]) == 0             # sends nothing anyway
    assert run.main(["--loop", "--role", "cloud"]) == 0       # the cloud runs as before
    monkeypatch.setenv("CALLMEBOT_APIKEY", "123456")
    assert run.main(["--loop"]) == 0
    monkeypatch.setenv("CALLMEBOT_PHONE", "")
    monkeypatch.setenv("NTFY_TOPIC", "s25-test")
    assert run.main(["--loop"]) == 0
    assert [kw["role"] for kw in called] == ["local", "cloud", "local", "local"]


def test_a_held_poll_stores_everything_and_sends_nothing(env, senders, capsys):
    r = env.poll(scan_promos=True, role="local", hold=True)
    assert "[hold] the DB couldn't be synced yet" in capsys.readouterr().out
    _nothing_sent(env, senders)
    assert env.rows("SELECT * FROM alerts_sent") == []
    assert (r["held"], r["standby"], r["ok"]) == (True, False, True)   # a hold isn't a failure
    assert (r["alerts"], r["unseen_alerts"]) == (1, 1)
    log = env.rows("SELECT host, ok, detail FROM run_log")[0]
    assert (log["host"], log["ok"]) == ("local", 1) and "hold: DB not synced" in log["detail"]
    assert len(env.rows("SELECT * FROM observations")) == 1
    env.poll(scan_promos=True, role="local")                 # synced: it all goes out
    assert len(env.alerts) == len(env.unseen) == len(env.promo_blocks) == 1


# --- review round: the loop across a suspend, and a network that isn't up yet ---------

class Suspending(Clock):
    """The loop clock plus a wall clock. ``suspend`` maps a sleep's number
    (1-based) to the seconds the machine spends suspended inside it: the wall
    clock always counts them, the loop's monotonic clock only with
    ``monotonic_counts`` (QueryPerformanceCounter may or may not)."""

    def __init__(self, suspend=None, monotonic_counts=True):
        super().__init__()
        self.suspend, self.monotonic_counts, self.skew = suspend or {}, monotonic_counts, 0.0

    def wall(self) -> float:
        return 1_761_220_800.0 + self.t + self.skew

    def sleep(self, s: float) -> None:
        super().sleep(s)
        extra = self.suspend.get(len(self.sleeps), 0.0)
        if self.monotonic_counts:
            self.t += extra
        else:
            self.skew += extra


def _watch(clock, role="local", pulls=(True,), syncs=(True,), minutes=345):
    """run.loop with stand-ins logging (kind, loop-clock t, detail): ("pull",
    t, None), ("poll", t, hold) — 10 s each — and ("sync", t, kwargs). Pulls
    and periodic syncs return the next of ``pulls`` / ``syncs`` (the last
    repeats)."""
    events = []

    def nxt(seq, kind):
        n = sum(1 for e in events if e[0] == kind and (kind != "sync" or not e[2]))
        return seq[min(n, len(seq)) - 1]

    def pull_fn():
        events.append(("pull", clock.t, None))
        return nxt(pulls, "pull")

    def poll_fn(**kw):
        events.append(("poll", clock.t, kw["hold"]))
        clock.t += 10
        return {}

    def sync_fn(**kw):
        events.append(("sync", clock.t, kw))
        return nxt(syncs, "sync") if not kw else True

    run.loop(poll_fn=poll_fn, sync_fn=sync_fn, pull_fn=pull_fn, cdi_fn=lambda: 0.01,
             clock=clock, sleep=clock.sleep, wall=clock.wall, uniform=lambda a, b: 1.0,
             minutes=minutes, interval=240, git_sync=True, role=role)
    return events


@pytest.mark.parametrize("monotonic_counts", [True, False])
def test_after_a_suspend_the_pc_syncs_before_it_polls(monotonic_counts, capsys):
    # the lid closes during the 3rd sleep for 2 h: the cloud took over and may
    # have alerted; the PC must merge its alerts_sent before judging anything
    clock = Suspending({3: 7200}, monotonic_counts)
    events = _watch(clock)
    t = 750 + (7200 if monotonic_counts else 0)            # the loop clock on waking
    assert events[:9] == [
        ("pull", 0, None), ("poll", 0, False), ("sync", 10, {}),   # announce
        ("poll", 250, False), ("poll", 500, False),
        ("sync", t, {}), ("poll", t, False),               # the resume: sync first
        ("sync", t + 10, {}),                              # announce again: standby back
        ("poll", t + 250, False)]
    assert "[loop] resumed after 7440s asleep (240s planned)" in capsys.readouterr().out


@pytest.mark.parametrize("monotonic_counts", [True, False])
def test_a_run_that_slept_past_its_budget_stops_without_polling(monotonic_counts):
    clock = Suspending({3: 8 * 3600}, monotonic_counts)
    events = _watch(clock)
    t = 750 + (8 * 3600 if monotonic_counts else 0)
    assert events == [("pull", 0, None), ("poll", 0, False), ("sync", 10, {}),
                      ("poll", 250, False), ("poll", 500, False),
                      ("sync", t, {"budget_s": run.EXIT_SYNC_BUDGET_S})]   # the exit sync


def test_after_a_suspend_the_cloud_syncs_first_but_does_not_announce():
    clock = Suspending({3: 7200})
    events = _watch(clock, role="cloud")
    assert events[:6] == [("pull", 0, None), ("poll", 0, False), ("poll", 250, False),
                          ("poll", 500, False), ("sync", 7950, {}), ("poll", 7950, False)]
    assert events[6] == ("poll", 8200, False)


def test_a_short_overrun_is_no_resume():
    clock = Suspending({3: run.RESUME_SLACK_S - 1})
    events = _watch(clock, minutes=30)
    i = events.index(("poll", 500, False))
    assert events[i + 1][0] == "poll"                      # no extra sync
    assert [e[1] for e in events if e[0] == "sync"] == [10, events[-1][1]]


def test_the_pc_retries_its_first_pull_then_holds_its_alerts_until_a_sync_works(capsys):
    clock = Suspending()
    events = _watch(clock, pulls=(False,), syncs=(False, False, True))
    assert clock.sleeps[:4] == list(run.PULL_RETRY_S)      # 15, 30, 60, 120 s
    t0 = sum(run.PULL_RETRY_S)
    assert events[:12] == [
        ("pull", 0, None), ("pull", 15, None), ("pull", 45, None), ("pull", 105, None),
        ("pull", t0, None),
        ("poll", t0, True), ("sync", t0 + 10, {}),         # store only; announce fails
        ("sync", t0 + 250, {}), ("poll", t0 + 250, True),  # held: sync first, fails
        ("sync", t0 + 500, {}), ("poll", t0 + 500, False), # it worked: alerts on again
        ("poll", t0 + 750, False)]
    out = capsys.readouterr().out
    assert "[loop] no pull: polling store-only (no alerts) until a sync works" in out
    assert "[loop] DB synced: alerts on again" in out


def test_a_pull_that_works_on_a_retry_holds_nothing():
    clock = Suspending()
    events = _watch(clock, pulls=(False, True))
    assert clock.sleeps[0] == 15
    assert [e[0] for e in events[:3]] == ["pull", "pull", "poll"]
    assert {e[2] for e in events if e[0] == "poll"} == {False}


def test_the_cloud_neither_retries_its_pull_nor_holds():
    # a CI checkout is minutes old: polling on it beats waiting
    clock = Suspending()
    events = _watch(clock, role="cloud", pulls=(False,))
    assert events[:2] == [("pull", 0, None), ("poll", 0, False)]
    assert {e[2] for e in events if e[0] == "poll"} == {False}


def test_a_failed_sync_after_a_resume_holds_until_one_works():
    # Wi-Fi reconnects a little after the lid opens
    clock = Suspending({3: 7200})
    events = _watch(clock, syncs=(True, False, False, True))
    i = events.index(("poll", 500, False))
    assert events[i + 1:i + 6] == [
        ("sync", 7950, {}), ("poll", 7950, True),          # the resume sync failed
        ("sync", 7960, {}),                                # so did the announce
        ("sync", 8200, {}), ("poll", 8200, False)]         # this one worked


# --- review round: clock skew -------------------------------------------------------

def test_a_pc_clock_running_ahead_cannot_stretch_the_standby(tmp_path, capsys):
    conn = store.connect(tmp_path / "x.db")
    store.init_db(conn)
    store.insert_run_log(conn, "2026-10-23T15:00:00+00:00", 1, 0, 0, 0, host="local")
    assert run._local_watcher(conn, NOW) is None             # alone: no standby at all
    assert ("[standby] ignoring local poll(s) from the future (2026-10-23T15:00:00+00:00): "
            "PC clock ahead?") in capsys.readouterr().out
    # the correct rows behind it still count, for LOCAL_FRESH_MIN at most
    store.insert_run_log(conn, "2026-10-23T11:50:00+00:00", 1, 0, 0, 0, host="local")
    assert run._local_watcher(conn, NOW) == ("2026-10-23T11:50:00+00:00", 35 * 60)
    store.insert_run_log(conn, "2026-10-23T12:04:00+00:00", 1, 0, 0, 0, host="local")
    last, left = run._local_watcher(conn, NOW)               # 4 min ahead: tolerated
    assert (last, left) == ("2026-10-23T12:04:00+00:00", run.LOCAL_FRESH_MIN * 60)
    assert run._local_watcher(conn, "2026-10-23T12:50:00+00:00") is None   # PC gone


# --- review round: offers the PC named, restocks the standby saw --------------------

SELLERX = "AXSELLERX0001"


def test_an_offer_the_pc_announced_by_name_is_no_hidden_offer_later(env, monkeypatch):
    now = {"ts": "2026-10-23T12:00:00+00:00"}
    monkeypatch.setattr(store, "now_iso", lambda: now["ts"])
    buybox = _offer(promos=[])                                # 3.950: nothing to say
    named = _offer(listing_id=f"{ASIN}:{SELLERX}:excelente", seller="Loja X",
                   list_price=3450.0, pix_discount=0.0, installments_n=None,
                   installment_value=None, promos=[])
    line = _line(n=4, price=3450.0, shipping=0.0)
    env.source = FakeSource([buybox, named], summary=line)
    env.poll(role="local")                                    # the PC's panel names it
    assert [(a.trigger, a.obs.listing_id) for a in env.alerts] == [
        ("instant_buy", named.listing_id)]
    # the PC shuts down; from CI only the buybox and the line are visible, and
    # after UNSEEN_KNOWN_HOURS the stored row no longer vouches for the offer
    env.source = FakeSource([buybox], summary=line)
    for ts in ("2026-10-23T13:00:00+00:00", "2026-10-23T14:04:00+00:00",
               "2026-10-23T16:00:00+00:00"):
        now["ts"] = ts
        r = env.poll()
        assert (r["standby"], r["unseen_alerts"]) == (False, 0), ts
    assert env.unseen == [] and len(env.alerts) == 1
    # a hidden offer R$ 50+ cheaper is still news
    env.source.summary = _line(n=5, price=3390.0, shipping=0.0)
    now["ts"] = "2026-10-23T16:04:00+00:00"
    env.poll()
    assert [a.metric for a in env.unseen] == [3390.0]


def test_a_restock_held_by_the_standby_goes_out_once_it_lapses(env):
    # 11:30: the PC stores listing R sold out (its full panel lost it), then
    # shuts down; from 12:00 the cloud sees R back at 12x R$ 4.100 (🟢 band)
    back = _offer(list_price=4100.0, pix_discount=0.0, installments_n=None,
                  installment_value=None, promos=[])
    conn = store.connect(env.db)
    try:
        store.init_db(conn)
        sold = _offer(list_price=4100.0, in_stock=False, ts="2026-10-23T11:30:00+00:00")
        store.insert_observation(conn, sold)
        store.insert_run_log(conn, "2026-10-23T11:30:00+00:00", 6, 0, 0, 0, host="local")
        conn.commit()
    finally:
        conn.close()
    env.source = FakeSource([back])
    polls = [env.poll() for _ in range(8)]                    # 12:00 ... 12:28
    assert [r["standby"] for r in polls] == [True] * 4 + [False] * 4
    assert [a.trigger for a in env.alerts] == ["restock"]     # once, when it lapsed
    assert [tuple(x) for x in env.rows("SELECT trigger, ts FROM alerts_sent")] == [
        ("restock", "2026-10-23T12:16:00+00:00")]


# --- review round: one watcher loop per clone ---------------------------------------

def test_one_watcher_loop_per_db(monkeypatch, capsys):
    path = run.watcher_lock_path()
    assert path == Path(f"{config.DB_PATH}.lock")
    held = run.acquire_lock(path)
    assert held
    assert run.acquire_lock(path) is None                    # a second taker is refused
    called = []
    monkeypatch.setattr(run, "loop", lambda **kw: called.append(kw) or {"polls": 0})
    assert run.main(["--loop", "--git-sync"]) == 0           # exits quietly...
    assert called == []                                      # ...without polling
    assert f"[loop] another watcher holds {path}: exiting" in capsys.readouterr().out
    run.release_lock(held)
    assert run.main(["--loop", "--git-sync"]) == 0 and len(called) == 1
    again = run.acquire_lock(path)                           # main let go of it
    assert again
    run.release_lock(again)


def test_the_lock_spans_processes_and_dies_with_its_holder(tmp_path):
    path = tmp_path / "prices.db.lock"
    code = ("import time; from pathlib import Path; from src import run; "
            f"lock = run.acquire_lock(Path({str(path)!r})); "
            "print('locked' if lock else 'refused', flush=True); time.sleep(60)")
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT,
                            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "locked"
        assert run.acquire_lock(path) is None
    finally:
        proc.kill()                                          # no release: killed
        proc.wait(timeout=30)
        proc.stdout.close()
    lock = run.acquire_lock(path)                            # the OS dropped it
    assert lock
    run.release_lock(lock)
