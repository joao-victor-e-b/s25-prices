"""SQLite persistence (spec §5).

The DB (data/prices.db) is committed to the repo to give a portable price
history for free. Credentials are never stored here.

The watcher polls every ~4 minutes, so ``insert_observation`` skips a snapshot
identical to the listing's latest stored one when that row is under an hour
old: the committed DB stays small while every listing still gets at least one
row per hour (the heartbeat's and sold-out marking's "seen since" windows rely
on that). Triggers never read this back for the current poll — they get the
in-memory observations — so nothing is lost by skipping.

The committed DB predates some columns; ``init_db`` adds missing ones in place
(``_ADDED_COLUMNS``) because CREATE TABLE IF NOT EXISTS never alters a table.
A table added later (``offer_summary``, Oct 2026) just appears on the next
``init_db`` of an old DB, and follows the same change-only storage.
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
  coupon_code       TEXT,          -- validated code behind coupon (NULL = Amazon-shown)
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

-- pv holds the metric the alert was judged on: the 12x card total for price
-- thresholds, PV for the other triggers (dedup compares like with like).
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

-- Amazon checkout codes seen on coupon aggregators (one row per code; the
-- first/last_seen window is the history). Informational: never folded into PV.
CREATE TABLE IF NOT EXISTS coupon_codes (
  code        TEXT PRIMARY KEY,
  sources     TEXT,
  title       TEXT,
  terms       TEXT,
  url         TEXT,
  pct         NUMERIC,
  abs_off     NUMERIC,
  cap         NUMERIC,
  min_order   NUMERIC,
  until       TEXT,
  flags       TEXT,
  active      BOOLEAN,
  first_seen  TIMESTAMP NOT NULL,
  last_seen   TIMESTAMP NOT NULL
);

-- Amazon cashback rate per portal per scan (all 0% since Apr 2026).
CREATE TABLE IF NOT EXISTS cashback_status (
  ts        TIMESTAMP NOT NULL,
  portal    TEXT NOT NULL,
  store     TEXT NOT NULL,
  rate_pct  NUMERIC,
  active    BOOLEAN,
  PRIMARY KEY (portal, store, ts)
);

-- Promotion messages Amazon showed on an offer (asin + seller), so a message
-- seen for the first time can be announced once.
CREATE TABLE IF NOT EXISTS offer_promos (
  asin        TEXT NOT NULL,
  seller_id   TEXT NOT NULL,
  message     TEXT NOT NULL,
  first_seen  TIMESTAMP NOT NULL,
  last_seen   TIMESTAMP NOT NULL,
  PRIMARY KEY (asin, seller_id, message)
);

-- The product page's "Comparar outras N ofertas a partir de R$ X" line per
-- ASIN (models.OfferSummary): every offer counted, buybox included, and the
-- lowest card price, sellers unnamed — all CI can see while Amazon 503s the
-- offers panel. Stored on change + hourly, like observations. scope NONE is
-- a page that loaded without the line: its buybox was the only offer
-- (offers_total 1, its card price; 0 and NULL when unavailable).
CREATE TABLE IF NOT EXISTS offer_summary (
  ts              TIMESTAMP NOT NULL,
  asin            TEXT NOT NULL,
  offers_total    INTEGER,
  min_card_price  NUMERIC,       -- NULL: the line showed no price
  min_shipping    NUMERIC,       -- "+R$ X de envio"; 0 = GRÁTIS; NULL = not shown
  scope           TEXT,          -- ALL | NEW | USED (the link's condition=) | NONE (no line)
  PRIMARY KEY (asin, ts)
);

-- One row per poll so the heartbeat can report real failure counts
-- (stderr in the Actions log is invisible day to day). host is the watcher
-- that polled (config.role: cloud | local; NULL = before Oct 2026, cloud):
-- a cloud poll stands by while a healthy local row is fresh
-- (run._local_watcher). ok = the poll could see and alert: some source
-- answered, Amazon didn't block it hard, every send was delivered (NULL =
-- before the column, counted healthy).
CREATE TABLE IF NOT EXISTS run_log (
  ts             TIMESTAMP PRIMARY KEY,
  polled         INTEGER,
  failures       INTEGER,      -- sources whose fetch raised
  partial        INTEGER,      -- sources that degraded (e.g. Amazon AOD blocked)
  coupon_errors  INTEGER,      -- coupon/cashback sites that failed
  detail         TEXT,
  host           TEXT,
  ok             INTEGER
);
"""

