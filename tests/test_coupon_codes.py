"""Amazon checkout-code + cashback tracker (src/coupons.py), on pages captured
2026-10-22. No network: scans run against a fake session serving fixtures."""
import sqlite3
from datetime import date
from pathlib import Path

import pytest
import requests

from src import coupons as C
from src.coupons import CashbackStatus, CouponCode

FX = Path(__file__).parent / "fixtures"
TODAY = date(2026, 10, 22)
PRICE = 3919.0  # Assurant Ecophone card price at the time


def _raw(name: str) -> bytes:
    # bytes on purpose: Pelando/Inter send no charset, the parsers decode UTF-8
    return (FX / name).read_bytes()


def _by_key(codes):
    out = {}
    for c in codes:
        out.setdefault(c.key, c)  # first listing (sites list some codes twice)
    return out


@pytest.fixture(scope="module")
def parsed():
    return {
        "promobit": C.parse_promobit(_raw("coupons_promobit_amazon.html")),
        "pelando": C.parse_pelando(_raw("coupons_pelando_amazon.html")),
        "cuponomia": C.parse_cuponomia(_raw("coupons_cuponomia_amazon.html")),
        "meliuz": C.parse_meliuz(_raw("coupons_meliuz_amazon.html")),
    }


@pytest.fixture(scope="module")
def all_codes(parsed):
    return [c for codes in parsed.values() for c in codes]


# --- pt-BR helpers ---------------------------------------------------------

def test_money_and_percent_helpers():
    assert C.parse_brl("R$ 1.499") == 1499.0
    assert C.parse_brl("R$150 OFF") == 150.0
    assert C.parse_brl("R$ 20,00") == 20.0
    assert C.parse_brl("R$ 1.499,90") == 1499.90
    assert C.parse_brl("sem preço") is None
    assert C.parse_pct("10% OFF") == 10.0
    assert C.parse_pct("1,5% de cashback") == 1.5


def test_min_order_and_cap_from_terms():
    t = "Insira o cupom e garanta 10% OFF em pedidos a partir de R$100. O desconto chega a no MÁXIMO R$20"
    assert C.min_order_from(t) == 100.0 and C.cap_from(t) == 20.0
    t = "10% de desconto em compras acima de R$150 (máx R$30)."
    assert C.min_order_from(t) == 150.0 and C.cap_from(t) == 30.0
    assert C.min_order_from("pedido mínimo R$ 40 para membros Prime") == 40.0
    assert C.min_order_from("Válido para compras de R$450") == 450.0
    assert C.min_order_from("R$ 10 OFF em R$ 40") == 40.0
    assert C.cap_from("com desconto máximo limitado a R$100.") == 100.0
    assert C.cap_from("até R$ 50 de desconto") == 50.0
    # Pelando titles drop the R$: "acima de 50 limite de 30"
    assert C.min_order_from("20% OFF acima de 50 limite de 30") == 50.0
    assert C.cap_from("20% OFF acima de 50 limite de 30") == 30.0
    # counts, dates and per-customer limits are not money
    assert C.min_order_from("A partir de 1 item do link") is None
    assert C.min_order_from("a partir de 1 unidade de produtos elegíveis") is None
    assert C.min_order_from("válido a partir de 18/10") is None
    assert C.cap_from("limitado a 1 uso por cliente") is None
    assert C.cap_from("Economize até 10% no carrinho") is None


