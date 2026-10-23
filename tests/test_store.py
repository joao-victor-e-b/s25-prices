"""store.py: schema migration, unchanged-snapshot skipping, offer promos."""
import sqlite3

import pytest

from src import store, triggers
from src.models import Observation

T0 = "2026-10-23T12:00:00+00:00"


def _at(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return f"2026-10-23T{12 + h:02d}:{m:02d}:00+00:00"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    store.init_db(c)
    return c


def _obs(ts=T0, **kw):
    base = dict(source_id="amazon_seminovos", seller="CellularStore", venue="amazon",
                listing_id="B0FTMQQ7L2:A2TO0KKQTM1BAB:excelente", url="u",
                grade_raw="Excelente", grade_rank=4, list_price=4582.47,
                pix_discount=229.13, installments_n=12, installment_value=381.90,
                in_stock=True, ts=ts, pv=4353.34)
    base.update(kw)
    return Observation(**base)


def _count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]


# --- migration -----------------------------------------------------------------

_OLD_OBSERVATIONS = """
CREATE TABLE observations (
  ts TIMESTAMP NOT NULL, source_id TEXT NOT NULL, seller TEXT NOT NULL,
  venue TEXT NOT NULL, listing_id TEXT NOT NULL, url TEXT NOT NULL, sku TEXT,
  storage_gb INTEGER, color TEXT, grade_raw TEXT, grade_rank INTEGER,
  battery_band TEXT, list_price NUMERIC, pix_discount NUMERIC DEFAULT 0,
  coupon NUMERIC DEFAULT 0, cashback NUMERIC DEFAULT 0, installments_n INTEGER,
  installment_value NUMERIC, interest_flag BOOLEAN, in_stock BOOLEAN, pv NUMERIC,
  PRIMARY KEY (source_id, listing_id, ts));
INSERT INTO observations (ts, source_id, seller, venue, listing_id, url, list_price,
  in_stock) VALUES ('2026-10-22T18:46:49+00:00', 'amazon_seminovos', 'X', 'amazon',
  'L', 'u', 4582.47, 1);
"""


