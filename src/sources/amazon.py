"""Amazon scraper: refurbished (Seminovos) and new-unit S25 Ultra listings.

One registry entry per ASIN (256GB/512GB, Titânio Preto/JetBlack, seminovo or
new are separate products); ``match.storage_gb`` decides which pages and offers
belong to it, and ``default_grade`` names the condition of a buybox that shows
none (a new-unit listing's dp says nothing; its AOD headings say "Novo").

Amazon is robots-hostile and rate-limits aggressively, especially from
datacenter IPs — the GitHub Actions runner may get a captcha where a home IP
succeeds. Blocks are detected (captcha markers, missing #productTitle, error
pages) and *raise*, so the run counts a fetch failure in the heartbeat rather
than silently reporting "no deal" (spec §7 decay warning).

Two pages per ASIN, every poll, on one shared session:

1. ``/dp/{asin}`` — the product page, first. It shows only the *buybox* offer,
   but it is the only place with the installment plan ("12x de R$ 381,90 sem
   juros"), the clip-coupon widget and the buybox promos, and it carries the
   "Comparar outras N ofertas a partir de R$ X (+R$ Y de envio)" line
   (``#aod-ingress-link``, read by ``parse_ingress`` into ``offer_summary``):
   how many offers the ASIN has, buybox included, and the lowest card price
   among them — sellers unnamed. A page without that line has no other
   offers; it gets a no-line marker instead (``_mark_no_line``).
2. ``aodAjaxMain`` — the "Comparar outras ofertas" (All Offers Display) panel:
   every seller's offer, each card price on its add-to-cart label. Without it
   a cheaper non-buybox seller can't be named (Assurant Ecophone at R$3.919
   was missed whenever CellularStore held the buybox). Tried once per poll;
   the dp buybox's plan/promos are merged onto its pinned offer.

Why the dp leads: from GitHub Actions IPs the panel mostly answers HTTP 503
(Oct 2026, two CI diagnostic runs blocked every variant: aodAjaxMain,
/gp/aod/ajax, full Chrome client hints, cold/warm/mobile sessions,
/gp/offer-listing, repeated retries; the live watcher gets it on and off),
while /dp/ always loads — and ``/dp/?smid=<seller>`` does not move its
buybox, so per-seller pages are no way round. In CI the dp is therefore often
the whole picture: the buybox plus the offers line, whose "a partir de" price
below every offer we know raises the 🔍 alert (``triggers.unseen_offers``). A
blocked panel is not retried (the 45 s retry never once helped) and is not
pushback.

Amazon rate-limits bursts from datacenter IPs, so every Amazon request in the
process goes through one ``PACER`` (AMAZON_MIN_GAP_S ± 30% apart, all sources
and the promotion-terms lookup together). fetch() raises only when neither
page gave us anything. ``block_level`` tells the loop whether to back off:
"hard" when the dp itself was blocked (captcha, HTTP error, no title), None
otherwise — the panel failing next to a good dp is CI's steady state.

Price semantics (same as the other sources, so normalize.pv() works):
``list_price`` is the *card* price — what the sem-juros plan totals — and
``pix_discount`` is card minus the shown "à vista no Pix ou NuPay (5% off)"
price. Amazon folds that promo into the displayed price-to-pay, so the card
price comes from elsewhere (dp: twister-plus JSON / "total parcelado"; AOD: the
add-to-cart button's aria-label). The discount is the R$ difference Amazon
shows, not card*pct, and is only credited when a Pix/NuPay promo is visible.
When the card price can't be read but a Pix promo is live, the shown price is
*not* the card price (that understated the 12x total by the Pix %): the card
price is rebuilt from the sem-juros plan (n x installment) or, failing that,
from the promo % (shown / (1 - pct), rounded up); otherwise the offer is
dropped and the AOD page marked incomplete. Each case lands in ``degraded``.
``coupon`` is a coupon Amazon displays on the offer, unless it's conditional
(card, NuPay, Pix/boleto/à vista only, first purchase, app:
``is_conditional_promo``). Both are stored raw even
though Amazon applies only the larger of the two: the user pays in 12x, where
only the coupon counts, so ``normalize`` applies the no-stack rule per payment
path instead of this module discarding one of them.

Every promotion shown on an offer (AOD promo rows, dp buybox promo blocks) is
read one promotion item at a time (Amazon wraps each in a
``data-csa-c-item-id="...amzn1.promotion.<ID>"`` element; hidden "cupom salvo"
nodes dropped) and lands in the transient ``Observation.promos`` with its id in
``promo_ids``, so the orchestrator can announce one it has never seen;
``is_routine_promo`` tells the everyday Pix/NuPay "(5% off)" line apart.

``blocked`` (set by fetch) is real pushback — a dp that raised (captcha, HTTP
error, no title) — as opposed to a panel that failed or came back empty while
the dp parsed fine, so the loop only backs off when Amazon actually resists.

``listing_id`` is ``{asin}:{seller_id}:{grade}`` (e.g.
``B0FTMQQ7L2:A2TO0KKQTM1BAB:excelente``): every seller shares the ASIN, and one
seller can list several grades, so the ASIN alone broke restock detection and
alert dedup. Amazon's offerListingId is a per-request token — never stable.
"""
from __future__ import annotations