def test_cap_ignores_usage_count_limits():
    # a per-customer / first-N limit is not money: the R$ cap later on wins
    once = ("Cupom limitado a 1 (um) uso por cliente, com desconto máximo de R$ 200. "
            "10% OFF acima de R$ 800")
    assert C.cap_from(once) == 200.0 and C.min_order_from(once) == 800.0
    assert C.cap_from("Cupom limitado a 500 primeiros resgates. Desconto máximo de R$ 50.") == 50.0
    # any R$ cap beats a bare number, even a count word nobody listed
    assert C.cap_from("Limitado a 300 participantes; desconto máximo de R$ 60") == 60.0
    assert C.cap_from("valor máximo do desconto: R$ 80") == 80.0
    # with no R$ cap anywhere, bare counts still aren't caps
    for t in ("limitado a 1 (um) uso", "limitado a 500 primeiros resgates",
              "limitado a 2 utilizações por conta", "limite de 1 pessoa por pedido",
              "limitado a 3 compras", "limite de 10 parcelas", "limitado a 5 mil cupons"):
        assert C.cap_from(t) is None, t
    # ...while Pelando's R$-less 'limite de 30' still reads as one
    assert C.cap_from("20% OFF acima de 50 limite de 30") == 30.0
    # end to end: the 10% code is worth its R$200 cap, not R$1
    c = C._make("test", "UMUSO10", "10% OFF", once, None)
    assert (c.pct, c.cap, c.min_order) == (10.0, 200.0, 800.0)
    assert [(x.key, v) for x, v in C.relevant_codes([c], PRICE, today=TODAY)] == [
        ("UMUSO10", 200.0)]
    c = C._make("test", "RESGATE10", "10% OFF",
                "Cupom limitado a 500 primeiros resgates. Desconto máximo de R$ 50.", None)
    assert c.cap == 50.0 and c.value_at(PRICE) == 50.0


def test_until_from_text():
    assert C.until_from("Cupom ativo no link. Válido até terça-feira 20 de outubro de 2026") == "2026-10-20"
    assert C.until_from("válido até 31/12/2026") == "2026-12-31"
    assert C.until_from("válido até 31/12") is None  # no year -> unknown, not guessed


# --- parsers -----------------------------------------------------------------

def test_parse_promobit(parsed):
    codes = _by_key(parsed["promobit"])
    assert codes, "no codes parsed"
    c = codes["CUPOMPRIME10"]
    assert (c.pct, c.cap, c.min_order) == (10.0, 20.0, 100.0)
    assert c.until == "2026-12-31"
    assert c.active and c.source == "promobit"
    assert "prime" in c.flags and "sitewide" in c.flags
    assert c.url == "https://www.promobit.com.br/Redirect/cupom/70449"
    v = codes["VOCEPRIME"]
    assert (v.pct, v.cap, v.min_order) == (10.0, 30.0, 150.0)
    # Promobit footnotes 'PRIME10DO10*'; the '*' isn't part of the code
    assert "PRIME10DO10" in codes
    # the 34 codeless (auto-applied) promos are skipped; the 'Direto no link'
    # stand-in is kept for relevant_codes/merge_codes to drop
    assert len(parsed["promobit"]) == 6
    assert C.is_placeholder(codes["DIRETO NO LINK"].code)


def test_parse_pelando(parsed):
    codes = _by_key(parsed["pelando"])
    assert len(parsed["pelando"]) == 13  # 16 cards, 3 without a code
    m = codes["MEUCARTAOAMZ"]
    assert "card" in m.flags
    assert (m.pct, m.cap, m.min_order) == (10.0, 40.0, 40.0)
    assert m.active
    assert m.url.startswith("https://www.pelando.com.br/d/cc-amazon-ganhe-10porcento")
    p = codes["PORTO20OFF"]
    assert "card" in p.flags and (p.pct, p.min_order, p.cap) == (20.0, 50.0, 30.0)
    n = codes["NATORCIDA"]
    assert {"first_purchase", "app", "prime"} <= set(n.flags)
    assert {"category", "app"} <= set(codes["FERRAMENTA15"].flags)
    # expired per the Temperature island's dealStatus
    assert codes["NOITEPRIME10"].active is False
    assert codes["NOITEPRIME10"].abs_off == 10.0


def test_parse_cuponomia(parsed):
    codes = _by_key(parsed["cuponomia"])
    assert codes
    o = codes["OIPRIME15"]
    assert (o.pct, o.cap, o.min_order) == (15.0, 200.0, 800.0)
    s = codes["CUPOMSEMPRE"]
    assert (s.abs_off, s.pct, s.min_order) == (150.0, None, 1499.0)
    assert codes["SIG3000"].abs_off == 3000.0
    assert {"category", "phone"} <= set(codes["SIG3000"].flags)
    assert codes["PAIS2026"].active is False  # expired list
    assert o.url == "https://www.cuponomia.com.br/desconto/amazon?c=306445"
    assert "category" in codes["VEMDEAMAZON"].flags  # 'itens selecionados'


