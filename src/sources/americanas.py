"""Americanas scraper (source_id: americanas).

Americanas renders search results client-side and blocks plain HTTP, so this
source uses the Playwright renderer. It captures refurbished ("Usado:")
S25 Ultra 256GB listings from whichever sellers (e.g. Trocafone) list there —
the Tier-2 marketplace arbitrage. The seller ships under the marketplace's
warranty policy, so `venue` is recorded as `americanas`.

Card text shape (post-render):
  "Usado: Samsung Galaxy S25 Ultra 256GB Titânio Prata Excelente - Trocafone
   0.0 (0) 0 avaliações  R$ 7.999,00  46%  R$ 4.334,15 no Pix
   em 8x de R$ 637,37 sem juros"
The first R$ is a marketing "de" price; the real price is the "no Pix" one.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from ..models import Observation
from ..render import render
from .base import Source

_HOST = "https://www.americanas.com.br"
_SEARCH = "https://www.americanas.com.br/busca/galaxy-s25-ultra-256gb"
_COLORS = ["jetblack", "preto", "azul", "prata", "cinza", "branco", "verde", "creme"]
_GRADES = ["Novo Reembalado", "Muito Bom", "Excelente", "Premium", "Como Novo",
           "Aceitável", "Aceitavel", "Bom"]
_STORAGE_RE = re.compile(r"(\d+)\s*GB", re.I)
_PIX_RE = re.compile(r"R\$\s*([\d.]+,\d{2})\s*no Pix", re.I)
_ANY_PRICE_RE = re.compile(r"R\$\s*([\d.]+,\d{2})")
_INSTALL_RE = re.compile(r"(\d+)x\s*de\s*R\$\s*([\d.]+,\d{2})\s*sem juros", re.I)
_NAME_RE = re.compile(r"Usado:\s*(.+?)\s+\d[.,]\d\s*\(", re.I)

_DEFAULT_MATCH = {"storage_gb": 256, "colors": ["preto", "jetblack"]}


def _brl(s: str) -> float | None:
    if not s:
        return None
    return float(s.replace(".", "").replace(",", "."))


def _find(tokens: list[str], text: str) -> str | None:
    t = (text or "").lower()
    for tok in tokens:
        if tok.lower() in t:
            return tok
    return None


class AmericanasSource(Source):
    seller = "Americanas"
    venue = "americanas"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.match = {**_DEFAULT_MATCH, **(cfg.get("match") or {})}
        self.search_url = cfg.get("search_url", _SEARCH)

    def fetch(self) -> list[Observation]:
        html = render(self.search_url, wait_ms=5000)
        return [o for o in self.parse(html) if self._matches(o)]

    def parse(self, html: str) -> list[Observation]:
        soup = BeautifulSoup(html, "lxml")
        out: list[Observation] = []
        seen: set[str] = set()
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if "s25-ultra" not in href.lower():
                continue
            text = a.get_text(" ", strip=True)
            if "usado" not in text.lower() and "usado" not in href.lower():
                continue  # refurb only
            if href in seen:
                continue
            seen.add(href)
            obs = self._parse_card(text, href)
            if obs:
                out.append(obs)
        return out

    def _parse_card(self, text: str, href: str) -> Observation | None:
        nm = _NAME_RE.search(text)
        name = nm.group(1).strip() if nm else text.replace("Usado:", "").strip()

        seller = self.seller
        left = name
        if " - " in name:
            left, seller = name.rsplit(" - ", 1)
            seller = seller.strip() or self.seller

        grade_raw = _find(_GRADES, left)
        color = _find(_COLORS, left)
        sm = _STORAGE_RE.search(left)
        storage_gb = int(sm.group(1)) if sm else None

        pix_m = _PIX_RE.search(text)
        pix_price = _brl(pix_m.group(1)) if pix_m else None

        installments_n = installment_value = None
        interest_flag = None
        im = _INSTALL_RE.search(text)
        if im:
            installments_n = int(im.group(1))
            installment_value = _brl(im.group(2))
            interest_flag = False

        if installments_n and installment_value:
            list_price = round(installment_value * installments_n, 2)
        elif pix_price:
            list_price = pix_price
        else:
            # last resort: the highest R$ on the card (skip the fake "de")
            prices = [_brl(p) for p in _ANY_PRICE_RE.findall(text)]
            list_price = max(prices) if prices else None
        if list_price is None:
            return None

        pix_discount = 0.0
        if pix_price and list_price and pix_price < list_price:
            pix_discount = round(list_price - pix_price, 2)

        # href like "/usado-...-trocafone-1782/p"; the slug is the stable id
        slug = href.split("?")[0].strip("/")
        parts = [x for x in slug.split("/") if x and x != "p"]
        listing_id = (parts[-1] if parts else slug)[:90]
        url = href if href.startswith("http") else _HOST + href

        return Observation(
            source_id=self.source_id,
            seller=seller,
            venue=self.venue,
            listing_id=listing_id,
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
            in_stock="indispon" not in text.lower(),
        )

    def _matches(self, o: Observation) -> bool:
        if self.match.get("storage_gb") and o.storage_gb != self.match["storage_gb"]:
            return False
        colors = self.match.get("colors")
        if colors and (o.color or "").lower() not in colors:
            return False
        return True


SOURCE = AmericanasSource