import copy
import json
import math
import random
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from ..models import Observation, OfferSummary
from ..normalize import coupon_from_text
from .base import Source, http_session

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
_COUPON_HINT_RE = re.compile(r"cupom|coupon|economize|aplicar|c[oó]digo", re.I)
_PIX_PCT_RE = re.compile(r"(\d{1,2})\s*%\s*off", re.I)
# dp buybox blocks that carry promotion messages (coupon widget, promo rows).
_DP_PROMO_SELECTORS = (
    "#promotionMessageInsideBuyBox_feature_div", "#couponsInBuybox_feature_div",
    "#promoPriceBlockMessage_feature_div", "#applicablePromotionList_feature_div",
    "#quickPromoBucketContent",
)
_PROMO_MAX_CHARS = 200
# A bare installment line ("em até 12x de R$ 381,90 sem juros") is routine too;
# anything naming a discount is not.
_INSTALLMENT_LINE_RE = re.compile(r"\d+\s*x\b", re.I)
_DISCOUNT_WORD_RE = re.compile(
    r"cupom|coupon|economiz|desconto|\boff\b|%|cashback|frete|brinde|\bganhe\b|\bleve\b",
    re.I)
# One promotion inside a promo block: Amazon's item wrapper, else a bare
# message span / coupon label.
_PROMO_ITEM_SEL = ("[data-csa-c-item-id*='amzn1.promotion.'], span[id^='promoMessage'], "
                   "label[id^='couponText']")
_AOD_PROMO_BLOCK = "aod-offer-promotion-"
_OFFER_ID_RE = re.compile(r"""id\s*=\s*["']aod-(?:pinned-)?offer["']""")
_NO_OFFERS = "no offers in panel"   # an empty listing, not necessarily a block
_CAPTCHA_MARKERS = (
    "validateCaptcha", "Digite os caracteres", "Enter the characters",
    "Type the characters you see",
)

_DEFAULT_MATCH = {"storage_gb": 256, "colors": ["preto", "jetblack"]}

AMAZON_MIN_GAP_S = 10.0   # between any two Amazon requests in the process
GAP_JITTER = 0.3          # ± share of the gap


class Pacer:
    """Process-wide spacing between Amazon requests: each waits until
    ``gap`` (± jitter) has passed since the previous one. ``count`` is
    requests made, for the per-poll log. Tests zero ``gap``."""

    def __init__(self, gap: float = AMAZON_MIN_GAP_S, jitter: float = GAP_JITTER,
                 clock=time.monotonic, sleep=time.sleep, uniform=random.uniform):
        self.gap, self.jitter = gap, jitter
        self.clock, self.sleep, self.uniform = clock, sleep, uniform
        self.last: float | None = None
        self.count = 0

    def wait(self) -> None:
        if self.last is not None and self.gap > 0:
            want = self.gap * self.uniform(1 - self.jitter, 1 + self.jitter)
            left = want - (self.clock() - self.last)
            if left > 0:
                self.sleep(left)
        self.last = self.clock()
        self.count += 1


PACER = Pacer()
_SESSION = None                       # shared by every Amazon source (cookies persist)


def amazon_session():
    """The process-wide Amazon session (cookies survive across polls)."""
    global _SESSION
    if _SESSION is None:
        _SESSION = http_session()
    return _SESSION


def amazon_get(session, url: str, **kwargs):
    """GET through the shared pacer."""
    PACER.wait()
    return session.get(url, **kwargs)