def test_parse_meliuz(parsed):
    codes = _by_key(parsed["meliuz"])
    a = codes["AMOPRIME15"]
    assert (a.pct, a.min_order, a.cap) == (15.0, 80.0, 30.0)
    assert "prime" in a.flags
    app = codes["COMPRANOAPP"]
    assert app.abs_off == 20.0 and {"app", "first_purchase"} <= set(app.flags)
    # most Méliuz "codes" are link-activated placeholders
    for ph in ("CUPOMAUTOMATICO", "OFERTANOLINK", "CUPOMNOLINK", "RESGATEAQUI"):
        assert ph in codes and C.is_placeholder(ph)
    assert any(not c.active for c in parsed["meliuz"])  # 'Cupons expirados' list


def test_is_placeholder():
    for code in ("CUPOMAUTOMATICO", "OFERTANOLINK", "CUPOMNOLINK", "Direto no link",
                 "Ative o cupom no link", "", "PEGUEAQUI"):
        assert C.is_placeholder(code), code
    for code in ("CUPOMPRIME10", "MEUCARTAOAMZ", "natorcida", "OIPRIME15"):
        assert not C.is_placeholder(code), code


# --- cashback ----------------------------------------------------------------

def test_cashback_parsers_all_off():
    inter = C.parse_inter_cashback(_raw("cashback_inter_amazon.html"))
    meliuz = C.parse_meliuz_cashback(_raw("coupons_meliuz_amazon.html"))
    cupo = C.parse_cuponomia_cashback(_raw("coupons_cuponomia_amazon.html"))
    for s, portal in ((inter, "inter"), (meliuz, "meliuz"), (cupo, "cuponomia")):
        assert isinstance(s, CashbackStatus)
        assert (s.portal, s.store, s.active, s.rate_pct) == (portal, "amazon", False, 0.0)
        assert s.url.startswith("https://")
    # Cuponomia's page carries a '22% de cashback' banner for another store
    assert b"22% de cashback" in _raw("coupons_cuponomia_amazon.html")


def test_cashback_parsers_turned_on():
    inter = ('<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":'
             '{"store":{"storeCashbacks":{"fullCashbackValue":5,"fullCashbackType":"percentage",'
             '"fullCashback":"Até 5% de cashback","partialCashbackValue":3.5},'
             '"couponsAndPromotions":[]}}}}</script>')
    s = C.parse_inter_cashback(inter)
    assert s.active and s.rate_pct == 5.0
    mel = ('<main><div class="hero-sec__logo" data-has-cashback="true"></div>'
           '<p>Ganhe até 8% de cashback na Amazon</p></main>')
    s = C.parse_meliuz_cashback(mel)
    assert s.active and s.rate_pct == 8.0
    s = C.parse_cuponomia_cashback(
        '<div class="store_header" data-store-cashback-actual="3,5%"></div>')
    assert s.active and s.rate_pct == 3.5


def test_cashback_parsers_none_when_marker_missing():
    assert C.parse_inter_cashback("<html>Just a moment...</html>") is None
    assert C.parse_meliuz_cashback("<html></html>") is None
    assert C.parse_cuponomia_cashback("<html></html>") is None


# --- value / ranking -----------------------------------------------------------

def test_value_at_math():
    pct = CouponCode("x", "A", pct=10.0, cap=20.0, min_order=100.0)
    assert pct.value_at(3919) == 20.0          # capped
    assert pct.value_at(150) == 15.0           # under the cap
    assert pct.value_at(99.99) == 0.0          # below the minimum order
    uncapped = CouponCode("x", "B", pct=15.0)
    assert uncapped.value_at(1000) == 150.0
    fixed = CouponCode("x", "C", abs_off=150.0, min_order=1499.0)
    assert fixed.value_at(3919) == 150.0
    assert fixed.value_at(1498) == 0.0
    assert CouponCode("x", "D", abs_off=50.0, active=False).value_at(3919) == 0.0
    assert CouponCode("x", "E").value_at(3919) == 0.0   # no discount known
    assert CouponCode("x", "F", abs_off=500.0).value_at(300) == 300.0  # never > price
    assert CouponCode("x", "lower").key == "LOWER"


