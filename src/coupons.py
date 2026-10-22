"""Amazon checkout codes + cashback portals (informational tracker).

Why this exists: the user buys the refurb S25 Ultra on amazon.com.br and wants
to know when a checkout code or a cashback portal could lower the total.
Research (2026-10-22) narrowed down what can actually help:

- Seller clip coupons require a *new* item, so seminovo listings never carry
  them; the 5% Pix/NuPay discount is parsed by the Amazon scraper, not here.
- Checkout codes (CUPOMPRIME10, OIPRIME15, ...) are small (capped R$20-200),
  often Prime/app/first-purchase/card/category-locked, one per order, and
  normally not combinable with the Pix promo. Whether Amazon accepts one for a
  *third-party* refurb seller can only be checked in a logged-in cart.
- Every Amazon cashback portal has been at 0% since Apr 2026 (Méliuz flag false,
  Inter Shop fullCashbackValue 0, Cuponomia store cashback empty).

So this module never touches PV. It collects the codes four aggregators list
(Promobit, Pelando, Cuponomia, Méliuz), estimates the most each could take off a
given price, classifies its restrictions, and watches three portals for
cashback turning back on. It runs inside the poll, so ``scan_codes`` and
``scan_cashback`` never raise: they return what they got plus an error list.
"""
from __future__ import annotations

import base64
import json
import re
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

PROMOBIT_URL = "https://www.promobit.com.br/cupons/loja/amazon/"
PELANDO_URL = "https://www.pelando.com.br/cupons-de-descontos/amazon"
CUPONOMIA_URL = "https://www.cuponomia.com.br/desconto/amazon"
MELIUZ_URL = "https://www.meliuz.com.br/desconto/cupom-desconto-amazon"
INTER_URL = "https://shopping.inter.co/site-parceiro/lojas/amazon"

# Restriction flags, in display order. 'no_phone' (terms exclude phones) and
# 'later_order' (the reward lands on a later order) are read per listing so a
# merge keeps them even when another source's title/terms survive.
FLAGS = ("prime", "app", "first_purchase", "card", "category", "sitewide", "phone",
         "no_phone", "later_order")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _UA,
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
_TIMEOUT = 20

# A value above this share of the price isn't a code that applies to a refurb
# phone: real Amazon BR codes cap at R$20-200 (<= 5% here), so a bigger number
# means the aggregator dropped the cap/minimum or the code targets one specific
# product (SIG3000 "R$3.000 OFF em Smartphones", DEVOLTA500 after an iPhone).
MAX_SHARE = 0.10


# --- data ----------------------------------------------------------------------

@dataclass
class CouponCode:
    """One Amazon checkout code as an aggregator lists it.

    ``source`` is the aggregator id ('promobit'); after ``merge_codes`` it can
    name several, comma-joined in scan order ('promobit,meliuz'). Money fields
    are R$, ``pct`` is a percentage (10.0 = 10%), ``until`` an ISO date
    (YYYY-MM-DD) when the source states one. ``active`` is the source's own
    status (expired sections / Pelando deal status); date expiry is applied by
    ``relevant_codes``. ``flags`` come from ``FLAGS``.
    """
    source: str
    code: str
    title: str = ""
    terms: str = ""
    url: str | None = None
    pct: float | None = None
    abs_off: float | None = None
    cap: float | None = None
    min_order: float | None = None
    until: str | None = None
    active: bool = True
    flags: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.code.strip().upper()

    def value_at(self, price: float) -> float:
        """Most this code could take off an order of ``price`` (R$) under its
        stated terms: 0 when inactive or below ``min_order``; a percentage is
        capped at ``cap``; a fixed amount is taken as is. An upper bound only:
        eligibility for the listing is never verified."""
        if not self.active or not price or price <= 0:
            return 0.0
        if self.min_order is not None and price < self.min_order:
            return 0.0
        if self.pct is not None:
            value = price * self.pct / 100
        elif self.abs_off is not None:
            value = self.abs_off
        else:
            return 0.0
        if self.cap is not None:
            value = min(value, self.cap)
        return round(min(value, price), 2)

    @classmethod
    def from_row(cls, row) -> "CouponCode":
        """Rebuild from a ``coupon_codes`` row (sqlite3.Row or dict). That table
        stores the key as ``code``, the sources as ``sources`` and the flags
        comma-joined."""
        keys = set(row.keys())

        def get(name):
            return row[name] if name in keys else None

        flags = get("flags") or []
        if isinstance(flags, str):
            flags = [f for f in flags.split(",") if f]
        active = get("active")
        return cls(
            source=get("sources") or get("source") or "",
            code=get("code") or "",
            title=get("title") or "",
            terms=get("terms") or "",
            url=get("url"),
            pct=_opt_float(get("pct")),
            abs_off=_opt_float(get("abs_off")),
            cap=_opt_float(get("cap")),
            min_order=_opt_float(get("min_order")),
            until=get("until"),
            active=True if active is None else bool(active),
            flags=list(flags),
        )


