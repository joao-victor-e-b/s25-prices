"""Amazon Seminovos scraper (source_id: amazon_seminovos).

Amazon is robots-hostile and rate-limits aggressively, especially from
datacenter IPs — the GitHub Actions runner may get a captcha where a home IP
succeeds. Blocks are detected (captcha markers, missing #productTitle, error
pages) and *raise*, so the run counts a fetch failure in the heartbeat rather
than silently reporting "no deal" (spec §7 decay warning).

Two requests per poll, on one session:

1. ``/dp/{asin}`` — the product page. Only shows the *buybox* offer, but it is
   the only place with the installment plan ("12x de R$ 381,90 sem juros").
2. ``aodAjaxMain`` — the "Comparar outras ofertas" (All Offers Display) panel:
   every seller's offer for the ASIN. Without it a cheaper non-buybox seller is
   invisible (Assurant Ecophone at R$3.919 was missed whenever CellularStore
   held the buybox). The AOD's pinned offer *is* the buybox, so the dp plan is
   copied onto it; other AOD offers get no plan (we don't invent one).

If the AOD is blocked we degrade to the dp buybox alone (yesterday's coverage,
not zero); only when both fail does fetch() raise.

Price semantics (same as the other sources, so normalize.pv() works):
``list_price`` is the *card* price — what the sem-juros plan totals — and
``pix_discount`` is card minus the shown "à vista no Pix ou NuPay (5% off)"
price. Amazon folds that promo into the displayed price-to-pay, so the card
price comes from elsewhere (dp: twister-plus JSON / "total parcelado"; AOD: the
add-to-cart button's aria-label). The discount is the R$ difference Amazon
shows, not card*pct, and is only credited when a Pix/NuPay promo is visible.

``listing_id`` is ``{asin}:{seller_id}:{grade}`` (e.g.
``B0FTMQQ7L2:A2TO0KKQTM1BAB:excelente``): every seller shares the ASIN, and one
seller can list several grades, so the ASIN alone broke restock detection and
alert dedup. Amazon's offerListingId is a per-request token — never stable.
"""
from __future__ import annotations

import json
import math
import random
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from ..models import Observation
from ..normalize import coupon_from_text
from .base import Source

_HOST = "https://www.amazon.com.br"
_AOD_URL = (_HOST + "/gp/product/ajax/aodAjaxMain/ref=dp_aod_ALL_mbc"
            "?asin={asin}&pc=dp&experienceId=aodAjaxMain")
_AOD_PAGE_URL = (_HOST + "/gp/product/ajax/aodAjaxMain/ref=aod_page_{n}"
                 "?asin={asin}&pc=dp&experienceId=aodAjaxMain"
                 "&pageno={n}&isonlyrenderofferlist=true")
_AOD_PAGE_SIZE = 10     # list offers per AOD page (pinned offer not counted)
_AOD_MAX_PAGES = 5      # politeness cap; our ASIN has ~3 offers
_SKU = "SM-S938B"

_COLORS = ["jetblack", "preto", "azul", "prata", "cinza", "branco", "verde", "creme"]
# "256GB, 12GB RAM" / "1TB, 12GB RAM": the first size that isn't RAM
_STORAGE_RE = re.compile(r"(\d+)\s*(GB|TB)\b(?!\s*(?:de\s+)?RAM)", re.I)
_INSTALL_SJ_RE = re.compile(
    r"(\d+)x\s*(?:de\s*)?R\$\s*([\d. ]+,\d{2})\s*sem juros", re.I)
_BRL_RE = re.compile(r"R\$\s*([\d.]+,\d{2})")
_PARCELADO_RE = re.compile(r"total parcelado\s*R\$\s*([\d.]+,\d{2})", re.I)
# AOD add-to-cart aria-label: "...do vendedor Trocafone e preço R$ 4.839,00"
_ATC_PRICE_RE = re.compile(r"pre[çc]o\s+R\$\s*([\d.]+,\d{2})", re.I)
_ATC_SELLER_RE = re.compile(r"vendedor\s+(.+?)\s+e\s+pre[çc]o", re.I | re.S)
_SELLER_ID_RE = re.compile(r"[?&](?:seller|smid)=([A-Z0-9]{8,20})")
_DP_ASIN_RE = re.compile(r"/dp/([A-Z0-9]{10})")
# The promo wording itself ("5% off à vista no Pix ou NuPay" in the buybox,
# "à vista no Pix ou NuPay (5% off)" in the AOD) — a bare "Pix" also appears in
# the payment-methods popover on every page, promo or not.
_PIX_PROMO_RE = re.compile(
    r"\d+\s*%\s*off[^.()]{0,40}?\b(?:pix|nupay)\b"
    r"|\b(?:pix|nupay)\b[^.()]{0,40}?\(\s*\d+\s*%\s*off", re.I)
