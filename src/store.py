"""SQLite persistence (spec §5).

The DB (data/prices.db) is committed to the repo to give a portable price
history for free. Credentials are never stored here.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Observation

_SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
  ts                TIMESTAMP NOT NULL,
  source_id         TEXT NOT NULL,
  seller            TEXT NOT NULL,
  venue             TEXT NOT NULL,
  listing_id        TEXT NOT NULL,
  url               TEXT NOT NULL,
  sku               TEXT,
  storage_gb        INTEGER,
  color             TEXT,
  grade_raw         TEXT,
  grade_rank        INTEGER,
  battery_band      TEXT,
  list_price        NUMERIC,
  pix_discount      NUMERIC DEFAULT 0,
  coupon            NUMERIC DEFAULT 0,
  cashback          NUMERIC DEFAULT 0,
  installments_n    INTEGER,
  installment_value NUMERIC,
  interest_flag     BOOLEAN,
  in_stock          BOOLEAN,
  pv                NUMERIC,
  PRIMARY KEY (source_id, listing_id, ts)
);

CREATE INDEX IF NOT EXISTS idx_seller_grade_ts ON observations(seller, grade_rank, ts);
CREATE INDEX IF NOT EXISTS idx_pv ON observations(pv);

CREATE TABLE IF NOT EXISTS alerts_sent (
  ts          TIMESTAMP,
  listing_id  TEXT,
  trigger     TEXT,
  pv          NUMERIC,
  PRIMARY KEY (listing_id, trigger, ts)
);

CREATE TABLE IF NOT EXISTS promotions (
  ts      TIMESTAMP NOT NULL,
  source  TEXT,
  title   TEXT,
  url     TEXT NOT NULL,
  price   NUMERIC,
  PRIMARY KEY (url, ts)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def insert_observation(conn: sqlite3.Connection, obs: Observation) -> None:
    row = obs.as_row()
    cols = ", ".join(row.keys())
    placeholders = ", ".join(f":{k}" for k in row.keys())
    conn.execute(
        f"INSERT OR REPLACE INTO observations ({cols}) VALUES ({placeholders})",
        row,
    )


def insert_promo(conn: sqlite3.Connection, promo, ts: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO promotions (ts, source, title, url, price) "
        "VALUES (?, ?, ?, ?, ?)",
        (ts, promo.source, promo.title, promo.url, promo.price),
    )


def record_alert(conn: sqlite3.Connection, listing_id: str, trigger: str,
                 pv: float | None, ts: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO alerts_sent (ts, listing_id, trigger, pv) "
        "VALUES (?, ?, ?, ?)",
        (ts, listing_id, trigger, pv),
    )


# --- queries used by triggers -------------------------------------------------

def last_alert(conn: sqlite3.Connection, listing_id: str, trigger: str,
               within_hours: int | None, now: str) -> sqlite3.Row | None:
    """Most recent alert for (listing_id, trigger). With within_hours=None,
    look at the whole history (dedup is now drop-based, not time-based)."""
    if within_hours is None:
        cur = conn.execute(
            "SELECT ts, pv FROM alerts_sent WHERE listing_id=? AND trigger=? "
            "ORDER BY ts DESC LIMIT 1",
            (listing_id, trigger),
        )
    else:
        cutoff = (datetime.fromisoformat(now) - timedelta(hours=within_hours)).isoformat()
        cur = conn.execute(
            "SELECT ts, pv FROM alerts_sent WHERE listing_id=? AND trigger=? "
            "AND ts >= ? ORDER BY ts DESC LIMIT 1",
            (listing_id, trigger, cutoff),
        )
    return cur.fetchone()


def last_observation(conn: sqlite3.Connection, source_id: str,
                     listing_id: str, before_ts: str) -> sqlite3.Row | None:
    """The prior observation for a listing (for restock detection)."""
    cur = conn.execute(
        "SELECT * FROM observations WHERE source_id=? AND listing_id=? "
        "AND ts < ? ORDER BY ts DESC LIMIT 1",
        (source_id, listing_id, before_ts),
    )
    return cur.fetchone()


def pv_series(conn: sqlite3.Connection, seller: str, grade_rank: int,
              days: int, now: str) -> list[float]:
    """Trailing PV values for a seller+grade over the last N days.

    Excludes the current poll (ts < now) so percentile/median comparisons run
    against genuine prior history, not rows just inserted this cycle.
    """
    cutoff = (datetime.fromisoformat(now) - timedelta(days=days)).isoformat()
    cur = conn.execute(
        "SELECT pv FROM observations WHERE seller=? AND grade_rank=? "
        "AND ts >= ? AND ts < ? AND pv IS NOT NULL",
        (seller, grade_rank, cutoff, now),
    )
    return [row["pv"] for row in cur.fetchall()]


def best_current_pv(conn: sqlite3.Connection, min_rank: int = 3) -> sqlite3.Row | None:
    """Lowest in-stock PV at acceptable grade, most recent snapshot per listing.
    Used by the daily heartbeat."""
    cur = conn.execute(
        """
        SELECT o.* FROM observations o
        JOIN (
            SELECT source_id, listing_id, MAX(ts) AS mts
            FROM observations GROUP BY source_id, listing_id
        ) latest
        ON o.source_id=latest.source_id AND o.listing_id=latest.listing_id
        AND o.ts=latest.mts
        WHERE o.in_stock=1 AND o.grade_rank >= ? AND o.pv IS NOT NULL
        ORDER BY o.pv ASC LIMIT 1
        """,
        (min_rank,),
    )
    return cur.fetchone()