def reset_state() -> None:
    """Forget the shared session (tests)."""
    global _SESSION
    _SESSION = None


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
    t = (text or "").lower().replace("jet black", "jetblack")
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
    missing card price means shown is taken as card with no discount — but only
    without a Pix promo: under one, shown is the Pix price, so the card price is
    unknown (None; see ``_derive_card``). A "card" price *below* the
    price-to-pay must belong to some other offer, so the price-to-pay wins.
    """
    if card is None:
        return (None, 0.0) if pix_promo else (shown, 0.0)
    if shown is None:
        return card, 0.0
    if shown < card and pix_promo and card - shown <= card * _MAX_PIX_SHARE:
        return card, round(card - shown, 2)
    if shown > card:
        return shown, 0.0
    return card, 0.0


def _pix_pct(texts) -> float | None:
    """The Pix/NuPay promo percentage ("(5% off)") from the first Pix promo
    line among ``texts``, or None when no Pix promo is shown."""
    for t in texts:
        if t and _PIX_PROMO_RE.search(t):
            m = _PIX_PCT_RE.search(t)
            if m:
                return float(m.group(1))
    return None


def _derive_card(shown: float | None, pct: float | None,
                 plan_total: float | None = None) -> float | None:
    """The card price when only the Pix price is shown: the sem-juros plan's
    total (n x installment, what the user pays in 12x) when it's a plausible
    Pix gap above shown, else shown / (1 - pct) rounded *up* to the cent (an
    overestimate by a cent never fires a false alert; an underestimate might).
    None when neither is available."""
    if shown is None:
        return None
    if plan_total and 0 < plan_total - shown <= plan_total * _MAX_PIX_SHARE:
        return round(plan_total, 2)
    if pct and 0 < pct < 100:
        card = math.ceil(shown / (1 - pct / 100) * 100 - 1e-6) / 100
        if 0 < card - shown <= card * _MAX_PIX_SHARE:
            return card
    return None


def _cash_price(o: Observation) -> float:
    """Pix price under Amazon's no-stack rule (the larger discount applies)."""
    return (o.list_price or math.inf) - max(o.pix_discount or 0.0, o.coupon or 0.0)


def _rank(o: Observation) -> tuple:
    """Which copy of a listing to keep: lowest 12x total (the alert metric),
    then lowest Pix price, then one that carries the installment plan."""
    total = (o.list_price - (o.coupon or 0.0)) if o.list_price is not None else math.inf
    return (total, _cash_price(o), 0 if o.installments_n else 1)


def _full_promo_text(el) -> str:
    """A promo element's visible text, whitespace squashed; hidden nodes (the
    "cupom foi salvo" confirmation) are dropped from a copy first."""
    if el is None:
        return ""
    el = copy.copy(el)
    for hidden in el.select(".aok-hidden"):
        hidden.decompose()
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()


def _outermost(found: list) -> list:
    """Drop elements nested inside another element of ``found``."""
    ids = {id(e) for e in found}
    return [e for e in found if not any(id(p) in ids for p in e.parents)]


def _promo_elements(container) -> list:
    """One element per promotion inside a dp promo block: the item wrappers
    (or bare message spans/labels), else the whole block."""
    if container is None:
        return []
    return _outermost(container.select(_PROMO_ITEM_SEL)) or [container]


def _aod_promo_elements(block) -> list:
    """One element per promotion in an AOD offer, in document order: each
    item inside a div#aod-offer-promotion-N (the whole div only when it holds
    no item), plus bare promoMessage spans outside those divs. A promo used to
    come out twice (div text with link labels + its inner span), and two promos
    sharing a div came out as one string whose Pix "NuPay" voided the coupon."""
    out, items = [], set()
    for el in block.select(f"[id^='{_AOD_PROMO_BLOCK}'], {_PROMO_ITEM_SEL}"):
        if (el.get("id") or "").startswith(_AOD_PROMO_BLOCK):
            if not el.select(_PROMO_ITEM_SEL):
                out.append(el)          # a plain block is one promotion
            continue
        if any(id(p) in items for p in el.parents):
            continue                    # nested inside an item already taken
        items.add(id(el))
        out.append(el)
    return out


_PROMO_ID_RE = re.compile(r"amzn1\.promotion\.([A-Z0-9]{8,20})")


def _promo_id(el) -> str | None:
    """The Amazon promotion id of one promo element: its own data-csa-c-item-id
    ("amzn1.asin.X:amzn1.promotion.<ID>"), an unambiguous single item inside it,
    or an enclosing item's — never a sibling's (stops at the promo block)."""
    if hasattr(el, "select"):
        ids = {m.group(1) for i in el.select("[data-csa-c-item-id*='amzn1.promotion.']")
               if (m := _PROMO_ID_RE.search(i.get("data-csa-c-item-id") or ""))}
        if len(ids) == 1:
            return ids.pop()
        if ids:
            return None                 # a container of several promos: ambiguous
    node = el
    for _ in range(4):
        if node is None or not hasattr(node, "get"):
            return None
        m = _PROMO_ID_RE.search(node.get("data-csa-c-item-id") or "")
        if m:
            return m.group(1)
        if (node.get("id") or "").startswith(_AOD_PROMO_BLOCK):
            return None                 # never borrow a sibling's id from the block
        node = node.parent
    return None


def _collect_promos(elements, list_price_hint=None) -> tuple[list[str], dict[str, str], list[str]]:
    """(display texts, display text -> promo id, full texts) for promo elements,
    de-duplicated. Checks (coupon, conditional) run on the full text; only the
    stored display string is cut to _PROMO_MAX_CHARS."""
    promos: list[str] = []
    ids: dict[str, str] = {}
    full: list[str] = []
    for el in elements:
        text = _full_promo_text(el)
        if not text:
            continue
        shown = text[:_PROMO_MAX_CHARS]
        if shown in promos:
            continue
        promos.append(shown)
        full.append(text)
        pid = _promo_id(el)
        if pid:
            ids[shown] = pid
    return promos, ids, full


