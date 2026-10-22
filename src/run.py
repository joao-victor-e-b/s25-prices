"""Orchestrator: poll -> normalize -> store -> trigger -> notify (+ heartbeat).

Usage:
  python -m src.run --init-db          create the DB schema
  python -m src.run --test-alert       send one test message (build order §10.1)
  python -m src.run --poll             one poll cycle (default)
  python -m src.run --heartbeat        send the daily heartbeat
  python -m src.run --poll --daily     also include tier-3 (daily) sources
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta

import yaml

from . import config, coupons as coupons_mod, notify, promos as promos_mod, store, triggers
from .cdi import get_cdi_monthly
from .normalize import MIN_RANK, apply_cashback, finalize, pv
from .sources import load_source


def load_registry() -> list[dict]:
    with open(config.REGISTRY_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("sources", [])


def poll(daily: bool = False, enable_new_low: bool = False,
         dry_run: bool = False, scan_promos: bool = True,
         max_tier: int = 2) -> dict:
    conn = store.connect(config.DB_PATH)
    store.init_db(conn)

    cdi = get_cdi_monthly()
    now = store.now_iso()

    sources = load_registry()
    polled = 0
    failures = 0
    problems: list[str] = []   # run_log detail: failed + degraded sources
    partial = 0
    observations = []

    # daily runs sweep all tiers; otherwise cap at max_tier (frequent 15-min
    # runs use tier 1 only to stay fast/cheap; hourly runs do tier 1+2).
    allowed_max = 3 if daily else max_tier
    for cfg in sources:
        tier = cfg.get("tier", 1)
        if tier > allowed_max:
            continue
        src = load_source(cfg)
        if src is None:
            continue  # scraper not implemented yet (Phase 2/3)
        polled += 1
        try:
            offers = src.fetch()
        except Exception as e:  # one broken site must not kill the run (spec §8)
            failures += 1
            problems.append(f"{cfg['id']}: {e}"[:200])
            print(f"[fetch-fail] {cfg['id']}: {e}", file=sys.stderr)
            continue
        if getattr(src, "degraded", None):
            partial += 1
            problems.append(f"{cfg['id']} partial: {'; '.join(src.degraded)}"[:200])
        for o in offers:
            o.ts = now
            apply_cashback(o)
            finalize(o, cdi)
            if o.cashback:            # recompute PV once cashback is folded in
                o.pv = pv(o, cdi)
            store.insert_observation(conn, o)
            observations.append(o)
        # A source that saw its whole offer list (Amazon's AOD) tells us which
        # listings sold out: they vanish instead of showing unavailable.
        if getattr(src, "complete", False):
            gone = store.mark_missing_out_of_stock(
                conn, cfg["id"], {o.listing_id for o in offers}, now)
            if gone:
                print(f"[sold-out] {cfg['id']}: {', '.join(gone)}")

    alerts = triggers.evaluate(conn, observations, now, enable_new_low=enable_new_low)
    if alerts and dry_run:
        # Local testing: show what would fire, send nothing, don't touch dedup state.
        print(f"[dry-run] {len(alerts)} alert(s) would send (no messages sent):")
        for a in alerts:
            print("  " + notify.format_alert(a).replace("\n", " | "))
    elif alerts:
        report = notify.notify(alerts)
        for a in alerts:
            store.record_alert(conn, a.obs.listing_id, a.trigger, a.obs.pv, now)
        print(f"[alerts] sent={report['sent']} wa_ok={report['wa_ok']} "
              f"ntfy_ok={report['ntfy_ok']}")
    else:
        print("[alerts] none")

    # --- Phase 4b: promotions scan (best-effort; never breaks the poll) ---
    coupon_errors = 0
    if scan_promos:
        _scan_promotions(conn, now, dry_run)
        coupon_errors = _scan_coupons(conn, now, dry_run, observations)

    store.insert_run_log(conn, now, polled, failures, partial, coupon_errors,
                         " | ".join(problems))
    conn.commit()
    conn.close()
    result = {"polled": polled, "failures": failures, "partial": partial,
              "observations": len(observations), "alerts": len(alerts)}
    print(f"[poll] {result}")
    return result


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

    ceiling = triggers.PRIMARY   # only alert genuinely cheap promos (<= R$4.300)
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
        for pr in fresh:
            store.record_alert(conn, pr.url, "promo", pr.price, now)
        print(f"[promos] sent={rep['sent']} wa_ok={rep['wa_ok']} ntfy_ok={rep['ntfy_ok']}")
    else:
        print(f"[promos] scanned={len(promos)} none new (cheap+active)")


COUPON_MIN_VALUE = 100      # R$; codes worth less at the phone's price stay silent
COUPON_CAP = 5              # max new codes pinged per run (aggregators list ~60)
DEFAULT_AMAZON_PRICE = 3990  # reference order value when no Amazon offer is known


def _amazon_reference(conn, observations: list, now: str) -> tuple[float, float] | None:
    """(card, pix) price of the cheapest eligible Amazon offer: this run's if
    Amazon answered, else the latest snapshot from the last 2 days."""
    amz = [o for o in observations
           if o.venue == "amazon" and o.in_stock and o.list_price
           and (o.grade_rank or 0) >= MIN_RANK]
    if amz:
        o = min(amz, key=lambda x: x.list_price - (x.pix_discount or 0))
        return o.list_price, o.list_price - (o.pix_discount or 0)
    since = (datetime.fromisoformat(now) - timedelta(days=2)).isoformat()
    row = store.best_current_pv(conn, venue="amazon", since=since)
    if row is not None and row["list_price"]:
        return row["list_price"], row["list_price"] - (row["pix_discount"] or 0)
    return None


def _scan_coupons(conn, now: str, dry_run: bool, observations: list) -> int:
    """Amazon checkout codes + cashback portals (daily, best-effort). Returns
    how many coupon/cashback sites failed, for the run log.

    Codes are stored and pinged once each when worth >= COUPON_MIN_VALUE on the
    current Amazon price; they never change PV (eligibility for marketplace
    refurb sellers can only be confirmed in a logged-in cart). A cashback portal
    pings when it turns on (first time, or again after being seen off) or
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
    fresh = [(c, v) for c, v in
             coupons_mod.relevant_codes(codes, price, min_value=COUPON_MIN_VALUE)
             if store.last_alert(conn, f"coupon:{c.key}", "coupon",
                                 within_hours=None, now=now) is None]
    if len(fresh) > COUPON_CAP:  # most valuable first; the rest wait for a later run
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
        if rep["wa_ok"] or rep["ntfy_ok"]:   # undelivered -> retried next run
            for lid, trig, val in sent:
                store.record_alert(conn, lid, trig, val, now)
        print(f"[coupons] sent={rep['sent']} wa_ok={rep['wa_ok']} ntfy_ok={rep['ntfy_ok']}")
    return len(errors) + len(cb_errors)


