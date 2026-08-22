"""Promotions scanner (Phase 4b).

Deal-aggregators (Promobit, Pelando, tudocelular, lipeofertas, tukofertas) are
JS SPAs with no API/RSS and are hostile to scraping — and the spec (§4) already
flags them as a "redundant human-layer signal, not a collector". Instead of
fragile per-SPA scrapers, we query DuckDuckGo's HTML endpoint scoped to those
domains: it returns deal-post titles (often with the price inline, "Por R$ X:")
and real URLs, with no per-site reverse-engineering.

Best-effort: DDG throttles bursts (HTTP 202); a single hourly query is fine, and
throttled/empty responses degrade to [] rather than raising.
"""
from __future__ import annotations

import re
import urllib.parse

import requests
from bs4 import BeautifulSoup

from .models import Promo

_DDG = "https://html.duckduckgo.com/html/"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

DOMAINS = [
    "promobit.com.br",
    "pelando.com.br",
    "tudocelular.com",
    "lipeofertas.com.br",
    "tukofertas.com.br",
]
TERM = "galaxy s25 ultra 256gb"
# Matches "R$ 4.320,00" and "R$ 4.320" (thousands, no cents).
_PRICE_RE = re.compile(r"R\$\s*(\d[\d.]*(?:,\d{2})?)")
# Non-deal paths to skip (reviews/specs/comparisons, not offers).
_SKIP_PATHS = ("/blog/", "/review/", "/analise", "/noticia",
               "/fichas-tecnicas/", "/provas/", "/opiniao", "/comparar")


def _parse_price(text: str) -> float | None:
    m = _PRICE_RE.search(text or "")
    if not m:
        return None
    s = m.group(1)
    if "," in s:                      # 4.033,71 -> 4033.71
        s = s.replace(".", "").replace(",", ".")
    else:                             # 4.320 -> 4320
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def _real_url(href: str) -> str | None:
    """Unwrap DDG's redirect (//duckduckgo.com/l/?uddg=...). Skip ads (y.js)."""
    if "y.js" in href:
        return None
    m = re.search(r"uddg=([^&]+)", href)
    if m:
        return urllib.parse.unquote(m.group(1))
    if href.startswith("http"):
        return href
    return None


def parse_ddg(html: str, domain: str | None = None) -> list[Promo]:
    """Extract Promo rows from a DuckDuckGo HTML results page."""
    soup = BeautifulSoup(html, "lxml")
    out: list[Promo] = []
    seen: set[str] = set()
    for a in soup.select("a.result__a"):
        url = _real_url(a.get("href", ""))
        if not url or url in seen:
            continue
        low = url.lower()
        if any(sk in low for sk in _SKIP_PATHS):
            continue
        title = a.get_text(" ", strip=True)
        blob = f"{title} {url}".lower()
        if "s25" not in blob:
            continue
        host = urllib.parse.urlparse(url).netloc.replace("www.", "")
        price = _parse_price(title)
        seen.add(url)
        out.append(Promo(source=host or (domain or "web"), title=title,
                         url=url, price=price))
    return out


def _query(session: requests.Session, term: str, domain: str) -> list[Promo]:
    q = f"{term} site:{domain}"
    try:
        r = session.post(_DDG, data={"q": q}, timeout=20)
        if r.status_code != 200:
            return []  # 202 = throttled; treat as empty, not an error
        return parse_ddg(r.text, domain)
    except requests.RequestException:
        return []


def scan(domains: list[str] | None = None, term: str = TERM) -> list[Promo]:
    """Query each domain via DDG and return de-duplicated promos (best-effort)."""
    session = requests.Session()
    session.headers.update({"User-Agent": _UA, "Accept-Language": "pt-BR,pt;q=0.9"})
    found: dict[str, Promo] = {}
    for domain in (domains or DOMAINS):
        for pr in _query(session, term, domain):
            found.setdefault(pr.url, pr)  # first (cheapest-seen) wins
    return list(found.values())