@dataclass
class CashbackStatus:
    """Amazon cashback on one portal at scan time. ``rate_pct`` is 0.0 when the
    portal says there is none, None when it's on but no rate could be read."""
    portal: str
    store: str = "amazon"
    rate_pct: float | None = None
    active: bool = False
    url: str = ""
    detail: str = ""


def _opt_float(v) -> float | None:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


# --- pt-BR text helpers --------------------------------------------------------

def _text(html: str | bytes) -> str:
    """Pelando and Inter send no charset header (requests then guesses
    ISO-8859-1 and garbles accents); every source is UTF-8, so decode bytes
    ourselves."""
    if isinstance(html, bytes):
        return html.decode("utf-8", "replace")
    return html or ""


def _fold(s: str | None) -> str:
    """Lowercase, accent-free text so the regexes below stay ASCII
    ('Máximo R$20' -> 'maximo r$20')."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in s if not unicodedata.combining(ch)).lower()


def _squash(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _el_text(el) -> str:
    return _squash(el.get_text(" ", strip=True)) if el is not None else ""


# Money digits: '1.499', '1.499,90', '1499', '150', '20,00'.
_NUM = r"\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:,\d{1,2})?"
_MONEY_RE = re.compile(rf"r\$\s*({_NUM})")
_PCT_RE = re.compile(r"(\d{1,3}(?:[.,]\d+)?)\s*%")
# A real "R$ X OFF" / "R$X de desconto" (titles also hold caps like
# "Limitado a R$ 40 OFF", so a percentage always wins over this).
_ABS_RE = re.compile(
    rf"r\$\s*({_NUM})\s*(?:de\s+)?(?:off|desconto)\b|desconto\s+de\s+r\$\s*({_NUM})")


def _num(digits: str) -> float:
    return float(digits.replace(".", "").replace(",", "."))


def parse_brl(text: str | None) -> float | None:
    """First R$ amount in ``text``: 'R$ 1.499' -> 1499.0, 'R$150' -> 150.0,
    'R$ 20,00' -> 20.0."""
    m = _MONEY_RE.search(_fold(text))
    return _num(m.group(1)) if m else None


def parse_pct(text: str | None) -> float | None:
    """First percentage in ``text``: '10% OFF' -> 10.0, '1,5%' -> 1.5."""
    m = _PCT_RE.search(_fold(text))
    return float(m.group(1).replace(",", ".")) if m else None


def _discount(text: str, bare: bool = False) -> tuple[float | None, float | None]:
    """(pct, abs_off) from a discount label or title. ``bare`` labels
    ('R$ 20', 'R$350 OFF') take any R$ amount; titles need 'OFF'/'desconto'."""
    pct = parse_pct(text)
    if pct is not None:
        return pct, None
    m = (_MONEY_RE if bare else _ABS_RE).search(_fold(text))
    if m:
        return None, _num(next(g for g in m.groups() if g))
    return None, None


# An amount must end there (not '1' of '18/10' or '1.499'), and must not be a
# count ('1 item', '2 unidades', '12x', '18 anos').
_END = r"(?![\d/]|[.,]\d)(?!\s*(?:%|x\b|ite|unid|produto|ano|dia|hora|mes|vez|pec))"
# A number without 'R$' is accepted ('acima de 50 limite de 30', Pelando titles)
# unless it reads as a usage limit, a count or a date ('limitado a 1 (um) uso',
# '500 primeiros resgates', '2 utilizações', '3 parcelas', '5 mil cupons').
_BARE = (r"(?!\s*(?:\(|por\b|uso|utiliz|cupo|pedido|cliente|cpf|resgate|primeir|pessoa|"
         r"compra|parcela|mil\b|"
         r"de\s+(?:jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)))")
_AMT = rf"(?:r\$\s*({_NUM}){_END}|({_NUM}){_END}{_BARE})"
_MIN_RE = re.compile(
    rf"(?:a\s+partir\s+de|acima\s+de|minim[oa](?:\s+de)?|(?:compras?|pedidos?)\s+de"
    rf"|off\s+em)\s*{_AMT}")
_CAP_LEAD = r"(?:limitad[oa]\s+a|limite\s+de)\s*"
# Caps written in R$: 'limitado a R$40', 'limite de R$100', 'máx R$20', 'desconto
# máximo de R$ 200', 'valor máximo do desconto: R$ 80', 'até R$50 de desconto'.
_CAP_RE = re.compile(
    rf"{_CAP_LEAD}r\$\s*({_NUM}){_END}"
    rf"|max(?:im[oa]|\.)?(?:\s*(?:(?:de|do|desconto|e)\b|:))*\s*r\$\s*({_NUM})"
    rf"|ate\s+r\$\s*({_NUM})\s*(?:de\s+)?(?:desconto|off)\b")
# 'limite de 30' with no R$ (Pelando titles). Only read when no R$ cap appears
# anywhere: terms like 'limitado a 300 participantes ... máximo de R$ 60' put
# an unlisted count word after the number, and the R$ figure is the real cap.
_BARE_CAP_RE = re.compile(rf"{_CAP_LEAD}({_NUM}){_END}{_BARE}")


def _first_amount(rx: re.Pattern, text: str) -> float | None:
    m = rx.search(_fold(text))
    if not m:
        return None
    return _num(next(g for g in m.groups() if g))


def min_order_from(text: str) -> float | None:
    """Minimum order: 'a partir de R$100', 'acima de R$150', 'pedido mínimo
    R$ 40', 'em compras de R$450', 'R$ 10 OFF em R$ 40'."""
    return _first_amount(_MIN_RE, text)


