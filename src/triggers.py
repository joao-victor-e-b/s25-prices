"""Alert triggers (spec §6) with 24h deduplication.

All triggers except ``trocafy_review`` require grade_rank >= 3 and in_stock.
Dedup: suppress a repeat (listing_id, trigger) within 24h unless PV moves
more than R$100 (spec §6).
"""
from __future__ import annotations

from dataclasses import dataclass

from . import store
from .models import Observation
from .normalize import MIN_RANK

# Price thresholds, calibrated Aug 2026 (spec §1). Re-calibrate PRIMARY monthly.
INSTANT_BUY = 3500
AGGRESSIVE = 3900
PRIMARY = 4300

DEDUP_PV_DELTA = 100  # R$; the same (listing, trigger) only re-fires on a drop this big

PRIORITY = {
    "instant_buy": "🔴",
    "aggressive": "🟠",
    "primary": "🟡",
    "new_low": "🟡",
    "restock": "🟢",
    "venue_spread": "🟢",
    "grade_upgrade": "🟢",
    "trocafy_review": "🟢",
}


@dataclass
class Alert:
    trigger: str
    priority: str
    obs: Observation
    note: str = ""


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _eligible(o: Observation) -> bool:
    return bool(o.in_stock) and (o.grade_rank or 0) >= MIN_RANK


def _threshold_alert(o: Observation) -> Alert | None:
    """Fire only the strongest matching price threshold to avoid triple alerts
    for one listing (triggers 1-3 overlap by construction)."""
    if o.pv is None:
        return None
    if o.pv <= INSTANT_BUY:
        return Alert("instant_buy", PRIORITY["instant_buy"], o)
    if o.pv <= AGGRESSIVE:
        return Alert("aggressive", PRIORITY["aggressive"], o)
    if o.pv <= PRIMARY:
        return Alert("primary", PRIORITY["primary"], o)
    return None


def evaluate(conn, observations: list[Observation], now: str,
             enable_new_low: bool = False) -> list[Alert]:
    """Return the alerts that should be sent this cycle (post-dedup)."""
    candidates: list[Alert] = []

    # --- per-listing triggers ---
    for o in observations:
        if _eligible(o):
            # 1-3: strongest price threshold
            ta = _threshold_alert(o)
            if ta:
                candidates.append(ta)

            # 5: restock (in_stock flips false -> true)
            prev = store.last_observation(conn, o.source_id, o.listing_id, now)
            if prev is not None and not prev["in_stock"]:
                candidates.append(Alert("restock", PRIORITY["restock"], o,
                                        "voltou ao estoque"))

            # 4: new_low (gated until ~30 days of history exists)
            if enable_new_low and o.pv is not None:
                series = store.pv_series(conn, o.seller, o.grade_rank, 90, now)
                if len(series) >= 20:
                    p05 = _percentile(series, 0.05)
                    if p05 is not None and o.pv < p05 and (p05 - o.pv) >= 150:
                        candidates.append(Alert("new_low", PRIORITY["new_low"], o,
                                                f"novo mínimo (p05 {p05:.0f})"))

            # 7: grade_upgrade (rank-4 at <= trailing median PV of rank-3)
            if (o.grade_rank or 0) >= 4 and o.pv is not None:
                r3 = store.pv_series(conn, o.seller, 3, 90, now)
                med = _percentile(r3, 0.5)
                if med is not None and o.pv <= med:
                    candidates.append(Alert("grade_upgrade", PRIORITY["grade_upgrade"],
                                            o, f"grade 4 ≤ mediana grade 3 ({med:.0f})"))

        # 8: trocafy_review — rank-2 exception, manual review not auto-reject
        if (o.manual_review and o.in_stock and o.pv is not None
                and o.pv <= INSTANT_BUY):
            candidates.append(Alert("trocafy_review", PRIORITY["trocafy_review"], o,
                                    "Trocafy rank-2 — revisar manualmente"))

    # --- cross-listing trigger 6: venue_spread ---
    groups: dict[tuple, list[Observation]] = {}
    for o in observations:
        if _eligible(o) and o.pv is not None:
            groups.setdefault((o.seller, o.grade_rank), []).append(o)
    for _key, offers in groups.items():
        if len(offers) < 2:
            continue
        lo = min(offers, key=lambda x: x.pv)
        hi = max(offers, key=lambda x: x.pv)
        if hi.pv - lo.pv >= 200:
            candidates.append(Alert("venue_spread", PRIORITY["venue_spread"], lo,
                                    f"spread R${hi.pv - lo.pv:.0f} vs {hi.venue}"))

    return _dedup(conn, candidates, now)


def _dedup(conn, candidates: list[Alert], now: str) -> list[Alert]:
    """Suppress a repeat (listing_id, trigger) unless the PV has dropped by at
    least DEDUP_PV_DELTA since it was last alerted. No time window — a rise or a
    repeat at the same price never re-fires; only a genuine drop does. Crossing
    into a stronger threshold still fires (it's a different trigger key)."""
    kept: list[Alert] = []
    seen_this_cycle: set[tuple] = set()
    for a in candidates:
        key = (a.obs.listing_id, a.trigger)
        if key in seen_this_cycle:
            continue
        seen_this_cycle.add(key)

        prior = store.last_alert(conn, a.obs.listing_id, a.trigger,
                                 within_hours=None, now=now)
        if prior is not None:
            prior_pv = prior["pv"]
            if prior_pv is None or a.obs.pv is None:
                continue  # can't confirm a drop -> suppress repeat
            if a.obs.pv > prior_pv - DEDUP_PV_DELTA:
                continue  # not a >=R$100 drop -> suppress repeat
        kept.append(a)
    return kept