_MAX_PIX_SHARE = 0.15   # a "Pix" gap above this is some other price, not the promo
_AMAZON_SELLER_ID = "A1ZZFT5FULY4LN"   # Amazon.com.br's own merchant id
_COUPON_HINT_RE = re.compile(r"cupom|coupon|economize|aplicar", re.I)
_OFFER_ID_RE = re.compile(r"""id\s*=\s*["']aod-(?:pinned-)?offer["']""")
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


def _aod_headers(asin: str) -> dict:
    """The AOD is an XHR from the product page. A bare GET with requests'
    default ``Accept: */*`` gets a 503 that JS-redirects to /error/500."""
    return {
        "Accept": "text/html,*/*",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Referer": f"{_HOST}/dp/{asin}",
        "X-Requested-With": "XMLHttpRequest",
    }


def _brl(text: str) -> float | None:
    m = re.search(r"([\d. ]+),(\d{2})", (text or "").replace("R$", ""))
    if not m:
        return None
    whole = m.group(1).replace(".", "").replace(" ", "").strip()
    return float(f"{whole}.{m.group(2)}")


def _first_brl(text: str | None) -> float | None:
    """First "R$ x.xxx,xx" in text, ignoring suffixes such as
    "com 11 por cento de desconto"; falls back to any BRL-looking number."""
    m = _BRL_RE.search(text or "")
    return _brl(m.group(1)) if m else _brl(text or "")


def _text(el) -> str:
    return el.get_text(" ", strip=True) if el is not None else ""


def _storage_gb(title: str | None) -> int | None:
    m = _STORAGE_RE.search(title or "")
    if not m:
        return None
    n = int(m.group(1))
    return n * 1024 if m.group(2).upper() == "TB" else n


def _find_color(text: str) -> str | None:
    t = (text or "").lower()
    for c in _COLORS:
        if c in t:
            return c
    return None


def _slug(text: str | None) -> str:
    """ASCII-lowercase, non-alphanumeric runs -> '-' ("Aceitável" -> "aceitavel")."""
    s = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _grade_from_heading(heading: str | None) -> str | None:
    """"Seminovo - Excelente" -> "Excelente"; a bare "Novo" stays as is."""
    if not heading:
        return None
    parts = re.split(r"\s*[-–]\s*", heading, maxsplit=1)
    return (parts[1] if len(parts) == 2 else parts[0]).strip() or None


def _listing_id(asin: str, seller_id: str | None, seller: str | None,
                grade_raw: str | None) -> str:
    who = seller_id or _slug(seller) or "unknown"
    return f"{asin}:{who}:{_slug(grade_raw) or 'na'}"


def _offer_key(o: Observation) -> str:
    """listing_id minus the ASIN: identifies seller + grade."""
    return o.listing_id.split(":", 1)[-1]


def _asin_of(o: Observation) -> str:
    return o.listing_id.split(":", 1)[0]


def _split_pix(card: float | None, shown: float | None,
               pix_promo: bool) -> tuple[float | None, float]:
    """(list_price, pix_discount) from the card price and the shown price-to-pay.

    The discount is credited only when a Pix/NuPay promo explains the gap, and
    is the R$ difference Amazon shows (its rounding differs from card*pct). A
    missing card price means shown is taken as card with no discount (never
    guess); a "card" price *below* the price-to-pay must belong to some other
    offer, so the price-to-pay wins.
    """
    if card is None:
        return shown, 0.0
    if shown is None:
        return card, 0.0
    if shown < card and pix_promo and card - shown <= card * _MAX_PIX_SHARE:
        return card, round(card - shown, 2)
    if shown > card:
        return shown, 0.0
    return card, 0.0


def _no_stack(o: Observation) -> Observation:
    """Amazon's Pix/NuPay promo "não pode ser combinada com outras ofertas" — the
    larger of it and a clip coupon applies, never both (PV subtracts both)."""
    if (o.pix_discount or 0) > 0 and (o.coupon or 0) > 0:
        if o.coupon > o.pix_discount:
            o.pix_discount = 0.0
        else:
            o.coupon = 0.0
    return o