def cap_from(text: str) -> float | None:
    """Discount cap: 'limitado a R$40', 'máx R$20', 'no máximo R$20',
    'com limite de R$100', 'até R$50 de desconto'; 'limite de 30' only when
    no R$ cap is stated (else 'limitado a 1 (um) uso ... máximo de R$ 200'
    would read as R$1)."""
    cap = _first_amount(_CAP_RE, text)
    return cap if cap is not None else _first_amount(_BARE_CAP_RE, text)


_MONTHS = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5, "junho": 6,
    "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11,
    "dezembro": 12,
}
# 'Válido até terça-feira 20 de outubro de 2026' / 'válido até 31/10/2026'.
# A date without a year is ignored rather than guessed.
_UNTIL_RE = re.compile(
    r"(?:valid[oa]s?|vale|expira(?:\s+em)?)\s+(?:ate\s+)?"
    r"(?:[a-z]+-feira,?\s+|sabado,?\s+|domingo,?\s+)?(?:o\s+)?(?:dia\s+)?"
    r"(\d{1,2})(?:\s+de\s+([a-z]+)\s+de\s+(\d{4})|/(\d{1,2})/(\d{4}))")


def _iso_date(value) -> str | None:
    """'2026-12-31T17:59:59-0300' -> '2026-12-31'."""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", str(value or ""))
    return m.group(1) if m else None


def until_from(text: str) -> str | None:
    m = _UNTIL_RE.search(_fold(text))
    if not m:
        return None
    day, month_name, year, month_num, year2 = m.groups()
    month = _MONTHS.get(month_name or "") if month_name else int(month_num)
    try:
        return date(int(year or year2), month, int(day)).isoformat()
    except (TypeError, ValueError):
        return None


# --- classification ------------------------------------------------------------

_PRIME_RE = re.compile(r"\bprime\b")
_APP_RE = re.compile(r"\bapp\b|aplicativo")
_FIRST_RE = re.compile(
    r"primeira compra|1a compra|primeiro pedido|novos? clientes?|novos? usuarios?")
# A specific card / bank / payment method ('[CC Amazon]', 'cartão Porto Bank').
_CARD_RE = re.compile(
    r"\bcartao\b|\bcc\b|porto\s*bank|bradesco|mastercard|\bvisa\b|\belo\b|"
    r"nubank|nupay|itau|santander|\bc6\b|picpay|\bbanco\b")
# 'todos os produtos' / 'qualquer produto' only count when not narrowed right
# after ('todos os produtos de beleza' is a category; 'do site' is still all).
# relevant_codes lets 'sitewide' outrank 'category', so a false one here would
# ping a category code.
_SITEWIDE_RE = re.compile(
    r"todo o site|todo site|site todo|site inteiro|todas as categorias|"
    r"(?:qualquer (?:produto|compra|item)|todos os produtos)"
    r"(?!\s+(?:d[aeo]s?|em)\s+(?!site\b|amazon\b|loja\b|carrinho\b|tod[ao]s?\b|"
    r"qualquer\b|r\$|\d))")
