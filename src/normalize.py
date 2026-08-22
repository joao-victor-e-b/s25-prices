"""Grade normalization (spec §2) and present-value computation (spec §3)."""
from __future__ import annotations

import unicodedata

from .models import Observation

MIN_RANK = 3  # accept rank >= 3 (spec §2)

# Generic labels that mean "open box" regardless of seller -> rank 5 (spec §2).
_GENERIC = [
    ("novo reembalado", 5),
    ("reembalado", 5),
    ("open box", 5),
    ("vitrine", 5),
]

# Per-seller taxonomy. Order matters: more specific substrings first so
# "muito bom" is matched before "bom" and "recondicionado pela marca" before
# "recondicionado".
_GRADE_TABLE: dict[str, list[tuple[str, int]]] = {
    "trocafone": [
        ("excelente", 4),
        ("muito bom", 3),
        ("bom", 2),
        ("outlet", 1),
    ],
    "trocafy": [
        ("sou como novo", 4),
        ("como novo", 4),
        ("marcas de uso", 2),
        ("fui mais usado", 1),
        ("mais usado", 1),
    ],
    "amazon seminovos": [
        ("premium", 4),
        ("excelente", 4),
        ("bom", 2),
        ("aceitavel", 1),
    ],
    "outlet do celular": [
        # Their real labels carry a Grade A/B/C letter (tier 1/2/3 -> rank 4/3/2).
        ("grade a", 4), ("como novo", 4),
        ("grade b", 3), ("usado com cuidado", 3),
        ("grade c", 2), ("marcas de uso", 2),
        ("tier 1", 4), ("tier 2", 3), ("tier 3", 2),
    ],
    "mercado livre": [
        ("recondicionado pela marca", 4),
        ("recondicionado", 2),
    ],
}

# Map a scraped seller string onto a table key.
_SELLER_KEYS = [
    "outlet do celular",
    "amazon seminovos",
    "mercado livre",
    "trocafone",
    "trocafy",
]


def _norm(s: str | None) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return s.lower().strip()


def _seller_key(seller: str | None) -> str | None:
    s = _norm(seller)
    for key in _SELLER_KEYS:
        if key in s:
            return key
    return None


def grade_rank(seller: str | None, grade_raw: str | None) -> int | None:
    """Normalize a raw grade label to an integer rank, or None if unknown."""
    g = _norm(grade_raw)
    if not g:
        return None
    for kw, rank in _GENERIC:
        if kw in g:
            return rank
    key = _seller_key(seller)
    if key:
        for kw, rank in _GRADE_TABLE[key]:
            if kw in g:
                return rank
    return None


def is_manual_review(seller: str | None, rank: int | None) -> bool:
    """Trocafy has no clean rank-3; treat their rank-2 as manual review, not
    auto-reject, since they're the price leader (spec §2)."""
    return _seller_key(seller) == "trocafy" and rank == 2


def pv(o: Observation, cdi_monthly: float) -> float | None:
    """Present value in R$ today (spec §3).

    PIX path: list minus all instant discounts and cashback.
    Installment path (sem juros or com juros both discount the nominal stream
    at CDI): sum of installment values discounted per month, then cashback.
    Return the cheaper of the two available paths.
    """
    effective_pix = (
        (o.list_price or 0.0)
        - (o.pix_discount or 0.0)
        - (o.coupon or 0.0)
        - (o.cashback or 0.0)
    )

    pv_inst: float | None = None
    if o.installments_n and o.installment_value:
        pv_inst = sum(
            o.installment_value / (1 + cdi_monthly) ** k
            for k in range(1, o.installments_n + 1)
        )
        pv_inst -= (o.cashback or 0.0)

    candidates = [x for x in (effective_pix, pv_inst) if x is not None]
    if not candidates:
        return None
    return round(min(candidates), 2)


def finalize(o: Observation, cdi_monthly: float) -> Observation:
    """Fill grade_rank, manual_review, and pv on an observation in place."""
    o.grade_rank = grade_rank(o.seller, o.grade_raw)
    o.manual_review = is_manual_review(o.seller, o.grade_rank)
    o.pv = pv(o, cdi_monthly)
    return o


# --- Cashback layer (spec §3). Per-(venue, portal) rate table. -----------------
# Left empty by default: build order §10 adds cashback last. Populate like:
#   CASHBACK_RATES = {"own_site": [("Meliuz", 0.05), ("Nubank", 0.03)]}
CASHBACK_RATES: dict[str, list[tuple[str, float]]] = {}


def apply_cashback(o: Observation, rates: dict | None = None) -> Observation:
    """Pick the best cashback portal for this venue and fold it into the offer.

    Cashback is modeled as a rate on list price (spec §3 example: R$4.300 list
    at 5% -> R$215 back). Sets o.cashback and o.portal; caller re-runs pv().
    """
    table = CASHBACK_RATES if rates is None else rates
    portals = table.get(o.venue) or []
    if not portals or not o.list_price:
        return o
    portal, rate = max(portals, key=lambda pr: pr[1])
    o.cashback = round(o.list_price * rate, 2)
    o.portal = portal
    return o