# Columns added after the committed DB was created: {table: {column: decl}}.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "observations": {"coupon_code": "TEXT"},
    "run_log": {"host": "TEXT", "ok": "INTEGER"},
}

# A listing re-stores an unchanged snapshot only after this long (see module doc).
UNCHANGED_REFRESH_MINUTES = 60
# Fields whose change makes a snapshot worth storing even inside that hour.
_CHANGE_FIELDS = ("list_price", "pix_discount", "coupon", "coupon_code",
                  "installments_n", "installment_value", "in_stock", "grade_raw")


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
    _add_missing_columns(conn)
    conn.commit()


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, cols in _ADDED_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def _same(a, b) -> bool:
    """Equality for values read back from SQLite: NUMERIC comes back as int or
    float and BOOLEAN as 0/1, so numbers compare to the cent (True == 1)."""
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 0.005
    return a == b


def _fresh_twin(prev: sqlite3.Row | None, item, ts: str | None, fields) -> bool:
    """Is ``prev`` (the latest stored row for ``item``'s key) identical on
    ``fields`` and younger than UNCHANGED_REFRESH_MINUTES at ``ts``?"""
    if not ts or prev is None or prev["ts"] >= ts:
        return False
    try:
        age = datetime.fromisoformat(ts) - datetime.fromisoformat(prev["ts"])
    except ValueError:
        return False
    if age >= timedelta(minutes=UNCHANGED_REFRESH_MINUTES):
        return False
    keys = set(prev.keys())
    return all(_same(getattr(item, f), prev[f] if f in keys else None) for f in fields)


def _unchanged_recently(conn: sqlite3.Connection, obs: Observation) -> bool:
    """Is the listing's latest stored row identical (``_CHANGE_FIELDS``) and
    younger than UNCHANGED_REFRESH_MINUTES?"""
    if not obs.ts:
        return False
    prev = conn.execute(
        "SELECT * FROM observations WHERE source_id=? AND listing_id=? "
        "ORDER BY ts DESC LIMIT 1",
        (obs.source_id, obs.listing_id),
    ).fetchone()
    return _fresh_twin(prev, obs, obs.ts, _CHANGE_FIELDS)


def insert_observation(conn: sqlite3.Connection, obs: Observation,
                       dedup: bool = True) -> bool:
    """Store one snapshot; returns False when it was skipped as an unchanged
    repeat (``dedup``; see the module docstring)."""
    if dedup and _unchanged_recently(conn, obs):
        return False
    row = obs.as_row()
    cols = ", ".join(row.keys())
    placeholders = ", ".join(f":{k}" for k in row.keys())
    conn.execute(
        f"INSERT OR REPLACE INTO observations ({cols}) VALUES ({placeholders})",
        row,
    )
    return True


_SUMMARY_FIELDS = ("offers_total", "min_card_price", "min_shipping", "scope")


def last_offer_summary(conn: sqlite3.Connection, asin: str,
                       before_ts: str | None = None) -> sqlite3.Row | None:
    """The ASIN's latest stored offers line (before ``before_ts``, if given)."""
    return conn.execute(
        "SELECT * FROM offer_summary WHERE asin=? AND (? IS NULL OR ts < ?) "
        "ORDER BY ts DESC LIMIT 1",
        (asin, before_ts, before_ts),
    ).fetchone()


def insert_offer_summary(conn: sqlite3.Connection, s, dedup: bool = True) -> bool:
    """Store one ``models.OfferSummary`` (``s.ts`` set); like observations,
    an unchanged line is skipped while the ASIN's latest row is under an hour
    old. Returns False when skipped."""
    if dedup and _fresh_twin(last_offer_summary(conn, s.asin), s, s.ts, _SUMMARY_FIELDS):
        return False
    row = s.as_row()
    conn.execute(
        f"INSERT OR REPLACE INTO offer_summary ({', '.join(row)}) "
        f"VALUES ({', '.join(':' + k for k in row)})",
        row,
    )
    return True