_PHONE_RE = re.compile(r"celular|smartphone|eletronico|samsung|galaxy|telefon")
# Named categories / brands that aren't this phone, plus curated selections
# ('itens selecionados', 'itens do link'), which lock the code just the same.
# Not a bare 'do link': 'ative o cupom através do link' is how sitewide codes
# are activated.
_CATEGORY_RE = re.compile(
    r"\b(?:ferrament|livro|brinquedo|pet\b|beleza|vinho|bebida|notebook|informatica|"
    r"moda\b|bebe\b|baby|philips|cozinha|casa\b|decorac|eletroportat|eletrodomest|"
    r"automotiv|carro\b|jardim|piscina|esport|fitness|snack|relogio|marmita|"
    r"instrumento|vinil|constru|reforma|papelaria|escritorio|garrafa|cuidados pessoais|"
    r"maquiagem|skincare|cabelo|pesca\b|futebol|chuteira|cama\b|banho\b|saude|"
    r"bem-estar|video ?games?|console|games?\b|alexa|kindle|echo\b|fire tv|acessorio|"
    r"puma\b|luuna|inflow|bigens|lyor|positivo|vaio|l.?oreal|johnson|arqueiro|iphone|"
    r"ipad|macbook|apple\b|mercearia|fralda|higiene|alimento|suplemento|roupa|calcado|"
    r"tenis\b|mochila|viralizad|alcoolic|perfum)"
    r"|selecionad|especificad|(?:da|uma) selecao|(?:itens?|item|produtos?) do link")
# An exclusion clause, from its lead word to the end of the clause ('.' or ';',
# but not the '.' inside 'R$ 1.000'): 'exceto livros, eBooks Kindle e Alexa',
# 'não válido para smartphones', 'não se aplica a ...', 'não inclui ...'.
_EXCL_LEAD = (r"(?:exceto|exclui\w*|nao\s+(?:e\s+)?valid\w*\s+(?:para|em)"
              r"|nao\s+se\s+aplica\w*|nao\s+inclui\w*)\b")
_EXCL_RE = re.compile(rf"{_EXCL_LEAD}(?:[^.;]|\.(?=\d))*")
# 'exceto celulares', 'não válido para smartphones': an exclusion naming phones.
_PHONE_EXCL_RE = re.compile(
    rf"{_EXCL_LEAD}(?:[^.;]|\.(?=\d))*?(?:celular|smartphone|eletronico|telefon)"
    rf"(?:[^.;]|\.(?=\d))*")
# A reward for a later order ('compre um iPhone e ganhe R$500 na próxima compra').
_LATER_ORDER_RE = re.compile(r"proxim[oa] (?:compra|pedido)")


def classify(code: str, title: str, terms: str) -> list[str]:
    """Restriction flags from the code and its title/terms (see ``FLAGS``).
    Codes named *PRIME* are Prime-member codes on Amazon BR even when the
    aggregator's text doesn't say so.

    Category, phone and sitewide words are looked for with the exclusion
    clauses blanked out: 'todo o site, exceto livros e Alexa' names categories
    only to rule them out, so it's sitewide, not category-locked. Phone
    exclusions become 'no_phone' instead, and a later-order reward
    'later_order'; both are read here, per listing, because a merged code
    keeps only its first listing's text."""
    text = _fold(f"{title} {terms}")
    scoped = _EXCL_RE.sub(" ", text)
    key = (code or "").upper()
    found = set()
    if _PRIME_RE.search(text) or "PRIME" in key:
        found.add("prime")
    if _APP_RE.search(text) or "NOAPP" in key:
        found.add("app")
    if _FIRST_RE.search(text):
        found.add("first_purchase")
    if _CARD_RE.search(text) or "CARTAO" in key:
        found.add("card")
    sitewide = bool(_SITEWIDE_RE.search(scoped))
    if sitewide:
        found.add("sitewide")
    phone = bool(_PHONE_RE.search(scoped))
    if phone:
        found.add("phone")
    if _CATEGORY_RE.search(scoped) or (phone and not sitewide):
        found.add("category")
    if _PHONE_EXCL_RE.search(text):
        found.add("no_phone")
    if _LATER_ORDER_RE.search(text):
        found.add("later_order")
    return [f for f in FLAGS if f in found]


# Link-activated offers listed with a fake "code" (nothing to type at checkout).
_PLACEHOLDERS = {
    "CUPOMAUTOMATICO", "OFERTANOLINK", "CUPOMNOLINK", "DESCONTONOLINK", "ATIVENOLINK",
    "RESGATEAQUI", "GANHEAQUI", "APROVEITEAQUI", "PEGUEAQUI", "ACESSEEGANHE",
    "CLIQUEEGANHE", "DESCONTOJAATIVO", "DIRETONOLINK", "ATIVEOCUPOMNOLINK",
}
_PLACEHOLDER_RE = re.compile(r"NOLINK|NOCARRINHO|AUTOMATIC|JAATIVO|AQUI$|EGANHE$")


def is_placeholder(code: str | None) -> bool:
    """True for empty codes and stand-ins like CUPOMAUTOMATICO, OFERTANOLINK or
    'Direto no link' (real Amazon codes are one alphanumeric token)."""
    c = (code or "").strip().upper()
    if not c or re.search(r"\s", c):
        return True
    return c in _PLACEHOLDERS or bool(_PLACEHOLDER_RE.search(c))


