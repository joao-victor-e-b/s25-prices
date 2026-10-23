"""Orchestrator: poll -> normalize -> store -> trigger -> notify (+ heartbeat).

Usage:
  python -m src.run --init-db          create the DB schema
  python -m src.run --test-alert       send one test message (build order §10.1)
  python -m src.run --poll             one poll cycle (default)
  python -m src.run --heartbeat        send the daily heartbeat
  python -m src.run --poll --daily     also include tier-3 (daily) sources
  python -m src.run --loop --minutes 345 --interval 240 --coupon-every 1800 --git-sync
                                       the watcher (see ``loop``)

Why a loop: GitHub's cron fires every 5 minutes at best and is routinely late
by 10-30, while a refurb offer at a good price can sell out within the hour.
With the repo public, Actions minutes are free, so one job runs ``loop`` for
~6 hours, polling every ~4 minutes (with jitter, backing off when Amazon
pushes back) and committing the DB every 30 minutes (gitsync.py); the next
scheduled run is already queued behind it. A registry entry with ``every: N``
is polled on every Nth loop iteration only (the four new-unit ASINs use 2), so
one poll never fetches more than ~6 ASINs.
"""
from __future__ import annotations

import argparse
import re
import hashlib
import random
import signal
import sys
import threading
import time
import traceback
from datetime import date, datetime, timedelta

import yaml

from . import (
    config,
    coupon_checks,
    coupons as coupons_mod,
    gitsync,
    notify,
    promos as promos_mod,
    store,
    triggers,
)
from .cdi import get_cdi_monthly
from .models import Observation
from .normalize import apply_cashback, card_total, finalize, pix_price, pv
from .sources import amazon as amazon_mod
from .sources import is_enabled, load_source
from .sources.amazon import is_routine_promo, parse_listing_id
from .sources.amazon import promo_terms as amazon_promo_terms