def latest_offer_summaries(conn: sqlite3.Connection,
                           since: str | None = None) -> dict[str, sqlite3.Row]:
    """The latest offers line per ASIN (only ASINs seen at/after ``since``)."""
    rows = conn.execute(
        """
        SELECT s.* FROM offer_summary s
        JOIN (SELECT asin, MAX(ts) AS mts FROM offer_summary GROUP BY asin) latest
        ON s.asin=latest.asin AND s.ts=latest.mts
        WHERE (? IS NULL OR s.ts >= ?)
        """,
        (since, since),
    ).fetchall()
    return {r["asin"]: r for r in rows}


def last_offer_departure(conn: sqlite3.Connection, asin: str, ceiling: float,
                         now: str) -> str | None:
    """ts of the ASIN's latest stored reading before ``now`` that shows an
    offer *left* — the 🔍 ladder's "sold out", after which a cheap offer
    showing up again is news:

    - a no-line marker (scope NONE): the page loaded without the line, so
      only the buybox was left;
    - a line whose count fell below the reading stored just before it while
      its cheapest offer (card + shipping) is above ``ceiling``: a seller
      left and nothing within the ceiling remains.

    A cheapest offer moving back and forth across the ceiling at the same
    count is a reprice, not a departure (it reset the ladder every time when
    any line above the ceiling counted, re-alerting at the same price)."""
    row = conn.execute(
        """
        SELECT MAX(ts) FROM (
          SELECT ts, scope, offers_total, min_card_price, min_shipping,
                 LAG(offers_total) OVER (ORDER BY ts) AS prev_total
          FROM offer_summary WHERE asin=? AND ts < ?)
        WHERE scope = 'NONE'
           OR (min_card_price IS NOT NULL
               AND min_card_price + COALESCE(min_shipping, 0) > ?
               AND offers_total < prev_total)
        """,
        (asin, now, ceiling),
    ).fetchone()
    return row[0] if row else None


def recent_offers(conn: sqlite3.Connection, asin: str, since: str,
                  now: str) -> list[sqlite3.Row]:
    """Latest stored snapshot (up to ``now``) of each Amazon listing of
    ``asin`` (``{asin}:...``), kept only when it is in stock, priced and
    from ``since`` on: the offers the 🔍 trigger still counts as known while
    a poll can't read the offers panel. A listing whose latest row is a
    sell-out, or that hasn't been seen since ``since``, is left out — it must
    not pass for the offer behind the line."""
    return conn.execute(
        """
        SELECT o.* FROM observations o
        JOIN (SELECT source_id, listing_id, MAX(ts) AS mts FROM observations
              WHERE listing_id LIKE ? AND ts <= ? GROUP BY source_id, listing_id) latest
        ON o.source_id=latest.source_id AND o.listing_id=latest.listing_id
        AND o.ts=latest.mts
        WHERE o.venue='amazon' AND o.in_stock=1 AND o.list_price IS NOT NULL
          AND o.ts >= ?
        """,
        (f"{asin}:%", now, since),
    ).fetchall()


def insert_promo(conn: sqlite3.Connection, promo, ts: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO promotions (ts, source, title, url, price) "
        "VALUES (?, ?, ?, ?, ?)",
        (ts, promo.source, promo.title, promo.url, promo.price),
    )


def upsert_coupon(conn: sqlite3.Connection, c, ts: str) -> bool:
    """Insert or refresh a coupon code (a ``coupons.CouponCode``). Returns True
    the first time the code is seen."""
    row = conn.execute("SELECT sources, first_seen FROM coupon_codes WHERE code=?",
                       (c.key,)).fetchone()
    sources = set(c.source.split(","))   # merged codes carry 'promobit,meliuz'
    if row is not None and row["sources"]:
        sources |= set(row["sources"].split(","))
    conn.execute(
        "INSERT OR REPLACE INTO coupon_codes (code, sources, title, terms, url, pct, "
        "abs_off, cap, min_order, until, flags, active, first_seen, last_seen) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (c.key, ",".join(sorted(sources)), c.title, c.terms, c.url, c.pct,
         c.abs_off, c.cap, c.min_order, c.until, ",".join(c.flags), c.active,
         row["first_seen"] if row is not None else ts, ts),
    )
    return row is None


def insert_cashback(conn: sqlite3.Connection, s, ts: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO cashback_status (ts, portal, store, rate_pct, active) "
        "VALUES (?, ?, ?, ?, ?)",
        (ts, s.portal, s.store, s.rate_pct, s.active),
    )