def _clean_code(raw) -> str:
    # Promobit footnotes some codes with '*' ('PRIME10DO10*').
    return _squash(str(raw or "")).strip("*").strip()


def _make(source: str, code: str, title: str, terms: str, url: str | None, *,
          label: str = "", until: str | None = None, active: bool = True,
          extra_flags: Iterable[str] = ()) -> CouponCode:
    """Build a CouponCode, reading discount/min/cap/expiry from the texts."""
    pct, abs_off = _discount(label, bare=True) if label else (None, None)
    if pct is None and abs_off is None:
        pct, abs_off = _discount(title)
    if pct is None and abs_off is None:
        pct, abs_off = _discount(terms)
    text = f"{title} {terms}"
    flags = set(classify(code, title, terms)) | set(extra_flags)
    return CouponCode(
        source=source, code=code, title=title, terms=terms, url=url,
        pct=pct, abs_off=abs_off, cap=cap_from(text), min_order=min_order_from(text),
        until=until or until_from(text), active=active,
        flags=[f for f in FLAGS if f in flags],
    )


# --- parsers -------------------------------------------------------------------

def _next_data(html: str) -> dict | None:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


# Promobit status names that mean the coupon is over (all 40 live ones read
# APPROVED; same vocabulary as its deals, see promos.py).
_DEAD_STATUSES = {"FINISHED", "EXPIRED", "REMOVED", "PAUSED", "INACTIVE",
                  "REJECTED", "DELETED", "OFFLINE"}


def parse_promobit(html: str | bytes) -> list[CouponCode]:
    """Promobit Next.js page: props.pageProps.serverCoupons.coupons[]. Entries
    without a couponCode are auto-applied promos and are skipped."""
    data = _next_data(_text(html)) or {}
    try:
        coupons = data["props"]["pageProps"]["serverCoupons"]["coupons"] or []
    except (KeyError, TypeError):
        return []
    out = []
    for c in coupons:
        code = _clean_code(c.get("couponCode"))
        if not code:
            continue
        status = str(c.get("couponStatusName") or "").upper()
        url = urljoin(PROMOBIT_URL, c["couponUrl"]) if c.get("couponUrl") else None
        out.append(_make(
            "promobit", code, _squash(c.get("couponTitle")),
            _squash(c.get("couponInstructions")), url,
            label=c.get("couponDiscount") or "", until=_iso_date(c.get("couponUntil")),
            active=status not in _DEAD_STATUSES,
        ))
    return out


def _astro_prop(props: dict, name: str):
    # Astro serializes island props as {"name": [type, value]}.
    v = props.get(name)
    return v[1] if isinstance(v, list) and len(v) > 1 else None


def _pelando_active(card) -> bool:
    """The Temperature island's dealStatus ('active'/'expired') when present,
    else the 'Expirada' badge text."""
    island = card.select_one("astro-island[component-url*='Temperature']")
    if island is not None and island.get("props"):
        try:
            props = json.loads(island["props"])
        except json.JSONDecodeError:
            props = {}
        status = _astro_prop(props, "dealStatus")
        if status:
            return status == "active"
        inactive = _astro_prop(props, "isInactive")
        if inactive is not None:
            return not inactive
    return "expirad" not in _fold(card.get_text(" "))


def parse_pelando(html: str | bytes) -> list[CouponCode]:
    """Pelando (Astro SSR): article.card[data-deal-id] with the code in
    [data-copy-code]; discount/min/cap live in the community-written title."""
    soup = BeautifulSoup(_text(html), "lxml")
    out = []
    for card in soup.select("article.card[data-deal-id]"):
        code_el = card.select_one("[data-copy-code]")
        code = _clean_code(code_el.get("data-copy-code") if code_el else "")
        if not code:
            continue
        link = card.select_one("a[href*='/d/']")
        url = urljoin(PELANDO_URL, link["href"]).split("?")[0] if link else None
        out.append(_make(
            "pelando", code, _el_text(card.select_one(".card__title")),
            _el_text(card.select_one(".card__description")), url,
            active=_pelando_active(card),
        ))
    return out


def _cuponomia_url(li) -> str | None:
    """data-coupon-modal is base64 of '/desconto/amazon?c=<id>' (opens that
    coupon on the store page)."""
    raw = li.get("data-coupon-modal") or ""
    try:
        path = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        path = ""
    return urljoin(CUPONOMIA_URL, path) if path.startswith("/") else CUPONOMIA_URL


