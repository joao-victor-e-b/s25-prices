"""run.loop (the watcher) on a fake clock: cadence, backoff, coupon sweeps,
git sync. No network, no real sleeping."""
import signal

import pytest

from src import run


class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def _poller(clock, took=10.0, results=None, raises=None):
    """poll stand-in: takes ``took`` seconds; returns results[i] (last one
    repeats) or raises raises[n] on call n (1-based)."""
    calls = []

    def poll_fn(**kw):
        calls.append({**kw, "t": clock.t})
        clock.t += took
        n = len(calls)
        if raises and n in raises:
            raise raises[n]
        if results:
            return results[min(n, len(results)) - 1]
        return {"amazon_trouble": False}

    return poll_fn, calls


def _loop(clock, poll_fn, jitter=1.0, **kw):
    syncs, cdi_calls, pulls = [], [], []
    kw.setdefault("minutes", 60)
    kw.setdefault("interval", 240)
    kw.setdefault("coupon_every", 1800)

    def cdi():
        cdi_calls.append(1)
        return 0.0091

    r = run.loop(poll_fn=poll_fn, sync_fn=lambda **k: syncs.append(clock.t), cdi_fn=cdi,
                 pull_fn=lambda: pulls.append(clock.t),
                 clock=clock, sleep=clock.sleep, uniform=lambda a, b: jitter, **kw)
    r["pulls"] = pulls
    return r, syncs, cdi_calls


def test_polls_until_the_time_budget():
    clock = Clock()
    poll_fn, calls = _poller(clock)
    r, _, _ = _loop(clock, poll_fn)
    # 250 s cycles (10 s poll + 240 s sleep) inside 3600 s
    assert r["polls"] == len(calls) == 15
    assert clock.t <= 3600                         # stopped before --minutes
    assert clock.t + 240 + 10 > 3600               # ...and not earlier than needed


def test_coupon_sweep_first_poll_then_every_coupon_every():
    clock = Clock()
    poll_fn, calls = _poller(clock)
    _loop(clock, poll_fn)
    flags = [c["scan_coupons"] for c in calls]
    assert [i for i, f in enumerate(flags) if f] == [0, 8]   # t=0 and t=2000 (>=1800)


def test_ddg_off_and_cdi_fetched_once():
    clock = Clock()
    poll_fn, calls = _poller(clock)
    _, _, cdi_calls = _loop(clock, poll_fn)
    assert all(c["scan_promos"] is False and c["cdi"] == 0.0091 for c in calls)
    assert len(cdi_calls) == 1


def test_backoff_doubles_up_to_20_min_and_resets():
    clock = Clock()
    trouble, clean = {"amazon_trouble": True}, {"amazon_trouble": False}
    poll_fn, _ = _poller(clock, results=[trouble] * 4 + [clean])
    _loop(clock, poll_fn, minutes=300)
    assert clock.sleeps[:6] == [480, 960, 1200, 1200, 240, 240]


def test_jitter_and_backoff_cap():
    clock = Clock()
    trouble, clean = {"amazon_trouble": True}, {"amazon_trouble": False}
    poll_fn, _ = _poller(clock, results=[trouble] * 3 + [clean])
    _loop(clock, poll_fn, minutes=300, jitter=1.2)
    assert clock.sleeps[:4] == pytest.approx([576, 1152, 1200, 288])  # never > 20 min


def test_git_sync_every_30_min_and_on_exit():
    clock = Clock()
    poll_fn, _ = _poller(clock)
    r, syncs, _ = _loop(clock, poll_fn, git_sync=True)
    assert syncs == [2010, 3510]                   # first poll past 1800 s, then exit
    assert r["syncs"] == 2


def test_no_git_sync_without_the_flag():
    clock = Clock()
    poll_fn, _ = _poller(clock)
    _, syncs, _ = _loop(clock, poll_fn)
    assert syncs == []


def test_sigterm_still_syncs_on_the_way_out():
    clock = Clock()
    poll_fn, calls = _poller(clock, raises={2: SystemExit(143)})
    with pytest.raises(SystemExit):
        _loop(clock, poll_fn, git_sync=True)
    assert len(calls) == 2


def test_sync_runs_on_exit_after_a_crash(monkeypatch):
    clock = Clock()
    syncs = []
    poll_fn, calls = _poller(clock, raises={n: RuntimeError("db locked") for n in range(1, 9)})
    with pytest.raises(RuntimeError):
        run.loop(poll_fn=poll_fn, sync_fn=lambda **k: syncs.append(clock.t),
                 pull_fn=lambda: None, cdi_fn=lambda: 0.01,
                 clock=clock, sleep=clock.sleep, uniform=lambda a, b: 1.0,
                 minutes=600, git_sync=True)
    assert len(calls) == run.MAX_ERRORS_IN_A_ROW   # gave up after 5 crashes in a row
    # one periodic sync during the backoff sleeps (480+960+1200+1200 s), one on exit
    assert len(syncs) == 2 and syncs[-1] == clock.t


def test_one_crash_backs_off_and_carries_on():
    clock = Clock()
    poll_fn, calls = _poller(clock, raises={1: RuntimeError("boom")})
    _loop(clock, poll_fn)
    assert clock.sleeps[:2] == [480, 240]
    assert len(calls) > 2


