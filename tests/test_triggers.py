import sqlite3
from datetime import timedelta

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


def test_restock_stored_by_a_poll_that_could_not_announce_it(conn):
    # sold out at 11:00; at 11:30 a poll that sends nothing (the cloud on
    # standby, or the other watcher's row merged in) stored it back in stock
    store.insert_observation(conn, _obs(4200.0, in_stock=False, ts="2026-08-22T11:00:00+00:00"))
    store.insert_observation(conn, _obs(4200.0, ts="2026-08-22T11:30:00+00:00"))
    (a,) = triggers.evaluate(conn, [_obs(4200.0)], NOW)        # the edge is long gone
    assert (a.trigger, a.note) == ("restock", "voltou ao estoque")
    assert triggers.evaluate(conn, [_obs(4200.0)], NOW)        # undelivered: retried
    store.record_alert(conn, "L1", a.trigger, a.metric, NOW)
    later = "2026-08-22T12:04:00+00:00"
    assert triggers.evaluate(conn, [_obs(4200.0, ts=later)], later) == []


def test_restock_only_within_the_window_of_the_return(conn):
    # back since 09:30 at 4.500 (no ping: above 4.300), drifting to 4.200 at
    # noon is no "voltou ao estoque"
    store.insert_observation(conn, _obs(4500.0, in_stock=False, ts="2026-08-22T09:00:00+00:00"))
    store.insert_observation(conn, _obs(4500.0, ts="2026-08-22T09:30:00+00:00"))
    assert triggers.evaluate(conn, [_obs(4200.0)], NOW) == []
    assert triggers.RESTOCK_WINDOW >= timedelta(minutes=45 + 30)   # standby + a sync
    store.insert_observation(conn, _obs(4500.0, listing_id="L2", in_stock=False,
                                        ts="2026-08-22T10:00:00+00:00"))
    store.insert_observation(conn, _obs(4500.0, listing_id="L2", ts="2026-08-22T10:01:00+00:00"))
    assert [a.trigger for a in triggers.evaluate(conn, [_obs(4200.0, listing_id="L2")],
                                                 NOW)] == ["restock"]


def test_a_threshold_alert_noted_as_restock_counts_as_the_restock(conn):
    store.insert_observation(conn, _obs(3450.0, in_stock=False, ts="2026-08-22T11:00:00+00:00"))
    (a,) = triggers.evaluate(conn, [_obs(3450.0)], NOW)
    assert (a.trigger, a.note) == ("instant_buy", "voltou ao estoque")
    store.record_alert(conn, "L1", a.trigger, a.metric, NOW)
    later = "2026-08-22T12:30:00+00:00"
    # a rise into the 🟢 band doesn't say "voltou ao estoque" a second time
    assert triggers.evaluate(conn, [_obs(4200.0, ts=later)], later) == []


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


# --- 🔍 unseen offer: the dp's offers line undercuts every attributed offer ---------

from src.models import OfferSummary   # noqa: E402

ASIN = "B0FTMWWS8Y"


def _line(price, shipping=None, asin=ASIN, n=4, ts=NOW):
    return OfferSummary(asin=asin, offers_total=n, min_card_price=price,
                        min_shipping=shipping, scope="ALL", ts=ts, storage_gb=512,
                        color="preto")


def _named(price, seller="A3SPPFNXUZXE4P", grade="excelente", **kw):
    """An offer we could attribute to the ASIN this poll."""
    return _obs(price, listing_id=f"{ASIN}:{seller}:{grade}", **kw)


def test_unseen_fires_below_every_attributed_offer(conn):
    (a,) = triggers.unseen_offers(conn, [_line(3700.0, 13.22)],
                                  [_named(4275.78), _named(4149.0, grade="aceitavel")], NOW)
    assert (a.trigger, a.priority, a.level) == ("unseen_offer", "🔍", "aggressive")
    assert a.metric == 3713.22                      # card + shipping
    assert (a.attributed, a.attributed_min) == (2, 4149.0)
    assert a.key == f"unseen:{ASIN}"