def parse_cuponomia(html: str | bytes) -> list[CouponCode]:
    """Cuponomia: li[data-test-id='coupon-list-item'][data-type='code']. Live
    codes sit in .js-itemCode; the expired list (li.expired-item) shows them
    in .coupon-code .code. No expiry dates, and stale codes linger."""
    soup = BeautifulSoup(_text(html), "lxml")
    out = []
    for li in soup.select("li[data-test-id='coupon-list-item']"):
        if li.get("data-type") != "code":
            continue  # 'sale' = plain deal link, no code
        code_el = li.select_one(".js-itemCode") or li.select_one(".coupon-code .code")
        code = _clean_code(_el_text(code_el))
        if not code:
            continue
        expired = ("expired-item" in (li.get("class") or [])
                   or li.find_parent("ul", class_="expired-coupons") is not None)
        sitewide = ("sitewide",) if li.get("data-is-store-wide") == "true" else ()
        out.append(_make(
            "cuponomia", code, _el_text(li.select_one(".js-itemTitle")),
            _el_text(li.select_one(".item-desc")), _cuponomia_url(li),
            label=_el_text(li.select_one(".js-couponSmallText")),
            active=not expired, extra_flags=sitewide,
        ))
    return out


def parse_meliuz(html: str | bytes) -> list[CouponCode]:
    """Méliuz: div.offer-cpn[data-offer-code] (rules in .cpn-layout__rules) plus
    the 'Cupons expirados' list. Most codes are placeholders for link-activated
    offers (CUPOMAUTOMATICO, OFERTANOLINK, ...); they're kept here and
    filtered by ``merge_codes`` / ``relevant_codes``."""
    soup = BeautifulSoup(_text(html), "lxml")
    out = []
    for blk in soup.select("div.offer-cpn[data-offer-code]"):
        code = _clean_code(blk.get("data-offer-code"))
        if not code:
            continue
        title = _squash(blk.get("data-offer-title")) or _el_text(
            blk.select_one(".offer-cpn__title"))
        out.append(_make(
            "meliuz", code, title, _el_text(blk.select_one(".cpn-layout__rules")),
            blk.get("data-offer-url") or MELIUZ_URL,
            label=_el_text(blk.select_one(".offer-cpn__offer-summary")),
        ))
    for li in soup.select(".expired-cpn-sec__items li"):
        code = _clean_code(_el_text(li.select_one(".expired-cpn-sec__code")))
        if not code:
            continue
        out.append(_make("meliuz", code, _el_text(li.find("p")), "",
                         li.get("data-offer-url") or MELIUZ_URL, active=False))
    return out


def parse_inter_cashback(html: str | bytes) -> CashbackStatus | None:
    """Inter Shop: props.pageProps.store.storeCashbacks.fullCashbackValue (rate
    for Inter account holders; partialCashbackValue for everyone else). None
    when the block is missing (layout change / bot wall)."""
    data = _next_data(_text(html)) or {}
    try:
        st = data["props"]["pageProps"]["store"]
        cb = st["storeCashbacks"]
        full = float(cb["fullCashbackValue"] or 0)
    except (KeyError, TypeError, ValueError):
        return None
    partial = _opt_float(cb.get("partialCashbackValue")) or 0.0
    is_pct = (cb.get("fullCashbackType") or "percentage") == "percentage"
    detail = (f"{cb.get('fullCashback') or f'{full:g}%'} p/ correntista, "
              f"{cb.get('partialCashback') or f'{partial:g}%'} p/ demais; "
              "cupom não oferecido pelo Inter anula o cashback")
    if st.get("couponsAndPromotions"):
        detail += f"; {len(st['couponsAndPromotions'])} cupom(ns) do Inter"
    return CashbackStatus(
        portal="inter", rate_pct=full if is_pct else None, active=full > 0,
        url=INTER_URL, detail=detail,
    )


_CASHBACK_RATE_RE = re.compile(r"(\d{1,2}(?:[.,]\d+)?)\s*%\s*(?:de\s+)?cashback")


def parse_meliuz_cashback(html: str | bytes) -> CashbackStatus | None:
    """Méliuz: .hero-sec__logo[data-has-cashback] (or window.hasCashback). The
    rate is read from the page text only when the flag is on."""
    text = _text(html)
    soup = BeautifulSoup(text, "lxml")
    logo = soup.select_one(".hero-sec__logo[data-has-cashback]")
    if logo is not None:
        flag = logo["data-has-cashback"].strip().lower()
    else:
        m = re.search(r"window\.hasCashback\s*=\s*(true|false)", text)
        if not m:
            return None
        flag = m.group(1)
    if flag != "true":
        return CashbackStatus(portal="meliuz", rate_pct=0.0, active=False,
                              url=MELIUZ_URL, detail="sem cashback na Amazon")
    m = _CASHBACK_RATE_RE.search(_fold(_el_text(soup.select_one("main") or soup)))
    rate = float(m.group(1).replace(",", ".")) if m else None
    return CashbackStatus(portal="meliuz", rate_pct=rate, active=True, url=MELIUZ_URL,
                          detail="cashback Amazon ativo no Méliuz")