def test_relevant_codes_on_fixtures(all_codes):
    rel = C.relevant_codes(all_codes, PRICE, today=TODAY)
    keys = [c.key for c, _ in rel]
    values = [v for _, v in rel]
    assert values == sorted(values, reverse=True)
    assert len(keys) == len(set(keys))  # one row per code
    got = dict((c.key, v) for c, v in rel)
    assert got["OIPRIME15"] == 200.0
    assert got["CUPOMSEMPRE"] == 150.0
    assert got["MEUCARTAOAMZ"] == 40.0
    assert got["CUPOMPRIME10"] == 20.0
    # category/brand/selection-locked
    for k in ("FERRAMENTA15", "PHILIPS20OFF", "NOTE200", "LIVRO20", "JBABY15",
              "BRINQDS30", "VEMDEAMAZON", "PUMA20"):
        assert k not in got, k
    # placeholders, expired, uncapped %, implausible / later-order rewards
    for k in ("CUPOMAUTOMATICO", "OFERTANOLINK", "CUPOMNOLINK", "DIRETO NO LINK",
              "NOITEPRIME10", "PAIS2026", "VCPRIME10", "15PRIME", "SIG3000", "DEVOLTA500"):
        assert k not in got, k


def test_relevant_codes_until_and_min_value(parsed):
    codes = parsed["promobit"]
    assert "CUPOMPRIME10" in {c.key for c, _ in C.relevant_codes(codes, PRICE, today=TODAY)}
    later = C.relevant_codes(codes, PRICE, today=date(2027, 1, 1))  # until 2026-12-31
    assert "CUPOMPRIME10" not in {c.key for c, _ in later}
    assert C.relevant_codes(codes, PRICE, min_value=25, today=TODAY) == [
        (c, v) for c, v in C.relevant_codes(codes, PRICE, today=TODAY) if v >= 25]


def _code(title, terms="", code="TESTE10"):
    """A code as a parser would build it from (title, terms)."""
    text = f"{title} {terms}"
    return CouponCode("test", code, title, terms, pct=C.parse_pct(title),
                      abs_off=None if C.parse_pct(title) else C.parse_brl(title),
                      cap=C.cap_from(text), min_order=C.min_order_from(text),
                      flags=C.classify(code, title, terms))


def test_relevant_codes_keeps_phone_category_and_drops_exclusions():
    phone = _code("10% OFF em celulares Samsung", "limitado a R$150")
    assert "category" in phone.flags and "phone" in phone.flags
    tools = _code("10% OFF em ferramentas", "limitado a R$150", code="TOOLS10")
    excl = _code("10% OFF no site", "limitado a R$150; exceto celulares e smartphones",
                 code="EXCL10")
    assert "phone" not in excl.flags
    got = [c.key for c, _ in C.relevant_codes([phone, tools, excl], PRICE, today=TODAY)]
    assert got == ["TESTE10"]


def test_sitewide_exclusions_are_not_a_category_lock():
    terms = ("Válido em todo o site, exceto livros, eBooks Kindle e dispositivos Alexa. "
             "Limitado a R$ 200.")
    flags = C.classify("SITE10", "10% OFF", terms)
    assert "sitewide" in flags and "category" not in flags
    c = C._make("test", "SITE10", "10% OFF", terms, None)
    assert C.relevant_codes([c], PRICE, today=TODAY) == [(c, 200.0)]
    # a category named before the exclusion still locks the code
    assert "category" in C.classify("X", "10% OFF em livros, exceto eBooks Kindle", "")
    # a price inside the clause doesn't end it early ('R$ 1.000' is not a full stop)
    assert "category" not in C.classify(
        "X", "Todo o site", "Não se aplica a pedidos acima de R$ 1.000 em livros.")
    # phone exclusions are still caught on their own
    assert "no_phone" in C.classify("X", "Todo o site, exceto celulares e livros", "")