def cashback_off_since(conn: sqlite3.Connection, portal: str, store: str,
                       since_ts: str) -> bool:
    """Was the portal seen inactive after ``since_ts`` (its last alert)? Then
    turning on again deserves a new ping."""
    return conn.execute(
        "SELECT 1 FROM cashback_status WHERE portal=? AND store=? AND ts > ? "
        "AND NOT active LIMIT 1",
        (portal, store, since_ts),
    ).fetchone() is not None


def mark_missing_out_of_stock(conn: sqlite3.Connection, source_id: str,
                              seen: set[str], ts: str, days: int = 7) -> list[str]:
    """Write an in_stock=0 row for each listing of ``source_id`` whose latest
    snapshot (within ``days``) is in stock but that this poll didn't return.

    Only for sources that report a *complete* offer list (Amazon's AOD drops a
    sold-out seller instead of showing it unavailable). Without these rows the
    vanished offer stays "current" for the heartbeat forever and restock can
    never fire when it returns. pv is NULL so the row stays out of PV history.
    """
    cutoff = (datetime.fromisoformat(ts) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """
        SELECT o.* FROM observations o
        JOIN (SELECT listing_id, MAX(ts) AS mts FROM observations
              WHERE source_id=? GROUP BY listing_id) latest
        ON o.listing_id=latest.listing_id AND o.ts=latest.mts
        WHERE o.source_id=? AND o.in_stock=1 AND o.ts >= ? AND o.ts < ?
        """,
        (source_id, source_id, cutoff, ts),
    ).fetchall()
    marked = []
    for r in rows:
        if r["listing_id"] in seen:
            continue
        o = Observation.from_row(r)
        o.ts, o.in_stock, o.pv = ts, False, None
        insert_observation(conn, o, dedup=False)   # the sold-out row must land
        marked.append(o.listing_id)
    return marked


def upsert_offer_promo(conn: sqlite3.Connection, asin: str, seller_id: str,
                       message: str, ts: str) -> bool:
    """Record a promotion message seen on an offer. Returns True the first
    time this (asin, seller_id, message) is seen."""
    row = conn.execute(
        "SELECT 1 FROM offer_promos WHERE asin=? AND seller_id=? AND message=?",
        (asin, seller_id, message),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO offer_promos (asin, seller_id, message, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?)",
            (asin, seller_id, message, ts, ts),
        )
        return True
    conn.execute(
        "UPDATE offer_promos SET last_seen=? WHERE asin=? AND seller_id=? AND message=?",
        (ts, asin, seller_id, message),
    )
    return False


def last_out_of_stock(conn: sqlite3.Connection, listing_id: str, now: str) -> str | None:
    """ts of the listing's latest sold-out (in_stock=0) row before ``now``."""
    row = conn.execute(
        "SELECT MAX(ts) FROM observations WHERE listing_id=? AND in_stock=0 AND ts < ?",
        (listing_id, now),
    ).fetchone()
    return row[0] if row else None


def first_in_stock_after(conn: sqlite3.Connection, listing_id: str, since: str,
                         now: str) -> str | None:
    """ts of the listing's first in-stock row after ``since`` (its last
    sell-out) and up to ``now``: when it came back. None = not stored yet."""
    row = conn.execute(
        "SELECT MIN(ts) FROM observations WHERE listing_id=? AND in_stock=1 "
        "AND ts > ? AND ts <= ?",
        (listing_id, since, now),
    ).fetchone()
    return row[0] if row else None


def alerted_since(conn: sqlite3.Connection, listing_id: str, triggers: tuple,
                  since: str, now: str) -> bool:
    """Was an alert with one of ``triggers`` recorded (delivered) for the
    listing after ``since`` and up to ``now``?"""
    marks = ",".join("?" * len(triggers))
    return conn.execute(
        f"SELECT 1 FROM alerts_sent WHERE listing_id=? AND trigger IN ({marks}) "
        "AND ts > ? AND ts <= ? LIMIT 1",
        (listing_id, *triggers, since, now),
    ).fetchone() is not None


def threshold_alerts(conn: sqlite3.Connection, listing_id: str, triggers: tuple,
                     since: str | None, now: str) -> list[tuple[str, float | None]]:
    """(trigger, metric) of the price-threshold alerts sent for a listing after
    ``since`` (its last sell-out; None = ever) and before ``now``."""
    marks = ",".join("?" * len(triggers))
    return [(r[0], r[1]) for r in conn.execute(
        f"SELECT trigger, pv FROM alerts_sent WHERE listing_id=? AND trigger IN ({marks}) "
        "AND (? IS NULL OR ts > ?) AND ts <= ?",
        (listing_id, *triggers, since, since, now)).fetchall()]