def parse_cuponomia_cashback(html: str | bytes) -> CashbackStatus | None:
    """Cuponomia: div.store_header[data-store-cashback-actual] is '' with no
    Amazon cashback. Only that attribute is read: the page also carries
    other stores' cashback banners ('22% de cashback' was Centauro)."""
    soup = BeautifulSoup(_text(html), "lxml")
    header = soup.select_one("div.store_header[data-store-cashback-actual]")
    if header is None:
        return None
    raw = (header.get("data-store-cashback-actual") or "").strip()
    m = re.search(r"\d+(?:[.,]\d+)?", raw)
    rate = float(m.group(0).replace(",", ".")) if m else 0.0
    if raw and not m:
        rate = None  # on, but in a format we don't read
    active = bool(raw) and (rate is None or rate > 0)
    return CashbackStatus(
        portal="cuponomia", rate_pct=rate, active=active, url=CUPONOMIA_URL,
        detail=f"cashback Amazon {raw}" if active else "sem cashback na Amazon",
    )


# --- merging / ranking ---------------------------------------------------------

def _merge_pair(a: CouponCode, b: CouponCode) -> CouponCode:
    sources = a.source.split(",")
    sources += [s for s in b.source.split(",") if s and s not in sources]
    pct, abs_off, cap, min_order = a.pct, a.abs_off, a.cap, a.min_order
    if pct is None and abs_off is None:
        pct, abs_off = b.pct, b.abs_off
        cap = cap if cap is not None else b.cap
        min_order = min_order if min_order is not None else b.min_order
    elif (pct, abs_off) == (b.pct, b.abs_off):
        # Same offer: let the other site fill in a cap/minimum this one omitted.
        cap = cap if cap is not None else b.cap
        min_order = min_order if min_order is not None else b.min_order
    # Only a's title/terms survive, so b's restrictions live on as flags
    # ('no_phone' from 'não válido para celulares' in b's terms, ...).
    flags = set(a.flags) | set(b.flags)
    return replace(
        a, source=",".join(sources), title=a.title or b.title,
        terms=a.terms or b.terms, url=a.url or b.url, pct=pct, abs_off=abs_off,
        cap=cap, min_order=min_order, until=a.until or b.until,
        active=a.active and b.active, flags=[f for f in FLAGS if f in flags],
    )


def merge_codes(codes: Iterable[CouponCode]) -> list[CouponCode]:
    """One entry per code (``key``), dropping placeholders. The first listing
    wins on terms and numbers (others fill in what it lacks), flags are
    unioned (so every source's restrictions count, see ``classify``), and an
    explicit 'expired' anywhere beats a stale listing elsewhere."""
    merged: dict[str, CouponCode] = {}
    for c in codes:
        if is_placeholder(c.code):
            continue
        cur = merged.get(c.key)
        merged[c.key] = replace(c, flags=list(c.flags)) if cur is None else _merge_pair(cur, c)
    return list(merged.values())


def _expired(c: CouponCode, today: date) -> bool:
    try:
        return bool(c.until) and date.fromisoformat(c.until[:10]) < today
    except ValueError:
        return False


def relevant_codes(codes: Iterable[CouponCode], price: float, min_value: float = 0,
                   today: date | None = None,
                   max_share: float | None = MAX_SHARE) -> list[tuple[CouponCode, float]]:
    """Codes worth trying on an order of ``price``, as (code, value) sorted by
    value (desc), one per key. Drops: inactive or past ``until``; placeholders;
    terms (from any source) that exclude phones or reward a later order;
    category/selection-locked codes unless they target phones/electronics or
    some source calls them sitewide; percentage codes with no stated cap
    (every Amazon BR code has one, so the aggregator lost it and pct x price
    would overstate it ~10x); values of 0 or below ``min_value``; and values
    above ``max_share`` of the price (see MAX_SHARE)."""
    today = today or date.today()
    out: list[tuple[CouponCode, float]] = []
    for c in codes:
        if not c.active or is_placeholder(c.code) or _expired(c, today):
            continue
        flags = set(c.flags)
        if flags & {"no_phone", "later_order"}:
            continue
        # A sitewide claim beats a vague 'itens selecionados' from another
        # aggregator, and Cuponomia's data-is-store-wide beats its own text.
        if "category" in flags and not flags & {"phone", "sitewide"}:
            continue
        # coupon_codes rows stored before no_phone/later_order existed carry
        # only the old flags; their text still says it.
        text = _fold(f"{c.title} {c.terms}")
        if _PHONE_EXCL_RE.search(text) or _LATER_ORDER_RE.search(text):
            continue
        if c.pct is not None and c.cap is None:
            continue
        value = c.value_at(price)
        if value <= 0 or value < min_value:
            continue
        if max_share is not None and value > max_share * price:
            continue
        out.append((c, value))
    out.sort(key=lambda cv: (-cv[1], cv[0].key))
    seen: set[str] = set()
    uniq = []
    for c, v in out:
        if c.key not in seen:
            seen.add(c.key)
            uniq.append((c, v))
    return uniq


