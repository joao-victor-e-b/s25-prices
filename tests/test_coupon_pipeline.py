"""Orchestrator wiring for the Amazon coupon/cashback tracker (run.py + store +
notify). Network is stubbed; src/coupons.py parsing has its own tests."""
import re
import sqlite3

import pytest

from src import coupons, notify, run, store, triggers
from src.coupons import CashbackStatus, CouponCode
from src.models import Observation

NOW = "2026-10-22T12:00:00+00:00"
LATER = "2026-10-23T12:00:00+00:00"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    store.init_db(c)
    return c


def _codes():
    return [
        CouponCode(source="cuponomia", code="OIPRIME15", title="15% OFF acima de R$800",
                   pct=15.0, cap=200.0, min_order=800.0, flags=["prime"]),
        CouponCode(source="promobit,meliuz", code="CUPOMPRIME10", title="10% OFF",
                   pct=10.0, cap=20.0, min_order=100.0, flags=["prime"]),
        CouponCode(source="cuponomia", code="CUPOMSEMPRE", title="R$150 OFF",
                   abs_off=150.0, min_order=1499.0),
    ]


def _amazon_obs(card=3919.0, pix_discount=195.95):
    return Observation(source_id="amazon_seminovos", seller="Assurant Ecophone",
                       venue="amazon", listing_id="B0FTMQQ7L2:A3SPPFNXUZXE4P:excelente",
                       url="u", grade_rank=4, in_stock=True, list_price=card,
                       pix_discount=pix_discount, ts=NOW, pv=card - pix_discount)


DELIVERY = {"ok": True}


@pytest.fixture
def stub_scan(monkeypatch):
    sent = []
    DELIVERY["ok"] = True
    monkeypatch.setattr(coupons, "scan_codes", lambda session=None: (_codes(), []))
    monkeypatch.setattr(coupons, "scan_cashback", lambda session=None: (
        [CashbackStatus(portal="inter", rate_pct=0.0, active=False, url="i")], []))
    monkeypatch.setattr(notify, "notify_coupons",
                        lambda blocks: sent.extend(blocks) or
                        {"sent": len(blocks), "wa_ok": False, "ntfy_ok": DELIVERY["ok"]})
    return sent


def _cashback(monkeypatch, rate, active=True):
    monkeypatch.setattr(coupons, "scan_codes", lambda session=None: ([], []))
    monkeypatch.setattr(coupons, "scan_cashback", lambda session=None: (
        [CashbackStatus(portal="inter", rate_pct=rate, active=active, url="i",
                        detail="Até 5% p/ correntista")], []))


def test_upsert_coupon_keeps_first_seen_and_merges_sources(conn):
    c = _codes()[1]
    assert store.upsert_coupon(conn, c, NOW) is True
    c2 = CouponCode(source="pelando", code="cupomprime10", pct=10.0, cap=20.0)
    assert store.upsert_coupon(conn, c2, LATER) is False
    row = conn.execute("SELECT * FROM coupon_codes").fetchone()
    assert row["first_seen"] == NOW and row["last_seen"] == LATER
    assert row["sources"] == "meliuz,pelando,promobit"   # no growth on re-scan
    store.upsert_coupon(conn, c2, LATER)
    assert conn.execute("SELECT sources FROM coupon_codes").fetchone()[0] == \
        "meliuz,pelando,promobit"


def test_coupon_row_round_trips(conn):
    store.upsert_coupon(conn, _codes()[0], NOW)
    back = CouponCode.from_row(conn.execute("SELECT * FROM coupon_codes").fetchone())
    assert back.key == "OIPRIME15" and back.value_at(3919.0) == 200.0
    assert back.flags == ["prime"]


def test_scan_pings_valuable_new_codes_once(conn, stub_scan):
    run._scan_coupons(conn, NOW, dry_run=False, observations=[_amazon_obs()])
    # R$200 and R$150 clear COUPON_MIN_VALUE; the R$20-capped code doesn't
    assert len(stub_scan) == 2
    assert "OIPRIME15" in stub_scan[0] and "CUPOMSEMPRE" in stub_scan[1]
    stub_scan.clear()
    run._scan_coupons(conn, LATER, dry_run=False, observations=[_amazon_obs()])
    assert stub_scan == []                       # already pinged
    assert conn.execute("SELECT COUNT(*) FROM coupon_codes").fetchone()[0] == 3


def test_scan_caps_pings_per_run(conn, stub_scan, monkeypatch):
    many = [CouponCode(source="cuponomia", code=f"CODE{i}", abs_off=100.0 + i)
            for i in range(8)]
    monkeypatch.setattr(coupons, "scan_codes", lambda session=None: (many, []))
    run._scan_coupons(conn, NOW, dry_run=False, observations=[])
    assert len(stub_scan) == run.COUPON_CAP
    assert "CODE7" in stub_scan[0]               # most valuable first


def test_dry_run_sends_and_records_nothing(conn, stub_scan):
    run._scan_coupons(conn, NOW, dry_run=True, observations=[_amazon_obs()])
    assert stub_scan == []
    assert conn.execute("SELECT COUNT(*) FROM alerts_sent").fetchone()[0] == 0


