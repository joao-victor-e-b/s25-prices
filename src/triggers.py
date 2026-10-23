"""Alert triggers (spec §6) with drop-based deduplication.

Price thresholds are written in the number the user actually pays: the 12x
card total after confirmed coupons (``normalize.card_total``), not PV — they
buy in installments, where the Pix promo doesn't apply. A coupon only counts
once confirmed (Amazon shows it on the offer, or a code was validated in the
cart; see coupon_checks.py), so a threshold alert never rests on a guess.

All triggers except ``trocafy_review`` require in_stock and grade_rank >=
ALERT_MIN_RANK (4: Amazon's Premium/Excelente; Bom/Aceitável don't count).
Dedup: the price thresholds (🔴/🟠/🟡) are one ladder per listing: a new
threshold alert needs a 12x total at least DEDUP_DELTA_12X below the lowest
one already sent for the listing since it was last stored sold out, unless it
climbs into a stronger level than any sent; a rise never alerts. Other
triggers repeat only on a DEDUP_PV_DELTA PV drop. A listing that sold out and
came back starts fresh (single-unit refurb offers come and go). 🟢 restock is
judged on what was sent, not on the previous stored row (``_restocked``), so
a poll that stores a return without announcing it can't swallow it.

The multi-store triggers (new_low, grade_upgrade, venue_spread,
trocafy_review) never apply to Amazon offers: in Amazon-only mode they would
fire on months of other stores' history and on grade-2 offers.

🔍 ``unseen_offers`` works on the product page's "N ofertas a partir de R$ X"
line instead of on observations: while Amazon 503s the offers panel from CI,
a cheap seller other than the buybox is only visible there, nameless and
ungraded. It gets its own alert (and its own per-ASIN ladder) rather than a
threshold alert, because nothing says the offer is Excelente — the user has to
look. An offer we already know — seen this poll, or stored in stock within
UNSEEN_KNOWN_HOURS (the panel works on and off in CI, and the buybox rotates)
— never passes for a hidden one, and an offer already announced by name
(🔴/🟠/🟡, e.g. by the PC watcher, whose panel works) is a rung of the 🔍
ladder.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from . import store
from .models import Observation, OfferSummary
from .normalize import card_total

# 12x card-total thresholds (R$), set by the user Oct 2026.
BUY_NOW_12X = 3500    # 🔴 COMPRE
GOOD_12X = 3800       # 🟠 bom preço
YEAR_LOW_12X = 3919   # 🟡 beats the 2026 low (Assurant 256GB Excelente, 12x R$326,62)

ALERT_MIN_RANK = 4    # Excelente/Premium and better (normalize.MIN_RANK stays 3)
# A restock is only worth a ping near the user's range: new-unit listings carry
# many sellers at R$ 6-10k that come and go (the old R$ 4.300 "primary" level).
RESTOCK_MAX_12X = 4300
# ...and only while the return is recent: an unannounced restock (the cloud
# stood by while the PC was away, an outage) is still sent this long after
# the listing came back — more than run.LOCAL_FRESH_MIN plus a 30-min sync.
RESTOCK_WINDOW = timedelta(hours=2)

DEDUP_PV_DELTA = 100   # R$; non-threshold triggers re-fire only on a PV drop this big
DEDUP_DELTA_12X = 50   # R$; threshold triggers re-fire on a 12x-total drop this big

THRESHOLD_TRIGGERS = ("instant_buy", "aggressive", "year_low")
# Ladder strength: moving into a stronger level always alerts.
_STRENGTH = {"year_low": 1, "aggressive": 2, "instant_buy": 3}

PRIORITY = {
    "instant_buy": "🔴",
    "aggressive": "🟠",
    "year_low": "🟡",
    "new_low": "🟡",
    "restock": "🟢",
    "venue_spread": "🟢",
    "grade_upgrade": "🟢",
    "trocafy_review": "🟢",
    "unseen_offer": "🔍",
}

# 🔍: an offers-line price must undercut every attributed offer by more than
# this. A card price rebuilt from the 12x plan can sit cents above Amazon's own
# (12 x R$ 381,90 = 4.582,80 for a 4.582,47 offer); the same offer must not
# read as a hidden one.
UNSEEN_MARGIN = 1.0
# 🔍: an offer stored in stock this recently still counts as known when this
# poll couldn't see it (offers panel blocked, buybox moved to another seller).
# Every listing seen is stored at least hourly, so two hours covers a missed
# panel or two; an offer gone longer must not hide a new one behind the line.
UNSEEN_KNOWN_HOURS = 2


@dataclass
class Alert:
    trigger: str
    priority: str
    obs: Observation
    note: str = ""
    # What the alert was judged on and what dedup compares/records: the 12x
    # card total for thresholds, PV for everything else (filled in if unset).
    metric: float | None = None

    def __post_init__(self) -> None:
        if self.metric is None:
            self.metric = (card_total(self.obs) if self.trigger in THRESHOLD_TRIGGERS
                           else self.obs.pv)


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


def eligible(o: Observation) -> bool:
    """In stock at an alertable grade (Excelente/Premium or better)."""
    return bool(o.in_stock) and (o.grade_rank or 0) >= ALERT_MIN_RANK


def threshold_level(total: float | None) -> str | None:
    """The strongest 12x threshold a total reaches (they overlap by
    construction: anything under BUY_NOW_12X is also under GOOD_12X and
    YEAR_LOW_12X), or None above all of them."""
    if total is None:
        return None
    if total <= BUY_NOW_12X:
        return "instant_buy"
    if total <= GOOD_12X:
        return "aggressive"
    if total <= YEAR_LOW_12X:
        return "year_low"
    return None


def _threshold_alert(o: Observation) -> Alert | None:
    """Fire only the strongest matching 12x threshold."""
    total = card_total(o)
    level = threshold_level(total)
    return Alert(level, PRIORITY[level], o, metric=total) if level else None


def evaluate(conn, observations: list[Observation], now: str,
             enable_new_low: bool = False) -> list[Alert]:
    """Return the alerts that should be sent this cycle (post-dedup)."""
    candidates: list[Alert] = []

    # --- per-listing triggers ---
    for o in observations:
        if eligible(o):
            # 1-2: strongest 12x threshold
            ta = _threshold_alert(o)
            if ta:
                candidates.append(ta)

            # 5: restock, near the user's range (``_restocked``)
            total = card_total(o)
            if (total is not None and total <= RESTOCK_MAX_12X
                    and _restocked(conn, o.listing_id, now)):
                candidates.append(Alert("restock", PRIORITY["restock"], o,
                                        "voltou ao estoque"))

            if o.venue == "amazon":
                continue   # the multi-store triggers below don't apply (module doc)

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
        total = card_total(o)
        if (o.venue != "amazon" and o.manual_review and o.in_stock
                and total is not None and total <= BUY_NOW_12X):
            candidates.append(Alert("trocafy_review", PRIORITY["trocafy_review"], o,
                                    "Trocafy rank-2 — revisar manualmente"))

    # --- cross-listing trigger 6: venue_spread ---
    # Same seller + grade on *different* venues only: one venue can carry the
    # same seller twice (Amazon's 256GB and 512GB listings), which is no spread.
    groups: dict[tuple, list[Observation]] = {}
    for o in observations:
        if eligible(o) and o.pv is not None and o.venue != "amazon":
            groups.setdefault((o.seller, o.grade_rank), []).append(o)
    for _key, offers in groups.items():
        lo = min(offers, key=lambda x: x.pv)
        elsewhere = [x for x in offers if x.venue != lo.venue]
        if not elsewhere:
            continue
        hi = max(elsewhere, key=lambda x: x.pv)
        if hi.pv - lo.pv >= 200:
            candidates.append(Alert("venue_spread", PRIORITY["venue_spread"], lo,
                                    f"spread R$ {hi.pv - lo.pv:.0f} vs {hi.venue}"))

    return _dedup(conn, _fold_restock(candidates), now)


def _restocked(conn, listing_id: str, now: str) -> bool:
    """Is the (in-stock) listing back from a sell-out that nobody announced
    yet? Level-based, not an edge on the previous stored row: it was stored
    sold out, no restock or threshold alert (``_fold_restock`` records the
    threshold trigger) was delivered for it since, and it came back within
    RESTOCK_WINDOW. A poll that stores the in-stock row without announcing it
    (the cloud's standby, an outage) therefore can't swallow the 🟢: the next
    poll that sends still sees it. The window keeps a listing in stock for
    days that drifts under RESTOCK_MAX_12X from passing for a return."""
    oos = store.last_out_of_stock(conn, listing_id, now)
    if oos is None or store.alerted_since(conn, listing_id,
                                          ("restock",) + THRESHOLD_TRIGGERS, oos, now):
        return False
    back = store.first_in_stock_after(conn, listing_id, oos, now) or now
    try:
        return datetime.fromisoformat(now) - datetime.fromisoformat(back) <= RESTOCK_WINDOW
    except (TypeError, ValueError):
        return False


def _fold_restock(candidates: list[Alert]) -> list[Alert]:
    """A listing that returns at a COMPRE/BOM PREÇO price gets one message: the
    threshold alert, noted "voltou ao estoque", instead of two."""
    threshold = {a.obs.listing_id: a for a in candidates if a.trigger in THRESHOLD_TRIGGERS}
    kept = []
    for a in candidates:
        t = threshold.get(a.obs.listing_id)
        if a.trigger == "restock" and t is not None:
            t.note = f"{t.note}; {a.note}" if t.note else a.note
            continue
        kept.append(a)
    return kept


def _threshold_repeat(conn, a: Alert, now: str) -> bool:
    """Is this threshold alert a repeat? Compared against every threshold alert
    sent for the listing since it was last stored sold out: it passes only
    with a 12x total DEDUP_DELTA_12X below the lowest of them, or when its
    level is stronger than any of them (🟡 -> 🟠 -> 🔴). A rise to a weaker
    level (🔴 3.499 -> 🟠 3.700) is never news."""
    since = store.last_out_of_stock(conn, a.obs.listing_id, now)
    priors = store.threshold_alerts(conn, a.obs.listing_id, THRESHOLD_TRIGGERS, since, now)
    if not priors:
        return False
    if _STRENGTH[a.trigger] > max(_STRENGTH.get(t, 0) for t, _ in priors):
        return False
    values = [v for _, v in priors if v is not None]
    if not values or a.metric is None:
        return True   # can't confirm a drop -> suppress
    return a.metric > min(values) - DEDUP_DELTA_12X


def _dedup(conn, candidates: list[Alert], now: str) -> list[Alert]:
    """Suppress repeats. Thresholds: see ``_threshold_repeat``. Others: a
    repeat (listing_id, trigger) fires only after a DEDUP_PV_DELTA PV drop. No
    time window — a rise or a repeat at the same price never re-fires — but a
    listing stored as sold out since its last alert starts fresh."""
    kept: list[Alert] = []
    seen_this_cycle: set[tuple] = set()
    for a in candidates:
        key = (a.obs.listing_id, a.trigger)
        if key in seen_this_cycle:
            continue
        seen_this_cycle.add(key)

        if a.trigger in THRESHOLD_TRIGGERS:
            if not _threshold_repeat(conn, a, now):
                kept.append(a)
            continue

        prior = store.last_alert(conn, a.obs.listing_id, a.trigger,
                                 within_hours=None, now=now)
        if prior is not None and store.out_of_stock_since(conn, a.obs.listing_id,
                                                          prior["ts"], now):
            prior = None   # sold out since the last alert -> a fresh opportunity
        if prior is not None:
            prior_value = prior["pv"]
            if prior_value is None or a.metric is None:
                continue  # can't confirm a drop -> suppress repeat
            if a.metric > prior_value - DEDUP_PV_DELTA:
                continue  # not a big enough drop -> suppress repeat
        kept.append(a)
    return kept


# --- 🔍 unseen offer: the dp's offers line undercuts everything we can name ---

@dataclass
class UnseenOfferAlert:
    """The product page says an offer exists at ``metric`` (card + shipping,
    from the "a partir de" line) below every offer we know for the ASIN
    (this poll's, plus recently stored ones) — a seller/condition the server
    can't see while Amazon 503s the offers panel. ``level`` is the 12x
    threshold it reaches."""
    summary: OfferSummary
    level: str                       # instant_buy | aggressive | year_low
    metric: float                    # what the ladder compares and records
    attributed: int = 0              # offers we know for the ASIN
    attributed_min: float | None = None   # their lowest card price
    trigger: str = "unseen_offer"
    priority: str = PRIORITY["unseen_offer"]

    @property
    def key(self) -> str:
        """alerts_sent.listing_id: one ladder per ASIN."""
        return f"unseen:{self.summary.asin}"


def unseen_offers(conn, summaries: list[OfferSummary], observations: list[Observation],
                  now: str) -> list[UnseenOfferAlert]:
    """🔍 alerts to send this cycle (post-dedup). Fires when an offers line's
    cheapest offer (``min_total``) is within YEAR_LOW_12X and more than
    UNSEEN_MARGIN below every offer known for that ASIN — at any grade or
    stock state, so the buybox (already judged by the thresholds) or a known
    Aceitável offer never re-alert as "unseen". Known means attributed this
    poll, or stored in stock within UNSEEN_KNOWN_HOURS (``store.recent_offers``):
    the panel loads on and off in CI and the buybox rotates between sellers,
    so an offer named four minutes ago is no secret when this poll can't name
    it. With nothing known (no buybox, panel blocked) any qualifying price
    fires. A no-line marker never does: it says the buybox is all there is."""
    out: list[UnseenOfferAlert] = []
    seen: set[str] = set()
    since = (datetime.fromisoformat(now) - timedelta(hours=UNSEEN_KNOWN_HOURS)).isoformat()
    for s in summaries:
        total = s.min_total
        level = threshold_level(total)
        if s.no_line or level is None or s.asin in seen:
            continue
        seen.add(s.asin)
        known = {o.listing_id: o.list_price for o in observations
                 if o.venue == "amazon" and o.list_price is not None
                 and o.listing_id.split(":", 1)[0] == s.asin}
        for r in store.recent_offers(conn, s.asin, since, now):
            known.setdefault(r["listing_id"], r["list_price"])   # this poll's price wins
        low = min(known.values(), default=None)
        if low is not None and total >= low - UNSEEN_MARGIN:
            continue   # the line's minimum is an offer we already know
        a = UnseenOfferAlert(s, level, total, len(known), low)
        if not _unseen_repeat(conn, a, now):
            out.append(a)
    return out


def _named_alert_values(conn, asin: str, since: str | None, now: str) -> list[float]:
    """12x totals of the price-threshold alerts (🔴/🟠/🟡) sent for the ASIN's
    named listings (``<ASIN>:<seller>:<grade>``) after ``since`` and up to
    ``now``, leaving out listings stored sold out since their alert. A cheap
    offer the PC watcher's panel named and announced stays behind the "a
    partir de" line after the PC stops, unnamed from the cloud: once its
    stored row is UNSEEN_KNOWN_HOURS old it must not come back as 🔍."""
    marks = ",".join("?" * len(THRESHOLD_TRIGGERS))
    rows = conn.execute(
        f"SELECT listing_id, ts, pv FROM alerts_sent WHERE listing_id LIKE ? "
        f"AND trigger IN ({marks}) AND (? IS NULL OR ts > ?) AND ts <= ?",
        (f"{asin}:%", *THRESHOLD_TRIGGERS, since, since, now)).fetchall()
    return [r[2] for r in rows if r[2] is not None
            and not store.out_of_stock_since(conn, r[0], r[1], now)]


def _unseen_repeat(conn, a: UnseenOfferAlert, now: str) -> bool:
    """The threshold ladder (``_threshold_repeat``) per ASIN: compared with the
    🔍 alerts sent since a reading last showed an offer leave (the page lost
    its line, or the count fell with nothing left within YEAR_LOW_12X —
    ``store.last_offer_departure``, the analogue of a sell-out), it passes
    only DEDUP_DELTA_12X below the lowest of them or at a stronger level; a
    rise never alerts, and neither does a price bouncing across the ceiling.
    Threshold alerts already sent for the ASIN's named offers since then
    (``_named_alert_values``) count as rungs too: an offer's 🔍 metric (card
    + shipping) is its 12x total or more, give or take the cents of a plan
    rebuilt from installments, so an offer already announced by name stays
    quiet, while one DEDUP_DELTA_12X cheaper or at a stronger level still
    fires."""
    since = store.last_offer_departure(conn, a.summary.asin, YEAR_LOW_12X, now)
    priors = store.threshold_alerts(conn, a.key, (a.trigger,), since, now)
    named = _named_alert_values(conn, a.summary.asin, since, now)
    if not priors and not named:
        return False
    values = [v for _, v in priors if v is not None] + named
    if not values:
        return True   # can't confirm a drop -> suppress
    if _STRENGTH[a.level] > max(_STRENGTH.get(threshold_level(v), 0) for v in values):
        return False
    return a.metric > min(values) - DEDUP_DELTA_12X
