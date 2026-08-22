import sqlite3

import pytest

from src import store, triggers
from src.models import Observation


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    store.init_db(c)
    return c


def _obs(pv, **kw):
    base = dict(source_id="trocafy_site", seller="Trocafy", venue="own_site",
                listing_id="L1", url="u", grade_rank=4, in_stock=True,
                ts="2026-08-22T12:00:00+00:00", pv=pv)
    base.update(kw)
    return Observation(**base)


def test_thresholds_fire_strongest_only(conn):
    now = "2026-08-22T12:00:00+00:00"
    alerts = triggers.evaluate(conn, [_obs(3400.0)], now)
    trigs = {a.trigger for a in alerts}
    assert trigs == {"instant_buy"}  # not also aggressive/primary


def test_primary_threshold(conn):
    now = "2026-08-22T12:00:00+00:00"
    alerts = triggers.evaluate(conn, [_obs(4200.0)], now)
    assert {a.trigger for a in alerts} == {"primary"}


def test_no_alert_above_primary(conn):
    now = "2026-08-22T12:00:00+00:00"
    assert triggers.evaluate(conn, [_obs(4500.0)], now) == []


def test_rejects_low_grade(conn):
    now = "2026-08-22T12:00:00+00:00"
    assert triggers.evaluate(conn, [_obs(3400.0, grade_rank=2)], now) == []


def test_rejects_out_of_stock(conn):
    now = "2026-08-22T12:00:00+00:00"
    assert triggers.evaluate(conn, [_obs(3400.0, in_stock=False)], now) == []


def test_dedup_suppresses_repeat_within_24h(conn):
    now1 = "2026-08-22T12:00:00+00:00"
    a1 = triggers.evaluate(conn, [_obs(4200.0)], now1)
    assert len(a1) == 1
    for a in a1:
        store.record_alert(conn, a.obs.listing_id, a.trigger, a.obs.pv, now1)

    now2 = "2026-08-22T15:00:00+00:00"  # 3h later, PV barely moved
    a2 = triggers.evaluate(conn, [_obs(4180.0)], now2)
    assert a2 == []


def test_dedup_refires_on_big_move(conn):
    now1 = "2026-08-22T12:00:00+00:00"
    for a in triggers.evaluate(conn, [_obs(4200.0)], now1):
        store.record_alert(conn, a.obs.listing_id, a.trigger, a.obs.pv, now1)

    now2 = "2026-08-22T15:00:00+00:00"  # PV dropped > R$100
    a2 = triggers.evaluate(conn, [_obs(4050.0)], now2)
    assert {a.trigger for a in a2} == {"primary"}


def test_restock_fires_on_flip(conn):
    # prior observation out of stock
    prev = _obs(4200.0, in_stock=False, ts="2026-08-22T11:00:00+00:00")
    store.insert_observation(conn, prev)
    now = "2026-08-22T12:00:00+00:00"
    alerts = triggers.evaluate(conn, [_obs(4200.0)], now)
    assert "restock" in {a.trigger for a in alerts}


def test_venue_spread(conn):
    now = "2026-08-22T12:00:00+00:00"
    a = _obs(4000.0, listing_id="A", venue="own_site")
    b = _obs(4300.0, listing_id="B", venue="magalu")
    alerts = triggers.evaluate(conn, [a, b], now)
    assert "venue_spread" in {al.trigger for al in alerts}


def test_grade_upgrade_needs_prior_history(conn):
    # rank-4 in stock but no prior rank-3 history -> no grade_upgrade (cold start)
    now = "2026-08-22T12:00:00+00:00"
    alerts = triggers.evaluate(conn, [_obs(4477.0, grade_rank=4)], now)
    assert "grade_upgrade" not in {a.trigger for a in alerts}


def test_grade_upgrade_fires_with_prior_history(conn):
    # prior rank-3 (Muito Bom) history at 4699
    prior = _obs(4699.0, listing_id="R3", grade_rank=3,
                 ts="2026-08-21T12:00:00+00:00")
    store.insert_observation(conn, prior)
    now = "2026-08-22T12:00:00+00:00"
    # rank-4 in stock at 4477 <= median rank-3 (4699) -> grade_upgrade
    alerts = triggers.evaluate(conn, [_obs(4477.0, listing_id="R4", grade_rank=4)], now)
    assert "grade_upgrade" in {a.trigger for a in alerts}


def test_trocafy_review(conn):
    now = "2026-08-22T12:00:00+00:00"
    o = _obs(3400.0, grade_rank=2, manual_review=True)
    alerts = triggers.evaluate(conn, [o], now)
    assert "trocafy_review" in {a.trigger for a in alerts}