def promo_terms(session, promo_id: str, max_chars: int = 240) -> str | None:
    """Public terms of an Amazon promotion (GET /promotion/tnc/<ID>, JSON), cut
    to the first sentences — e.g. the "Até R$ 50 de desconto ... primeira
    compra ... Cartão Amazon" conditions a promo line doesn't show. Best-effort:
    None on any failure."""
    try:
        r = amazon_get(session, f"{_HOST}/promotion/tnc/{promo_id}",
                       headers={**_aod_headers(""), "Referer": _HOST + "/"},
                       timeout=20)
        if r.status_code != 200:
            return None
        content = (r.json().get("tncSectionContentMap") or {}).get("TNC_CONTENT") or ""
    except Exception:
        return None
    text = re.sub(r"\(https?://[^)]*\)", "", content)   # drop category link dumps
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + "…"
    return text or None


def _fold(s: str | None) -> str:
    """Lowercase, accent-free ("1ª compra com cartão" -> "1a compra com cartao")."""
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


# Conditions the user can't meet (or that don't fit paying 12x on their card):
# a specific card (Cartão Amazon, a bank/brand), any bank or wallet named on its
# own ("clientes Itaú", "com Mercado Pago"), NuPay/Nubank, Pix/boleto/à vista
# only, first purchase, app only, or several units ("ao comprar 2 itens").
# Prime-only is fine — the user has Prime.
_CARD_BRANDS = (r"amazon|itau|bradesco|santander|nubank|c6|inter\b|caixa|banco do brasil|"
                r"bb\b|porto|mercado pago|picpay|elo\b|visa|master(?:card)?|amex|"
                r"american express|hipercard|xp\b|btg|neon|pan\b|sicredi|sicoob|ourocard")
_BANKS_AND_WALLETS = (
    r"itau|bradesco|santander|mercado\s+pago|picpay|banco\s+inter|inter|c6(?:\s+bank)?|"
    r"nubank|nupay|caixa|banco\s+do\s+brasil|btg(?:\s+pactual)?|sicredi|sicoob|ourocard|"
    r"banco\s+pan|neon|porto\s+(?:bank|seguro)|american\s+express|amex|hipercard|"
    r"mastercard|visa|ame\s+digital|pagbank|pagseguro|will\s+bank")
_MULTI = r"(?:[2-9]|\d{2,})"   # two or more units
_CONDITIONAL_PROMO_RE = re.compile(
    rf"cart(?:ao|oes)\s+(?:de\s+credito\s+)?(?:d[oa]s?\s+)?(?:{_CARD_BRANDS})"
    rf"|\b(?:{_BANKS_AND_WALLETS})\b"
    r"|\bpix\b|\bboleto\b|\ba\s+vista\b"
    r"|\b(?:primeira|1a)\s+compra|primeiro\s+pedido"
    r"|\b(?:app|aplicativo)\b"
    rf"|\b(?:compr(?:ar|a|e)|leve|levando)\s+(?:de\s+)?{_MULTI}\b"
    rf"|\bna\s+compra\s+de\s+{_MULTI}\b"
    rf"|\b{_MULTI}\s+(?:itens|unidades|item\(ns\)|produtos)")


def is_conditional_promo(message: str | None) -> bool:
    """Does an Amazon-shown promo carry a condition the user can't count on
    (a specific payment card, NuPay/Nubank or Pix/boleto/à vista only, first
    purchase, app only)?
    Such a promo is still announced, but never feeds ``coupon``: the 12x
    total must only fall by discounts the user's own checkout gets."""
    return bool(_CONDITIONAL_PROMO_RE.search(_fold(message)))


def _shown_coupon(text: str | None, list_price: float | None) -> float:
    """R$ off from an Amazon-shown coupon message, unless it's conditional."""
    if is_conditional_promo(text):
        return 0.0
    return coupon_from_text(text, list_price)


_PIX_ONLY_RE = re.compile(r"\bpix\b|\ba\s+vista\b")
_CODE_WORD_RE = re.compile(r"cupom|codigo")
_DISCOUNT_SIGNAL_RE = re.compile(r"r\$|%|cupom|codigo|desconto|economiz|\boff\b|cashback")


def is_routine_promo(message: str | None) -> bool:
    """Lines that never warrant a new-promotion alert: the everyday Pix/NuPay
    "(N% off)" promo and other Pix/à-vista-only price lines without a code
    (their amount moves with the price, so each move would look new), a bare
    installment plan, and lines with no discount at all ("90 dias de Amazon
    Music grátis", "Frete GRÁTIS")."""
    m = message or ""
    f = _fold(m)
    if _PIX_PROMO_RE.search(m):
        return True
    if _PIX_ONLY_RE.search(f) and not _CODE_WORD_RE.search(f):
        return True
    if not _DISCOUNT_SIGNAL_RE.search(f):
        return True
    return bool(_INSTALLMENT_LINE_RE.search(m)) and not _DISCOUNT_WORD_RE.search(m)


