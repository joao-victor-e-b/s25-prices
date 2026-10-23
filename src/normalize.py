"""Grade normalization (spec §2), present-value computation (spec §3) and the
12x card total the alerts are written in (``card_total``)."""
from __future__ import annotations

import re
import unicodedata

from .models import Observation

MIN_RANK = 3  # accept rank >= 3 (spec §2); alerts use triggers.ALERT_MIN_RANK

# Generic labels that mean "open box" regardless of seller -> rank 5 (spec §2).
_GENERIC = [
    ("novo reembalado", 5),
    ("reembalado", 5),
    ("open box", 5),
    ("vitrine", 5),
]

# Fallback for the common Portuguese condition words, used only when no
# seller-specific table matched (e.g. Amazon marketplace offers whose shipper
# isn't one of the known sellers). Specific phrases first ("muito bom" > "bom").
_GENERIC_GRADES = [
    ("como novo", 4),
    ("muito bom", 3),
    ("excelente", 4),
    ("premium", 4),
    ("aceitavel", 1),
    ("bom", 2),
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
    if g == "novo":       # a new unit (Amazon AOD heading "Novo") — not "Seminovo"
        return 5
    for kw, rank in _GENERIC:
        if kw in g:
            return rank
    key = _seller_key(seller)
    if key:
        for kw, rank in _GRADE_TABLE[key]:
            if kw in g:
                return rank
    # fall back to the universal condition vocabulary
    for kw, rank in _GENERIC_GRADES:
        if kw in g:
            return rank
    return None


def is_manual_review(seller: str | None, rank: int | None) -> bool:
    """Trocafy has no clean rank-3; treat their rank-2 as manual review, not
    auto-reject, since they're the price leader (spec §2)."""
    return _seller_key(seller) == "trocafy" and rank == 2


def card_total(o: Observation) -> float | None:
    """What the card/12x plan totals after confirmed coupons: list − coupon.

    This is the number the user's alert thresholds are written in (they pay in
    12x). The Pix/NuPay promo is deliberately absent — it only applies to a
    one-off Pix payment, never to installments — while a coupon (Amazon-shown
    or validated in the cart) comes off the order total and so off the plan.
    """
    if o.list_price is None:
        return None
    return round(o.list_price - (o.coupon or 0.0), 2)


def pix_price(o: Observation) -> float | None:
    """The one-off Pix price: list − the larger of the Pix promo and the coupon.

    Amazon's Pix/NuPay promo "não pode ser combinada com outras ofertas": the
    bigger discount applies, never both.
    """
    if o.list_price is None:
        return None
    return round(o.list_price - max(o.pix_discount or 0.0, o.coupon or 0.0), 2)


def _pv_amazon(o: Observation, cdi_monthly: float) -> float | None:
    """Amazon PV: Pix path with the no-stack rule (see ``pix_price``), or the
    card total (list − coupon) spread evenly over the plan and discounted at
    CDI. The plan is rebuilt from the card total instead of Amazon's
    installment_value because a coupon lowers the installments too."""
    cash = pix_price(o)
    if cash is None:
        return None
    candidates = [cash - (o.cashback or 0.0)]
    total = card_total(o)
    if o.installments_n and total is not None:
        per = total / o.installments_n
        candidates.append(sum(per / (1 + cdi_monthly) ** k
                              for k in range(1, o.installments_n + 1))
                          - (o.cashback or 0.0))
    return round(min(candidates), 2)


def pv(o: Observation, cdi_monthly: float) -> float | None:
    """Present value in R$ today (spec §3).

    PIX path: list minus all instant discounts and cashback.
    Installment path (sem juros or com juros both discount the nominal stream
    at CDI): sum of installment values discounted per month, then cashback.
    Return the cheaper of the two available paths. Amazon has its own rules
    (``_pv_amazon``): its Pix promo and coupons don't stack.
    """
    if o.venue == "amazon":
        return _pv_amazon(o, cdi_monthly)
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


# --- Coupon extraction (spec §3 `coupon` field) -------------------------------
# Gated on an explicit coupon word so a PIX "% de desconto" is never mistaken for
# a coupon (which would double-count against pix_discount). Absolute R$ patterns
# and coupon-scoped % patterns only. "Insira o código X" is Amazon's wording for
# a checkout code shown on the offer ("R$ 900 off. Insira o código SMART900").
_COUPON_WORDS = ("cupom", "cupons", "economize", "codigo")
_COUPON_ABS = [
    r"economize\s+r\$\s*([\d.,]+)",
    r"cupom[^r%]{0,25}r\$\s*([\d.,]+)",
    r"r\$\s*([\d.,]+)\s+de\s+desconto",
    r"r\$\s*([\d.,]+)\s*off\b",
]
# Terms that bound a coupon: a minimum order above the price voids it, a cap
# limits a percentage ("10% (até R$ 100)").
_COUPON_MIN = r"(?:acima\s+de|a\s+partir\s+de|minimo\s+de|compras\s+de)\s*r\$\s*([\d.,]+)"
_COUPON_CAP = r"(?:ate|limitado\s+a|maximo\s+de)\s*r\$\s*([\d.,]+)"
_COUPON_PCT = [
    r"cupom[^%]{0,25}?(\d{1,2})\s*%",   # non-greedy so "10%" isn't read as "0%"
    r"(\d{1,2})\s*%[^%]{0,25}cupom",
]


def _brl_num(s: str) -> float | None:
    s = s.strip()
    s = s.replace(".", "").replace(",", ".") if "," in s else s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def coupon_from_text(text: str | None, list_price: float | None = None) -> float:
    """Parse a coupon value (R$ off) from a scoped snippet, or 0.0.

    Requires an explicit coupon word. Absolute amounts win; percent is applied to
    list_price. Returns 0.0 for PIX/other non-coupon discounts.
    """
    if not text:
        return 0.0
    t = _norm(text)
    if not any(w in t for w in _COUPON_WORDS):
        return 0.0
    m = re.search(_COUPON_MIN, t)
    if m and list_price and (_brl_num(m.group(1)) or 0) > list_price:
        return 0.0   # the order doesn't reach the coupon's minimum
    for pat in _COUPON_ABS:
        m = re.search(pat, t)
        if m:
            val = _brl_num(m.group(1))
            if val:
                return round(val, 2)
    if list_price:
        for pat in _COUPON_PCT:
            m = re.search(pat, t)
            if m:
                val = list_price * int(m.group(1)) / 100
                cap = re.search(_COUPON_CAP, t)
                if cap and _brl_num(cap.group(1)):
                    val = min(val, _brl_num(cap.group(1)))
                return round(val, 2)
    return 0.0


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
