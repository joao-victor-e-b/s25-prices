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


NOW = "2026-08-22T12:00:00+00:00"


def _obs(total, **kw):
    """An eligible offer whose 12x card total is ``total`` (list, no coupon)."""
    base = dict(source_id="amazon_seminovos", seller="CellularStore", venue="amazon",
                listing_id="L1", url="u", grade_rank=4, in_stock=True,
                ts=NOW, list_price=total, pv=total)
    base.update(kw)
    return Observation(**base)


def test_thresholds_fire_strongest_only(conn):
    alerts = triggers.evaluate(conn, [_obs(3400.0)], NOW)
    assert {a.trigger for a in alerts} == {"instant_buy"}   # not also aggressive


def test_good_price_threshold_is_inclusive(conn):
    assert {a.trigger for a in triggers.evaluate(conn, [_obs(3800.0)], NOW)} == {"aggressive"}
    assert {a.trigger for a in triggers.evaluate(conn, [_obs(3500.0, listing_id="L2")],
                                                 NOW)} == {"instant_buy"}


def test_no_alert_above_the_2026_low(conn):
    # the old R$4.300 'primary' level is gone; just above 3.800 is only 🟡
    assert [a.trigger for a in triggers.evaluate(conn, [_obs(3800.01)], NOW)] == ["year_low"]
    assert triggers.evaluate(conn, [_obs(3919.01, listing_id="L2")], NOW) == []
    assert triggers.evaluate(conn, [_obs(4200.0, listing_id="L3")], NOW) == []


def test_threshold_uses_12x_total_not_pix_or_pv(conn):
    # Pix R$3.500 and PV below 3.800, but the 12x total is R$3.900 -> only the
    # 🟡 2026-low level, judged on the 12x total
    o = _obs(3900.0, pix_discount=400.0, pv=3480.0)
    (a,) = triggers.evaluate(conn, [o], NOW)
    assert a.trigger == "year_low" and a.metric == 3900.0


def test_confirmed_coupon_lowers_the_12x_total(conn):
    o = _obs(3900.0, coupon=150.0, coupon_code="CUPOMX")
    (a,) = triggers.evaluate(conn, [o], NOW)
    assert a.trigger == "aggressive" and a.metric == 3750.0


def test_rejects_below_excelente(conn):
    # rank 3 (Muito Bom) passed before; alerts now need rank 4 (Excelente/Premium)
    assert triggers.evaluate(conn, [_obs(3400.0, grade_rank=3)], NOW) == []
    assert triggers.evaluate(conn, [_obs(3400.0, grade_rank=2)], NOW) == []
    assert triggers.ALERT_MIN_RANK == 4


def test_rejects_out_of_stock(conn):
    assert triggers.evaluate(conn, [_obs(3400.0, in_stock=False)], NOW) == []


def _fire_and_record(conn, obs, now):
    alerts = triggers.evaluate(conn, [obs], now)
    for a in alerts:
        store.record_alert(conn, a.obs.listing_id, a.trigger, a.metric, now)
    return alerts


def test_alert_records_the_12x_total(conn):
    _fire_and_record(conn, _obs(3750.0, pix_discount=187.5, pv=3562.5), NOW)
    row = conn.execute("SELECT trigger, pv FROM alerts_sent").fetchone()
    assert (row["trigger"], row["pv"]) == ("aggressive", 3750.0)   # not the PV


def test_dedup_suppresses_small_drop(conn):
    assert len(_fire_and_record(conn, _obs(3750.0), NOW)) == 1
    # 12x total down R$30 (< R$50) -> suppressed
    assert triggers.evaluate(conn, [_obs(3720.0)], "2026-08-22T15:00:00+00:00") == []


def test_dedup_refires_on_50_drop(conn):
    _fire_and_record(conn, _obs(3750.0), NOW)
    a2 = triggers.evaluate(conn, [_obs(3700.0)], "2026-08-22T15:00:00+00:00")
    assert {a.trigger for a in a2} == {"aggressive"}
    assert triggers.DEDUP_DELTA_12X == 50


def test_dedup_suppresses_price_increase(conn):
    _fire_and_record(conn, _obs(3750.0), NOW)
    assert triggers.evaluate(conn, [_obs(3790.0)], "2026-08-23T12:00:00+00:00") == []


def test_dedup_no_refire_same_price_after_long_time(conn):
    _fire_and_record(conn, _obs(3750.0), NOW)
    assert triggers.evaluate(conn, [_obs(3750.0)], "2026-08-29T12:00:00+00:00") == []


