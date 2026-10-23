"""Data models.

An ``Observation`` maps 1:1 to a row in the ``observations`` table (spec §5).
Scrapers build one with the raw scraped fields; ``normalize.finalize`` fills in
``grade_rank`` and ``pv``. ``portal``, ``manual_review`` and ``promos`` are
transient (used for alert text / routing) and are not persisted.

Amazon pricing fields keep what the page shows, unmerged: ``pix_discount`` is
the Pix/NuPay promo and ``coupon`` the best *confirmed* coupon (one Amazon
displays on the offer, or a code validated in a logged-in cart, named by
``coupon_code``). The two never stack at checkout; ``normalize`` applies that
rule, so the raw values stay available for the 12x total (which only the
coupon lowers).

``OfferSummary`` is the product page's "other offers" line (count + lowest
price, no seller), or the marker that a page showed none, stored in
``offer_summary``.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields


@dataclass
class Observation:
    # --- identity / provenance ---
    source_id: str
    seller: str
    venue: str                      # own_site | magalu | amazon | ml | americanas
    listing_id: str
    url: str
    ts: str | None = None           # ISO8601 UTC; set by the orchestrator

    # --- product ---
    sku: str | None = None          # must resolve to SM-S938B
    storage_gb: int | None = None
    color: str | None = None
    grade_raw: str | None = None
    grade_rank: int | None = None   # normalized, spec §2
    battery_band: str | None = None

    # --- pricing (raw) ---
    list_price: float | None = None
    pix_discount: float = 0.0
    coupon: float = 0.0
    coupon_code: str | None = None  # validated code behind ``coupon``; None = Amazon-shown
    cashback: float = 0.0
    installments_n: int | None = None
    installment_value: float | None = None
    interest_flag: bool | None = None
    in_stock: bool = False

    # --- computed ---
    pv: float | None = None         # present value, spec §3

    # --- transient (not persisted) ---
    portal: str | None = field(default=None)          # cashback portal used
    manual_review: bool = field(default=False)         # Trocafy rank-2 case
    # Promotion messages Amazon shows on the offer ("à vista no Pix ou NuPay
    # (5% off)", "Aplicar cupom de R$ 200"); feeds the new-promotion alert.
    promos: list[str] = field(default_factory=list)
    # message -> Amazon promotion id ("amzn1.promotion.<ID>"), when the page
    # exposes one; its public terms (/promotion/tnc/<ID>) enrich the alert.
    promo_ids: dict[str, str] = field(default_factory=dict)

    # Column order for the observations table insert.
    PERSISTED = (
        "ts", "source_id", "seller", "venue", "listing_id", "url", "sku",
        "storage_gb", "color", "grade_raw", "grade_rank", "battery_band",
        "list_price", "pix_discount", "coupon", "coupon_code", "cashback",
        "installments_n", "installment_value", "interest_flag", "in_stock", "pv",
    )

    def as_row(self) -> dict:
        return {name: getattr(self, name) for name in self.PERSISTED}

    @classmethod
    def from_row(cls, row) -> "Observation":
        known = {f.name for f in fields(cls)}
        data = {k: row[k] for k in row.keys() if k in known}
        return cls(**data)


@dataclass
class OfferSummary:
    """Amazon's "Comparar outras N ofertas a partir de R$ X (+R$ Y de envio)"
    line on a product page (``#aod-ingress-link``), one per ASIN per poll.

    Why it exists: from GitHub Actions IPs the All Offers panel always answers
    HTTP 503, so the server can name only the buybox seller. This line still
    loads with the product page and says how many offers the ASIN has
    (``offers_total`` counts *all* of them, the buybox included) and the
    lowest *card* price among them — enough to notice a cheaper offer whose
    seller and condition we can't see. It is deliberately not an Observation:
    with no seller or grade it can't be a listing, so it never enters the
    price thresholds; it feeds the 🔍 alert (``triggers.unseen_offers``), the
    ``offer_summary`` table and the heartbeat instead.

    A product page that loads fine *without* the line (a listing whose only
    offer is the buybox shows none) is recorded too, as a marker with scope
    ``NO_LINE``: 1 offer at the buybox's card price (0 offers when the buybox
    is unavailable). Without it, a line that vanished would read as still
    there — stale counts in the heartbeat, and a 🔍 ladder that never learns
    the hidden offer left (``store.last_offer_departure``).
    """
    asin: str
    offers_total: int
    min_card_price: float | None = None   # None: the line showed no price
    min_shipping: float | None = None     # "+R$ 13,22 de envio"; 0.0 for GRÁTIS; None: not shown
    # "ALL" | "NEW" | "USED" (the link's condition=), None when the link names
    # none, NO_LINE for the no-line marker.
    scope: str | None = None
    ts: str | None = None                 # set by the orchestrator

    # --- transient (from the page title; for alert/heartbeat text) ---
    storage_gb: int | None = None
    color: str | None = None
    source_id: str | None = None

    PERSISTED = ("ts", "asin", "offers_total", "min_card_price", "min_shipping", "scope")
    NO_LINE = "NONE"

    @property
    def no_line(self) -> bool:
        """Is this the no-line marker (the page showed no other offers)?"""
        return self.scope == self.NO_LINE

    @property
    def min_total(self) -> float | None:
        """The cheapest offer as the user would pay it: card price + its
        shipping (none shown counts as free)."""
        if self.min_card_price is None:
            return None
        return round(self.min_card_price + (self.min_shipping or 0.0), 2)

    def as_row(self) -> dict:
        return {name: getattr(self, name) for name in self.PERSISTED}


@dataclass
class Promo:
    """A promotion post found on a deal-aggregator (Phase 4b).

    A looser signal than an Observation: free-text deal posts, often for new
    (not refurb) units, with no normalized grade. Used for the 'promo' alert.
    """
    source: str          # aggregator domain
    title: str
    url: str
    price: float | None = None
    ts: str | None = None