def test_cashback_pings_when_it_turns_on(conn, stub_scan, monkeypatch):
    run._scan_coupons(conn, NOW, dry_run=False, observations=[])
    stub_scan.clear()
    _cashback(monkeypatch, 5.0)
    run._scan_coupons(conn, LATER, dry_run=False, observations=[])
    assert len(stub_scan) == 1 and "inter 5%" in stub_scan[0]
    stub_scan.clear()
    run._scan_coupons(conn, "2026-10-24T12:00:00+00:00", dry_run=False, observations=[])
    assert stub_scan == []                       # still on at the same rate


def test_cashback_turn_on_survives_a_dry_run(conn, stub_scan, monkeypatch):
    _cashback(monkeypatch, 5.0)
    run._scan_coupons(conn, NOW, dry_run=True, observations=[])
    assert conn.execute("SELECT COUNT(*) FROM cashback_status").fetchone()[0] == 0
    run._scan_coupons(conn, LATER, dry_run=False, observations=[])
    assert len(stub_scan) == 1                   # the dry run didn't consume it


def test_undelivered_alerts_are_retried(conn, stub_scan, monkeypatch):
    DELIVERY["ok"] = False                       # both channels down
    _cashback(monkeypatch, 5.0)
    run._scan_coupons(conn, NOW, dry_run=False, observations=[])
    assert len(stub_scan) == 1
    DELIVERY["ok"] = True
    run._scan_coupons(conn, LATER, dry_run=False, observations=[])
    assert len(stub_scan) == 2                   # sent again once a channel works


def test_cashback_repings_after_turning_off_and_on(conn, stub_scan, monkeypatch):
    _cashback(monkeypatch, 5.0)
    run._scan_coupons(conn, NOW, dry_run=False, observations=[])
    _cashback(monkeypatch, 0.0, active=False)
    run._scan_coupons(conn, LATER, dry_run=False, observations=[])
    _cashback(monkeypatch, 5.0)
    run._scan_coupons(conn, "2026-10-24T12:00:00+00:00", dry_run=False, observations=[])
    assert len(stub_scan) == 2


def test_scan_survives_coupon_module_crash(conn, monkeypatch):
    def boom(session=None):
        raise RuntimeError("x")
    monkeypatch.setattr(coupons, "scan_codes", boom)
    run._scan_coupons(conn, NOW, dry_run=False, observations=[])  # no raise


def test_amazon_summary_uses_pix_vs_code(conn):
    o = _amazon_obs()
    store.insert_observation(conn, o)
    for c in _codes():
        store.upsert_coupon(conn, c, NOW)
    line = run._amazon_summary(conn, LATER)
    assert "Pix R$ 3.723,05" in line and "cartão R$ 3.919,00" in line
    assert "OIPRIME15: R$ 3.719,00" in line      # R$200 code beats Pix by R$4.05


def test_amazon_summary_hides_code_that_loses_to_pix(conn):
    store.insert_observation(conn, _amazon_obs())
    store.upsert_coupon(conn, _codes()[2], NOW)  # R$150 < R$195.95 Pix
    assert "cupom" not in run._amazon_summary(conn, LATER)


def test_coupon_messages_have_no_dollar_digit():
    c = CouponCode(source="pelando", code="X", title="Cupom R$150 OFF", abs_off=150.0,
                   flags=["prime", "app"], url="https://e.x")
    msg = notify.format_coupon(c, 150.0, 3919.0)
    assert not re.search(r"\$\d", msg)
    assert "exige prime, app" in msg
    cb = notify.format_cashback(CashbackStatus(portal="inter", rate_pct=3.5,
                                               active=True, url="u", detail="R$10 mín"))
    assert not re.search(r"\$\d", cb) and "inter 3.5%" in cb


# --- sold-out marking (Amazon AOD drops sold-out sellers) ----------------------

def test_vanished_offer_is_marked_sold_out_and_restock_fires(conn):
    a = _amazon_obs()
    store.insert_observation(conn, a)
    other = _amazon_obs(card=4582.47, pix_discount=229.13)
    other.listing_id, other.ts = "B0FTMQQ7L2:A2TO0KKQTM1BAB:excelente", LATER
    store.insert_observation(conn, other)
    gone = store.mark_missing_out_of_stock(conn, "amazon_seminovos",
                                           {other.listing_id}, LATER)
    assert gone == [a.listing_id]
    # the heartbeat no longer counts the vanished offer as current
    assert store.best_current_pv(conn)["listing_id"] == other.listing_id
    # ...and when it comes back, restock fires
    back = _amazon_obs()
    back.ts = "2026-10-24T12:00:00+00:00"
    alerts = triggers.evaluate(conn, [back], back.ts)
    assert "restock" in {x.trigger for x in alerts}


def test_sold_out_marking_ignores_old_and_already_out_rows(conn):
    old = _amazon_obs()
    old.ts = "2026-10-01T12:00:00+00:00"          # outside the 7-day window
    store.insert_observation(conn, old)
    out = _amazon_obs()
    out.listing_id, out.in_stock = "B0FTMQQ7L2:X:bom", False
    store.insert_observation(conn, out)
    assert store.mark_missing_out_of_stock(conn, "amazon_seminovos", set(), LATER) == []


def test_run_log_totals(conn):
    store.insert_run_log(conn, NOW, 5, 1, 1, 2, "amazon partial")
    store.insert_run_log(conn, LATER, 5, 0, 0, 0)
    tot = store.run_log_totals(conn, LATER)
    assert (tot["runs"], tot["failures"], tot["partial"], tot["coupon_errors"]) == (2, 1, 1, 2)
