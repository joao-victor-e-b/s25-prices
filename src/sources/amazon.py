"""Amazon Seminovos scraper (source_id: amazon_seminovos).

Amazon is robots-hostile and rate-limits aggressively, especially from
datacenter IPs — the GitHub Actions runner may get a captcha where a home IP
succeeds. The scraper therefore detects a block (missing #productTitle or a
captcha page) and *raises*, so the run counts it as a fetch failure surfaced in
the heartbeat rather than silently reporting "no deal" (spec §7 decay warning).

Parses the buybox: apex price, condition ("Seminovo - Excelente"), the longest
sem-juros installment plan, seller (who actually ships), and availability.
"""
from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup

from ..models import Observation
from ..normalize import coupon_from_text
from .base import Source

_HOST = "https://www.amazon.com.br"
_COLORS = ["jetblack", "preto", "azul", "prata", "cinza", "branco", "verde", "creme"]
_STORAGE_RE = re.compile(r"(\d+)\s*GB", re.I)
_INSTALL_SJ_RE = re.compile(
    r"(\d+)x\s*(?:de\s*)?R\$\s*([\d. ]+,\d{2})\s*sem juros", re.I)
_CAPTCHA_MARKERS = (
    "validateCaptcha", "Digite os caracteres", "Enter the characters",
    "Type the characters you see",
)

_DEFAULT_MATCH = {"storage_gb": 256, "colors": ["preto", "jetblack"]}

_BROWSER_HEADERS = {
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}


def _brl(text: str) -> float | None:
    m = re.search(r"([\d. ]+),(\d{2})", (text or "").replace("R$", ""))
    if not m:
        return None
    whole = m.group(1).replace(".", "").replace(" ", "").strip()
    return float(f"{whole}.{m.group(2)}")


def _find_color(text: str) -> str | None:
    t = (text or "").lower()
    for c in _COLORS:
        if c in t:
            return c
    return None