def test_category_keyword_scope():
    # 'através do link' is how sitewide codes get activated; only the link's
    # *items* lock a code to a selection
    assert "category" not in C.classify("X", "10% OFF", "Ative o cupom através do link")
    assert "category" in C.classify("X", "15% off", "A partir de 1 item do link")
    assert "category" in C.classify("X", "30% OFF", "Itens do Link com 30%")
    assert "category" in C.classify(
        "X", "17%", "Condição sujeita a uma seleção de artigos; consulte através do link.")
    # 'todos os produtos de beleza' is a category, not the whole site
    beauty = C.classify("X", "10% OFF em todos os produtos de beleza", "")
    assert "sitewide" not in beauty and "category" in beauty
    assert "sitewide" in C.classify("X", "10% OFF em todos os produtos do site", "")
    assert "sitewide" in C.classify("X", "10% OFF em qualquer compra de R$ 100", "")


def test_cuponomia_store_wide_attribute_wins():
    html = ('<ul><li data-test-id="coupon-list-item" data-type="code" '
            'data-is-store-wide="true"><span class="js-couponSmallText">10% OFF</span>'
            '<h3 class="js-itemTitle">Cupom Amazon com 10% OFF</h3>'
            '<div class="item-desc">Desconto válido para itens selecionados, '
            'limitado a R$ 200.</div><span class="js-itemCode">LOJA200</span></li></ul>')
    (c,) = C.parse_cuponomia(html)
    assert {"sitewide", "category"} <= set(c.flags)
    assert [(x.key, v) for x, v in C.relevant_codes([c], PRICE, today=TODAY)] == [
        ("LOJA200", 200.0)]


def test_relevant_codes_drops_value_0_and_implausible():
    small = _code("R$ 50 OFF", "a partir de R$5000", code="BIGMIN")  # min above price
    huge = _code("R$ 900 OFF", "", code="HUGE")                       # > 10% of price
    ok = _code("R$ 100 OFF", "", code="OK100")
    got = [c.key for c, _ in C.relevant_codes([small, huge, ok], PRICE, today=TODAY)]
    assert got == ["OK100"]
    assert [c.key for c, _ in C.relevant_codes([huge], PRICE, today=TODAY,
                                                max_share=None)] == ["HUGE"]


# --- merge / best total -------------------------------------------------------

def test_merge_codes(all_codes):
    merged = C.merge_codes(all_codes)
    keys = [c.key for c in merged]
    assert len(keys) == len(set(keys))
    assert not any(C.is_placeholder(k) for k in keys)
    by = {c.key: c for c in merged}
    assert by["CUPOMPRIME10"].source == "promobit,meliuz"
    assert by["CUPOMPRIME10"].until == "2026-12-31"
    assert by["COMPRANOAPP"].source == "cuponomia,meliuz"
    assert "first_purchase" in by["COMPRANOAPP"].flags  # flag from Méliuz' terms


def test_merge_codes_expired_wins_and_fills_gaps():
    a = CouponCode("promobit", "X10", pct=10.0, cap=None, min_order=100.0, flags=["prime"])
    b = CouponCode("pelando", "x10", pct=10.0, cap=30.0, active=False, flags=["app"])
    (m,) = C.merge_codes([a, b])
    assert (m.source, m.cap, m.min_order, m.active) == ("promobit,pelando", 30.0, 100.0, False)
    assert m.flags == ["prime", "app"]


def _black200():
    """Promobit's listing of a code: fine on its own."""
    return C._make("promobit", "BLACK200", "Cupom Amazon com 10% OFF",
                   "Economize até 10% (máx R$200) em compras a partir de R$ 1.000", None)


