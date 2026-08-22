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

import yaml

from . import config, notify, store, triggers
from .cdi import get_cdi_monthly
from .normalize import apply_cashback, finalize, pv
from .sources import load_source


def load_registry() -> list[dict]:
    with open(config.REGISTRY_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("sources", [])


def poll(daily: bool = False, enable_new_low: bool = False) -> dict:
    conn = store.connect(config.DB_PATH)
    store.init_db(conn)

    cdi = get_cdi_monthly()
    now = store.now_iso()

    sources = load_registry()
    polled = 0
    failures = 0
    observations = []

    for cfg in sources:
        tier = cfg.get("tier", 1)
        if tier >= 3 and not daily:
            continue
        src = load_source(cfg)
        if src is None:
            continue  # scraper not implemented yet (Phase 2/3)
        polled += 1
        try:
            offers = src.fetch()
        except Exception as e:  # one broken site must not kill the run (spec §8)
            failures += 1
            print(f"[fetch-fail] {cfg['id']}: {e}", file=sys.stderr)
            continue
        for o in offers:
            o.ts = now
            apply_cashback(o)
            finalize(o, cdi)
            if o.cashback:            # recompute PV once cashback is folded in
                o.pv = pv(o, cdi)
            store.insert_observation(conn, o)
            observations.append(o)

    alerts = triggers.evaluate(conn, observations, now, enable_new_low=enable_new_low)
    if alerts:
        report = notify.notify(alerts)
        for a in alerts:
            store.record_alert(conn, a.obs.listing_id, a.trigger, a.obs.pv, now)
        print(f"[alerts] sent={report['sent']} wa_ok={report['wa_ok']} "
              f"ntfy_ok={report['ntfy_ok']}")
    else:
        print("[alerts] none")

    conn.commit()
    conn.close()
    result = {"polled": polled, "failures": failures,
              "observations": len(observations), "alerts": len(alerts)}
    print(f"[poll] {result}")
    return result


def heartbeat() -> dict:
    conn = store.connect(config.DB_PATH)
    store.init_db(conn)
    row = store.best_current_pv(conn)
    best = row["pv"] if row else None
    polled = sum(1 for c in load_registry() if load_source(c) is not None)
    conn.close()
    return notify.send_heartbeat(best, polled, 0)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="S25 Ultra refurb monitor")
    p.add_argument("--init-db", action="store_true")
    p.add_argument("--test-alert", action="store_true")
    p.add_argument("--poll", action="store_true")
    p.add_argument("--heartbeat", action="store_true")
    p.add_argument("--daily", action="store_true", help="include tier-3 sources")
    p.add_argument("--enable-new-low", action="store_true",
                   help="enable trigger 4 (needs ~30d history)")
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
    poll(daily=args.daily, enable_new_low=args.enable_new_low)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