def test_unseen_needs_the_2026_low_and_counts_shipping(conn):
    assert triggers.unseen_offers(conn, [_line(3919.0)], [], NOW)[0].level == "year_low"
    assert triggers.unseen_offers(conn, [_line(3910.0, 13.22)], [], NOW) == []  # 3923.22
    assert triggers.unseen_offers(conn, [_line(4582.47, 13.22)], [], NOW) == []
    assert triggers.unseen_offers(conn, [_line(None)], [], NOW) == []          # no price
    assert triggers.unseen_offers(conn, [_line(3500.0)], [], NOW)[0].level == "instant_buy"


def test_unseen_is_quiet_when_the_minimum_is_an_offer_we_see(conn):
    # 2026-10-22: "3 ofertas a partir de R$4.582,47" was the buybox itself
    assert triggers.unseen_offers(conn, [_line(3850.0, 13.22)], [_named(3850.0)], NOW) == []
    assert triggers.unseen_offers(conn, [_line(3850.0)], [_named(3850.0)], NOW) == []
    # a card price rebuilt from the plan sits cents above Amazon's: same offer
    assert triggers.unseen_offers(conn, [_line(3850.0)], [_named(3850.33)], NOW) == []
    # a known Bom or sold-out offer at the minimum is known too (no 🔍 for it)
    assert triggers.unseen_offers(conn, [_line(3700.0)],
                                  [_named(3700.0, grade="bom", grade_rank=2)], NOW) == []
    assert triggers.unseen_offers(conn, [_line(3700.0)],
                                  [_named(3700.0, in_stock=False)], NOW) == []
    # other ASINs' offers don't count as attributed here
    other = _obs(3600.0, listing_id="B0FTMQQ7L2:X:excelente")
    assert len(triggers.unseen_offers(conn, [_line(3700.0)], [other], NOW)) == 1


def test_unseen_fires_with_nothing_attributed(conn):
    (a,) = triggers.unseen_offers(conn, [_line(3800.0)], [], NOW)   # no buybox, panel 503
    assert (a.level, a.attributed, a.attributed_min) == ("aggressive", 0, None)


def _unseen_send(conn, price, ts, shipping=None, attributed=(), n=4):
    alerts = triggers.unseen_offers(conn, [_line(price, shipping, n=n, ts=ts)],
                                    list(attributed), ts)
    store.insert_offer_summary(conn, _line(price, shipping, n=n, ts=ts))
    for a in alerts:
        store.record_alert(conn, a.key, a.trigger, a.metric, ts)
    return [a.level for a in alerts]


def _no_line(ts, price=4582.47, asin=ASIN):
    """The marker a product page without the other-offers line leaves."""
    return OfferSummary(asin=asin, offers_total=1, min_card_price=price,
                        scope=OfferSummary.NO_LINE, ts=ts, storage_gb=512, color="preto")


def test_unseen_ladder_per_asin(conn):
    t = lambda m: f"2026-08-22T12:{m:02d}:00+00:00"   # noqa: E731
    assert _unseen_send(conn, 3850.0, t(0)) == ["year_low"]
    assert _unseen_send(conn, 3850.0, t(4)) == []                   # same price
    assert _unseen_send(conn, 3900.0, t(8)) == []                   # a rise never alerts
    assert _unseen_send(conn, 3810.0, t(12)) == []                  # -40 only
    assert _unseen_send(conn, 3795.0, t(16)) == ["aggressive"]      # stronger level
    assert _unseen_send(conn, 3760.0, t(20)) == []                  # -35
    assert _unseen_send(conn, 3745.0, t(24)) == ["aggressive"]      # -50 vs 3.795
    assert _unseen_send(conn, 3880.0, t(28)) == []                  # rise to 🟡
    # another ASIN has its own ladder
    (a,) = triggers.unseen_offers(conn, [_line(3850.0, asin="B0FTMQQ7L2", ts=t(32))], [], t(32))
    assert a.key == "unseen:B0FTMQQ7L2"