def parse_listing_id(listing_id: str) -> tuple[str, str]:
    """(asin, seller_id) from ``{asin}:{seller_id}:{grade}``; seller_id is the
    seller slug when the page hid the id."""
    parts = (listing_id or "").split(":")
    return parts[0], (parts[1] if len(parts) > 1 else "")


# The dp's other-offers line: "Comparar outras 3 ofertas a partir de R$4.582,47
# +R$ 13,22 de envio" (count on accent-folded text; "1 oferta" / "uma oferta"
# singular, "Novo (3)" in older layouts).
_INGRESS_SEL = "a#aod-ingress-link"
_INGRESS_COUNT_RE = re.compile(r"\b(\d+|uma)\s+ofertas?\b|\((\d+)\)")
_INGRESS_SHIP_RE = re.compile(r"\+?\s*R\$\s*([\d.]+,\d{2})\s*de\s+(?:envio|frete)", re.I)
_INGRESS_FREE_RE = re.compile(r"\bgratis\b")
_INGRESS_ASIN_RE = re.compile(r"/offer-listing/([A-Z0-9]{10})")
# The scope is the link's condition= parameter; the ref token ("dp_olp_NEW_mbc")
# only stands in when there is none — it precedes the query string, so one
# pattern for both let a ref like "dp_olp_unknown_mbc" win over condition=NEW.
_INGRESS_COND_RE = re.compile(r"[?&](?:amp;)?condition=([A-Za-z]+)")
_INGRESS_REF_SCOPE_RE = re.compile(r"dp_olp_([A-Za-z]+)_")
_INGRESS_SCOPES = ("ALL", "NEW", "USED")
# Where the line lives on a product page. A page that has this slot but no
# line has no other offers (a single-offer listing; Aug 2026 capture); a page
# without the slot is some other layout and proves nothing.
_OFFERS_SLOT_SEL = "#olpLinkWidget_feature_div, #all-offers-display"


def parse_ingress(page, asin: str | None = None) -> OfferSummary | None:
    """The product page's "Comparar outras N ofertas a partir de R$ X" line
    (``#aod-ingress-link``) as an OfferSummary, or None when the page has no
    such line (a listing with a single offer shows none) or it names no count.

    ``page`` is the parsed soup (or raw HTML). The count is every offer on the
    ASIN, buybox included; the price is the lowest *card* price among them
    (2026-10-22: "3 ofertas a partir de R$4.582,47" was the buybox itself) and
    the shipping is that offer's ("+R$ 13,22 de envio"; GRÁTIS = 0). The ASIN
    and the condition scope come from the link (``/gp/offer-listing/<ASIN>/
    ...?condition=ALL``; new-unit pages say NEW), so a page Amazon served for
    another variation can be told apart from ``asin`` (only the fallback).
    The scope is ``condition=``, else the ref token (``dp_olp_USED_mbc``),
    and None unless it is ALL/NEW/USED (a ref such as ``dp_olp_unknown_mbc``
    names no scope)."""
    soup = page if hasattr(page, "select_one") else BeautifulSoup(page, "lxml")
    link = soup.select_one(_INGRESS_SEL)
    if link is None:
        return None
    text = re.sub(r"\s+", " ", link.get_text(" ", strip=True))
    m = _INGRESS_COUNT_RE.search(_fold(text))
    if not m:
        return None
    n = m.group(1) or m.group(2)
    count = 1 if n == "uma" else int(n)

    price = None
    for el in link.select(".a-price"):
        price = _first_brl(_text(el.select_one(".a-offscreen")))
        if price is None:   # an empty offscreen: rebuild from the visible parts
            whole = re.sub(r"\D", "", _text(el.select_one(".a-price-whole")))
            frac = _text(el.select_one(".a-price-fraction"))
            price = float(f"{whole}.{frac}") if whole and frac.isdigit() else None
        if price is not None:
            break
    if price is None:      # no price widget: the first R$ that isn't the shipping
        rest = _BRL_RE.search(_INGRESS_SHIP_RE.sub(" ", text))
        price = _brl(rest.group(1)) if rest else None

    ship = _INGRESS_SHIP_RE.search(text)
    shipping = (_brl(ship.group(1)) if ship
                else 0.0 if _INGRESS_FREE_RE.search(_fold(text)) else None)

    href = link.get("href") or ""
    am = _INGRESS_ASIN_RE.search(href)
    sm = _INGRESS_COND_RE.search(href) or _INGRESS_REF_SCOPE_RE.search(href)
    scope = sm.group(1).upper() if sm else None
    if scope not in _INGRESS_SCOPES:
        scope = None
    return OfferSummary(asin=am.group(1) if am else (asin or ""), offers_total=count,
                        min_card_price=price, min_shipping=shipping, scope=scope)


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
        return f"{_NO_OFFERS} (empty listing, blocked or layout change)"
    return None


