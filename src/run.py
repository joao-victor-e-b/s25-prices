"""Orchestrator: poll -> normalize -> store -> trigger -> notify (+ heartbeat).

Usage:
  python -m src.run --init-db          create the DB schema
  python -m src.run --test-alert       send one test message (build order §10.1)
  python -m src.run --poll             one poll cycle (default)
  python -m src.run --heartbeat        send the daily heartbeat
  python -m src.run --poll --daily     also include tier-3 (daily) sources
  python -m src.run --loop --minutes 345 --interval 240 --coupon-every 1800 --git-sync
                                       the watcher (see ``loop``)
  ... --role local                     the PC watcher (default: S25_ROLE, else cloud)

Why a loop: GitHub's cron fires every 5 minutes at best and is routinely late
by 10-30, while a refurb offer at a good price can sell out within the hour.
With the repo public, Actions minutes are free, so one job runs ``loop`` for
~6 hours, polling every ~4 minutes (with jitter, backing off when Amazon
pushes back) and committing the DB every 30 minutes (gitsync.py); the next
scheduled run is already queued behind it. A registry entry with ``every: N``
is polled on every Nth loop iteration only (the four new-unit ASINs use 3), so
one poll never fetches more than ~6 ASINs.

Two watchers (``--role``, S25_ROLE): the cloud loop above runs 24/7, and the
user's PC can run the same loop from a home IP (``--role local``,
scripts/local_watcher.ps1), where Amazon serves the full offers panel. Every
poll logs its role in run_log.host and its health in run_log.ok; while the DB
holds a healthy local poll younger than LOCAL_FRESH_MIN, a cloud poll stands
by: it fetches and stores as usual but sends nothing and records no
alerts_sent (``_local_watcher``). A PC watcher with no alert channel refuses
to start, and one loop at a time runs per DB (``acquire_lock``).
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
from pathlib import Path

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
from .models import Observation, OfferSummary
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


LOCAL_FRESH_MIN = 45     # a healthy local poll this recent puts the cloud on standby
LOCAL_SICK_POLLS = 3     # ...unless the PC's latest this many polls all failed
LOCAL_SKEW_TOL_MIN = 5   # local polls stamped further ahead than this are ignored


def _local_watcher(conn, now: str) -> tuple[str, float] | None:
    """(ts, seconds left) of the PC watcher's latest *healthy* poll
    (run_log.ok: a source answered, Amazon didn't block it hard, every send
    was delivered) while it is at most LOCAL_FRESH_MIN old at ``now``; None
    when there is none that fresh.

    - A PC that is blind (a captcha on the home IP) or mute (a stale .env)
      keeps logging polls; they don't hold the cloud. It takes over
      LOCAL_FRESH_MIN after the PC's last good poll, or at once when the PC's
      LOCAL_SICK_POLLS latest polls all failed.
    - Clock skew: a local poll stamped more than LOCAL_SKEW_TOL_MIN after
      ``now`` (the PC's clock runs ahead) is ignored, and reported, so it
      can't stretch the standby; the time left never exceeds LOCAL_FRESH_MIN.
      A PC clock running behind can't be told from a late push: the README
      asks for automatic time sync on the PC.

    The cloud only sees local polls its checkout has pulled: the PC pushes
    right after its first poll and then every 30 min, and the cloud pulls at
    each of its syncs — plus once more just before a standby would lapse
    (``loop``), so a live PC is never taken for gone between two syncs."""
    try:
        t_now = datetime.fromisoformat(now)
    except (TypeError, ValueError):
        return None
    until = (t_now + timedelta(minutes=LOCAL_SKEW_TOL_MIN)).isoformat(timespec="seconds")
    newest = store.last_poll(conn, "local")
    if newest is not None and newest > until:
        print(f"[standby] ignoring local poll(s) from the future ({newest}): PC clock ahead?")
    last = store.last_poll(conn, "local", until=until, healthy=True)
    if last is None:
        return None
    try:
        age = (t_now - datetime.fromisoformat(last)).total_seconds()
    except (TypeError, ValueError):
        return None
    left = LOCAL_FRESH_MIN * 60 - max(age, 0.0)
    if left < 0:
        return None
    recent = store.last_polls(conn, "local", LOCAL_SICK_POLLS, until=until)
    if len(recent) == LOCAL_SICK_POLLS and all(r["ok"] == 0 for r in recent):
        print(f"[standby] off: the PC's last {LOCAL_SICK_POLLS} polls failed (blind, or "
              f"its alerts undelivered; latest {recent[0]['ts']}): sending")
        return None
    return last, left


class _Outbox:
    """A poll's delivery tally for run_log.ok: senders take the indices to
    record from ``delivered(report)``, which counts what the channels left
    undelivered (an outage, a revoked key, a missing secret)."""

    def __init__(self) -> None:
        self.undelivered = 0

    def delivered(self, report: dict) -> list[int]:
        got = list(report.get("delivered", []))
        self.undelivered += max(int(report.get("sent", 0)) - len(got), 0)
        return got


def poll(daily: bool = False, enable_new_low: bool = False,
         dry_run: bool = False, scan_promos: bool = True,
         max_tier: int = 2, scan_coupons: bool | None = None,
         cdi: float | None = None, iteration: int = 0,
         role: str | None = None, hold: bool = False) -> dict:
    """One cycle. ``scan_promos`` is the DuckDuckGo deal scan; ``scan_coupons``
    the coupon/cashback sweep (defaults to ``scan_promos``, the old single
    switch). ``cdi`` lets the loop fetch the rate once instead of every poll;
    ``iteration`` (the loop's poll number) staggers ``every: N`` sources.
    ``role`` (config.role() when None) goes to run_log.host; a 'cloud' poll
    stands by while the PC watcher is active (``_local_watcher``): it fetches
    and stores everything (observations, offer lines, promotion messages,
    codes, cashback, run_log) but sends nothing and records no alerts_sent,
    so the PC's next poll still announces whatever is news. ``hold`` does the
    same for any role: the loop sets it while the PC watcher's DB couldn't be
    synced (``loop``), so alerts are judged on a current DB, once.

    run_log.ok records whether the poll could see and alert: some source
    answered, no hard Amazon block, nothing left undelivered (a standby or
    hold sends nothing, so it can't fail that part). The cloud stands by only
    for healthy local polls.

    Result keys: polled, failures, partial, observations, stored, alerts,
    unseen_alerts (🔍), amazon_block ("hard": an Amazon source raised or its
    product page was blocked; None otherwise — a blocked offers panel next to
    a good product page is CI's steady state, not pushback), amazon_trouble
    (= a level is set; the loop backs off on it), requests, aod_ok/aod_tried
    and ingress_ok/ingress_tried (product pages whose offers line was read /
    requested; logged per poll and at the start of run_log.detail), role,
    standby and standby_left_s (seconds until the local poll behind the
    standby turns LOCAL_FRESH_MIN old; None without standby), held,
    undelivered (alert items no channel took) and ok.
    """
    if scan_coupons is None:
        scan_coupons = scan_promos
    role = config.role(role)
    conn = store.connect(config.DB_PATH)
    try:
        store.init_db(conn)
        if cdi is None:
            cdi = get_cdi_monthly()
        now = store.now_iso()
        local = _local_watcher(conn, now) if role == "cloud" else None
        standby = local is not None
        if standby:
            print(f"[standby] local watcher active (last poll {local[0]})")
        elif hold:
            print("[hold] the DB couldn't be synced yet: storing only, sending nothing")
        quiet = standby or hold
        outbox = _Outbox()
        today = datetime.fromisoformat(now).date()
        try:
            checks = coupon_checks.load_checks(today=today)
        except Exception as e:   # the checks file must never stop the price poll
            print(f"[coupon-checks] load failed: {e}", file=sys.stderr)
            checks = []
        codes, seen = _validation_codes(conn, now, checks)

        polled = failures = partial = stored = 0
        problems: list[str] = []   # run_log detail: failed + degraded sources
        # run_log detail: record-only facts (standby/hold, offer counts up)
        notes: list[str] = ([f"standby: local poll {local[0]}"] if standby
                            else ["hold: DB not synced"] if hold else [])
        observations: list[Observation] = []
        summaries: list[OfferSummary] = []   # product pages' offers lines
        block: str | None = None   # Amazon pushback this poll ("hard" or None)
        aod_ok = aod_tried = ingress_ok = ingress_tried = 0
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
                    ingress_tried += getattr(src, "ingress_tried", 0)
                problems.append(f"{cfg['id']}: {e}"[:200])
                print(f"[fetch-fail] {cfg['id']}: {e}", file=sys.stderr)
                continue
            if is_amazon:
                aod_ok += getattr(src, "aod_ok", 0)
                aod_tried += getattr(src, "aod_tried", 0)
                ingress_ok += getattr(src, "ingress_ok", 0)
                ingress_tried += getattr(src, "ingress_tried", 0)
            if getattr(src, "degraded", None):
                partial += 1
                # A source without the attributes counts as blocked (safe side).
                if is_amazon and (getattr(src, "block_level", None)
                                  or getattr(src, "blocked", True)):
                    block = "hard"
                problems.append(f"{cfg['id']} partial: {'; '.join(src.degraded)}"[:200])
            summary = getattr(src, "offer_summary", None)
            if summary is not None:
                summary.ts = now
                note = _record_offer_summary(conn, summary)
                if note:
                    notes.append(note)
                summaries.append(summary)
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
        _send_alerts(conn, alerts, now, dry_run, quiet, outbox)
        unseen = triggers.unseen_offers(conn, summaries, observations, now)
        _send_unseen_alerts(conn, unseen, now, dry_run, quiet, outbox)
        _offer_promo_alerts(conn, observations, now, dry_run, quiet, outbox)

        # --- Phase 4b: promotions + coupons (best-effort; never break the poll) ---
        coupon_errors = 0
        if scan_promos:
            _scan_promotions(conn, now, dry_run, quiet, outbox)
        if scan_coupons:
            coupon_errors = _scan_coupons(conn, now, dry_run, observations, quiet, outbox)

        # Could this poll see and alert? (run_log.ok; see the docstring)
        ok = polled > failures and block != "hard" and outbox.undelivered == 0
        if outbox.undelivered:
            problems.append(f"undelivered: {outbox.undelivered} alert item(s)")
        requests_made = amazon_mod.PACER.count - req0
        stats = (f"aod_ok={aod_ok}/{aod_tried} req={requests_made} "
                 f"ingress={ingress_ok}/{ingress_tried}")
        print(f"[amazon] requests={requests_made} aod_ok={aod_ok}/{aod_tried} "
              f"ingress={ingress_ok}/{ingress_tried}"
              + (f" backoff={block}" if block else ""), file=sys.stderr)
        store.insert_run_log(conn, now, polled, failures, partial, coupon_errors,
                             " | ".join([stats] + notes + problems), host=role, ok=ok)
        conn.commit()
    finally:
        conn.close()   # uncommitted work (a crash mid-poll) is rolled back
    result = {"polled": polled, "failures": failures, "partial": partial,
              "observations": len(observations), "stored": stored,
              "alerts": len(alerts), "unseen_alerts": len(unseen),
              "amazon_trouble": block is not None,
              "amazon_block": block, "requests": requests_made,
              "aod_ok": aod_ok, "aod_tried": aod_tried,
              "ingress_ok": ingress_ok, "ingress_tried": ingress_tried,
              "role": role, "standby": standby,
              "standby_left_s": round(local[1]) if standby else None,
              "held": hold, "undelivered": outbox.undelivered, "ok": ok}
    print(f"[poll] {result}")
    return result


def _record_offer_summary(conn, s: OfferSummary) -> str | None:
    """Store an offers line or no-line marker (change-only + hourly,
    store.insert_offer_summary) and return a run-log note when its offer
    count went up since the ASIN's last stored reading at the same scope —
    a no-line marker matches any scope, so a line appearing next to a lone
    buybox (1 -> 2) is noted too. Record-only on purpose: a new seller alone
    says nothing about price — the 🔍 alert covers a cheaper one."""
    prev = store.last_offer_summary(conn, s.asin, s.ts)
    store.insert_offer_summary(conn, s)
    if prev is None or prev["offers_total"] is None or s.offers_total <= prev["offers_total"]:
        return None
    if prev["scope"] != s.scope and OfferSummary.NO_LINE not in (prev["scope"], s.scope):
        return None
    low = f" from {s.min_card_price:.2f}" if s.min_card_price is not None else ""
    note = f"{s.asin} offers {prev['offers_total']}->{s.offers_total}{low}"
    print(f"[offers] {note}")
    return note


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


def _send_alerts(conn, alerts: list, now: str, dry_run: bool,
                 standby: bool = False, outbox: _Outbox | None = None) -> None:
    """Send price/stock alerts. Dedup state records each alert's metric (the
    12x total for thresholds), and only for alerts whose batch a channel
    delivered, so an outage retries on the next poll instead of swallowing a
    COMPRE (and a batch that did arrive isn't repeated). On ``standby`` (the
    PC watcher is active, or the loop holds sends) nothing is sent or
    recorded. ``outbox`` tallies what went undelivered (run_log.ok)."""
    if standby:
        return
    outbox = outbox or _Outbox()
    if alerts and dry_run:
        # Local testing: show what would fire, send nothing, don't touch dedup state.
        print(f"[dry-run] {len(alerts)} alert(s) would send (no messages sent):")
        for a in alerts:
            print("  " + notify.format_alert(a).replace("\n", " | "))
    elif alerts:
        report = notify.notify(alerts)
        for i in outbox.delivered(report):
            a = alerts[i]
            store.record_alert(conn, a.obs.listing_id, a.trigger, a.metric, now)
        print(f"[alerts] sent={report['sent']} wa_ok={report['wa_ok']} "
              f"ntfy_ok={report['ntfy_ok']}")
    else:
        print("[alerts] none")


def _send_unseen_alerts(conn, alerts: list, now: str, dry_run: bool,
                        standby: bool = False, outbox: _Outbox | None = None) -> None:
    """Send 🔍 alerts (triggers.unseen_offers). Like ``_send_alerts``: the
    ladder state (alerts_sent under ``unseen:<ASIN>``, metric = card +
    shipping) is written only for alerts a channel delivered, a dry run
    prints and writes nothing, and a standby does neither."""
    if not alerts or standby:
        return
    outbox = outbox or _Outbox()
    if dry_run:
        print(f"[dry-run] {len(alerts)} unseen-offer alert(s) would send:")
        for a in alerts:
            print("  " + notify.format_unseen_offer(a).replace("\n", " | "))
        return
    report = notify.notify_unseen(alerts)
    for i in outbox.delivered(report):
        a = alerts[i]
        store.record_alert(conn, a.key, a.trigger, a.metric, now)
    print(f"[unseen] sent={report['sent']} wa_ok={report['wa_ok']} "
          f"ntfy_ok={report['ntfy_ok']}")


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


def _offer_promo_alerts(conn, observations: list, now: str, dry_run: bool,
                        standby: bool = False, outbox: _Outbox | None = None) -> int:
    """Announce promotion messages Amazon starts showing on an eligible target
    offer (a coupon, a card deal...) near the user's range (``_promo_worth_it``).
    Every message is recorded in offer_promos (first/last seen); routine lines
    never alert. One alert per (ASIN, message) per poll, shown on its cheapest
    offer with the count of other sellers showing it; each seller's key goes to
    alerts_sent, written only after a delivery like the coupon pings, so an
    outage can't swallow one. A dry run reads but writes nothing; a standby
    records the messages but sends nothing (no terms lookups either: they are
    Amazon requests). Returns how many blocks were (or would be) sent."""
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
    if standby:
        return 0
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
    for i in (outbox or _Outbox()).delivered(rep):
        for key in keys[i]:
            store.record_alert(conn, key, "offer_promo", None, now)
    print(f"[offer-promos] sent={rep['sent']} wa_ok={rep['wa_ok']} ntfy_ok={rep['ntfy_ok']}")
    return len(blocks)


PROMO_CAP = 10  # max promos verified/alerted per run


def _scan_promotions(conn, now: str, dry_run: bool, standby: bool = False,
                     outbox: _Outbox | None = None) -> None:
    try:
        promos = promos_mod.scan()
    except Exception as e:  # DDG hiccup must not sink the poll
        print(f"[promo-fail] {e}", file=sys.stderr)
        return

    for pr in promos:            # store ALL found promos for history
        pr.ts = now
        store.insert_promo(conn, pr, now)
    if standby:                  # history only: the PC watcher announces
        return

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
        for i in (outbox or _Outbox()).delivered(rep):
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


def _scan_coupons(conn, now: str, dry_run: bool, observations: list,
                  standby: bool = False, outbox: _Outbox | None = None) -> int:
    """Amazon checkout codes + cashback portals (best-effort). Returns how many
    coupon/cashback sites failed, for the run log.

    Codes are stored and pinged once each when worth >= COUPON_MIN_VALUE on the
    current Amazon price, labelled "não validado": aggregator codes never lower
    the 12x total until a cart test lands in coupon_checks.yaml. A cashback
    portal pings when it turns on (first time, or again after being seen off) or
    raises its rate. Decisions come from alerts_sent, and nothing is marked
    handled unless a channel delivered it, so a dry run, a standby (codes and
    rates are still stored) or an outage can't swallow the one alert this
    exists for.
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
    if standby:
        blocks = []   # the PC watcher's sweep pings them; nothing recorded here

    print(f"[coupons] codes={len(codes)} new={new} "
          f"cashback={[(s.portal, s.rate_pct) for s in rates]} to_send={len(blocks)}")
    if blocks and dry_run:
        print(f"[dry-run] {len(blocks)} coupon/cashback message(s) would send:")
        for b in blocks:
            print("  " + b.replace("\n", " | "))
    elif blocks:
        rep = notify.notify_coupons(blocks)
        for i in (outbox or _Outbox()).delivered(rep):   # undelivered -> retried next run
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


def _offer_lines(conn, now: str, registry: list[dict],
                 days: int = 2) -> list[tuple[str, int, float | None]]:
    """(label, offers_total, min_card_price) for the heartbeat's "Ofertas:"
    line: the latest reading (last ``days``) of each registry ASIN, in
    registry order, labelled by its storage. A no-line marker reads as what
    it is — the buybox alone ("1", its price; "0" when unavailable) — so a
    vanished line isn't reported for two more days. New-unit listings
    (``default_grade: Novo``, or a line scoped NEW) are left out: the line is
    about the seminovo listings, where the panel the server can't read hides
    sellers."""
    since = (datetime.fromisoformat(now) - timedelta(days=days)).isoformat()
    latest = store.latest_offer_summaries(conn, since)
    out = []
    for cfg in registry:
        if str(cfg.get("default_grade") or "").lower() == "novo":
            continue
        row = latest.get(cfg.get("asin") or "")
        if row is None or row["scope"] == "NEW" or row["offers_total"] is None:
            continue
        gb = (cfg.get("match") or {}).get("storage_gb")
        out.append((f"{gb}GB" if gb else row["asin"], row["offers_total"],
                    row["min_card_price"]))
    return out


def _heartbeat_message(conn, now: str) -> str:
    best = _best_amazon_offer(conn, now)
    try:
        hint = _codes_to_try(conn, best, now)
    except Exception as e:  # the heartbeat itself must always go out
        print(f"[heartbeat] code hint failed: {e}", file=sys.stderr)
        hint = []
    registry = load_registry()
    sources = sum(1 for c in registry if load_source(c) is not None)
    try:
        offers = _offer_lines(conn, now, registry)
    except Exception as e:  # never let the extra line stop the heartbeat
        print(f"[heartbeat] offers line failed: {e}", file=sys.stderr)
        offers = []
    return notify.format_heartbeat(best, hint, sources, store.run_log_totals(conn, now),
                                   offers=offers)


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
MAX_BACKOFF_S = 1200       # pushback (dp blocked / a source raised) doubles the sleep up to 20 min
JITTER = (0.8, 1.2)       # ±20% so polls don't land on a fixed beat
MAX_ERRORS_IN_A_ROW = 5    # a poll that keeps crashing ends the run (visible in CI)
RESUME_SLACK_S = 300       # a sleep overrunning its wait by more was a suspend (laptop lid)
PULL_RETRY_S = (15, 30, 60, 120)   # the PC's first pull: Wi-Fi may still be connecting


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
         role: str | None = None,
         *, poll_fn=None, sync_fn=None, pull_fn=None, cdi_fn=None,
         clock=time.monotonic, sleep=time.sleep, uniform=random.uniform,
         wall=time.time) -> dict:
    """Poll repeatedly until ``minutes`` would be exceeded.

    - sleep ``interval`` seconds ±20% between polls; when Amazon pushed back
      (a source raised, product page blocked/captcha) double it up to
      MAX_BACKOFF_S, and go back to ``interval`` after a poll without
      pushback. A blocked or empty offers panel next to a good product page
      is not pushback: from CI that panel always 503s (amazon.py), and
      backing off on it only slowed every poll to 12-17 min for nothing;
    - the coupon/cashback sweep runs on the first poll and then at most every
      ``coupon_every`` seconds; the DuckDuckGo deal scan stays off;
    - CDI is fetched once for the whole loop; each poll gets its iteration
      number so ``every: N`` sources are staggered;
    - with ``git_sync``: pull before the first poll (the previous watcher's
      final snapshot landed after this job was queued); commit + push the DB
      before the first poll that finds the last sync SYNC_EVERY_S old, so that
      poll judges dedup on what the other watcher pushed meanwhile (a failed
      sync is logged, never fatal); and once more on the way out with both
      stop signals ignored: the normal retrying sync within
      EXIT_SYNC_BUDGET_S when the time budget ran out (no kill is coming), a
      single fast attempt after SIGINT/SIGTERM or a crash;
    - time runs on two clocks, ``clock`` (monotonic) and ``wall``: a laptop
      suspended inside ``sleep`` wakes with its wall clock hours ahead,
      whether or not the monotonic clock counted the suspend, and its DB as
      old as the suspend. A sleep that overran its wait by RESUME_SLACK_S is
      a resume, synced before the next poll (the cloud may have alerted
      meanwhile); the 30-min sync and the time budget go by whichever clock
      ran further, so a run that slept past its budget stops without polling;
    - ``role`` (config.role() when None) goes to every poll. With git sync,
      the PC watcher (local) also syncs right after its first poll and after
      its first poll since a resume, so the cloud learns it is up at its next
      sync instead of ~30 min later; and a cloud loop whose standby is about
      to lapse (the poll's ``standby_left_s``) syncs before the next poll, so
      it judges the PC on what the PC last pushed, not on a checkout up to 30
      min old (two independent 30-min sync cycles alone would have it judge
      local polls up to ~1h old, dropping a live PC's standby for part of
      most cycles);
    - the PC watcher never alerts on a DB it couldn't bring up to date: its
      first pull is retried (PULL_RETRY_S: Wi-Fi may connect after log on),
      and while that pull, or the sync after a resume, failed, its polls run
      with ``hold`` (store only), each preceded by another sync, until one
      works.

    The keyword-only hooks exist for tests.
    """
    poll_fn = poll_fn or poll
    sync_fn = sync_fn or gitsync.sync_db
    pull_fn = pull_fn or gitsync.pull_latest
    role = config.role(role)
    local = role == "local"
    cdi = (cdi_fn or get_cdi_monthly)()
    held = False   # the PC's DB isn't known to be current: its polls store only
    if git_sync:
        pulled = pull_fn()
        if local:
            for pause in PULL_RETRY_S:
                if pulled:
                    break
                print(f"[loop] pull before the first poll failed: retrying in {pause:.0f}s")
                sleep(pause)
                pulled = pull_fn()
            held = not pulled
            if held:
                print("[loop] no pull: polling store-only (no alerts) until a sync works")
    start, wall_start = clock(), wall()
    budget = minutes * 60
    last_coupons: float | None = None
    last_sync, last_sync_wall = start, wall_start
    refresh_at: float | None = None   # loop clock: the cloud's standby lapses
    slept_from, planned = wall_start, 0.0   # the last sleep: wall() at its start, its wait
    base = interval
    polls = syncs = errors_in_row = 0
    cap = max(MAX_BACKOFF_S, interval)
    budget_exit = False

    def elapsed() -> float:
        return max(clock() - start, wall() - wall_start)

    def periodic_sync() -> bool:
        nonlocal syncs, last_sync, last_sync_wall, refresh_at, held
        synced = False
        try:
            synced = bool(sync_fn())
            syncs += 1
        except Exception:   # a sync problem must never stop the watcher
            traceback.print_exc()
        last_sync, last_sync_wall = clock(), wall()
        refresh_at = None   # the next poll judges the standby on the synced DB
        if held and synced:
            print("[loop] DB synced: alerts on again")
        held = held and not synced
        return synced

    old_handler = _install_sigterm()
    try:
        while True:
            resumed = False
            if polls:   # back from sleep(wait)
                asleep = wall() - slept_from
                resumed = asleep - planned > RESUME_SLACK_S
                if resumed:
                    print(f"[loop] resumed after {asleep:.0f}s asleep ({planned:.0f}s planned)")
                if elapsed() >= budget:
                    print(f"[loop] {polls} poll(s); time budget spent while asleep, stopping")
                    budget_exit = True
                    break
                stale = (clock() - last_sync >= SYNC_EVERY_S
                         or wall() - last_sync_wall >= SYNC_EVERY_S)
                lapsing = refresh_at is not None and clock() >= refresh_at
                if git_sync and (resumed or held or stale or lapsing):
                    why = ("resumed" if resumed else "held" if held
                           else "30-min sync" if stale
                           else "standby lapsing: to see the PC's latest polls")
                    print(f"[loop] syncing before the poll ({why})")
                    if not periodic_sync() and local and resumed:
                        held = True
                        print("[loop] no sync after the resume: polling store-only "
                              "(no alerts) until one works")
            t0 = clock()
            do_coupons = last_coupons is None or t0 - last_coupons >= coupon_every
            try:
                result = poll_fn(dry_run=dry_run, enable_new_low=enable_new_low,
                                 scan_promos=False, scan_coupons=do_coupons, cdi=cdi,
                                 iteration=polls, role=role, hold=held)
                level = result.get("amazon_block") or (
                    "hard" if result.get("amazon_trouble") else None)
                left = result.get("standby_left_s")
                refresh_at = None if left is None else t0 + left
                errors_in_row = 0
            except Exception:
                traceback.print_exc()
                level = "hard"
                refresh_at = None
                errors_in_row += 1
                if errors_in_row >= MAX_ERRORS_IN_A_ROW:
                    raise
            polls += 1
            if do_coupons:
                last_coupons = t0
            took = clock() - t0

            trouble = level is not None
            base = min(base * 2, cap) if trouble else interval
            if git_sync and local and (polls == 1 or resumed):
                periodic_sync()   # announce: the cloud stands by from its next sync

            wait = base * uniform(*JITTER)
            if trouble:
                wait = min(wait, cap)
            if elapsed() + wait + took > budget:
                print(f"[loop] {polls} poll(s); time budget reached, stopping")
                budget_exit = True
                break
            print(f"[loop] poll {polls} took {took:.0f}s; next in {wait:.0f}s"
                  + (f" (backing off: {level})" if trouble else ""))
            slept_from, planned = wall(), wait
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


# --- one watcher loop per DB ---------------------------------------------------

def watcher_lock_path() -> Path:
    """The lock a ``--loop`` holds: next to the DB it polls into and syncs
    (data/prices.db.lock in a clone; gitignored)."""
    return Path(f"{config.DB_PATH}.lock")


def acquire_lock(path: Path):
    """Take a non-blocking exclusive lock on ``path`` for the life of this
    process: the open file to keep (and ``release_lock``), or None when
    another process holds it. Two loops in one clone would run git and
    rewrite the DB under each other (a PC watcher outliving a Task Scheduler
    stop of its wrapper, a manual run next to the task). The OS drops the
    lock when the process dies, so a killed run never leaves a stale one."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "a+")
    except OSError as e:
        print(f"[loop] can't open the lock {path} ({e}): running unlocked", file=sys.stderr)
        return False   # not None: go on, with nothing to release
    try:
        if sys.platform == "win32":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def release_lock(lock) -> None:
    if not lock:
        return
    try:
        if sys.platform == "win32":
            import msvcrt
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    finally:
        lock.close()   # closing drops a flock too


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
    p.add_argument("--role", choices=config.ROLES, default=None,
                   help="which watcher this is: cloud (GitHub Actions; stands by "
                        "while the PC polls) or local (the PC watcher); "
                        "default: S25_ROLE, else cloud")
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

    try:
        role = config.role(args.role)
    except ValueError as e:   # a bad S25_ROLE: refuse to poll rather than guess
        p.error(str(e))

    if role == "local" and not args.dry_run and not notify.channels():
        # its polls would put the cloud on standby for a watcher that can't alert
        print("[local] no alert channel configured (CALLMEBOT_PHONE + CALLMEBOT_APIKEY, "
              "or NTFY_TOPIC, in .env): the PC watcher refuses to run", file=sys.stderr)
        return 2

    if args.loop:
        path = watcher_lock_path()
        lock = acquire_lock(path)
        if lock is None:
            print(f"[loop] another watcher holds {path}: exiting")
            return 0
        try:
            r = loop(minutes=args.minutes, interval=args.interval,
                     coupon_every=args.coupon_every, git_sync=args.git_sync,
                     dry_run=args.dry_run, enable_new_low=args.enable_new_low, role=role)
        finally:
            release_lock(lock)
        print(f"[loop] done: {r}")
        return 0

    # default action
    poll(daily=args.daily, enable_new_low=args.enable_new_low,
         dry_run=args.dry_run, scan_promos=not args.no_promos,
         max_tier=1 if args.tier1 else 2, role=role)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
