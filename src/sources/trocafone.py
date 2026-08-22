"""Trocafone scraper (source_id: trocafone_site).

Trocafone runs on VTEX. Rather than render the React storefront, we hit the
public VTEX catalog search API, which returns every color×grade SKU with price,
availability, and installment plans as JSON (spec §4: "hit the JSON endpoint
directly, don't render"). This is the cleanest source of the lot.

Notes on the VTEX offer shape (observed):
- ``Price``      current selling price; Pix 1x equals it (no extra Pix discount)
- ``ListPrice``  the crossed-out "de" price (marketing, not used for PV)
- ``Installments`` many plans; the best sem-juros is 10x (InterestRate 0)
The grouped "S25 Ultra 256GB" product carries per-condition SKUs; Trocafone also
publishes some conditions as standalone products — both are captured.
"""
from __future__ import annotations

import urllib.parse

from ..models import Observation
from .base import Source

_API = "https://www.trocafone.com.br/api/catalog_system/pub/products/search"
_SEARCH = "galaxy s25 ultra 256gb"

# Grade phrases, most specific first (so "Muito Bom" wins over "Bom", and
# "Novo Reembalado" is caught before a bare "Novo"). normalize.py maps these to
# ranks; we only extract the human label + gate out ungradeable SKUs here.
_GRADES = [
    "Novo Reembalado", "Muito Bom", "Excelente", "Bom", "Outlet",
    "Reembalado", "Vitrine",
]
_COLORS = ["jetblack", "preto", "azul", "prata", "cinza", "branco", "verde", "creme"]

_DEFAULT_MATCH = {"storage_gb": 256, "colors": ["preto", "jetblack"]}


def _find_grade(text: str) -> str | None:
    t = text.lower()
    for g in _GRADES:
        if g.lower() in t:
            return g
    return None


def _find_color(text: str) -> str | None:
    t = text.lower()
    for c in _COLORS:
        if c in t:
            return c
    return None


def _best_sem_juros(installments: list[dict]) -> tuple[int, float] | None:
    """Return (n, value) for the longest interest-free plan, else None."""
    best = None
    for ins in installments or []:
        if (ins.get("InterestRate") or 0) == 0 and ins.get("NumberOfInstallments"):
            n = ins["NumberOfInstallments"]
            if best is None or n > best[0]:
                best = (n, ins.get("Value"))
    return best


def _pix_price(installments: list[dict]) -> float | None:
    for ins in installments or []:
        name = (ins.get("PaymentSystemName") or "").lower()
        if "pix" in name and ins.get("NumberOfInstallments") == 1:
            return ins.get("Value")
    return None


class TrocafoneSource(Source):
    seller = "Trocafone"
    venue = "own_site"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.match = {**_DEFAULT_MATCH, **(cfg.get("match") or {})}

    def fetch(self) -> list[Observation]:
        # VTEX's ft search rejects '+' for spaces (400); build with %20 encoding.
        ft = urllib.parse.quote(_SEARCH)
        url = f"{_API}?ft={ft}&_from=0&_to=49"
        r = self.session.get(url, timeout=25)
        r.raise_for_status()
        return self.parse(r.json())

    def parse(self, products: list[dict]) -> list[Observation]:
        out: list[Observation] = []
        for p in products:
            if not self._is_target_product(p.get("productName", "")):
                continue
            link = p.get("linkText", "")
            for item in p.get("items", []):
                obs = self._parse_item(p, item, link)
                if obs and self._matches(obs):
                    out.append(obs)
        return out

    @staticmethod
    def _is_target_product(name: str) -> bool:
        n = (name or "").lower()
        return "s25 ultra" in n and "256" in n and "capa" not in n

    def _matches(self, o: Observation) -> bool:
        if self.match.get("storage_gb") and o.storage_gb != self.match["storage_gb"]:
            return False
        colors = self.match.get("colors")
        if colors and (o.color or "").lower() not in colors:
            return False
        return True

    def _parse_item(self, product: dict, item: dict, link: str) -> Observation | None:
        sellers = item.get("sellers") or [{}]
        offer = sellers[0].get("commertialOffer", {})
        price = offer.get("Price")
        if not price or price <= 0:
            return None  # unpriced / sealed-new placeholder SKU

        text = f"{item.get('name', '')} {product.get('productName', '')}"
        grade_raw = _find_grade(text)
        if grade_raw is None:
            return None  # can't grade it -> don't store
        color = _find_color(text)

        pixv = _pix_price(offer.get("Installments"))
        pix_discount = round(price - pixv, 2) if pixv and pixv < price else 0.0

        installments_n = installment_value = None
        interest_flag = None
        sj = _best_sem_juros(offer.get("Installments"))
        if sj:
            installments_n, installment_value = sj[0], sj[1]
            interest_flag = False

        avail = offer.get("AvailableQuantity") or 0
        in_stock = bool(offer.get("IsAvailable")) and avail > 0

        item_id = str(item.get("itemId"))
        url = f"https://www.trocafone.com.br/{link}/p?skuId={item_id}"

        return Observation(
            source_id=self.source_id,
            seller=self.seller,
            venue=self.venue,
            listing_id=item_id,
            url=url,
            sku="SM-S938B",
            storage_gb=256,
            color=color,
            grade_raw=grade_raw,
            battery_band=None,
            list_price=float(price),
            pix_discount=pix_discount,
            installments_n=installments_n,
            installment_value=installment_value,
            interest_flag=interest_flag,
            in_stock=in_stock,
        )


SOURCE = TrocafoneSource