class AmazonSeminovosSource(Source):
    seller = "Amazon Seminovos"
    venue = "amazon"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.match = {**_DEFAULT_MATCH, **(cfg.get("match") or {})}
        self.asin = cfg.get("asin")
        self.product_url = cfg.get("url") or f"{_HOST}/dp/{self.asin}"

    def fetch(self) -> list[Observation]:
        url = f"{_HOST}/dp/{self.asin}" if self.asin else self.product_url
        r = self.session.get(url, headers=_BROWSER_HEADERS, timeout=30)
        r.raise_for_status()

        results: list[Observation] = []
        obs = self.parse(url, r.content)  # raises on captcha/block
        if obs and self._matches(obs):
            results.append(obs)

        # The configured ASIN is a variation family whose default child rotates
        # (256GB one poll, 512GB the next). If the parent didn't land on our
        # target, follow the twister to the 256GB Preto/JetBlack child.
        if not results:
            for child in self._target_children(r.content):
                curl = f"{_HOST}/dp/{child}"
                try:
                    cr = self.session.get(curl, headers=_BROWSER_HEADERS, timeout=30)
                    cr.raise_for_status()
                    cobs = self.parse(curl, cr.content)
                except Exception:
                    continue
                if cobs and self._matches(cobs):
                    results.append(cobs)

        seen, uniq = set(), []
        for o in results:
            if o.listing_id not in seen:
                seen.add(o.listing_id)
                uniq.append(o)
        return uniq

    def _target_children(self, content: bytes) -> list[str]:
        """ASINs from the variation twister whose values match the target
        storage + color (so we can deep-link the 256GB Preto child)."""
        html = content.decode("utf-8", "ignore")
        m = re.search(r'"dimensionValuesDisplayData"\s*:\s*(\{.*?\})\s*,\s*"', html, re.S)
        if not m:
            return []
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            return []
        want_storage = str(self.match.get("storage_gb") or "")
        colors = self.match.get("colors") or []
        out = []
        for asin, vals in data.items():
            joined = " ".join(str(v) for v in vals).lower()
            if want_storage and want_storage not in joined:
                continue
            if colors and not any(c in joined for c in colors):
                continue
            out.append(asin)
        return out

    def parse(self, url: str, content: bytes) -> Observation | None:
        html = content.decode("utf-8", "ignore")
        if any(m in html for m in _CAPTCHA_MARKERS):
            raise RuntimeError("amazon: captcha / bot wall")
        soup = BeautifulSoup(html, "lxml")

        title_el = soup.select_one("#productTitle")
        if not title_el:
            raise RuntimeError("amazon: no product title (blocked or layout change)")
        title = title_el.get_text(strip=True)
        tl = title.lower()
        if "s25 ultra" not in tl or "256" not in tl:
            return None  # wrong product

        price_el = soup.select_one("span.a-price span.a-offscreen")
        list_price = _brl(price_el.get_text(strip=True)) if price_el else None
        if not list_price:
            return None  # no buybox price -> treat as unavailable

        cond_el = soup.select_one(".a-text-bold.a-text-normal")
        grade_raw = cond_el.get_text(" ", strip=True) if cond_el else None
        # e.g. "Seminovo - Excelente" -> keep the graded part
        if grade_raw and "-" in grade_raw:
            grade_raw = grade_raw.split("-", 1)[1].strip() or grade_raw
        # Buybox layout varies; fall back to the condition description text
        # ("...em condição Excelente foi inspecionado...").
        if not grade_raw or grade_raw.lower().startswith("seminovo"):
            m = re.search(
                r"condi[çc][ãa]o\s+(Excelente|Muito Bom|Bom|Aceit[áa]vel|Premium|Como novo)",
                html, re.I)
            if m:
                grade_raw = m.group(1)

        avail = soup.select_one("#availability")
        at = avail.get_text(" ", strip=True).lower() if avail else ""
        in_stock = ("estoque" in at and "sem estoque" not in at
                    and "indispon" not in at)

        # longest sem-juros installment plan
        installments_n = installment_value = None
        interest_flag = None
        best = None
        for m in _INSTALL_SJ_RE.finditer(html):
            n = int(m.group(1))
            if best is None or n > best[0]:
                best = (n, _brl(m.group(2)))
        if best:
            installments_n, installment_value = best
            interest_flag = False

        # clip-coupon widget in the buybox (folds into PV via the coupon field)
        coupon = 0.0
        cel = soup.select_one("#couponsInBuybox_feature_div")
        if cel:
            coupon = coupon_from_text(cel.get_text(" ", strip=True), list_price)

        seller = self._seller(soup)
        color = _find_color(title)
        sm = _STORAGE_RE.search(title)
        storage_gb = int(sm.group(1)) if sm else None

        return Observation(
            source_id=self.source_id,
            seller=seller,
            venue=self.venue,
            listing_id=self.asin or url.rsplit("/", 1)[-1],
            url=url,
            sku="SM-S938B",
            storage_gb=storage_gb,
            color=color,
            grade_raw=grade_raw,
            battery_band=None,
            list_price=list_price,
            pix_discount=0.0,
            coupon=coupon,
            installments_n=installments_n,
            installment_value=installment_value,
            interest_flag=interest_flag,
            in_stock=in_stock,
        )

    @staticmethod
    def _seller(soup) -> str:
        """Who actually ships (buybox 'Vendido por'), falling back to the
        Amazon Seminovos program label."""
        for sel in ("#sellerProfileTriggerId", "#merchant-info"):
            e = soup.select_one(sel)
            if e and e.get_text(strip=True):
                return e.get_text(" ", strip=True)[:60]
        feat = soup.select_one(".offer-display-feature-text")
        if feat:
            first = feat.get_text(" ", strip=True).split("  ")[0].strip()
            if first:
                return first[:60]
        return "Amazon Seminovos"

    def _matches(self, o: Observation) -> bool:
        if self.match.get("storage_gb") and o.storage_gb != self.match["storage_gb"]:
            return False
        colors = self.match.get("colors")
        if colors and (o.color or "").lower() not in colors:
            return False
        return True


SOURCE = AmazonSeminovosSource
