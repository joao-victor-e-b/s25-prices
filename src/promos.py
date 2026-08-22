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

import json
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

# Product match: require "S25 Ultra", reject the sibling models.
_ULTRA_RE = re.compile(r"s25[\s-]*ultra", re.I)
_REJECT_TOKENS = ("s25 fe", "s25fe", "s25+", "s25 plus", "s25-plus", "s25 edge")
# Cheap staleness signals in the DDG snippet (avoids a page fetch).
_EXPIRY_RE = re.compile(r"expirad|encerrad|finalizad|vencid|inativ", re.I)
_OLD_AGE_RE = re.compile(r"h[áa]\s+\d+\s+(mes|ano)", re.I)


def _is_target(blob: str) -> bool:
    if not _ULTRA_RE.search(blob):
        return False
    return not any(tok in blob for tok in _REJECT_TOKENS)


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
    """Extract Promo rows from a DuckDuckGo HTML results page.

    Filters: real (non-ad) URL, deal path, strict S25-Ultra match, and a cheap
    snippet-based staleness pre-filter (expired keywords / old-age hints).
    """
    soup = BeautifulSoup(html, "lxml")
    out: list[Promo] = []
    seen: set[str] = set()
    for res in soup.select("div.result"):
        a = res.select_one("a.result__a")
        if not a:
            continue
        url = _real_url(a.get("href", ""))
        if not url or url in seen:
            continue
        low = url.lower()
        if any(sk in low for sk in _SKIP_PATHS):
            continue
        title = a.get_text(" ", strip=True)
        if not _is_target(f"{title} {url}".lower()):
            continue
        snip_el = res.select_one("a.result__snippet") or res.select_one(".result__snippet")
        snippet = snip_el.get_text(" ", strip=True) if snip_el else ""
        if _EXPIRY_RE.search(snippet) or _OLD_AGE_RE.search(snippet):
            continue  # stale per snippet -> skip before any page fetch
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


# --- Maximum-rigor expiry verification (per-page) -----------------------------
# Promobit exposes offerStatus/offerStatusName in __NEXT_DATA__; these names mean
# the deal is over. Pelando (Cloudflare) needs a rendered page checked for markers.
_EXPIRED_STATUS_NAMES = {
    "FINISHED", "EXPIRED", "REMOVED", "PAUSED", "INACTIVE", "REJECTED",
    "DELETED", "OFFLINE",
}
_PAGE_EXPIRY_RE = re.compile(
    r"oferta expirada|expirad|encerrad|indispon[íi]vel|oferta acabou|"
    r"n[ãa]o est[áa] mais dispon",
    re.I,
)


def _http_get(url: str):
    return requests.get(
        url, headers={"User-Agent": _UA, "Accept-Language": "pt-BR,pt;q=0.9"},
        timeout=20,
    )


def _promobit_active(url: str) -> bool:
    r = _http_get(url)
    if r.status_code != 200:
        return False
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        return False
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return False
    so = data.get("props", {}).get("pageProps", {}).get("serverOffer") or {}
    name = str(so.get("offerStatusName") or "").upper()
    if name:
        return name not in _EXPIRED_STATUS_NAMES
    status = so.get("offerStatus")
    if status is not None:
        try:
            return int(status) != 5  # 5 = FINISHED (observed)
        except (TypeError, ValueError):
            return False
    return False  # couldn't determine -> conservative (max accuracy)


def _pelando_active(url: str) -> bool:
    from .render import render  # Cloudflare -> needs a real browser
    html = render(url, wait_ms=3500)
    return bool(html) and not _PAGE_EXPIRY_RE.search(html)


def _generic_active(url: str) -> bool:
    r = _http_get(url)
    if r.status_code != 200:
        return False
    return not _PAGE_EXPIRY_RE.search(r.text)


def is_active(url: str) -> bool:
    """True if the deal page still looks active. Unknown / fetch-failure -> False
    (conservative, per 'maximum' expiry rigor). Never raises."""
    host = urllib.parse.urlparse(url).netloc.replace("www.", "")
    try:
        if "promobit" in host:
            return _promobit_active(url)
        if "pelando" in host:
            return _pelando_active(url)
        return _generic_active(url)
    except Exception:
        return False