def out_of_stock_since(conn: sqlite3.Connection, listing_id: str, since_ts: str,
                       now: str) -> bool:
    """Was the listing stored as sold out after ``since_ts`` (its last alert)?
    Then it came back, and its next appearance is a fresh opportunity."""
    return conn.execute(
        "SELECT 1 FROM observations WHERE listing_id=? AND in_stock=0 "
        "AND ts > ? AND ts < ? LIMIT 1",
        (listing_id, since_ts, now),
    ).fetchone() is not None


# Tables whose rows are updated in place (upserts): on a merge the row with the
# newer last_seen wins, first_seen is the earliest, last_seen the latest, and
# coupon_codes' sources are unioned. {table: primary-key columns}
_UPSERTED = {
    "coupon_codes": ("code",),
    "offer_promos": ("asin", "seller_id", "message"),
}


def _merge_upserted(conn: sqlite3.Connection, table: str, key: tuple[str, ...],
                    cols: list[str]) -> int:
    where = " AND ".join(f"{k}=?" for k in key)
    col_list = ", ".join(cols)
    changed = 0
    for src in conn.execute(f"SELECT {col_list} FROM src.{table}").fetchall():
        theirs = dict(zip(cols, src))
        row = conn.execute(f"SELECT {col_list} FROM main.{table} WHERE {where}",
                           tuple(theirs[k] for k in key)).fetchone()
        if row is None:
            merged = theirs
        else:
            ours = dict(zip(cols, row))
            newer = theirs if (theirs.get("last_seen") or "") > (ours.get("last_seen") or "") \
                else ours
            merged = dict(newer)
            seen = [x for x in (ours.get("first_seen"), theirs.get("first_seen")) if x]
            if seen:
                merged["first_seen"] = min(seen)
            last = [x for x in (ours.get("last_seen"), theirs.get("last_seen")) if x]
            if last:
                merged["last_seen"] = max(last)
            if "sources" in merged:
                parts = set((ours.get("sources") or "").split(",")) | \
                    set((theirs.get("sources") or "").split(","))
                merged["sources"] = ",".join(sorted(x for x in parts if x))
            if merged == ours:
                continue
        conn.execute(f"INSERT OR REPLACE INTO main.{table} ({col_list}) "
                     f"VALUES ({', '.join('?' * len(cols))})",
                     tuple(merged[c] for c in cols))
        changed += 1
    return changed


def merge_db_rows(src_path: Path, dst_path: Path) -> dict[str, int]:
    """Copy the rows of the DB at ``src_path`` into ``dst_path``. Used by
    gitsync when two watchers' snapshots of the binary DB conflict (with the
    PC watcher running next to the cloud one, most syncs), so the result
    loses nothing either side saw: append-only tables (observations,
    alerts_sent, run_log — host and ok included —, offer_summary, ...) take
    the rows ``dst`` lacks (INSERT OR IGNORE on the primary key); upserted
    tables (``_UPSERTED``) keep the newer state of a row both have. Columns
    are those both DBs share (``init_db`` first brings dst up to this code's
    schema, so a column an older dst lacks, like run_log.host, is carried
    too); a table only one side has yet is created by ``init_db`` (dst) or
    simply has nothing to copy (src). Returns rows added or updated per
    table."""
    conn = connect(Path(dst_path))
    try:
        init_db(conn)
        conn.execute("ATTACH DATABASE ? AS src", (str(src_path),))
        added: dict[str, int] = {}
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM src.sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")]
        for t in tables:
            dst_cols = [r[1] for r in conn.execute(f"PRAGMA main.table_info({t})")]
            if not dst_cols:
                continue
            src_cols = {r[1] for r in conn.execute(f"PRAGMA src.table_info({t})")}
            shared = [c for c in dst_cols if c in src_cols]
            if t in _UPSERTED and all(k in shared for k in _UPSERTED[t]):
                added[t] = _merge_upserted(conn, t, _UPSERTED[t], shared)
                continue
            cols = ", ".join(shared)
            before = conn.total_changes
            conn.execute(f"INSERT OR IGNORE INTO main.{t} ({cols}) SELECT {cols} FROM src.{t}")
            added[t] = conn.total_changes - before
        conn.commit()
        conn.execute("DETACH DATABASE src")
        return added
    finally:
        conn.close()