def test_merge_keeps_a_later_sources_phone_exclusion():
    promobit = _black200()
    assert C.relevant_codes([promobit], PRICE, today=TODAY) == [(promobit, 200.0)]
    meliuz = C._make("meliuz", "BLACK200", "Cupom Amazon BLACK200",
                     "10% OFF em compras a partir de R$ 1.000. "
                     "Não válido para celulares e smartphones.", None)
    assert "no_phone" in meliuz.flags
    for pair in ([promobit, meliuz], [meliuz, promobit]):
        (m,) = C.merge_codes(pair)
        assert "no_phone" in m.flags
        assert C.relevant_codes([m], PRICE, today=TODAY) == []


def test_merge_keeps_a_later_sources_next_order_terms():
    later = C._make("cuponomia", "BLACK200", "Cupom Amazon BLACK200",
                    "Ganhe R$ 200 OFF na sua próxima compra", None)
    assert "later_order" in later.flags
    (m,) = C.merge_codes([_black200(), later])
    assert "later_order" in m.flags
    assert C.relevant_codes([m], PRICE, today=TODAY) == []


def test_merged_sitewide_beats_a_vague_selection():
    site = C._make("promobit", "SITE200", "Todo o site com 10% OFF",
                   "Insira o cupom no carrinho; no máximo R$ 200", None)
    sel = C._make("cuponomia", "SITE200", "Cupom Amazon 10% OFF",
                  "Desconto válido para itens selecionados.", None)
    (m,) = C.merge_codes([site, sel])
    assert {"sitewide", "category"} <= set(m.flags)
    assert [(x.key, v) for x, v in C.relevant_codes([m], PRICE, today=TODAY)] == [
        ("SITE200", 200.0)]
    # ...but not past a phone exclusion from any source
    excl = C._make("meliuz", "SITE200", "10% OFF", "Não válido para smartphones.", None)
    assert C.relevant_codes(C.merge_codes([site, sel, excl]), PRICE, today=TODAY) == []


def test_relevant_codes_filters_rows_stored_before_the_new_flags():
    # coupon_codes rows written before no_phone/later_order existed: the stored
    # text still excludes them
    base = {"code": "OLDROW", "sources": "meliuz", "title": "10% OFF", "pct": 10.0,
            "cap": 200.0, "flags": "sitewide", "active": 1}
    for terms in ("Limitado a R$ 200. Não válido para celulares.",
                  "Ganhe R$ 200 na próxima compra"):
        c = CouponCode.from_row({**base, "terms": terms})
        assert c.flags == ["sitewide"]
        assert C.relevant_codes([c], PRICE, today=TODAY) == []
    c = CouponCode.from_row({**base, "terms": "Limitado a R$ 200."})
    assert C.relevant_codes([c], PRICE, today=TODAY) == [(c, 200.0)]
    # and the new flags round-trip through the comma-joined column
    c = CouponCode.from_row({**base, "flags": "sitewide,no_phone,later_order"})
    assert c.flags == ["sitewide", "no_phone", "later_order"]
    assert C.relevant_codes([c], PRICE, today=TODAY) == []


def test_best_total_picks_pix_when_better():
    codes = [CouponCode("x", "CUPOMPRIME10", pct=10.0, cap=20.0, min_order=100.0)]
    bt = C.best_total(3919.0, 3723.05, codes)
    assert bt["best_code"].key == "CUPOMPRIME10"
    assert bt["code_total"] == 3899.0
    assert bt["pix_total"] == 3723.05
    assert bt["best_total"] == 3723.05
    assert "Pix" in bt["note"]


def test_best_total_picks_code_when_better_and_accepts_pairs():
    big = CouponCode("x", "BIG", abs_off=300.0)
    small = CouponCode("x", "SMALL", abs_off=20.0)
    bt = C.best_total(3919.0, 3723.05, [(small, 20.0), (big, 300.0)])  # relevant_codes output
    assert bt["best_code"] is big and bt["code_total"] == 3619.0
    assert bt["best_total"] == 3619.0


def test_best_total_without_codes():
    bt = C.best_total(3919.0, None, [])
    assert bt["best_code"] is None
    assert bt["code_total"] == bt["best_total"] == 3919.0
    assert bt["pix_total"] is None