def test_crossing_into_buy_now_fires(conn):
    _fire_and_record(conn, _obs(3750.0), NOW)
    a2 = triggers.evaluate(conn, [_obs(3490.0)], "2026-08-22T15:00:00+00:00")
    assert {a.trigger for a in a2} == {"instant_buy"}


def _own(total, **kw):
    """A legacy multi-store offer (the grade_upgrade/venue_spread/trocafy rules)."""
    return _obs(total, **{"source_id": "trocafy_site", "seller": "Trocafy",
                          "venue": "own_site", **kw})


def test_non_threshold_dedup_still_uses_pv_and_100(conn):
    prior = _own(4699.0, listing_id="R3", grade_rank=3, ts="2026-08-21T12:00:00+00:00")
    store.insert_observation(conn, prior)
    (a,) = [x for x in triggers.evaluate(conn, [_own(4477.0, listing_id="R4")], NOW)
            if x.trigger == "grade_upgrade"]
    assert a.metric == 4477.0                                  # PV, not 12x
    store.record_alert(conn, "R4", a.trigger, a.metric, NOW)
    later = "2026-08-22T14:00:00+00:00"
    assert not triggers.evaluate(conn, [_own(4430.0, listing_id="R4", ts=later)], later)
    again = triggers.evaluate(conn, [_own(4370.0, listing_id="R4", ts=later)], later)
    assert {x.trigger for x in again} == {"grade_upgrade"}     # dropped >= R$100


def test_listing_that_sells_out_and_returns_alerts_every_time(conn):
    """A COMPRE-priced unit that keeps coming back must alert on each return
    (it used to go silent from the second restock on)."""
    sent = []

    def appear(ts):
        o = _obs(3450.0, ts=ts)
        alerts = triggers.evaluate(conn, [o], ts)
        store.insert_observation(conn, o)
        for x in alerts:
            store.record_alert(conn, x.obs.listing_id, x.trigger, x.metric, ts)
        sent.append({x.trigger for x in alerts})

    def sell_out(ts):
        store.mark_missing_out_of_stock(conn, "amazon_seminovos", set(), ts)

    appear("2026-08-22T12:00:00+00:00")
    sell_out("2026-08-22T12:04:00+00:00")
    appear("2026-08-22T12:08:00+00:00")
    sell_out("2026-08-22T12:12:00+00:00")
    appear("2026-08-22T12:16:00+00:00")
    appear("2026-08-22T12:20:00+00:00")           # still there: deduped
    # each return is one COMPRE noted "voltou ao estoque" (restock folded in)
    assert sent == [{"instant_buy"}, {"instant_buy"}, {"instant_buy"}, set()]


def test_restock_folds_into_a_threshold_alert_and_needs_a_near_price(conn):
    store.insert_observation(conn, _obs(3450.0, in_stock=False,
                                        ts="2026-08-22T11:00:00+00:00"))
    (a,) = triggers.evaluate(conn, [_obs(3450.0)], NOW)
    assert a.trigger == "instant_buy" and a.note == "voltou ao estoque"
    # a far-off price coming back is not worth a ping (new-unit sellers at R$ 6-10k)
    store.insert_observation(conn, _obs(6043.49, listing_id="NEW", in_stock=False,
                                        ts="2026-08-22T11:00:00+00:00"))
    assert triggers.evaluate(conn, [_obs(6043.49, listing_id="NEW")], NOW) == []
    store.insert_observation(conn, _obs(4300.0, listing_id="NEAR", in_stock=False,
                                        ts="2026-08-22T11:00:00+00:00"))
    (r,) = triggers.evaluate(conn, [_obs(4300.0, listing_id="NEAR")], NOW)
    assert r.trigger == "restock"


def test_restock_fires_on_flip(conn):
    prev = _obs(4200.0, in_stock=False, ts="2026-08-22T11:00:00+00:00")
    store.insert_observation(conn, prev)
    alerts = triggers.evaluate(conn, [_obs(4200.0)], NOW)
    assert "restock" in {a.trigger for a in alerts}


def test_venue_spread_across_venues(conn):
    a = _obs(4000.0, listing_id="A", venue="own_site")
    b = _obs(4300.0, listing_id="B", venue="magalu")
    alerts = triggers.evaluate(conn, [a, b], NOW)
    assert "venue_spread" in {al.trigger for al in alerts}


def test_no_venue_spread_within_one_venue(conn):
    # one seller's 256GB and 512GB Amazon listings are not an arbitrage
    a = _obs(4582.47, listing_id="B0FTMQQ7L2:S:excelente", storage_gb=256)
    b = _obs(5299.0, listing_id="B0FTMWWS8Y:S:excelente", storage_gb=512)
    alerts = triggers.evaluate(conn, [a, b], NOW)
    assert "venue_spread" not in {al.trigger for al in alerts}