def test_sigterm_handler_installed_during_the_loop_and_restored():
    before = signal.getsignal(signal.SIGTERM)
    clock = Clock()
    seen = []

    def poll_fn(**kw):
        seen.append(signal.getsignal(signal.SIGTERM))
        clock.t += 10
        return {"amazon_trouble": False}

    run.loop(poll_fn=poll_fn, sync_fn=lambda **k: None, cdi_fn=lambda: 0.01, clock=clock,
             sleep=clock.sleep, uniform=lambda a, b: 1.0, minutes=5)
    assert seen and seen[0] is not before
    with pytest.raises(SystemExit):
        seen[0](signal.SIGTERM, None)              # SIGTERM -> SystemExit -> finally
    assert signal.getsignal(signal.SIGTERM) == before


def test_cli_loop_flags(monkeypatch):
    got = {}
    monkeypatch.setattr(run, "loop", lambda **kw: got.update(kw) or {"polls": 0})
    assert run.main(["--loop", "--minutes", "2", "--interval", "30",
                     "--coupon-every", "600"]) == 0
    assert got == {"minutes": 2.0, "interval": 30.0, "coupon_every": 600.0,
                   "git_sync": False, "dry_run": False, "enable_new_low": False}
    run.main(["--loop", "--git-sync"])
    assert got["git_sync"] is True and got["minutes"] == 345 and got["interval"] == 240


# --- review round 2 --------------------------------------------------------------

def test_git_sync_pulls_before_the_first_poll():   # C0
    clock = Clock()
    order = []

    def poll_fn(**kw):
        order.append("poll")
        clock.t += 10
        return {"amazon_trouble": False}

    run.loop(poll_fn=poll_fn, sync_fn=lambda **k: order.append("sync"),
             pull_fn=lambda: order.append("pull"), cdi_fn=lambda: 0.01, clock=clock,
             sleep=clock.sleep, uniform=lambda a, b: 1.0, minutes=5, git_sync=True)
    assert order[0] == "pull" and order[1] == "poll" and order[-1] == "sync"
    order.clear()
    run.loop(poll_fn=poll_fn, sync_fn=lambda **k: order.append("sync"),
             pull_fn=lambda: order.append("pull"), cdi_fn=lambda: 0.01, clock=clock,
             sleep=clock.sleep, uniform=lambda a, b: 1.0, minutes=5)
    assert "pull" not in order and "sync" not in order


def test_exit_sync_is_final_and_ignores_signals():   # C1
    clock = Clock()
    seen = []
    before_term, before_int = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)

    def sync_fn(**kw):
        seen.append((kw, signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)))

    poll_fn, _ = _poller(clock, raises={1: KeyboardInterrupt()})   # SIGINT from the runner
    with pytest.raises(KeyboardInterrupt):
        run.loop(poll_fn=poll_fn, sync_fn=sync_fn, pull_fn=lambda: None,
                 cdi_fn=lambda: 0.01, clock=clock, sleep=clock.sleep,
                 uniform=lambda a, b: 1.0, minutes=5, git_sync=True)
    assert seen == [({"final": True}, signal.SIG_IGN, signal.SIG_IGN)]
    assert signal.getsignal(signal.SIGTERM) == before_term
    assert signal.getsignal(signal.SIGINT) == before_int


def test_each_poll_gets_its_iteration_number():
    clock = Clock()
    poll_fn, calls = _poller(clock)
    _loop(clock, poll_fn, minutes=20)
    assert [c["iteration"] for c in calls] == list(range(len(calls)))


# --- pre-deploy round ----------------------------------------------------------

def test_budget_exit_uses_the_regular_capped_sync():   # G1
    clock = Clock()
    calls = []
    poll_fn, _ = _poller(clock)
    run.loop(poll_fn=poll_fn, sync_fn=lambda **kw: calls.append(kw), pull_fn=lambda: None,
             cdi_fn=lambda: 0.01, clock=clock, sleep=clock.sleep,
             uniform=lambda a, b: 1.0, minutes=10, git_sync=True)
    assert calls[-1] == {"budget_s": run.EXIT_SYNC_BUDGET_S}   # not the 1-shot final


def test_periodic_sync_failure_never_stops_the_watcher():   # G2
    clock = Clock()
    calls = []

    def flaky(**kw):
        calls.append(kw)
        if not kw:
            raise RuntimeError("merge blew up")
    poll_fn, polls = _poller(clock)
    r = run.loop(poll_fn=poll_fn, sync_fn=flaky, pull_fn=lambda: None, cdi_fn=lambda: 0.01,
                 clock=clock, sleep=clock.sleep, uniform=lambda a, b: 1.0,
                 minutes=60, git_sync=True)
    assert r["polls"] == len(polls) == 15            # kept polling to the budget
    assert calls[0] == {} and calls[-1] == {"budget_s": run.EXIT_SYNC_BUDGET_S}


def test_heartbeat_exit_code(monkeypatch):   # Y1
    monkeypatch.setattr(run, "heartbeat",
                        lambda: {"wa_ok": False, "ntfy_ok": False, "message": "x"})
    assert run.main(["--heartbeat"]) == 1            # nothing delivered: fail the job
    monkeypatch.setattr(run, "heartbeat",
                        lambda: {"wa_ok": False, "ntfy_ok": True, "message": "x"})
    assert run.main(["--heartbeat"]) == 0