def test_unseen_ladder_restarts_once_the_hidden_offer_is_gone(conn):
    t = lambda m: f"2026-08-22T12:{m:02d}:00+00:00"   # noqa: E731
    assert _unseen_send(conn, 3850.0, t(0)) == ["year_low"]
    assert _unseen_send(conn, 4582.47, t(4), shipping=13.22, n=3) == []   # seller left
    assert _unseen_send(conn, 3850.0, t(8)) == ["year_low"]         # a new one: news
    assert _unseen_send(conn, 3850.0, t(12)) == []


def test_unseen_price_bouncing_across_the_ceiling_alerts_once(conn):
    # one hidden offer repriced back and forth: every change is stored, and a
    # line above R$ 3.919 used to reset the ladder each time
    t = lambda m: f"2026-08-22T12:{m:02d}:00+00:00"   # noqa: E731
    sent = [_unseen_send(conn, p, t(4 * i), shipping=0.0)
            for i, p in enumerate((3915.0, 3925.0, 3915.0, 3925.0, 3915.0))]
    assert sent == [["year_low"], [], [], [], []]
    # shipping pushing the same offer over the ceiling is no departure either
    assert _unseen_send(conn, 3910.0, t(20), shipping=13.22) == []      # 3923.22
    assert _unseen_send(conn, 3915.0, t(24), shipping=0.0) == []


def test_unseen_ladder_restarts_when_the_line_disappears(conn):
    # S1-S5: the hidden offer sold, the page showed only the buybox (no line),
    # then a different seller listed just R$ 10 lower — that is news
    t = lambda m: f"2026-08-22T12:{m:02d}:00+00:00"   # noqa: E731
    assert _unseen_send(conn, 4149.0, t(0), n=2) == []
    assert _unseen_send(conn, 3890.0, t(4), n=2) == ["year_low"]
    for m in (8, 12):
        marker = _no_line(t(m))
        assert triggers.unseen_offers(conn, [marker], [], t(m)) == []   # never fires
        store.insert_offer_summary(conn, marker)
    assert _unseen_send(conn, 3880.0, t(16), n=2) == ["year_low"]
    assert _unseen_send(conn, 3880.0, t(20), n=2) == []


def test_unseen_same_price_after_the_line_disappeared_alerts_again(conn):
    t = lambda m: f"2026-08-22T12:{m:02d}:00+00:00"   # noqa: E731
    assert _unseen_send(conn, 3700.0, t(0)) == ["aggressive"]
    store.insert_offer_summary(conn, _no_line(t(4)))
    assert _unseen_send(conn, 3700.0, t(8), n=2) == ["aggressive"]   # it came back


def _t(m):
    return f"2026-08-22T{12 + m // 60:02d}:{m % 60:02d}:00+00:00"


def test_unseen_counts_a_recently_stored_offer_as_known(conn):
    # Q1: the panel named an Aceitável unit at 3.890; Q2, four minutes later,
    # the panel 503s and only the buybox is named — the line is unchanged
    store.insert_observation(conn, _named(3890.0, seller="ASSURANT", grade="aceitavel",
                                          grade_rank=1, ts=_t(0)))
    buybox = _named(4275.78, ts=_t(4))
    assert triggers.unseen_offers(conn, [_line(3890.0, ts=_t(4))], [buybox], _t(4)) == []
    # stored too long ago: no longer proof of anything
    (a,) = triggers.unseen_offers(conn, [_line(3890.0, ts=_t(125))], [buybox], _t(125))
    assert (a.metric, a.attributed, a.attributed_min) == (3890.0, 1, 4275.78)
    # stored as sold out since: it can't be the offer behind the line
    store.insert_observation(conn, _named(3890.0, seller="ASSURANT", grade="aceitavel",
                                          grade_rank=1, in_stock=False, ts=_t(8)))
    (a,) = triggers.unseen_offers(conn, [_line(3890.0, ts=_t(12))], [buybox], _t(12))
    assert a.metric == 3890.0