def _amazon_summary(conn, now: str) -> str | None:
    """Heartbeat line: today's best Amazon refurb price, Pix vs card, and the
    best stored checkout code at that price."""
    ref = _amazon_reference(conn, [], now)
    if ref is None:
        return None
    card, pix = ref
    since = (datetime.fromisoformat(now) - timedelta(days=2)).isoformat()
    rows = conn.execute("SELECT * FROM coupon_codes WHERE active=1 AND last_seen >= ?",
                        (since,)).fetchall()
    codes = [coupons_mod.CouponCode.from_row(r) for r in rows]
    bt = coupons_mod.best_total(card, pix, coupons_mod.relevant_codes(codes, card))
    line = f"🛒 Amazon: Pix {notify.money(pix)} · cartão {notify.money(card)}"
    if bt["best_code"] is not None and bt["code_total"] < pix:  # codes don't stack with Pix
        line += (f" · c/ cupom {bt['best_code'].key}: {notify.money(bt['code_total'])}"
                 " (não verificado)")
    return line


def heartbeat() -> dict:
    conn = store.connect(config.DB_PATH)
    store.init_db(conn)
    now = store.now_iso()
    # Only listings seen in the last 2 days count as "current" — a source that
    # has been failing for days must not keep reporting its old best price.
    since = (datetime.fromisoformat(now) - timedelta(days=2)).isoformat()
    row = store.best_current_pv(conn, since=since)
    best = row["pv"] if row else None
    polled = sum(1 for c in load_registry() if load_source(c) is not None)
    tot = store.run_log_totals(conn, now)
    lines = []
    if tot["partial"] or tot["coupon_errors"]:
        lines.append(f"⚠️ 24h: {tot['partial']} fetch(es) parcial(is), "
                     f"{tot['coupon_errors']} falha(s) em sites de cupom")
    try:
        amazon = _amazon_summary(conn, now)
    except Exception as e:  # the heartbeat itself must always go out
        print(f"[heartbeat] amazon summary failed: {e}", file=sys.stderr)
        amazon = None
    if amazon:
        lines.insert(0, amazon)
    conn.close()
    return notify.send_heartbeat(best, polled, tot["failures"],
                                 extra="\n".join(lines) or None)


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
                   help="skip the deal-aggregator promotions scan")
    p.add_argument("--tier1", action="store_true",
                   help="poll only tier-1 sources (fast 15-min cadence)")
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
        return 0

    # default action
    poll(daily=args.daily, enable_new_low=args.enable_new_low,
         dry_run=args.dry_run, scan_promos=not args.no_promos,
         max_tier=1 if args.tier1 else 2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