def test_from_row_roundtrip():
    # same columns as store.py's coupon_codes table
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE coupon_codes (code TEXT PRIMARY KEY, sources TEXT, title TEXT, "
                 "terms TEXT, url TEXT, pct NUMERIC, abs_off NUMERIC, cap NUMERIC, "
                 "min_order NUMERIC, until TEXT, flags TEXT, active BOOLEAN, "
                 "first_seen TIMESTAMP, last_seen TIMESTAMP)")
    c = CouponCode("promobit,meliuz", "CUPOMPRIME10", "t", "terms", "http://u", 10.0, None,
                   20.0, 100.0, "2026-12-31", True, ["prime", "sitewide"])
    conn.execute("INSERT INTO coupon_codes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (c.key, c.source, c.title, c.terms, c.url, c.pct, c.abs_off, c.cap,
                  c.min_order, c.until, ",".join(c.flags), c.active, "t0", "t1"))
    back = CouponCode.from_row(conn.execute("SELECT * FROM coupon_codes").fetchone())
    assert back == c
    assert back.value_at(PRICE) == 20.0


# --- scans never raise --------------------------------------------------------

class _Resp:
    def __init__(self, content: bytes, status: int = 200):
        self.content = content
        self.status_code = status


class _FixtureSession:
    """Serves the captured pages by URL; ``status`` overrides per URL."""
    PAGES = {
        C.PROMOBIT_URL: "coupons_promobit_amazon.html",
        C.PELANDO_URL: "coupons_pelando_amazon.html",
        C.CUPONOMIA_URL: "coupons_cuponomia_amazon.html",
        C.MELIUZ_URL: "coupons_meliuz_amazon.html",
        C.INTER_URL: "cashback_inter_amazon.html",
    }

    def __init__(self, status=None):
        self.status = status or {}
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        return _Resp(_raw(self.PAGES[url]), self.status.get(url, 200))


class _BrokenSession:
    def get(self, url, **kw):
        raise requests.ConnectionError("Name or service not known")


def test_scan_codes_from_fixtures():
    sess = _FixtureSession()
    codes, errors = C.scan_codes(session=sess)
    assert errors == []
    assert len(sess.calls) == 4
    keys = {c.key for c in codes}
    assert {"CUPOMPRIME10", "MEUCARTAOAMZ", "OIPRIME15", "AMOPRIME15"} <= keys
    assert not any(C.is_placeholder(k) for k in keys)


def test_scan_codes_reports_http_error_and_keeps_the_rest():
    codes, errors = C.scan_codes(session=_FixtureSession({C.PELANDO_URL: 403}))
    assert errors == ["pelando: HTTP 403"]
    assert "CUPOMPRIME10" in {c.key for c in codes}
    assert "MEUCARTAOAMZ" not in {c.key for c in codes}


def test_scan_codes_never_raises():
    codes, errors = C.scan_codes(session=_BrokenSession())
    assert codes == []
    assert [e.split(":")[0] for e in errors] == ["promobit", "pelando", "cuponomia", "meliuz"]
    assert "ConnectionError" in errors[0]


def test_scan_codes_flags_empty_page():
    class Challenge(_FixtureSession):
        def get(self, url, **kw):
            if url == C.PELANDO_URL:
                return _Resp(b"<html><title>Just a moment...</title></html>")
            return super().get(url, **kw)
    _, errors = C.scan_codes(session=Challenge())
    assert errors == ["pelando: Cloudflare challenge"]


def test_scan_cashback_from_fixtures():
    rates, errors = C.scan_cashback(session=_FixtureSession())
    assert errors == []
    assert [(s.portal, s.active, s.rate_pct) for s in rates] == [
        ("inter", False, 0.0), ("meliuz", False, 0.0), ("cuponomia", False, 0.0)]


def test_scan_cashback_never_raises():
    rates, errors = C.scan_cashback(session=_BrokenSession())
    assert rates == [] and len(errors) == 3
    rates, errors = C.scan_cashback(session=_FixtureSession({C.INTER_URL: 503}))
    assert errors == ["inter: HTTP 503"] and len(rates) == 2