def test_init_db_adds_new_columns_to_the_committed_db(tmp_path):
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.executescript(_OLD_OBSERVATIONS)
    c.commit()
    c.close()

    conn = store.connect(db)
    store.init_db(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(observations)")}
    assert "coupon_code" in cols
    old = conn.execute("SELECT * FROM observations").fetchone()
    assert old["list_price"] == 4582.47 and old["coupon_code"] is None   # history kept
    assert Observation.from_row(old).coupon_code is None
    store.insert_observation(conn, _obs(coupon=200.0, coupon_code="CUPOMX"))
    row = conn.execute("SELECT coupon_code FROM observations WHERE ts=?", (T0,)).fetchone()
    assert row["coupon_code"] == "CUPOMX"
    store.init_db(conn)                       # idempotent
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert "offer_promos" in tables


# --- unchanged-snapshot skipping -------------------------------------------------

def test_unchanged_snapshot_skipped_within_the_hour(conn):
    assert store.insert_observation(conn, _obs()) is True
    assert store.insert_observation(conn, _obs(_at(4))) is False
    assert store.insert_observation(conn, _obs(_at(56), pv=4300.0)) is False  # pv not a key
    assert _count(conn) == 1
    assert store.insert_observation(conn, _obs(_at(60))) is True   # hourly refresh
    assert _count(conn) == 2


def test_any_tracked_change_is_stored(conn):
    changes = [dict(list_price=4499.0), dict(pix_discount=0.0), dict(coupon=100.0),
               dict(coupon_code="CUPOMX"), dict(installments_n=10),
               dict(installment_value=380.0), dict(in_stock=False),
               dict(grade_raw="Premium")]
    for i, kw in enumerate(changes):
        lid = f"L{i}"
        store.insert_observation(conn, _obs(listing_id=lid))
        assert store.insert_observation(conn, _obs(_at(4), listing_id=lid, **kw)) is True, kw
    assert _count(conn) == 2 * len(changes)


def test_dedup_can_be_turned_off(conn):
    store.insert_observation(conn, _obs())
    assert store.insert_observation(conn, _obs(_at(4)), dedup=False) is True
    assert _count(conn) == 2


def test_other_listings_are_independent(conn):
    store.insert_observation(conn, _obs())
    assert store.insert_observation(conn, _obs(_at(4), listing_id="OTHER")) is True


def test_sold_out_marking_still_inserts(conn):
    store.insert_observation(conn, _obs())
    store.insert_observation(conn, _obs(_at(4)))              # skipped repeat
    gone = store.mark_missing_out_of_stock(conn, "amazon_seminovos", set(), _at(8))
    assert gone == ["B0FTMQQ7L2:A2TO0KKQTM1BAB:excelente"]
    row = conn.execute("SELECT * FROM observations ORDER BY ts DESC").fetchone()
    assert (row["ts"], row["in_stock"], row["pv"]) == (_at(8), 0, None)


def test_restock_still_fires_with_skipping(conn):
    store.insert_observation(conn, _obs(list_price=4200.0))
    store.mark_missing_out_of_stock(conn, "amazon_seminovos", set(), _at(4))
    back = _obs(_at(8), list_price=4200.0)                    # same price, back in stock
    assert store.insert_observation(conn, back) is True       # in_stock changed
    assert "restock" in {a.trigger for a in triggers.evaluate(conn, [back], _at(8))}
    again = _obs(_at(12), list_price=4200.0)
    assert store.insert_observation(conn, again) is False     # unchanged -> skipped
    assert "restock" not in {a.trigger for a in triggers.evaluate(conn, [again], _at(12))}


# --- offer promos / current offers -------------------------------------------------

def test_upsert_offer_promo(conn):
    assert store.upsert_offer_promo(conn, "A", "S", "cupom", T0) is True
    assert store.upsert_offer_promo(conn, "A", "S", "cupom", _at(4)) is False
    assert store.upsert_offer_promo(conn, "A", "S2", "cupom", _at(4)) is True
    row = conn.execute("SELECT * FROM offer_promos WHERE seller_id='S'").fetchone()
    assert (row["first_seen"], row["last_seen"]) == (T0, _at(4))


def test_current_offers_latest_in_stock_rank_and_window(conn):
    store.insert_observation(conn, _obs("2026-10-20T12:00:00+00:00", listing_id="OLD"))
    store.insert_observation(conn, _obs(listing_id="BOM", grade_rank=2, grade_raw="Bom"))
    store.insert_observation(conn, _obs(listing_id="GONE"))
    store.insert_observation(conn, _obs(_at(4), listing_id="GONE", in_stock=False))
    store.insert_observation(conn, _obs(listing_id="OK"))
    rows = store.current_offers(conn, venue="amazon", min_rank=4,
                                since="2026-10-21T12:00:00+00:00")
    assert [r["listing_id"] for r in rows] == ["OK"]


# --- merging two watchers' DBs (G3) ---------------------------------------------

def _db(path, coupon=None, promo=None, obs_ids=()):
    from src.coupons import CouponCode
    conn = store.connect(path)
    store.init_db(conn)
    if coupon:
        ts, first, active, sources = coupon
        store.upsert_coupon(conn, CouponCode(source=sources, code="SHARED", abs_off=100.0,
                                             active=active), first)
        conn.execute("UPDATE coupon_codes SET last_seen=?, sources=? WHERE code='SHARED'",
                     (ts, sources))
    if promo:
        first, last = promo
        store.upsert_offer_promo(conn, "A", "S", "cupom", first)
        store.upsert_offer_promo(conn, "A", "S", "cupom", last)
    for lid in obs_ids:
        store.insert_observation(conn, _obs(listing_id=lid))
    conn.commit()
    conn.close()
    return path


def _row(path, sql):
    conn = store.connect(path)
    try:
        return conn.execute(sql).fetchone()
    finally:
        conn.close()


def test_merge_keeps_the_newer_state_of_upserted_rows(tmp_path):
    upstream = _db(tmp_path / "up.db", coupon=("2026-10-23T05:45:00+00:00",
                                               "2026-10-23T05:00:00+00:00", True, "promobit"),
                   promo=("2026-10-23T05:00:00+00:00", "2026-10-23T05:45:00+00:00"),
                   obs_ids=("U",))
    ours = _db(tmp_path / "ours.db", coupon=("2026-10-23T06:10:00+00:00",
                                             "2026-10-23T05:30:00+00:00", False,
                                             "meliuz,promobit"),
               promo=("2026-10-23T05:30:00+00:00", "2026-10-23T06:10:00+00:00"),
               obs_ids=("O",))
    store.merge_db_rows(ours, upstream)
    c = _row(upstream, "SELECT * FROM coupon_codes WHERE code='SHARED'")
    assert c["active"] == 0                              # newer state wins
    assert c["sources"] == "meliuz,promobit"             # sources unioned
    assert (c["first_seen"], c["last_seen"]) == ("2026-10-23T05:00:00+00:00",
                                                 "2026-10-23T06:10:00+00:00")
    pr = _row(upstream, "SELECT * FROM offer_promos")
    assert (pr["first_seen"], pr["last_seen"]) == ("2026-10-23T05:00:00+00:00",
                                                   "2026-10-23T06:10:00+00:00")
    ids = {r[0] for r in store.connect(upstream).execute(
        "SELECT listing_id FROM observations")}
    assert ids == {"U", "O"}                             # append-only: union


def test_merge_never_lets_an_older_row_overwrite(tmp_path):
    upstream = _db(tmp_path / "up.db", coupon=("2026-10-23T06:10:00+00:00",
                                               "2026-10-23T05:30:00+00:00", False, "pelando"))
    older = _db(tmp_path / "old.db", coupon=("2026-10-23T05:45:00+00:00",
                                             "2026-10-23T05:00:00+00:00", True, "promobit"))
    store.merge_db_rows(older, upstream)
    c = _row(upstream, "SELECT * FROM coupon_codes WHERE code='SHARED'")
    assert (c["active"], c["last_seen"]) == (0, "2026-10-23T06:10:00+00:00")
    assert c["sources"] == "pelando,promobit" and c["first_seen"] == "2026-10-23T05:00:00+00:00"