def insert_run_log(conn: sqlite3.Connection, ts: str, polled: int, failures: int,
                   partial: int, coupon_errors: int, detail: str = "",
                   host: str | None = None, ok: bool = True) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO run_log (ts, polled, failures, partial, "
        "coupon_errors, detail, host, ok) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ts, polled, failures, partial, coupon_errors, detail, host, int(bool(ok))),
    )


def last_poll(conn: sqlite3.Connection, host: str, until: str | None = None,
              healthy: bool = False) -> str | None:
    """ts of the latest poll ``host`` logged (run_log), or None: only those
    at or before ``until`` when given (a row stamped later by a clock running
    ahead must not hide the correct ones), and with ``healthy`` only polls
    whose ok is 1 or NULL (logged before the column)."""
    row = conn.execute(
        "SELECT MAX(ts) FROM run_log WHERE host=? AND (? IS NULL OR ts <= ?) "
        "AND (? = 0 OR ok IS NULL OR ok <> 0)",
        (host, until, until, int(healthy)),
    ).fetchone()
    return row[0] if row else None


def last_polls(conn: sqlite3.Connection, host: str, n: int,
               until: str | None = None) -> list[sqlite3.Row]:
    """The ``n`` latest polls ``host`` logged (ts, ok), newest first; only
    those at or before ``until`` when given."""
    return conn.execute(
        "SELECT ts, ok FROM run_log WHERE host=? AND (? IS NULL OR ts <= ?) "
        "ORDER BY ts DESC LIMIT ?",
        (host, until, until, n),
    ).fetchall()


def run_log_totals(conn: sqlite3.Connection, now: str, hours: int = 24) -> sqlite3.Row:
    """Summed failure counters over the last ``hours`` (for the heartbeat),
    plus polls per watcher: ``local_runs`` (the PC) and ``cloud_runs`` (the
    rest — rows from before the host column count as cloud)."""
    cutoff = (datetime.fromisoformat(now) - timedelta(hours=hours)).isoformat()
    return conn.execute(
        "SELECT COUNT(*) AS runs, COALESCE(SUM(failures), 0) AS failures, "
        "COALESCE(SUM(partial), 0) AS partial, "
        "COALESCE(SUM(coupon_errors), 0) AS coupon_errors, "
        "COALESCE(SUM(host = 'local'), 0) AS local_runs, "
        "COALESCE(SUM(host IS NULL OR host <> 'local'), 0) AS cloud_runs "
        "FROM run_log WHERE ts >= ? AND ts <= ?",
        (cutoff, now),
    ).fetchone()


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


def current_offers(conn: sqlite3.Connection, venue: str | None = None,
                   min_rank: int = 3, since: str | None = None) -> list[sqlite3.Row]:
    """Latest snapshot of every in-stock listing at ``min_rank`` or better
    (optionally one venue, and only listings seen at/after ``since``)."""
    return conn.execute(
        """
        SELECT o.* FROM observations o
        JOIN (
            SELECT source_id, listing_id, MAX(ts) AS mts
            FROM observations GROUP BY source_id, listing_id
        ) latest
        ON o.source_id=latest.source_id AND o.listing_id=latest.listing_id
        AND o.ts=latest.mts
        WHERE o.in_stock=1 AND o.grade_rank >= ? AND o.list_price IS NOT NULL
          AND (? IS NULL OR o.venue = ?) AND (? IS NULL OR o.ts >= ?)
        """,
        (min_rank, venue, venue, since, since),
    ).fetchall()


def best_current_pv(conn: sqlite3.Connection, min_rank: int = 3,
                    venue: str | None = None,
                    since: str | None = None) -> sqlite3.Row | None:
    """Lowest in-stock PV at acceptable grade, most recent snapshot per listing.
    Used by the daily heartbeat (optionally narrowed to one venue, and to
    listings whose latest snapshot is at/after ``since`` so offers that
    vanished days ago don't count as current)."""
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
          AND (? IS NULL OR o.venue = ?) AND (? IS NULL OR o.ts >= ?)
        ORDER BY o.pv ASC LIMIT 1
        """,
        (min_rank, venue, venue, since, since),
    )
    return cur.fetchone()