def _money(v: float) -> str:
    s = f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


def best_total(card_price: float, pix_price: float | None,
               codes: Iterable[CouponCode | tuple[CouponCode, float]]) -> dict:
    """Cheapest way to pay: the Pix/NuPay price, or the card price minus the
    best code. Assumes the two don't stack: Amazon's Pix promo terms say it
    can't be combined with other offers and that only the largest discount
    applies, so the best total is the min of the two, not both discounts.

    ``codes`` is CouponCodes or ``relevant_codes`` output ((code, value)
    pairs); values are recomputed at ``card_price``. Keys: pix_total,
    best_code, code_value, code_total (= card_price when there's no code),
    best_total, note.
    """
    best_code, best_value = None, 0.0
    for item in codes or []:
        c = item[0] if isinstance(item, tuple) else item
        v = c.value_at(card_price)
        if v > best_value:
            best_code, best_value = c, v
    code_total = round(card_price - best_value, 2)
    pix_total = round(pix_price, 2) if pix_price is not None else None
    best = min(code_total, pix_total) if pix_total is not None else code_total
    if best_code is None:
        note = "nenhum cupom aplicável; " + (
            f"Pix {_money(pix_total)}" if pix_total is not None else "preço no cartão")
    elif pix_total is not None and pix_total <= code_total:
        note = (f"Pix {_money(pix_total)} bate o cupom {best_code.key} "
                f"({_money(code_total)} no cartão); não acumulam")
    else:
        note = (f"cupom {best_code.key} (-{_money(best_value)}) daria {_money(code_total)}"
                + (f" vs Pix {_money(pix_total)}" if pix_total is not None else "")
                + "; elegibilidade p/ vendedor parceiro não verificada")
    return {"pix_total": pix_total, "best_code": best_code, "code_value": best_value,
            "code_total": code_total, "best_total": best, "note": note}


# --- network (best-effort) -----------------------------------------------------

class _FetchError(Exception):
    pass


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_HEADERS)
    return s


def _get(session, url: str) -> str:
    r = session.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    if r.status_code != 200:
        raise _FetchError(f"HTTP {r.status_code}")
    return _text(r.content)


def _why_empty(html: str) -> str:
    if "<title>Just a moment" in html or "_cf_chl_opt" in html:
        return "Cloudflare challenge"
    return "nothing parsed (layout change?)"


def _err(name: str, e: Exception) -> str:
    msg = str(e) if isinstance(e, _FetchError) else f"{type(e).__name__}: {e}"
    return f"{name}: {msg}"[:200]


_CODE_SITES = (
    ("promobit", PROMOBIT_URL, parse_promobit),
    ("pelando", PELANDO_URL, parse_pelando),
    ("cuponomia", CUPONOMIA_URL, parse_cuponomia),
    ("meliuz", MELIUZ_URL, parse_meliuz),
)
_CASHBACK_SITES = (
    ("inter", INTER_URL, parse_inter_cashback),
    ("meliuz", MELIUZ_URL, parse_meliuz_cashback),
    ("cuponomia", CUPONOMIA_URL, parse_cuponomia_cashback),
)


def scan_codes(session=None) -> tuple[list[CouponCode], list[str]]:
    """Fetch the four aggregators and return (merged codes, errors). Never
    raises: a failing site becomes an entry like 'pelando: HTTP 403'."""
    s = session or _session()
    found: list[CouponCode] = []
    errors: list[str] = []
    for name, url, parse in _CODE_SITES:
        try:
            html = _get(s, url)
            got = parse(html)
        except Exception as e:  # one broken site must not sink the poll
            errors.append(_err(name, e))
            continue
        if not got:
            errors.append(f"{name}: {_why_empty(html)}")
        found.extend(got)
    return merge_codes(found), errors


def scan_cashback(session=None) -> tuple[list[CashbackStatus], list[str]]:
    """Fetch the three portals' Amazon cashback status. Never raises."""
    s = session or _session()
    out: list[CashbackStatus] = []
    errors: list[str] = []
    for name, url, parse in _CASHBACK_SITES:
        try:
            html = _get(s, url)
            status = parse(html)
        except Exception as e:
            errors.append(_err(name, e))
            continue
        if status is None:
            errors.append(f"{name}: cashback flag not found ({_why_empty(html)})")
        else:
            out.append(status)
    return out, errors