def _cash_price(o: Observation) -> float:
    return (o.list_price or math.inf) - (o.pix_discount or 0.0) - (o.coupon or 0.0)


def aod_block_reason(status: int, html: str) -> str | None:
    """Why an AOD response is unusable (blocked, error page, no offers), or None.

    Amazon answers a block with 200 *or* 503, a captcha or an /error/500
    redirect stub; a page with neither a pinned nor a list offer is treated the
    same, since we can't tell "no sellers" from a degraded panel.
    """
    if status != 200:
        return f"HTTP {status}"
    if any(m in html for m in _CAPTCHA_MARKERS):
        return "captcha / bot wall"
    if "/error/500" in html:
        return "error page (/error/500)"
    if not _OFFER_ID_RE.search(html):
        return "no offers in panel (blocked or layout change)"
    return None


@dataclass
class AodPage:
    """Parsed All Offers Display response."""
    pinned: Observation | None                     # the buybox offer (also in offers)
    offers: list[Observation] = field(default_factory=list)  # pinned first
    total: int | None = None    # #aod-total-offer-count: list offers, excl. pinned
    title: str | None = None    # #aod-asin-title-text (absent on pages 2+)
    # True only when every offer page was read for the target product, so an
    # offer missing from ``offers`` really left the panel (sold out).
    complete: bool = True