def load_registry(include_disabled: bool = False) -> list[dict]:
    """Registry entries, minus ``enabled: false`` ones unless asked."""
    with open(config.REGISTRY_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    sources = data.get("sources", [])
    return sources if include_disabled else [c for c in sources if is_enabled(c)]


def _due(cfg: dict, iteration: int) -> bool:
    """Is this registry entry polled on loop iteration ``iteration``? An entry
    with ``every: N`` runs on iterations 0, N, 2N, ... (a one-off poll is 0)."""
    every = max(int(cfg.get("every", 1) or 1), 1)
    return iteration % every == 0


def poll(daily: bool = False, enable_new_low: bool = False,
         dry_run: bool = False, scan_promos: bool = True,
         max_tier: int = 2, scan_coupons: bool | None = None,
         cdi: float | None = None, iteration: int = 0) -> dict:
    """One cycle. ``scan_promos`` is the DuckDuckGo deal scan; ``scan_coupons``
    the coupon/cashback sweep (defaults to ``scan_promos``, the old single
    switch). ``cdi`` lets the loop fetch the rate once instead of every poll;
    ``iteration`` (the loop's poll number) staggers ``every: N`` sources.

    Result keys: polled, failures, partial, observations, stored, alerts,
    amazon_block ("hard": an Amazon source raised or its product page was
    blocked; "soft": only the offers panel was blocked; None: clean or just an
    empty listing — the loop backs off by level), amazon_trouble (= a level is
    set), requests and aod_ok/aod_tried (logged per poll and in run_log).
    """
    if scan_coupons is None:
        scan_coupons = scan_promos
    conn = store.connect(config.DB_PATH)
    try:
        store.init_db(conn)
        if cdi is None:
            cdi = get_cdi_monthly()
        now = store.now_iso()
        today = datetime.fromisoformat(now).date()
        try:
            checks = coupon_checks.load_checks(today=today)
        except Exception as e:   # the checks file must never stop the price poll
            print(f"[coupon-checks] load failed: {e}", file=sys.stderr)
            checks = []
        codes, seen = _validation_codes(conn, now, checks)

        polled = failures = partial = stored = 0
        problems: list[str] = []   # run_log detail: failed + degraded sources
        observations: list[Observation] = []
        block: str | None = None   # worst Amazon pushback this poll
        aod_ok = aod_tried = 0
        req0 = amazon_mod.PACER.count

        # daily runs sweep all tiers; otherwise cap at max_tier.
        allowed_max = 3 if daily else max_tier
        for cfg in load_registry():
            if cfg.get("tier", 1) > allowed_max or not _due(cfg, iteration):
                continue
            src = load_source(cfg)
            if src is None:
                continue  # scraper not implemented yet (Phase 2/3)
            polled += 1
            is_amazon = cfg.get("venue") == "amazon"
            try:
                offers = src.fetch()
            except Exception as e:  # one broken site must not kill the run (spec §8)
                failures += 1
                if is_amazon:
                    block = "hard"
                    aod_tried += getattr(src, "aod_tried", 0)
                problems.append(f"{cfg['id']}: {e}"[:200])
                print(f"[fetch-fail] {cfg['id']}: {e}", file=sys.stderr)
                continue
            if is_amazon:
                aod_ok += getattr(src, "aod_ok", 0)
                aod_tried += getattr(src, "aod_tried", 0)
            if getattr(src, "degraded", None):
                partial += 1
                if is_amazon:
                    level = getattr(src, "block_level", None) or (
                        "hard" if getattr(src, "blocked", True) else None)
                    block = _worse(block, level)
                problems.append(f"{cfg['id']} partial: {'; '.join(src.degraded)}"[:200])
            for o in offers:
                o.ts = now
                apply_cashback(o)
                finalize(o, cdi)
                if o.cashback:            # recompute PV once cashback is folded in
                    o.pv = pv(o, cdi)
                if o.venue == "amazon":
                    _apply_validated_coupon(o, checks, codes, today, cdi, seen)
                stored += store.insert_observation(conn, o)
                observations.append(o)
            # A source that saw its whole offer list (Amazon's AOD) tells us which
            # listings sold out: they vanish instead of showing unavailable.
            if getattr(src, "complete", False):
                gone = store.mark_missing_out_of_stock(
                    conn, cfg["id"], {o.listing_id for o in offers}, now)
                if gone:
                    print(f"[sold-out] {cfg['id']}: {', '.join(gone)}")

        alerts = triggers.evaluate(conn, observations, now, enable_new_low=enable_new_low)
        _send_alerts(conn, alerts, now, dry_run)
        _offer_promo_alerts(conn, observations, now, dry_run)

        # --- Phase 4b: promotions + coupons (best-effort; never break the poll) ---
        coupon_errors = 0
        if scan_promos:
            _scan_promotions(conn, now, dry_run)
        if scan_coupons:
            coupon_errors = _scan_coupons(conn, now, dry_run, observations)

        requests_made = amazon_mod.PACER.count - req0
        stats = f"aod_ok={aod_ok}/{aod_tried} req={requests_made}"
        print(f"[amazon] requests={requests_made} aod_ok={aod_ok}/{aod_tried}"
              + (f" backoff={block}" if block else ""), file=sys.stderr)
        store.insert_run_log(conn, now, polled, failures, partial, coupon_errors,
                             " | ".join([stats] + problems))
        conn.commit()
    finally:
        conn.close()   # uncommitted work (a crash mid-poll) is rolled back
    result = {"polled": polled, "failures": failures, "partial": partial,
              "observations": len(observations), "stored": stored,
              "alerts": len(alerts), "amazon_trouble": block is not None,
              "amazon_block": block, "requests": requests_made,
              "aod_ok": aod_ok, "aod_tried": aod_tried}
    print(f"[poll] {result}")
    return result


def _worse(a: str | None, b: str | None) -> str | None:
    """The harder of two Amazon pushback levels (None < soft < hard)."""
    order = {None: 0, "soft": 1, "hard": 2}
    return a if order[a] >= order[b] else b


def _recent_codes(conn, now: str, days: int = 2) -> dict[str, coupons_mod.CouponCode]:
    """Aggregator codes seen in the last ``days``, keyed by code. Inactive ones
    are kept on purpose: an aggregator calling a code expired vetoes a cart
    validation (coupon_checks.best_validated skips it)."""
    since = (datetime.fromisoformat(now) - timedelta(days=days)).isoformat()
    rows = conn.execute("SELECT * FROM coupon_codes WHERE last_seen >= ?",
                        (since,)).fetchall()
    out = {}
    for r in rows:
        c = coupons_mod.CouponCode.from_row(r)
        out[c.key] = c
    return out


def _validation_codes(conn, now: str, checks: list,
                      days: int = 2) -> tuple[dict[str, coupons_mod.CouponCode], dict[str, date]]:
    """Aggregator listings for validating cart checks: codes seen in the last
    ``days`` plus — whatever their age — every code that has a check, so an
    expiry or "inactive" the aggregator reported never stops vetoing once the
    code drops off its page. Returns (codes, code -> last_seen date)."""
    since = (datetime.fromisoformat(now) - timedelta(days=days)).isoformat()
    checked = sorted({c.code for c in checks})
    marks = ",".join("?" * len(checked))
    sql = "SELECT * FROM coupon_codes WHERE last_seen >= ?"
    if checked:
        sql += f" OR code IN ({marks})"
    codes, seen = {}, {}
    for r in conn.execute(sql, (since, *checked)).fetchall():
        c = coupons_mod.CouponCode.from_row(r)
        codes[c.key] = c
        try:
            seen[c.key] = datetime.fromisoformat(r["last_seen"]).date()
        except (TypeError, ValueError):
            pass
    return codes, seen


def _apply_validated_coupon(o: Observation, checks: list, codes: dict,
                            today: date, cdi: float,
                            seen: dict | None = None) -> None:
    """Fold the best cart-validated code into an Amazon offer when it beats the
    coupon Amazon itself shows (coupons don't stack: the bigger one wins)."""
    asin, seller_id = parse_listing_id(o.listing_id)
    best = coupon_checks.best_validated(asin, seller_id, o.list_price, codes, today,
                                        checks=checks, seen=seen)
    if best is not None and best[1] > (o.coupon or 0):
        o.coupon_code, o.coupon = best
        o.pv = pv(o, cdi)


def _send_alerts(conn, alerts: list, now: str, dry_run: bool) -> None:
    """Send price/stock alerts. Dedup state records each alert's metric (the
    12x total for thresholds), and only for alerts whose batch a channel
    delivered, so an outage retries on the next poll instead of swallowing a
    COMPRE (and a batch that did arrive isn't repeated)."""
    if alerts and dry_run:
        # Local testing: show what would fire, send nothing, don't touch dedup state.
        print(f"[dry-run] {len(alerts)} alert(s) would send (no messages sent):")
        for a in alerts:
            print("  " + notify.format_alert(a).replace("\n", " | "))
    elif alerts:
        report = notify.notify(alerts)
        for i in report.get("delivered", []):
            a = alerts[i]
            store.record_alert(conn, a.obs.listing_id, a.trigger, a.metric, now)
        print(f"[alerts] sent={report['sent']} wa_ok={report['wa_ok']} "
              f"ntfy_ok={report['ntfy_ok']}")
    else:
        print("[alerts] none")


def _promo_key(asin: str, seller_id: str, message: str) -> str:
    digest = hashlib.sha1(message.encode("utf-8")).hexdigest()[:10]
    return f"promo:{asin}:{seller_id}:{digest}"


# "primeira compra na Amazon" promos need a brand-new Amazon account — never
# usable here. (A first purchase *with a card* is still announced.)
_ACCOUNT_FIRST_BUY_RE = re.compile(
    r"(?:primeira|1[ªa]) compra (?:na|da) amazon|primeiro pedido na amazon", re.I)
PROMO_TERMS_CAP = 3   # terms lookups per poll (one Amazon request each)
def _terms_session():
    """Terms lookups ride the shared Amazon session (and its pacer)."""
    return amazon_mod.amazon_session()


_PROMO_BRL_RE = re.compile(r"R\$\s*(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?|\d+(?:,\d{1,2})?)")
_PROMO_PCT_RE = re.compile(r"(\d{1,2})\s*%")


def _promo_amount(message: str, total: float) -> float:
    """The R$ a promo message states ("Economize R$ 300", "R$ 900 off"): its
    largest R$ figure, else its percentage of the 12x total, else 0."""
    amounts = [float(m.replace(".", "").replace(",", "."))
               for m in _PROMO_BRL_RE.findall(message or "")]
    if amounts:
        return max(amounts)
    m = _PROMO_PCT_RE.search(message or "")
    return round(total * int(m.group(1)) / 100, 2) if m else 0.0


def _promo_worth_it(o: Observation, message: str) -> bool:
    """A new promotion is news near the user's range only: the offer's 12x total
    is within RESTOCK_MAX_12X, or the promo's stated amount would take it under
    YEAR_LOW_12X (new-unit listings carry sellers at R$ 6-10k)."""
    total = card_total(o)
    if total is None:
        return False
    return (total <= triggers.RESTOCK_MAX_12X
            or total - _promo_amount(message, total) <= triggers.YEAR_LOW_12X)


def _offer_promo_alerts(conn, observations: list, now: str, dry_run: bool) -> int:
    """Announce promotion messages Amazon starts showing on an eligible target
    offer (a coupon, a card deal...) near the user's range (``_promo_worth_it``).
    Every message is recorded in offer_promos (first/last seen); routine lines
    never alert. One alert per (ASIN, message) per poll, shown on its cheapest
    offer with the count of other sellers showing it; each seller's key goes to
    alerts_sent, written only after a delivery like the coupon pings, so an
    outage can't swallow one. A dry run reads but writes nothing. Returns how
    many blocks were (or would be) sent."""
    groups: dict[tuple[str, str], list[tuple[Observation, str]]] = {}
    for o in observations:
        if o.venue != "amazon" or not o.promos or not triggers.eligible(o):
            continue
        asin, seller_id = parse_listing_id(o.listing_id)
        for msg in o.promos:
            if not dry_run:
                store.upsert_offer_promo(conn, asin, seller_id, msg, now)
            if is_routine_promo(msg) or _ACCOUNT_FIRST_BUY_RE.search(msg):
                continue
            if not _promo_worth_it(o, msg):
                continue
            key = _promo_key(asin, seller_id, msg)
            group = groups.setdefault((asin, msg), [])
            if any(k == key for _, k in group) or store.last_alert(
                    conn, key, "offer_promo", within_hours=None, now=now):
                continue
            group.append((o, key))
    blocks, keys = [], []
    for (_asin, msg), members in groups.items():
        if not members:
            continue
        o = min((m for m, _ in members), key=card_total)
        terms = None
        pid = o.promo_ids.get(msg)
        if pid and len(blocks) < PROMO_TERMS_CAP:
            terms = amazon_promo_terms(_terms_session(), pid)
        blocks.append(notify.format_offer_promo(msg, o, terms, others=len(members) - 1))
        keys.append([k for _, k in members])
    if not blocks:
        return 0
    if dry_run:
        print(f"[dry-run] {len(blocks)} new-promotion message(s) would send:")
        for b in blocks:
            print("  " + b.replace("\n", " | "))
        return len(blocks)
    rep = notify.notify_offer_promos(blocks)
    for i in rep.get("delivered", []):
        for key in keys[i]:
            store.record_alert(conn, key, "offer_promo", None, now)
    print(f"[offer-promos] sent={rep['sent']} wa_ok={rep['wa_ok']} ntfy_ok={rep['ntfy_ok']}")
    return len(blocks)


PROMO_CAP = 10  # max promos verified/alerted per run


def _scan_promotions(conn, now: str, dry_run: bool) -> None:
    try:
        promos = promos_mod.scan()
    except Exception as e:  # DDG hiccup must not sink the poll
        print(f"[promo-fail] {e}", file=sys.stderr)
        return

    for pr in promos:            # store ALL found promos for history
        pr.ts = now
        store.insert_promo(conn, pr, now)

    ceiling = triggers.GOOD_12X  # only alert genuinely cheap promos
    candidates = []
    for pr in promos:
        if pr.price is None or pr.price > ceiling:
            continue
        prior = store.last_alert(conn, pr.url, "promo", within_hours=None, now=now)
        if prior is None or (prior["pv"] is not None
                             and pr.price <= prior["pv"] - triggers.DEDUP_PV_DELTA):
            candidates.append(pr)
    candidates.sort(key=lambda p: p.price)
    candidates = candidates[:PROMO_CAP]

    # maximum-rigor expiry check on the (few) cheap candidates
    fresh = []
    for pr in candidates:
        if promos_mod.is_active(pr.url):
            fresh.append(pr)
        else:
            print(f"[promo-skip] inactive/expired: {pr.url}")

    if fresh and dry_run:
        print(f"[dry-run] {len(fresh)} promo(s) would send:")
        for pr in fresh:
            print("  " + notify.format_promo(pr).replace("\n", " | "))
    elif fresh:
        rep = notify.notify_promos(fresh)
        for i in rep.get("delivered", []):
            store.record_alert(conn, fresh[i].url, "promo", fresh[i].price, now)
        print(f"[promos] sent={rep['sent']} wa_ok={rep['wa_ok']} ntfy_ok={rep['ntfy_ok']}")
    else:
        print(f"[promos] scanned={len(promos)} none new (cheap+active)")


COUPON_MIN_VALUE = 100      # R$; codes worth less at the phone's price stay silent
COUPON_CAP = 5              # max new codes pinged per run (aggregators list ~60)
DEFAULT_AMAZON_PRICE = 3990  # reference order value when no Amazon offer is known


def _eligible_amazon(observations: list) -> list[Observation]:
    return [o for o in observations
            if o.venue == "amazon" and o.in_stock and o.list_price
            and (o.grade_rank or 0) >= triggers.ALERT_MIN_RANK]


def _best_amazon_offer(conn, now: str, observations: list | None = None) -> Observation | None:
    """Cheapest eligible Amazon offer by 12x total: this run's if Amazon
    answered, else the latest snapshots from the last 2 days."""
    amz = _eligible_amazon(observations or [])
    if not amz:
        since = (datetime.fromisoformat(now) - timedelta(days=2)).isoformat()
        amz = [Observation.from_row(r) for r in store.current_offers(
            conn, venue="amazon", min_rank=triggers.ALERT_MIN_RANK, since=since)]
    return min(amz, key=card_total) if amz else None


def _amazon_reference(conn, observations: list, now: str) -> tuple[float, float] | None:
    """(card, pix) price of the cheapest eligible Amazon offer (see
    ``_best_amazon_offer``); codes are valued on the card price."""
    o = _best_amazon_offer(conn, now, observations)
    if o is None:
        return None
    return o.list_price, pix_price(o)


def _corroborated(code: coupons_mod.CouponCode) -> bool:
    """A code with a stated expiry date or listed by two or more aggregators."""
    return bool(code.until) or "," in (code.source or "")


def _scan_coupons(conn, now: str, dry_run: bool, observations: list) -> int:
    """Amazon checkout codes + cashback portals (best-effort). Returns how many
    coupon/cashback sites failed, for the run log.

    Codes are stored and pinged once each when worth >= COUPON_MIN_VALUE on the
    current Amazon price, labelled "não validado": aggregator codes never lower
    the 12x total until a cart test lands in coupon_checks.yaml. A cashback
    portal pings when it turns on (first time, or again after being seen off) or
    raises its rate. Decisions come from alerts_sent, and nothing is marked
    handled unless a channel delivered it, so a dry run or an outage can't
    swallow the one alert this exists for.
    """
    try:
        codes, errors = coupons_mod.scan_codes()
        rates, cb_errors = coupons_mod.scan_cashback()
    except Exception as e:  # never sink the poll
        print(f"[coupon-fail] {e}", file=sys.stderr)
        return 1
    for err in errors + cb_errors:
        print(f"[coupon-fail] {err}", file=sys.stderr)

    new = sum(store.upsert_coupon(conn, c, now) for c in codes)
    ref = _amazon_reference(conn, observations, now)
    price = ref[0] if ref else DEFAULT_AMAZON_PRICE

    blocks, sent = [], []
    today = datetime.fromisoformat(now).date()
    try:
        dead = coupon_checks.failed_codes(coupon_checks.load_checks(today=today), today)
    except Exception as e:   # never sink the poll over the checks file
        print(f"[coupon-checks] load failed: {e}", file=sys.stderr)
        dead = set()
    fresh = [(c, v) for c, v in
             coupons_mod.relevant_codes(codes, price, min_value=COUPON_MIN_VALUE)
             if c.key not in dead
             and store.last_alert(conn, f"coupon:{c.key}", "coupon",
                                  within_hours=None, now=now) is None]
    # Corroborated codes first (a stated expiry date, or listed by 2+ sites):
    # undated single-site listings are mostly stale (9/9 tested dead, Oct 2026).
    fresh.sort(key=lambda cv: (not _corroborated(cv[0]), -cv[1], cv[0].key))
    if len(fresh) > COUPON_CAP:  # best first; the rest wait for a later run
        print(f"[coupons] {len(fresh) - COUPON_CAP} more new code(s) held back: "
              f"{', '.join(c.key for c, _ in fresh[COUPON_CAP:])}")
    for c, value in fresh[:COUPON_CAP]:
        blocks.append(notify.format_coupon(c, value, price))
        sent.append((f"coupon:{c.key}", "coupon", value))
    for s in rates:
        lid = f"cashback:{s.portal}"
        last = store.last_alert(conn, lid, "cashback", within_hours=None, now=now)
        if s.active and (last is None
                         or store.cashback_off_since(conn, s.portal, s.store, last["ts"])
                         or (s.rate_pct or 0) > (last["pv"] or 0)):
            blocks.append(notify.format_cashback(s))
            sent.append((lid, "cashback", s.rate_pct))
        if not dry_run:
            store.insert_cashback(conn, s, now)

    print(f"[coupons] codes={len(codes)} new={new} "
          f"cashback={[(s.portal, s.rate_pct) for s in rates]} to_send={len(blocks)}")
    if blocks and dry_run:
        print(f"[dry-run] {len(blocks)} coupon/cashback message(s) would send:")
        for b in blocks:
            print("  " + b.replace("\n", " | "))
    elif blocks:
        rep = notify.notify_coupons(blocks)
        for i in rep.get("delivered", []):   # undelivered -> retried next run
            lid, trig, val = sent[i]
            store.record_alert(conn, lid, trig, val, now)
        print(f"[coupons] sent={rep['sent']} wa_ok={rep['wa_ok']} ntfy_ok={rep['ntfy_ok']}")
    return len(errors) + len(cb_errors)


CODES_TO_TRY = 5   # one cart session can test ~10 codes before Amazon locks entry


def _codes_to_try(conn, best: Observation | None, now: str,
                  limit: int = CODES_TO_TRY) -> list[tuple[str, float]]:
    """Aggregator codes worth >= COUPON_MIN_VALUE on the best offer that nobody
    has tried on it lately, that haven't failed anywhere recently, and that
    would beat its current coupon — the ones to ask Claude to test in the cart.
    Corroborated codes (a stated expiry date, or 2+ aggregators) come first,
    then by value, like the coupon pings: cart attempts are scarce."""
    if best is None or not best.list_price:
        return []
    today = datetime.fromisoformat(now).date()
    asin, seller_id = parse_listing_id(best.listing_id)
    checks = coupon_checks.load_checks(today=today)
    skip = (coupon_checks.recently_checked(checks, asin, seller_id, today)
            | coupon_checks.failed_codes(checks, today))
    codes = _recent_codes(conn, now).values()
    rel = coupons_mod.relevant_codes(codes, best.list_price,
                                     min_value=COUPON_MIN_VALUE, today=today)
    rel.sort(key=lambda cv: (not _corroborated(cv[0]), -cv[1], cv[0].key))
    out = []
    for c, value in rel:
        if c.key not in skip and value > (best.coupon or 0):
            out.append((c.key, value))
            if len(out) >= limit:
                break
    return out


def _heartbeat_message(conn, now: str) -> str:
    best = _best_amazon_offer(conn, now)
    try:
        hint = _codes_to_try(conn, best, now)
    except Exception as e:  # the heartbeat itself must always go out
        print(f"[heartbeat] code hint failed: {e}", file=sys.stderr)
        hint = []
    sources = sum(1 for c in load_registry() if load_source(c) is not None)
    return notify.format_heartbeat(best, hint, sources, store.run_log_totals(conn, now))


def heartbeat() -> dict:
    """Daily Amazon summary: best eligible 12x total + Pix price, one code
    worth testing, 24h poll/failure counts."""
    conn = store.connect(config.DB_PATH)
    try:
        store.init_db(conn)
        msg = _heartbeat_message(conn, store.now_iso())
    finally:
        conn.close()
    return notify.send_heartbeat(msg)


# --- watcher loop -------------------------------------------------------------

SYNC_EVERY_S = 1800        # commit + push the DB this often (and on exit)
EXIT_SYNC_BUDGET_S = 180   # the budget exit syncs normally (retries) within this
MAX_BACKOFF_S = 1200       # hard pushback (dp blocked) doubles the sleep up to 20 min
SOFT_BACKOFF = 1.5         # soft pushback (only the AOD blocked): x1.5 ...
SOFT_BACKOFF_S = 720       # ... up to 12 min
JITTER = (0.8, 1.2)        # ±20% so polls don't land on a fixed beat
MAX_ERRORS_IN_A_ROW = 5    # a poll that keeps crashing ends the run (visible in CI)


def _install_sigterm():
    """Turn SIGTERM (Actions cancel/timeout) into SystemExit so ``loop``'s
    finally still commits the DB. Returns the previous handler, or None."""
    if threading.current_thread() is not threading.main_thread():
        return None

    def _exit(signum, _frame):
        raise SystemExit(128 + signum)

    try:
        return (signal.signal(signal.SIGTERM, _exit),)
    except (ValueError, OSError):
        return None


def _ignore_signals() -> dict:
    """Ignore SIGTERM/SIGINT while the exit sync runs: the runner sends SIGINT,
    then SIGTERM ~7.5 s later, then SIGKILL — a second signal must not kill a
    push that is half done. Returns the previous handlers."""
    prev = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                prev[sig] = signal.signal(sig, signal.SIG_IGN)
            except (ValueError, OSError):
                pass
    return prev


def _restore_signals(prev: dict) -> None:
    for sig, handler in prev.items():
        signal.signal(sig, handler if handler is not None else signal.SIG_DFL)


def loop(minutes: float = 345, interval: float = 240, coupon_every: float = 1800,
         git_sync: bool = False, dry_run: bool = False, enable_new_low: bool = False,
         *, poll_fn=None, sync_fn=None, pull_fn=None, cdi_fn=None,
         clock=time.monotonic, sleep=time.sleep, uniform=random.uniform) -> dict:
    """Poll repeatedly until ``minutes`` would be exceeded.

    - sleep ``interval`` seconds ±20% between polls; when Amazon pushed back
      grow it — hard (a source raised, product page blocked/captcha): x2 up to
      MAX_BACKOFF_S; soft (only the offers panel blocked): x1.5 up to
      SOFT_BACKOFF_S — and go back to ``interval`` after a clean poll (an
      empty listing is not pushback);
    - the coupon/cashback sweep runs on the first poll and then at most every
      ``coupon_every`` seconds; the DuckDuckGo deal scan stays off;
    - CDI is fetched once for the whole loop; each poll gets its iteration
      number so ``every: N`` sources are staggered;
    - with ``git_sync``: pull before the first poll (the previous watcher's
      final snapshot landed after this job was queued), commit + push the DB
      every SYNC_EVERY_S (a failed sync is logged, never fatal), and once more
      on the way out with both stop signals ignored: the normal retrying sync
      within EXIT_SYNC_BUDGET_S when the time budget ran out (no kill is
      coming), a single fast attempt after SIGINT/SIGTERM or a crash.

    The keyword-only hooks exist for tests.
    """
    poll_fn = poll_fn or poll
    sync_fn = sync_fn or gitsync.sync_db
    pull_fn = pull_fn or gitsync.pull_latest
    cdi = (cdi_fn or get_cdi_monthly)()
    if git_sync:
        pull_fn()
    start = clock()
    deadline = start + minutes * 60
    last_coupons: float | None = None
    last_sync = start
    base = interval
    polls = syncs = errors_in_row = 0
    cap = max(MAX_BACKOFF_S, interval)
    budget_exit = False

    old_handler = _install_sigterm()
    try:
        while True:
            t0 = clock()
            do_coupons = last_coupons is None or t0 - last_coupons >= coupon_every
            try:
                result = poll_fn(dry_run=dry_run, enable_new_low=enable_new_low,
                                 scan_promos=False, scan_coupons=do_coupons, cdi=cdi,
                                 iteration=polls)
                level = result.get("amazon_block") or (
                    "hard" if result.get("amazon_trouble") else None)
                errors_in_row = 0
            except Exception:
                traceback.print_exc()
                level = "hard"
                errors_in_row += 1
                if errors_in_row >= MAX_ERRORS_IN_A_ROW:
                    raise
            polls += 1
            if do_coupons:
                last_coupons = t0
            took = clock() - t0

            trouble = level is not None
            soft_cap = max(SOFT_BACKOFF_S, interval)
            if level == "hard":
                base = min(base * 2, cap)
            elif level == "soft":
                base = min(base * SOFT_BACKOFF, soft_cap)
            else:
                base = interval
            if git_sync and clock() - last_sync >= SYNC_EVERY_S:
                try:
                    sync_fn()
                    syncs += 1
                except Exception:   # a sync problem must never stop the watcher
                    traceback.print_exc()
                last_sync = clock()

            wait = base * uniform(*JITTER)
            if trouble:
                wait = min(wait, cap if level == "hard" else soft_cap)
            if clock() + wait + took > deadline:
                print(f"[loop] {polls} poll(s); time budget reached, stopping")
                budget_exit = True
                break
            print(f"[loop] poll {polls} took {took:.0f}s; next in {wait:.0f}s"
                  + (f" (backing off: {level})" if trouble else ""))
            sleep(wait)
    finally:
        if git_sync:
            prev = _ignore_signals()
            try:
                if budget_exit:
                    sync_fn(budget_s=EXIT_SYNC_BUDGET_S)
                else:
                    sync_fn(final=True)
                syncs += 1
            except Exception:
                traceback.print_exc()
            finally:
                _restore_signals(prev)
        if old_handler is not None:
            signal.signal(signal.SIGTERM, old_handler[0] or signal.SIG_DFL)
    return {"polls": polls, "syncs": syncs}


def main(argv=None) -> int:
    # Windows consoles default to cp1252 and choke on the alert emoji; force UTF-8.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    p = argparse.ArgumentParser(description="S25 Ultra refurb monitor")
    p.add_argument("--init-db", action="store_true")
    p.add_argument("--test-alert", action="store_true")
    p.add_argument("--poll", action="store_true")
    p.add_argument("--heartbeat", action="store_true")
    p.add_argument("--daily", action="store_true", help="include tier-3 sources")
    p.add_argument("--enable-new-low", action="store_true",
                   help="enable trigger 4 (needs ~30d history)")
    p.add_argument("--dry-run", action="store_true",
                   help="evaluate + print alerts without sending or recording them")
    p.add_argument("--no-promos", action="store_true",
                   help="skip the deal-aggregator promotions + coupon scans")
    p.add_argument("--tier1", action="store_true",
                   help="poll only tier-1 sources")
    p.add_argument("--loop", action="store_true",
                   help="keep polling (the watcher); see --minutes/--interval")
    p.add_argument("--minutes", type=float, default=345,
                   help="loop: total run time budget in minutes")
    p.add_argument("--interval", type=float, default=240,
                   help="loop: seconds between polls (±20%% jitter)")
    p.add_argument("--coupon-every", type=float, default=1800,
                   help="loop: minimum seconds between coupon/cashback sweeps")
    p.add_argument("--git-sync", action="store_true",
                   help="loop: commit + push data/prices.db every 30 min and on exit")
    args = p.parse_args(argv)

    if args.init_db:
        conn = store.connect(config.DB_PATH)
        store.init_db(conn)
        conn.close()
        print(f"[init-db] schema ready at {config.DB_PATH}")
        return 0

    if args.test_alert:
        r = notify.send_test()
        print(f"[test-alert] wa_ok={r['wa_ok']} ntfy_ok={r['ntfy_ok']}")
        return 0 if (r["wa_ok"] or r["ntfy_ok"]) else 1

    if args.heartbeat:
        r = heartbeat()
        print(f"[heartbeat] wa_ok={r['wa_ok']} ntfy_ok={r['ntfy_ok']} :: {r['message']}")
        return 0 if (r["wa_ok"] or r["ntfy_ok"]) else 1   # a silent heartbeat fails the job

    if args.loop:
        r = loop(minutes=args.minutes, interval=args.interval,
                 coupon_every=args.coupon_every, git_sync=args.git_sync,
                 dry_run=args.dry_run, enable_new_low=args.enable_new_low)
        print(f"[loop] done: {r}")
        return 0

    # default action
    poll(daily=args.daily, enable_new_low=args.enable_new_low,
         dry_run=args.dry_run, scan_promos=not args.no_promos,
         max_tier=1 if args.tier1 else 2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