_UNPRICED = object()   # _aod_offer: offer dropped, card price unknown under a Pix promo


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

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.session = amazon_session()
        self.match = {**_DEFAULT_MATCH, **(cfg.get("match") or {})}
        self.asin = cfg.get("asin")
        self.product_url = cfg.get("url") or f"{_HOST}/dp/{self.asin}"
        self.default_grade = cfg.get("default_grade")   # e.g. "Novo" for new units
        # Set by fetch() for the orchestrator: ``complete`` means the result is
        # every live offer (so listings missing from it can be marked sold
        # out); ``degraded`` lists partial-fetch reasons for the run log;
        # ``blocked`` is real pushback (the loop backs off only on that).
        self.complete = False
        self.degraded: list[str] = []
        self.blocked = False
        self.block_level: str | None = None   # "hard" | None
        self.notes: list[str] = []   # parse-level problems (missing card price)
        self.aod_tried = self.aod_ok = 0           # this fetch, for the per-poll log
        self.ingress_tried = self.ingress_ok = 0   # dp requested / offers line read
        # The dp's "Comparar outras N ofertas" line for this ASIN, or the
        # no-line marker when the page showed none (set by parse).
        self.offer_summary: OfferSummary | None = None

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
        self.blocked = False
        self.block_level = None
        self.notes = []
        self.aod_tried = self.aod_ok = 0
        self.ingress_tried = self.ingress_ok = 0
        self.offer_summary = None

        # 1. The product page, every poll: buybox + plan + promos + offers line.
        dp_ok = False
        self.ingress_tried = 1
        try:
            dp, asin = self._fetch_dp(asin)
            dp_ok = True
        except Exception as e:  # captcha, HTTP error, no title: Amazon resisting
            errors.append(f"dp: {e}")
            self.block_level = "hard"
        # Only a real line counts as read; the no-line marker doesn't.
        self.ingress_ok = int(self.offer_summary is not None
                              and not self.offer_summary.no_line)

        # 2. The offers panel, once: every seller's card price when Amazon
        # serves it. From CI it 503s; then the dp + offers line is the whole
        # picture, which is no reason to back off (module docstring).
        if asin:
            self.aod_tried = 1
            try:
                aod = self._fetch_aod(asin)
                self.aod_ok = 1
            except Exception as e:
                errors.append(f"aod: {e}")
        else:
            errors.append("aod: no ASIN configured")

        if aod is None and not dp_ok:  # neither the dp nor the AOD gave us anything
            raise RuntimeError("amazon: " + "; ".join(errors))
        if aod is not None and aod.offers and not any(self._matches(o) for o in aod.offers):
            aod.complete = False  # a panel for some other product never counts
        if aod is not None and not aod.complete:
            errors.append("aod: incomplete (extra page failed, unpriced offer or "
                          "variation drift)")
        errors += self.notes
        if errors:  # degraded but usable — leave a trace in the run log
            print(f"[amazon] partial fetch: {'; '.join(errors)}", file=sys.stderr)
            self.degraded = errors
        self.blocked = self.block_level is not None
        self.complete = aod is not None and aod.complete
        return self.combine(dp, aod)

    def combine(self, dp: Observation | None, aod: AodPage | None) -> list[Observation]:
        """Merge the dp buybox into the AOD offers; keep target offers, one per
        listing_id: the lowest 12x total wins (``_rank``), and an installment
        plan on the dropped copy at the same card price is kept."""
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
            cur = best.get(o.listing_id)
            if cur is None:
                best[o.listing_id] = o
                continue
            keep, drop = (o, cur) if _rank(o) < _rank(cur) else (cur, o)
            if (not keep.installments_n and drop.installments_n
                    and keep.list_price == drop.list_price):
                keep.installments_n, keep.installment_value, keep.interest_flag = \
                    drop.installments_n, drop.installment_value, drop.interest_flag
            best[o.listing_id] = keep
        return list(best.values())

    @staticmethod
    def _merge_buybox(pinned: Observation, dp: Observation) -> bool:
        """Copy the dp installment plan, clip coupon and promo messages onto
        the AOD pinned offer when both describe the same grade at the same card
        price — a different price means the buybox moved between the two
        requests and the plan would be stale. The seller keys normally agree too, but a page that
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
        for msg in dp.promos:
            if msg not in pinned.promos:
                pinned.promos.append(msg)
                if msg in dp.promo_ids:
                    pinned.promo_ids[msg] = dp.promo_ids[msg]
        return True

    # --- product page (buybox) --------------------------------------------

    def _fetch_dp(self, asin: str | None) -> tuple[Observation | None, str | None]:
        """GET the product page; return (buybox observation or None, the ASIN
        whose AOD should be read)."""
        url = f"{_HOST}/dp/{asin}" if asin else self.product_url
        r = amazon_get(self.session, url, headers=_BROWSER_HEADERS, timeout=30)
        r.raise_for_status()
        obs = self.parse(url, r.content)  # raises on captcha/block
        if obs and self._matches(obs):
            return obs, _asin_of(obs)

        # The configured ASIN is a variation family whose default child rotates
        # (256GB one poll, 512GB the next). If the parent didn't land on our
        # target, follow the twister to the configured storage's Preto child.
        for child in self._target_children(r.content):
            if child == asin:
                continue
            curl = f"{_HOST}/dp/{child}"
            try:
                cr = amazon_get(self.session, curl, headers=_BROWSER_HEADERS, timeout=30)
                cr.raise_for_status()
                cobs = self.parse(curl, cr.content)
            except Exception:
                continue
            if cobs and self._matches(cobs):
                return cobs, child
        return None, asin

    def _target_children(self, content: bytes) -> list[str]:
        """ASINs from the variation twister whose values match the target
        storage + color (so we can deep-link e.g. the 256GB Preto child)."""
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
        """Parse the product page's buybox offer (card + Pix price, plan,
        seller); along the way, its offers line or no-line marker goes to
        ``offer_summary``."""
        html = content.decode("utf-8", "ignore")
        if any(m in html for m in _CAPTCHA_MARKERS):
            raise RuntimeError("amazon: captcha / bot wall")
        soup = BeautifulSoup(html, "lxml")

        title_el = soup.select_one("#productTitle")
        if not title_el:
            raise RuntimeError("amazon: no product title (blocked or layout change)")
        title = title_el.get_text(strip=True)
        if not self._is_target_title(title):
            return None  # wrong product (other model or the other storage)

        asin_el = soup.select_one("input#ASIN")
        m = _DP_ASIN_RE.search(url)
        asin = ((asin_el.get("value") or "").strip() if asin_el else "") \
            or (m.group(1) if m else "") or self.asin or url.rsplit("/", 1)[-1]
        # Before the buybox: a page without a buybox price still has the
        # offers line, and then it is all we know. The no-line marker is the
        # buybox's own offer, so it waits for the buybox.
        no_line = self._read_ingress(soup, asin, title)
        obs = self._parse_buybox(soup, html, url, asin, title)
        if no_line and obs is not None and self._matches(obs):
            self._mark_no_line(asin, obs)
        return obs

    def _parse_buybox(self, soup, html: str, url: str, asin: str,
                      title: str) -> Observation | None:
        """The buybox offer of a target product page, or None when it has no
        usable price."""
        card, shown = self._dp_prices(soup, html)
        pix_pct = self._dp_pix_pct(soup)   # None when no Pix promo is shown

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

        if card is None and pix_pct is not None and shown is not None:
            plan_total = (installments_n * installment_value
                          if installments_n and installment_value else None)
            card = _derive_card(shown, pix_pct, plan_total)
            self.notes.append("dp: card price missing under Pix promo"
                              + ("" if card is None else f" (rebuilt: {card:.2f})"))
            if card is None:
                return None  # unknown card price: never alert on the Pix price
        list_price, pix_discount = _split_pix(card, shown, pix_pct is not None)
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
        if not grade_raw:
            grade_raw = self.default_grade   # new-unit listings name no condition

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

        # First block per selector only: pages with a new/used accordion repeat
        # these ids for the secondary offer, whose messages aren't the buybox's.
        elements = [el for sel in _DP_PROMO_SELECTORS
                    for el in _promo_elements(soup.select_one(sel))]
        promos, promo_ids, full = _collect_promos(elements)
        # clip coupons / codes shown in the buybox (fold into the 12x total)
        coupon = max((_shown_coupon(t, list_price) for t in full
                      if _COUPON_HINT_RE.search(t)), default=0.0)

        seller = self._seller(soup)
        mid = soup.select_one("input#merchantID")
        seller_id = (mid.get("value") or "").strip() if mid else ""
        if not seller_id:  # same id the AOD reads, so listing_ids agree
            m = _SELLER_ID_RE.search((soup.select_one("#sellerProfileTriggerId") or {})
                                     .get("href", ""))
            seller_id = m.group(1) if m else ""

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
            promos=promos,
            promo_ids=promo_ids,
        )

    def _read_ingress(self, soup, asin: str, title: str) -> bool:
        """Keep the page's offers line as ``offer_summary`` — only for the
        configured ASIN: the parent page may have been served for another
        variation, whose offers aren't ours. A line that is there but can't be
        read is a layout change worth a partial-fetch note.

        Returns True when the page shows *no* other offers: it is the
        configured ASIN's page and has the slot the line lives in, but no line
        (``_OFFERS_SLOT_SEL``). The caller then records the no-line marker
        once the buybox is known (``_mark_no_line``)."""
        summary = parse_ingress(soup, asin)
        if summary is None:
            if soup.select_one(_INGRESS_SEL) is not None:
                self.notes.append("dp: offers line unreadable (layout change?)")
                return False
            return ((not self.asin or asin == self.asin)
                    and soup.select_one(_OFFERS_SLOT_SEL) is not None)
        if self.asin and summary.asin != self.asin:
            return False
        summary.storage_gb, summary.color = _storage_gb(title), _find_color(title)
        summary.source_id = self.source_id
        self.offer_summary = summary
        return False

    def _mark_no_line(self, asin: str, buybox: Observation) -> None:
        """The configured ASIN's page loaded without the other-offers line, so
        its buybox is the only offer: record that as the no-line marker
        (``OfferSummary.no_line``) — 1 offer at the buybox's card price, or 0
        when the buybox is unavailable. It makes the heartbeat drop a line
        that vanished and tells the 🔍 ladder the hidden offer left. Only ever
        from a page that parsed with a target buybox: a blocked page raised
        before getting here, and a page with no buybox price may keep its
        offers behind "ver todas as opções de compra"."""
        live = bool(buybox.in_stock)
        self.offer_summary = OfferSummary(
            asin=asin, offers_total=int(live),
            min_card_price=buybox.list_price if live else None,
            scope=OfferSummary.NO_LINE, storage_gb=buybox.storage_gb,
            color=buybox.color, source_id=self.source_id)

    def _is_target_title(self, title: str | None) -> bool:
        """An S25 Ultra whose storage (first non-RAM size in the title) is the
        configured ``match.storage_gb``."""
        if "s25 ultra" not in (title or "").lower():
            return False
        want = self.match.get("storage_gb")
        return not want or _storage_gb(title) == want

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
    def _dp_pix_pct(soup) -> float | None:
        """The Pix/NuPay promo % shown in the buybox ("5% off à vista no Pix ou
        NuPay"), or None. Only with it may a price-to-pay below card count as
        pix_discount. Matches the promo wording, not a bare "Pix" (the
        payment-methods popover inside #apex_desktop lists Pix on every page)."""
        return _pix_pct(_text(soup.select_one(sel)) for sel in (
            "#promotionMessageInsideBuyBox_feature_div",
            "#oneTimePaymentPrice_feature_div", "#apex_desktop",
            "#corePrice_feature_div"))

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
        r = amazon_get(self.session, _AOD_URL.format(asin=asin), headers=headers, timeout=30)
        reason = aod_block_reason(r.status_code, r.text)
        if reason:
            raise RuntimeError(f"amazon aod: {reason}")
        page = self.parse_aod(asin, r.content)

        # Pages 2+ carry only list offers (no pinned, no total, no title), and
        # asking past the last page repeats offers — so stop at ceil(total/10).
        n_pages = min(math.ceil((page.total or 0) / _AOD_PAGE_SIZE), _AOD_MAX_PAGES)
        for n in range(2, n_pages + 1):
            try:
                pr = amazon_get(self.session, _AOD_PAGE_URL.format(asin=asin, n=n),
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
        if not self._is_target_title(title):
            # missing title, another model, or the sibling storage's panel:
            # nothing here can prove which listings sold out
            page.complete = False
            return page

        blocks = [(True, b) for b in soup.select("#aod-pinned-offer")]
        blocks += [(False, b) for b in soup.select("#aod-offer")]
        unpriced = 0
        self._derived = 0
        for is_pinned, block in blocks:
            if block.find_parent(id="aod-sticky-pinned-offer"):
                continue
            o = self._aod_offer(asin, block, title)
            if o is _UNPRICED:
                unpriced += 1
                page.complete = False   # a dropped offer must not read as sold out
                continue
            if o is None:
                continue
            if is_pinned and page.pinned is None:
                page.pinned = o
            page.offers.append(o)
        if unpriced:
            self.notes.append(f"aod: {unpriced} offer(s) without card price under Pix promo")
        if self._derived:
            self.notes.append(f"aod: {self._derived} card price(s) rebuilt from the Pix %")
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

        promos, promo_ids, full = _collect_promos(_aod_promo_elements(block))
        pix_pct = _pix_pct(full)
        if card is None and pix_pct is not None:
            card = _derive_card(shown, pix_pct)
            if card is None:
                return _UNPRICED  # the shown price is the Pix price: never use it
            self._derived = getattr(self, "_derived", 0) + 1
        list_price, pix_discount = _split_pix(card, shown, pix_pct is not None)
        if not list_price:
            return None
        coupon = max((_shown_coupon(t, list_price) for t in full
                      if _COUPON_HINT_RE.search(t)), default=0.0)

        seller, seller_id = self._aod_seller(block)
        if not seller:
            sm = _ATC_SELLER_RE.search(label)
            seller = sm.group(1).strip() if sm else self.seller

        obs = Observation(
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
            promos=promos,
            promo_ids=promo_ids,
        )
        return obs

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
