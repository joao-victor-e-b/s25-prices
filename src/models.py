"""Data models.

An ``Observation`` maps 1:1 to a row in the ``observations`` table (spec §5).
Scrapers build one with the raw scraped fields; ``normalize.finalize`` fills in
``grade_rank`` and ``pv``. ``portal`` and ``manual_review`` are transient
(used for alert text / routing) and are not persisted.
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

    # Column order for the observations table insert.
    PERSISTED = (
        "ts", "source_id", "seller", "venue", "listing_id", "url", "sku",
        "storage_gb", "color", "grade_raw", "grade_rank", "battery_band",
        "list_price", "pix_discount", "coupon", "cashback", "installments_n",
        "installment_value", "interest_flag", "in_stock", "pv",
    )

    def as_row(self) -> dict:
        return {name: getattr(self, name) for name in self.PERSISTED}

    @classmethod
    def from_row(cls, row) -> "Observation":
        known = {f.name for f in fields(cls)}
        data = {k: row[k] for k in row.keys() if k in known}
        return cls(**data)
