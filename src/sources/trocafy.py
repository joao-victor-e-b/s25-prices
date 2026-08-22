"""Trocafy scraper (source_id: trocafy_site).

Trocafy runs on the iSET platform. The category page is server-rendered with
prices in the HTML (the spec's "price loads client-side" note is outdated), so
a plain ``requests`` GET + BeautifulSoup is enough — no headless browser.

Each product card carries everything we need:
- ``data-id``            -> listing_id
- title / slug           -> storage, color, grade
- ``.price-vista``       -> PIX price (already 10% discounted)
- ``.installment-plan``  -> "10x de R$ 448,19 sem juros" (nominal sticker)
- ``out-of-stock`` class -> stock flag

Battery band is not exposed on the category page (product-page only), so it is
left None here; the grade floor still gates buys.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from ..models import Observation
from .base import Source

_PRICE_RE = re.compile(r"R\$\s*([\d.]+,\d{2})")
_INSTALL_RE = re.compile(r"(\d+)\s*x\s*de\s*R\$\s*([\d.]+,\d{2})", re.I)
_STORAGE_RE = re.compile(r"(\d+)\s*(GB|TB)", re.I)

# Default target (spec §1): 256GB, Titânio Preto / JetBlack. Overridable per
# registry entry via a `match:` block.
_DEFAULT_MATCH = {"storage_gb": 256, "colors": ["preto", "jetblack"]}


def _brl_to_float(text: str) -> float | None:
    m = _PRICE_RE.search(text or "")
    if not m:
        return None
    return float(m.group(1).replace(".", "").replace(",", "."))


def _storage_to_gb(text: str) -> int | None:
    m = _STORAGE_RE.search(text or "")
    if not m:
        return None
    n = int(m.group(1))
    return n * 1024 if m.group(2).upper() == "TB" else n


class TrocafySource(Source):
    seller = "Trocafy"
    venue = "own_site"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.match = {**_DEFAULT_MATCH, **(cfg.get("match") or {})}

    def fetch(self) -> list[Observation]:
        try:
            r = self.session.get(self.url, timeout=25)
            r.raise_for_status()
            return self.parse(r.content)
        except Exception:
            # Trocafy's gocache CDN 403s datacenter IPs (e.g. GitHub Actions)
            # while serving browsers fine. Fall back to a real headless browser,
            # which passes the CDN fingerprinting. Local runs rarely hit this.
            from ..render import render
            html = render(self.url, wait_ms=3500,
                          wait_selector="div.product-card")
            return self.parse(html)

    # Separated from fetch() so it can be unit-tested against a saved fixture.
    # Accepts bytes (requests) or str (Playwright-rendered).
    def parse(self, content) -> list[Observation]:
        if isinstance(content, bytes):
            soup = BeautifulSoup(content, "lxml", from_encoding="iso-8859-1")
        else:
            soup = BeautifulSoup(content, "lxml")
        out: list[Observation] = []
        for card in soup.select("div.product-card"):
            obs = self._parse_card(card)
            if obs and self._matches(obs):
                out.append(obs)
        return out

    def _matches(self, o: Observation) -> bool:
        if self.match.get("storage_gb") and o.storage_gb != self.match["storage_gb"]:
            return False
        colors = self.match.get("colors")
        if colors:
            c = (o.color or "").lower()
            if not any(tok in c for tok in colors):
                return False
        return True

    def _parse_card(self, card) -> Observation | None:
        link = card.select_one("a.product-link-data") or card.select_one("a[href]")
        if not link:
            return None
        url = link.get("href", "")
        title = (link.get("title") or link.get_text(" ", strip=True) or "").strip()
        if "s25 ultra" not in title.lower() and "s25-ultra" not in url.lower():
            return None

        listing_id = card.get("data-id") or url.rsplit("-p", 1)[-1]
        classes = card.get("class", [])
        in_stock = "out-of-stock" not in classes

        # storage / color / grade from the title:
        #   "Galaxy S25 Ultra 256GB Titânio Preto - Sou Como Novo"
        grade_raw = None
        color = None
        if " - " in title:
            head, grade_raw = title.rsplit(" - ", 1)
            grade_raw = grade_raw.strip()
        else:
            head = title
        storage_gb = _storage_to_gb(head)
        sm = _STORAGE_RE.search(head)
        if sm:
            color = head[sm.end():].strip() or None

        # pricing
        pix_el = card.select_one("div.price-vista")
        pix_price = _brl_to_float(pix_el.get_text(" ", strip=True)) if pix_el else None

        installments_n = installment_value = None
        interest_flag = None
        inst_el = card.select_one("div.installment-plan")
        if inst_el:
            inst_text = inst_el.get_text(" ", strip=True)
            im = _INSTALL_RE.search(inst_text)
            if im:
                installments_n = int(im.group(1))
                installment_value = float(im.group(2).replace(".", "").replace(",", "."))
                interest_flag = "sem juros" not in inst_text.lower()

        if pix_price is None and installment_value is None:
            return None  # no usable price

        # Nominal sticker = installment total; PIX price is the discounted cash
        # price. Encode the gap as pix_discount so PV can compare both paths.
        if installments_n and installment_value:
            list_price = round(installment_value * installments_n, 2)
        else:
            list_price = pix_price
        pix_discount = 0.0
        if pix_price is not None and list_price is not None and list_price > pix_price:
            pix_discount = round(list_price - pix_price, 2)

        return Observation(
            source_id=self.source_id,
            seller=self.seller,
            venue=self.venue,
            listing_id=str(listing_id),
            url=url,
            sku="SM-S938B",
            storage_gb=storage_gb,
            color=color,
            grade_raw=grade_raw,
            battery_band=None,
            list_price=list_price,
            pix_discount=pix_discount,
            installments_n=installments_n,
            installment_value=installment_value,
            interest_flag=interest_flag,
            in_stock=in_stock,
        )


SOURCE = TrocafySource