def test_unseen_counts_an_offer_rotated_out_of_the_buybox_as_known(conn):
    store.insert_observation(conn, _named(3900.0, seller="ASSURANT", ts=_t(0)))
    cell = _named(4582.47, seller="CELL", ts=_t(4))
    assert triggers.unseen_offers(conn, [_line(3900.0, ts=_t(4))], [cell], _t(4)) == []
    # a cheaper one behind it is still hidden: the known 3.900 doesn't mask it
    (a,) = triggers.unseen_offers(conn, [_line(3840.0, ts=_t(8))], [cell], _t(8))
    assert (a.level, a.attributed, a.attributed_min) == ("year_low", 2, 3900.0)


def test_unseen_counts_offers_announced_by_name(conn):
    # the PC's panel named Loja X at 3.450 and sent 🔴; three hours later the
    # cloud has only the line (the stored row is too old to vouch for it)
    store.insert_observation(conn, _named(3450.0, seller="LOJAX", ts=_t(0)))
    store.record_alert(conn, f"{ASIN}:LOJAX:excelente", "instant_buy", 3450.0, _t(0))
    assert triggers.unseen_offers(conn, [_line(3450.0, ts=_t(180))], [], _t(180)) == []
    # the same offer with its shipping, or cents off the rebuilt plan
    assert triggers.unseen_offers(conn, [_line(3449.70, 13.22, ts=_t(180))], [], _t(180)) == []
    (a,) = triggers.unseen_offers(conn, [_line(3399.0, ts=_t(180))], [], _t(180))
    assert a.metric == 3399.0                                # R$ 51 cheaper: news


def test_unseen_named_rungs_follow_the_ladder(conn):
    store.record_alert(conn, f"{ASIN}:LOJAX:excelente", "year_low", 3900.0, _t(0))
    assert triggers.unseen_offers(conn, [_line(3900.0, ts=_t(180))], [], _t(180)) == []
    (a,) = triggers.unseen_offers(conn, [_line(3790.0, ts=_t(180))], [], _t(180))
    assert a.level == "aggressive"                           # a stronger level fires
    # another ASIN's named alerts are no rungs here
    store.record_alert(conn, "B0FTMQQ7L2:LOJAY:excelente", "instant_buy", 3400.0, _t(0))
    assert len(triggers.unseen_offers(conn, [_line(3790.0, ts=_t(180))], [], _t(180))) == 1


def test_unseen_named_rung_ends_with_a_sell_out_or_a_departure(conn):
    store.record_alert(conn, f"{ASIN}:LOJAX:excelente", "year_low", 3900.0, _t(0))
    # the PC's complete panel stored Loja X sold out since: the line's 3.900
    # is someone else
    store.insert_observation(conn, _named(3900.0, seller="LOJAX", in_stock=False, ts=_t(60)))
    assert len(triggers.unseen_offers(conn, [_line(3900.0, ts=_t(180))], [], _t(180))) == 1
    conn.execute("DELETE FROM observations")
    assert triggers.unseen_offers(conn, [_line(3900.0, ts=_t(180))], [], _t(180)) == []
    # a reading since showed the offer leave (the line vanished): fresh start
    store.insert_offer_summary(conn, _no_line(_t(90)))
    assert len(triggers.unseen_offers(conn, [_line(3900.0, ts=_t(180))], [], _t(180))) == 1


def test_unseen_ladder_only_counts_delivered_alerts(conn):
    alerts = triggers.unseen_offers(conn, [_line(3850.0)], [], NOW)
    assert alerts                                    # nothing recorded (outage) ...
    assert triggers.unseen_offers(conn, [_line(3850.0)], [], NOW)   # ... so it retries


def test_threshold_level():
    assert [triggers.threshold_level(v) for v in (3500.0, 3500.01, 3800.0, 3919.0,
                                                  3919.01, None)] == [
        "instant_buy", "aggressive", "aggressive", "year_low", None, None]
