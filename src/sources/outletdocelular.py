"""Outlet do Celular scraper (source_id: outletdocelular).

Outlet runs on Tray. Unlike Trocafy/Trocafone, each color+condition is a
*separate product* (one variation per page), so scraping is two-level:

1. GET the S25 Ultra category and collect product URLs.
2. GET each product page and read:
   - JSON-LD Product -> price + availability
   - #product-priceBox -> "A Partir de: R$ X   R$ Y à vista Pix" (10% Pix cut)
   - li[data-id] variation label -> color + condition ("Titanio Cinza ...
     Como Novo - Grade A")

Outlet's 6-month warranty is best-in-class, so it's worth polling even when
inventory is thin. Installment plans aren't reliably in the price box, so PV
rests on the Pix price (the 10% discount dominates anyway).
"""
from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup

from ..models import Observation
from .base import Source

_CATEGORY = "https://www.outletdocelular.com.br/celular-samsung/s25-ultra"
_HOST = "https://www.outletdocelular.com.br"
# Tray renders some prices across spans -> "R$ 4.791 , 60"; tolerate the spaces.
_PRICE_RE = re.compile(r"R\$\s*([\d.]+)\s*,\s*(\d{2})")
_STORAGE_RE = re.compile(r"(\d+)\s*GB", re.I)
_COLORS = ["jetblack", "preto", "azul", "prata", "cinza", "branco", "verde", "creme"]

_DEFAULT_MATCH = {"storage_gb": 256, "colors": ["preto", "jetblack"]}


def _price(text: str) -> float | None:
    m = _PRICE_RE.search(text or "")
    if not m:
        return None
    return float(m.group(1).replace(".", "") + "." + m.group(2))


def _all_prices(text: str) -> list[float]:
    return [float(a.replace(".", "") + "." + b)
            for a, b in _PRICE_RE.findall(text or "")]


def _find_color(text: str) -> str | None:
    t = (text or "").lower()
    for c in _COLORS:
        if c in t:
            return c
    return None


def _ld_products(soup: BeautifulSoup) -> list[dict]:
    out = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "", strict=False)
        except (json.JSONDecodeError, TypeError):
            continue
        for d in (data if isinstance(data, list) else [data]):
            if isinstance(d, dict) and d.get("@type") == "Product":
                out.append(d)
    return out


class OutletDoCelularSource(Source):
    seller = "Outlet do Celular"
    venue = "own_site"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.match = {**_DEFAULT_MATCH, **(cfg.get("match") or {})}
        self.category_url = cfg.get("category_url", _CATEGORY)

    def fetch(self) -> list[Observation]:
        r = self.session.get(self.category_url, timeout=25)
        r.raise_for_status()
        out: list[Observation] = []
        for url in self.product_urls(r.content):
            try:
                pr = self.session.get(url, timeout=25)
                pr.raise_for_status()
            except Exception:
                continue  # one bad product page shouldn't sink the source
            obs = self.parse_product(url, pr.content)
            if obs and self._matches(obs):
                out.append(obs)
        return out

    def product_urls(self, category_content: bytes) -> list[str]:
        soup = BeautifulSoup(category_content, "lxml", from_encoding="iso-8859-1")
        urls = []
        for a in soup.select("a[href*='samsung-galaxy-s25-ultra']"):
            href = a.get("href", "")
            if href.startswith("/"):
                href = _HOST + href
            if "samsung-galaxy-s25-ultra" in href and href not in urls:
                urls.append(href)
        return urls

    def parse_product(self, url: str, content: bytes) -> Observation | None:
        soup = BeautifulSoup(content, "lxml", from_encoding="iso-8859-1")

        products = _ld_products(soup)
        if not products:
            return None
        prod = products[0]
        offer = prod.get("offers") or {}
        if isinstance(offer, list):
            offer = offer[0] if offer else {}
        availability = str(offer.get("availability", "")).lower()
        in_stock = "instock" in availability.replace("/", "").replace(" ", "")

        name = prod.get("name", "")
        storage_gb = None
        sm = _STORAGE_RE.search(name)
        if sm:
            storage_gb = int(sm.group(1))

        # variation label -> color + condition/grade
        var = soup.select_one("li[data-id]")
        var_text = var.get_text(" ", strip=True) if var else ""
        color = _find_color(f"{var_text} {name}")
        grade_raw = None
        if "\\" in var_text or "Condição" in var_text or "Condicao" in var_text:
            # "Titanio Cinza Condição \ Como Novo - Grade A R$ 5.324,00"
            m = re.search(r"Condi[çc][ãa]o\s*\\?\s*(.+?)\s*R\$", var_text)
            if m:
                grade_raw = m.group(1).strip()
        if not grade_raw:
            grade_raw = var_text or None

        # price box: list ("A Partir de: R$ X") then Pix ("R$ Y à vista ... Pix")
        pb = soup.select_one("#product-priceBox")
        pb_text = pb.get_text(" ", strip=True) if pb else ""
        prices = _all_prices(pb_text)
        list_price = prices[0] if prices else _price(str(offer.get("price")))
        pix_price = None
        if "pix" in pb_text.lower() and len(prices) >= 2:
            pix_price = prices[1]
        if list_price is None:
            return None
        pix_discount = 0.0
        if pix_price and pix_price < list_price:
            pix_discount = round(list_price - pix_price, 2)

        item_id = None
        menu = soup.select_one("[data-product-id]")
        if menu:
            item_id = menu.get("data-product-id")
        item_id = item_id or str(prod.get("sku") or "")

        return Observation(
            source_id=self.source_id,
            seller=self.seller,
            venue=self.venue,
            listing_id=str(item_id),
            url=url,
            sku="SM-S938B",
            storage_gb=storage_gb,
            color=color,
            grade_raw=grade_raw,
            battery_band=None,
            list_price=list_price,
            pix_discount=pix_discount,
            installments_n=None,
            installment_value=None,
            interest_flag=None,
            in_stock=in_stock,
        )

    def _matches(self, o: Observation) -> bool:
        if self.match.get("storage_gb") and o.storage_gb != self.match["storage_gb"]:
            return False
        colors = self.match.get("colors")
        if colors and (o.color or "").lower() not in colors:
            return False
        return True


SOURCE = OutletDoCelularSource