def test_grade_upgrade_needs_prior_history(conn):
    alerts = triggers.evaluate(conn, [_obs(4477.0, grade_rank=4)], NOW)
    assert "grade_upgrade" not in {a.trigger for a in alerts}


def test_grade_upgrade_fires_with_prior_history(conn):
    prior = _own(4699.0, listing_id="R3", grade_rank=3, ts="2026-08-21T12:00:00+00:00")
    store.insert_observation(conn, prior)
    alerts = triggers.evaluate(conn, [_own(4477.0, listing_id="R4", grade_rank=4)], NOW)
    assert "grade_upgrade" in {a.trigger for a in alerts}


def test_multi_store_triggers_never_fire_for_amazon(conn):
    # months of a disabled store's grade-3 history must not drive Amazon alerts
    store.insert_observation(conn, _obs(4699.0, seller="Trocafone", listing_id="OLD",
                                        grade_rank=3, venue="own_site",
                                        source_id="trocafone_site",
                                        ts="2026-08-21T12:00:00+00:00"))
    amz = _obs(4500.0, seller="Trocafone", listing_id="A", pv=4400.0)
    assert triggers.evaluate(conn, [amz], NOW, enable_new_low=True) == []
    # a grade-2 'Bom' Amazon offer from seller Trocafy is not a manual-review case
    bom = _obs(3400.0, seller="Trocafy", listing_id="B", grade_rank=2, manual_review=True)
    assert triggers.evaluate(conn, [bom], NOW) == []
    # one seller on two Amazon listings is no venue spread
    a = _obs(4000.0, listing_id="X1")
    b = _obs(4400.0, listing_id="X2")
    assert "venue_spread" not in {x.trigger for x in triggers.evaluate(conn, [a, b], NOW)}


def _send(conn, total, ts, **kw):
    alerts = triggers.evaluate(conn, [_obs(total, ts=ts, **kw)], ts)
    for x in alerts:
        store.record_alert(conn, x.obs.listing_id, x.trigger, x.metric, ts)
    return [x.trigger for x in alerts]


def test_threshold_ladder_never_alerts_on_a_rise(conn):
    assert _send(conn, 3499.0, "2026-08-22T12:00:00+00:00") == ["instant_buy"]
    assert _send(conn, 3700.0, "2026-08-22T12:04:00+00:00") == []    # 🔴 -> 🟠 is a rise
    assert _send(conn, 3850.0, "2026-08-22T12:08:00+00:00") == []    # -> 🟡 too
    assert _send(conn, 3460.0, "2026-08-22T12:12:00+00:00") == []    # only -39 vs 3.499
    assert _send(conn, 3449.0, "2026-08-22T12:16:00+00:00") == ["instant_buy"]


def test_threshold_ladder_climbs_into_stronger_levels(conn):
    assert _send(conn, 3900.0, "2026-08-22T12:00:00+00:00") == ["year_low"]
    assert _send(conn, 3790.0, "2026-08-22T12:04:00+00:00") == ["aggressive"]
    assert _send(conn, 3770.0, "2026-08-22T12:08:00+00:00") == []      # -20
    assert _send(conn, 3880.0, "2026-08-22T12:12:00+00:00") == []      # rise to 🟡
    assert _send(conn, 3500.0, "2026-08-22T12:16:00+00:00") == ["instant_buy"]


def test_threshold_ladder_restarts_after_a_sell_out(conn):
    assert _send(conn, 3499.0, "2026-08-22T12:00:00+00:00") == ["instant_buy"]
    store.insert_observation(conn, _obs(3499.0, in_stock=False,
                                        ts="2026-08-22T12:04:00+00:00"))
    assert _send(conn, 3700.0, "2026-08-22T12:08:00+00:00") == ["aggressive"]


def test_trocafy_review(conn):
    o = _obs(3400.0, source_id="trocafy_site", seller="Trocafy", venue="own_site",
             grade_rank=2, manual_review=True)
    alerts = triggers.evaluate(conn, [o], NOW)
    assert "trocafy_review" in {a.trigger for a in alerts}


def test_year_low_level_between_good_and_2026_low(conn):
    # 12x total R$ 3.850: above BOM PREÇO (3.800) but beats the 2026 low (3.919)
    now = "2026-10-23T12:00:00+00:00"
    o = _obs(3850.0)
    alerts = triggers.evaluate(conn, [o], now)
    assert [a.trigger for a in alerts] == ["year_low"]
    assert alerts[0].priority == "🟡"
    assert triggers.evaluate(conn, [_obs(3950.0, listing_id="L2")], now) == []