class AmazonSeminovosSource(Source):
    seller = "Amazon Seminovos"
    venue = "amazon"
    # Politeness pause between requests (seconds, uniform). Tests zero it.
    pause_range: tuple[float, float] = (1.0, 2.5)

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.match = {**_DEFAULT_MATCH, **(cfg.get("match") or {})}
        self.asin = cfg.get("asin")
        self.product_url = cfg.get("url") or f"{_HOST}/dp/{self.asin}"
        # Set by fetch() for the orchestrator: ``complete`` means the result is
        # every live offer (so listings missing from it can be marked sold
        # out); ``degraded`` lists partial-fetch reasons for the run log.
        self.complete = False
        self.degraded: list[str] = []

    # --- orchestration ----------------------------------------------------

    def fetch(self) -> list[Observation]:
        asin = self.asin
        if not asin:
            m = _DP_ASIN_RE.search(self.product_url)
            asin = m.group(1) if m else None
        dp: Observation | None = None
        aod: AodPage | None = None
        errors: list[str] = []
        self.complete = False
        self.degraded = []

        try:
            dp, asin = self._fetch_dp(asin)
        except Exception as e:  # captcha, HTTP error, layout change
            errors.append(f"dp: {e}")

        if asin:
            self._pause()
            try:
                aod = self._fetch_aod(asin)
            except Exception as e:
                errors.append(f"aod: {e}")
        else:
            errors.append("aod: no ASIN configured")

        if len(errors) == 2:  # neither the dp nor the AOD gave us anything
            raise RuntimeError("amazon: " + "; ".join(errors))
        if aod is not None and not aod.complete:
            errors.append("aod: incomplete (extra page failed or variation drift)")
        if errors:  # degraded but usable — leave a trace in the run log
            print(f"[amazon] partial fetch: {'; '.join(errors)}", file=sys.stderr)
            self.degraded = errors
        self.complete = aod is not None and aod.complete
        return self.combine(dp, aod)

    def combine(self, dp: Observation | None, aod: AodPage | None) -> list[Observation]:
        """Merge the dp buybox into the AOD offers; keep target offers, one per
        listing_id (cheapest wins)."""
        offers: list[Observation] = []
        if aod is not None:
            pinned = aod.pinned if aod.pinned is not None and self._matches(aod.pinned) else None
            if pinned is not None and dp is not None:
                self._merge_buybox(pinned, dp)
            offers.extend(aod.offers)
            if pinned is None and dp is not None:
                offers.append(dp)  # AOD showed no (target) buybox; keep the dp one
        elif dp is not None:
            offers.append(dp)

        best: dict[str, Observation] = {}
        for o in offers:
            if not self._matches(o):
                continue
            _no_stack(o)
            cur = best.get(o.listing_id)
            if cur is None or _cash_price(o) < _cash_price(cur):
                best[o.listing_id] = o
        return list(best.values())

    @staticmethod
    def _merge_buybox(pinned: Observation, dp: Observation) -> bool:
        """Copy the dp installment plan (and clip coupon) onto the AOD pinned
        offer when both describe the same grade at the same card price — a
        different price means the buybox moved between the two requests and the
        plan would be stale. The seller keys normally agree too, but a page that
        hides the seller id (slug fallback) must not lose the merge: the pinned
        offer *is* the buybox, and an equal card price to the cent is proof."""
        if pinned.list_price is None or dp.list_price is None:
            return False
        if abs(pinned.list_price - dp.list_price) > 0.01:
            return False
        if _offer_key(pinned).rsplit(":", 1)[-1] != _offer_key(dp).rsplit(":", 1)[-1]:
            return False  # different grade
        if dp.installments_n and dp.installment_value:
            pinned.installments_n = dp.installments_n
            pinned.installment_value = dp.installment_value
            pinned.interest_flag = False
        if not pinned.coupon and dp.coupon:
            pinned.coupon = dp.coupon
        return True

    def _pause(self) -> None:
        lo, hi = self.pause_range
        if hi > 0:
            time.sleep(random.uniform(lo, hi))

    # --- product page (buybox) --------------------------------------------

    def _fetch_dp(self, asin: str | None) -> tuple[Observation | None, str | None]:
        """GET the product page; return (buybox observation or None, the ASIN
        whose AOD should be read)."""
        url = f"{_HOST}/dp/{asin}" if asin else self.product_url
        r = self.session.get(url, headers=_BROWSER_HEADERS, timeout=30)
        r.raise_for_status()
        obs = self.parse(url, r.content)  # raises on captcha/block
        if obs and self._matches(obs):
            return obs, _asin_of(obs)

        # The configured ASIN is a variation family whose default child rotates
        # (256GB one poll, 512GB the next). If the parent didn't land on our
        # target, follow the twister to the 256GB Preto/JetBlack child.
        for child in self._target_children(r.content):
            if child == asin:
                continue
            curl = f"{_HOST}/dp/{child}"
            try:
                cr = self.session.get(curl, headers=_BROWSER_HEADERS, timeout=30)
                cr.raise_for_status()
                cobs = self.parse(curl, cr.content)
            except Exception:
                continue
            if cobs and self._matches(cobs):
                return cobs, child
        return None, asin

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
        """Parse the product page's buybox offer (card + Pix price, plan, seller)."""
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

        card, shown = self._dp_prices(soup, html)
        list_price, pix_discount = _split_pix(card, shown, self._dp_pix_promo(soup))
        if not list_price:
            return None  # no buybox price -> treat as unavailable

        cond_el = soup.select_one(".a-text-bold.a-text-normal")
        grade_raw = _grade_from_heading(_text(cond_el)) if cond_el else None
        # Buybox layout varies; fall back to the condition description text
        # ("...em condição Excelente foi inspecionado...").
        if not grade_raw or grade_raw.lower().startswith("seminovo"):
            m = re.search(
                r"condi[çc][ãa]o\s+(Excelente|Muito Bom|Bom|Aceit[áa]vel|Premium|Como novo)",
                html, re.I)
            if m:
                grade_raw = m.group(1)

        # #availability is missing on many page variants (CI gets them often);
        # then a live add-to-cart button is the in-stock signal.
        at = _text(soup.select_one("#availability")).lower()
        if "indispon" in at or "sem estoque" in at or "não disponível" in at:
            in_stock = False
        elif "estoque" in at:
            in_stock = True
        else:
            in_stock = soup.select_one(
                "#add-to-cart-button, input[name='submit.add-to-cart']") is not None

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
        mid = soup.select_one("input#merchantID")
        seller_id = (mid.get("value") or "").strip() if mid else ""
        if not seller_id:  # same id the AOD reads, so listing_ids agree
            m = _SELLER_ID_RE.search((soup.select_one("#sellerProfileTriggerId") or {})
                                     .get("href", ""))
            seller_id = m.group(1) if m else ""
        asin_el = soup.select_one("input#ASIN")
        m = _DP_ASIN_RE.search(url)
        asin = ((asin_el.get("value") or "").strip() if asin_el else "") \
            or (m.group(1) if m else "") or self.asin or url.rsplit("/", 1)[-1]

        return Observation(
            source_id=self.source_id,
            seller=seller,
            venue=self.venue,
            listing_id=_listing_id(asin, seller_id, seller, grade_raw),
            url=url,
            sku=_SKU,
            storage_gb=_storage_gb(title),
            color=_find_color(title),
            grade_raw=grade_raw,
            battery_band=None,
            list_price=list_price,
            pix_discount=pix_discount,
            coupon=coupon,
            installments_n=installments_n,
            installment_value=installment_value,
            interest_flag=interest_flag,
            in_stock=in_stock,
        )

    @staticmethod
    def _dp_prices(soup, html: str) -> tuple[float | None, float | None]:
        """(card, shown) for the buybox. The card price is in the twister-plus
        JSON (present with and without the Pix promo) or the "(total parcelado
        R$ X)" line; shown is the apex price-to-pay, which is the Pix price
        whenever the Pix/NuPay promo is live."""
        card = None
        tp = soup.select_one(".twister-plus-buying-options-price-data")
        if tp:
            try:
                for group in json.loads(tp.get_text()).values():
                    if group and group[0].get("priceAmount"):
                        card = float(group[0]["priceAmount"])
                        break
            except (ValueError, TypeError, AttributeError, KeyError, IndexError):
                pass
        if card is None:
            m = _PARCELADO_RE.search(html)
            if m:
                card = _brl(m.group(1))

        shown = _first_brl(_text(soup.select_one("#apex-pricetopay-accessibility-label")))
        if shown is None:
            cvp = soup.select_one("input[name='items[0.base][customerVisiblePrice][amount]']")
            try:
                shown = float(cvp["value"]) if cvp and cvp.get("value") else None
            except ValueError:
                shown = None
        if shown is None:
            # First non-empty apex price — never the strike-through "Novo preço"
            # (.apex-basisprice-value) and never a stray a-price elsewhere on the
            # page (those were the junk R$6.887 rows).
            for el in soup.select("#corePrice_feature_div span.a-price span.a-offscreen, "
                                  "#apex_desktop span.a-price span.a-offscreen"):
                if el.find_parent(class_=["apex-basisprice-value", "a-text-price"]):
                    continue
                shown = _first_brl(_text(el))
                if shown is not None:
                    break
        return card, shown

    @staticmethod
    def _dp_pix_promo(soup) -> bool:
        """Is a Pix/NuPay promo shown in the buybox ("5% off à vista no Pix ou
        NuPay")? Only then may a price-to-pay below card count as pix_discount.
        Matches the promo wording, not a bare "Pix" (the payment-methods popover
        inside #apex_desktop lists Pix on every page)."""
        for sel in ("#promotionMessageInsideBuyBox_feature_div",
                    "#oneTimePaymentPrice_feature_div", "#apex_desktop",
                    "#corePrice_feature_div"):
            if _PIX_PROMO_RE.search(_text(soup.select_one(sel))):
                return True
        return False

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

    # --- All Offers Display (every seller) ---------------------------------

    def _fetch_aod(self, asin: str) -> AodPage:
        """GET the AOD panel (plus extra pages only when >10 list offers)."""
        headers = _aod_headers(asin)
        r = self.session.get(_AOD_URL.format(asin=asin), headers=headers, timeout=30)
        reason = aod_block_reason(r.status_code, r.text)
        if reason:
            raise RuntimeError(f"amazon aod: {reason}")
        page = self.parse_aod(asin, r.content)

        # Pages 2+ carry only list offers (no pinned, no total, no title), and
        # asking past the last page repeats offers — so stop at ceil(total/10).
        n_pages = min(math.ceil((page.total or 0) / _AOD_PAGE_SIZE), _AOD_MAX_PAGES)
        for n in range(2, n_pages + 1):
            self._pause()
            try:
                pr = self.session.get(_AOD_PAGE_URL.format(asin=asin, n=n),
                                      headers=headers, timeout=30)
                if aod_block_reason(pr.status_code, pr.text):
                    page.complete = False
                    break
                more = self.parse_aod(asin, pr.content, title=page.title)
            except Exception:
                page.complete = False
                break  # keep what page 1 gave us
            page.offers.extend(o for o in more.offers if o is not more.pinned)
        return page

    def parse_aod(self, asin: str, content: bytes, title: str | None = None) -> AodPage:
        """Parse an AOD response into one Observation per offer.

        Offers are ``#aod-pinned-offer`` (the buybox) plus each ``#aod-offer``;
        ``#aod-sticky-pinned-offer`` duplicates the pinned one and is skipped.
        Sub-element ids repeat per offer, so every lookup is scoped to its block.
        """
        html = content.decode("utf-8", "ignore")
        reason = aod_block_reason(200, html)
        if reason:
            raise RuntimeError(f"amazon aod: {reason}")
        soup = BeautifulSoup(html, "lxml")

        title = _text(soup.select_one("#aod-asin-title-text")) or title
        total_el = soup.select_one("#aod-total-offer-count")
        try:
            total = int(total_el.get("value")) if total_el else None
        except (TypeError, ValueError):
            total = None
        page = AodPage(pinned=None, total=total, title=title)
        if title and "s25 ultra" not in title.lower():
            page.complete = False  # variation drift: AOD for some other product
            return page

        blocks = [(True, b) for b in soup.select("#aod-pinned-offer")]
        blocks += [(False, b) for b in soup.select("#aod-offer")]
        for is_pinned, block in blocks:
            if block.find_parent(id="aod-sticky-pinned-offer"):
                continue
            o = self._aod_offer(asin, block, title)
            if o is None:
                continue
            if is_pinned and page.pinned is None:
                page.pinned = o
            page.offers.append(o)
        return page

    def _aod_offer(self, asin: str, block, title: str | None) -> Observation | None:
        heading = _text(block.select_one("#aod-offer-heading"))  # "Seminovo - Bom"
        grade_raw = _grade_from_heading(heading)

        atc = block.select_one("input[name='submit.addToCart'][aria-label]")
        label = atc.get("aria-label", "") if atc else ""
        m = _ATC_PRICE_RE.search(label)
        card = _brl(m.group(1)) if m else None

        # Price-to-pay (Pix-discounted when the promo applies). The AOD's
        # .a-offscreen is empty, and .apex-basisprice-value is "Novo preço".
        shown = _first_brl(_text(block.select_one(".apex-pricetopay-accessibility-label")))
        if shown is None:
            whole = _text(block.select_one(".apex-pricetopay-value .a-price-whole"))
            frac = _text(block.select_one(".apex-pricetopay-value .a-price-fraction"))
            digits = re.sub(r"\D", "", whole)
            if digits and frac.isdigit():
                shown = float(f"{digits}.{frac}")

        promos: list[str] = []
        for el in block.select("[id^='aod-offer-promotion-'], span[id^='promoMessage']"):
            t = _text(el)
            if t and t not in promos:
                promos.append(t)
        list_price, pix_discount = _split_pix(
            card, shown, any(_PIX_PROMO_RE.search(p) for p in promos))
        if not list_price:
            return None
        coupon = max((coupon_from_text(p, list_price) for p in promos
                      if _COUPON_HINT_RE.search(p)), default=0.0)

        seller, seller_id = self._aod_seller(block)
        if not seller:
            sm = _ATC_SELLER_RE.search(label)
            seller = sm.group(1).strip() if sm else self.seller

        return Observation(
            source_id=self.source_id,
            seller=seller[:60],
            venue=self.venue,
            listing_id=_listing_id(asin, seller_id, seller, grade_raw),
            # deep link to this seller's offer (smid), else the product page
            url=f"{_HOST}/dp/{asin}?smid={seller_id}" if seller_id else f"{_HOST}/dp/{asin}",
            sku=_SKU,
            storage_gb=_storage_gb(title),
            color=_find_color(title or ""),
            grade_raw=grade_raw,
            battery_band=None,
            list_price=list_price,
            pix_discount=pix_discount,
            coupon=coupon,
            installments_n=None,   # AOD shows no plans; the dp merge adds the buybox's
            installment_value=None,
            interest_flag=None,
            in_stock=block.select_one(
                "input[name='submit.addToCart'], [data-aod-atc-action]") is not None,
        )

    @staticmethod
    def _aod_seller(block) -> tuple[str | None, str | None]:
        """(name, seller_id) from the offer's "Vendido por" row. Amazon's own
        offers (Amazon.com.br) have no seller link, only plain text."""
        sold = block.select_one("#aod-offer-soldBy")
        if sold is None:
            return None, None
        name = seller_id = None
        for a in sold.select("a[href*='seller=']"):
            m = _SELLER_ID_RE.search(a.get("href", ""))
            if m and not seller_id:
                seller_id = m.group(1)
            t = _text(a)
            if t and not name and "devolu" not in t.lower():  # skip "Política de devolução"
                name = t
        if not name:
            name = _text(sold.select_one(".a-col-right .a-size-small")) or None
        if not seller_id and _slug(name) == "amazon-com-br":
            seller_id = _AMAZON_SELLER_ID  # the dp's merchantID for Amazon's own offers
        return name, seller_id

    def _matches(self, o: Observation) -> bool:
        if self.match.get("storage_gb") and o.storage_gb != self.match["storage_gb"]:
            return False
        colors = self.match.get("colors")
        if colors and (o.color or "").lower() not in colors:
            return False
        return True


SOURCE = AmazonSeminovosSource
