import re
from pathlib import Path

import pytest
import requests

from src.normalize import card_total, finalize, grade_rank
from src.sources.amazon import (
    AmazonSeminovosSource,
    aod_block_reason,
    is_conditional_promo,
    is_routine_promo,
    parse_listing_id,
)

FIX = Path(__file__).parent / "fixtures"
FIXTURE = FIX / "amazon_asin.html"      # Aug 2026 dp layout: no Pix promo
DP_PIX = FIX / "amazon_dp_pix.html"     # Oct 2026 dp: "5% off à vista no Pix ou NuPay"
AOD = FIX / "amazon_aod.html"           # All Offers Display, captured 2026-10-22
ASIN = "B0FTMQQ7L2"
URL = f"https://www.amazon.com.br/dp/{ASIN}"
CELLULAR = "A2TO0KKQTM1BAB"
TROCAFONE = "A1708Z2OZTF5YE"

# What the AOD endpoint returns (HTTP 503) to a request without the XHR headers.
AOD_ERROR_BODY = (
    b'<!DOCTYPE html><html><head><noscript><meta http-equiv="refresh" '
    b'content="0; url=https://www.amazon.com.br/error/500" /></noscript>'
    b'<script>window.location.replace("https://www.amazon.com.br/error/500");'
    b"</script></head><body></body></html>")
CAPTCHA_BODY = (b"<html><body><form action='/errors/validateCaptcha'>"
                b"Digite os caracteres que voc\xc3\xaa v\xc3\xaa</form></body></html>")


def _src(**match):
    return AmazonSeminovosSource(
        {"id": "amazon_seminovos", "asin": ASIN, "url": URL,
         "match": match or None})


def _aod_offers():
    page = _src().parse_aod(ASIN, AOD.read_bytes())
    return page, {o.listing_id: o for o in page.offers}


# --- product page (buybox) -------------------------------------------------

def test_parse_buybox():
    o = _src().parse(URL, FIXTURE.read_bytes())
    finalize(o, 0.0088)
    assert o.storage_gb == 256
    assert o.color == "preto"
    assert o.grade_raw == "Excelente"
    assert o.grade_rank == 4                       # generic fallback -> rank 4
    assert o.seller == "CellularStore Oficial"     # actual shipper
    assert o.in_stock is True
    assert o.list_price == 4582.47                 # no promo: shown == card
    assert o.pix_discount == 0.0
    assert o.listing_id == f"{ASIN}:{CELLULAR}:excelente"
    assert o.installments_n == 12
    assert abs(o.installment_value - 381.90) < 0.01
    assert o.interest_flag is False
    assert o.pv is not None and o.pv < o.list_price  # 12x sem juros lowers PV


def test_parse_buybox_pix_layout():
    # Shown price R$4.353,34 is the 5% Pix/NuPay price; card stays the list price.
    o = _src().parse(URL, DP_PIX.read_bytes())
    assert o.list_price == 4582.47
    assert o.pix_discount == 229.13               # card - shown, not card * 5%
    assert o.installments_n == 12
    assert abs(o.installment_value - 381.90) < 0.01
    assert o.listing_id == f"{ASIN}:{CELLULAR}:excelente"
    assert o.seller == "CellularStore Oficial"
    assert o.grade_raw == "Excelente"
    assert o.in_stock is True


def test_shown_below_card_without_pix_promo_is_not_a_discount():
    # never guess: a lower price-to-pay with no Pix/NuPay message stays undiscounted
    html = ("<html><body><span id='productTitle'>Galaxy S25 Ultra 256GB Preto</span>"
            "<div id='corePrice_feature_div'><span class='a-price'>"
            "<span class='a-offscreen'>R$4.353,34</span></span></div>"
            "<script>(total parcelado R$ 4.582,47)</script></body></html>").encode()
    o = _src().parse(URL, html)
    assert (o.list_price, o.pix_discount) == (4582.47, 0.0)


def test_buybox_in_stock_from_add_to_cart_when_no_availability():
    html = (b"<html><body><span id='productTitle'>Galaxy S25 Ultra 256GB Preto</span>"
            b"<div id='corePrice_feature_div'><span class='a-price'>"
            b"<span class='a-offscreen'>R$4.582,47</span></span></div>"
            b"<input id='add-to-cart-button' name='submit.add-to-cart'></body></html>")
    assert _src().parse(URL, html).in_stock is True
    assert _src().parse(URL, html.replace(b"add-to-cart", b"x")).in_stock is False


def test_stray_price_outside_buybox_is_ignored():
    # pages without a buybox used to yield junk rows from the first a-price
    html = (b"<html><body><span id='productTitle'>Galaxy S25 Ultra 256GB Preto</span>"
            b"<div id='carousel'><span class='a-price'><span class='a-offscreen'>"
            b"R$6.887,78</span></span></div></body></html>")
    assert _src().parse(URL, html) is None


def test_matches_target():
    o = _src().parse(URL, FIXTURE.read_bytes())
    assert _src()._matches(o) is True


def test_generic_grade_fallback_for_unknown_seller():
    # a shipper not in the seller table still ranks via the generic vocabulary
    assert grade_rank("CellularStore Oficial", "Seminovo - Excelente") == 4
    assert grade_rank("Loja Qualquer", "Muito Bom") == 3
    assert grade_rank("Loja Qualquer", "Aceitável") == 1


def test_captcha_raises():
    with pytest.raises(RuntimeError):
        _src().parse(URL, b"<html><body>Digite os caracteres</body></html>")


def test_wrong_storage_returns_none():
    # a 512GB page must not be recorded under the 256GB target
    html = b"<html><body><span id='productTitle'>Galaxy S25 Ultra 512GB Preto</span>"
    assert _src().parse(URL, html) is None


# --- All Offers Display (every seller) --------------------------------------

def test_parse_aod_offers():
    page, by_id = _aod_offers()
    assert page.total == 2                        # list offers, pinned excluded
    assert len(page.offers) == 3                  # sticky duplicate skipped
    assert len(by_id) == 3                        # distinct listing_ids

    cs = by_id[f"{ASIN}:{CELLULAR}:excelente"]
    assert page.pinned is cs
    assert cs.seller == "CellularStore Oficial"
    assert cs.grade_raw == "Excelente"
    assert (cs.list_price, cs.pix_discount) == (4582.47, 229.13)
    assert cs.in_stock is True
    assert cs.url == f"{URL}?smid={CELLULAR}"

    bom = by_id[f"{ASIN}:{TROCAFONE}:bom"]
    assert bom.seller == "Trocafone" and bom.grade_raw == "Bom"
    assert (bom.list_price, bom.pix_discount) == (4839.00, 241.95)

    exc = by_id[f"{ASIN}:{TROCAFONE}:excelente"]
    assert (exc.list_price, exc.pix_discount) == (5099.00, 254.95)

    for o in page.offers:
        assert (o.storage_gb, o.color, o.sku) == (256, "preto", "SM-S938B")
        assert o.installments_n is None           # AOD shows no plans
        assert o.coupon == 0.0
        assert _src()._matches(o)


def test_aod_pix_path_pv():
    _, by_id = _aod_offers()
    cs = finalize(by_id[f"{ASIN}:{CELLULAR}:excelente"], 0.0088)
    assert cs.pv == 4353.34                       # list - pix_discount


def test_aod_offer_without_card_price_rebuilds_card_from_pix_pct():
    # no add-to-cart (no card price) under a 5% Pix promo: the shown price is the
    # Pix price, so the card price is rebuilt from it (rounded up), not taken as is
    html = ("<div id='aod-asin-title-text'>Galaxy S25 Ultra 256GB Preto</div>"
            "<div id='aod-offer'><div id='aod-offer-heading'>Seminovo - Aceitável</div>"
            "<span class='apex-pricetopay-accessibility-label'>R$ 3.723,05 com 5 por "
            "cento de desconto</span><span id='promoMessage1'>à vista no Pix (5% off)"
            "</span><div id='aod-offer-soldBy'><a href='/gp/aag/main?seller="
            "A3SPPFNXUZXE4P'>Assurant Ecophone</a></div></div>").encode()
    (o,) = _src().parse_aod(ASIN, html).offers
    assert (o.list_price, o.pix_discount, o.in_stock) == (3919.0, 195.95, False)
    assert o.listing_id == f"{ASIN}:A3SPPFNXUZXE4P:aceitavel"


def test_aod_coupon_promo():
    html = ("<div id='aod-asin-title-text'>Galaxy S25 Ultra 256GB Preto</div>"
            "<div id='aod-offer'><div id='aod-offer-heading'>Seminovo - Excelente</div>"
            "<input name='submit.addToCart' aria-label='Adicionar ao carrinho do "
            "vendedor Loja X e preço R$ 4.000,00'>"
            "<div id='aod-offer-promotion-0'>Aplicar cupom: Economize R$ 200,00</div>"
            "</div>").encode()
    (o,) = _src().parse_aod(ASIN, html).offers
    assert (o.list_price, o.pix_discount, o.coupon) == (4000.0, 0.0, 200.0)
    assert o.seller == "Loja X"                   # from the add-to-cart label
    assert o.listing_id == f"{ASIN}:loja-x:excelente"


def test_aod_block_detection():
    assert aod_block_reason(200, AOD.read_text(encoding="utf-8")) is None
    assert aod_block_reason(503, AOD_ERROR_BODY.decode())
    assert aod_block_reason(200, AOD_ERROR_BODY.decode())          # /error/500 stub
    assert aod_block_reason(200, CAPTCHA_BODY.decode())
    assert aod_block_reason(200, "<html><body>nada</body></html>")  # no offers
    with pytest.raises(RuntimeError):
        _src().parse_aod(ASIN, CAPTCHA_BODY)
    with pytest.raises(RuntimeError):
        _src().parse_aod(ASIN, AOD_ERROR_BODY)


# --- merge -------------------------------------------------------------------

def test_merge_puts_dp_installments_on_pinned_offer():
    src = _src()
    dp = src.parse(URL, DP_PIX.read_bytes())
    page = src.parse_aod(ASIN, AOD.read_bytes())
    by_id = {o.listing_id: o for o in src.combine(dp, page)}
    assert len(by_id) == 3
    cs = by_id[f"{ASIN}:{CELLULAR}:excelente"]
    assert (cs.installments_n, cs.installment_value, cs.interest_flag) == (12, 381.90, False)
    assert cs.pix_discount == 229.13
    # non-buybox offers get no invented plan
    assert by_id[f"{ASIN}:{TROCAFONE}:bom"].installments_n is None
    assert by_id[f"{ASIN}:{TROCAFONE}:excelente"].installments_n is None


def test_merge_skipped_when_buybox_moved():
    src = _src()
    dp = src.parse(URL, DP_PIX.read_bytes())
    dp.list_price = 4499.00                       # price changed between requests
    page = src.parse_aod(ASIN, AOD.read_bytes())
    src.combine(dp, page)
    assert page.pinned.installments_n is None


# --- fetch() with a fake session (no network) --------------------------------

class _Resp:
    def __init__(self, status: int, body: bytes):
        self.status_code = status
        self.content = body
        self.text = body.decode("utf-8", "ignore")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")


def _fetch_src(monkeypatch, routes):
    """Run fetch() with session.get answering from ``routes`` (first URL
    substring that matches wins). Returns (source, observations, calls)."""
    src = _src()
    src.pause_range = (0.0, 0.0)
    calls = []

    def get(url, headers=None, timeout=None):
        calls.append((url, headers or {}))
        for needle, resp in routes:
            if needle in url:
                return resp
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(src.session, "get", get)
    return src, src.fetch(), calls


def _fetch_with(monkeypatch, routes):
    _, obs, calls = _fetch_src(monkeypatch, routes)
    return obs, calls


def test_fetch_dp_plus_aod(monkeypatch):
    obs, calls = _fetch_with(monkeypatch, [
        ("aodAjaxMain", _Resp(200, AOD.read_bytes())),
        (f"/dp/{ASIN}", _Resp(200, DP_PIX.read_bytes())),
    ])
    assert [u for u, _ in calls] == [
        URL,
        f"https://www.amazon.com.br/gp/product/ajax/aodAjaxMain/ref=dp_aod_ALL_mbc"
        f"?asin={ASIN}&pc=dp&experienceId=aodAjaxMain",
    ]
    aod_headers = calls[1][1]
    assert aod_headers["X-Requested-With"] == "XMLHttpRequest"
    assert aod_headers["Referer"] == URL
    assert aod_headers["Accept"] != "*/*"         # bare */* gets a 503
    assert len(obs) == 3
    cs = next(o for o in obs if o.listing_id == f"{ASIN}:{CELLULAR}:excelente")
    assert cs.installments_n == 12


def test_fetch_degrades_to_dp_when_aod_blocked(monkeypatch):
    obs, _ = _fetch_with(monkeypatch, [
        ("aodAjaxMain", _Resp(503, AOD_ERROR_BODY)),
        (f"/dp/{ASIN}", _Resp(200, DP_PIX.read_bytes())),
    ])
    (o,) = obs
    assert o.listing_id == f"{ASIN}:{CELLULAR}:excelente"
    assert (o.list_price, o.pix_discount, o.installments_n) == (4582.47, 229.13, 12)


def test_fetch_uses_aod_when_dp_captcha(monkeypatch):
    obs, _ = _fetch_with(monkeypatch, [
        ("aodAjaxMain", _Resp(200, AOD.read_bytes())),
        (f"/dp/{ASIN}", _Resp(200, CAPTCHA_BODY)),
    ])
    assert len(obs) == 3
    assert all(o.installments_n is None for o in obs)


def test_fetch_raises_when_both_blocked(monkeypatch):
    with pytest.raises(RuntimeError):
        _fetch_with(monkeypatch, [
            ("aodAjaxMain", _Resp(200, CAPTCHA_BODY)),
            (f"/dp/{ASIN}", _Resp(503, AOD_ERROR_BODY)),
        ])


def test_fetch_paginates_only_past_ten_offers(monkeypatch):
    # total=12 -> exactly one extra page (ceil(12/10) = 2); repeats are deduped
    page1 = AOD.read_bytes().replace(
        b'value="2" id="aod-total-offer-count"', b'value="12" id="aod-total-offer-count"')
    assert page1 != AOD.read_bytes()
    obs, calls = _fetch_with(monkeypatch, [
        ("pageno=2", _Resp(200, AOD.read_bytes())),
        ("aodAjaxMain", _Resp(200, page1)),
        (f"/dp/{ASIN}", _Resp(200, DP_PIX.read_bytes())),
    ])
    page_urls = [u for u, _ in calls if "pageno=" in u]
    assert len(page_urls) == 1
    assert "ref=aod_page_2" in page_urls[0] and "isonlyrenderofferlist=true" in page_urls[0]
    assert len(obs) == 3


# --- review regressions ------------------------------------------------------

def test_payment_popover_pix_is_not_a_promo():
    # every dp lists "Pix" in the payment-methods popover; without the promo
    # wording a lower price-to-pay must not become a made-up Pix discount
    html = FIXTURE.read_bytes().replace(b'"priceAmount":4582.47', b'"priceAmount":4899.00')
    assert html != FIXTURE.read_bytes()
    o = _src().parse(URL, html)
    assert (o.list_price, o.pix_discount) == (4899.0, 0.0)


def test_implausible_pix_gap_is_not_credited():
    # a "Pix" price 30% below card is some other number, not the 5% promo
    html = ("<div id='aod-asin-title-text'>Galaxy S25 Ultra 256GB Preto</div>"
            "<div id='aod-offer'><div id='aod-offer-heading'>Seminovo - Bom</div>"
            "<input name='submit.addToCart' aria-label='Adicionar ao carrinho do "
            "vendedor Loja X e preço R$ 5.000,00'>"
            "<span class='apex-pricetopay-accessibility-label'>R$ 3.500,00</span>"
            "<span id='promoMessage1'>à vista no Pix ou NuPay (5% off)</span>"
            "</div>").encode()
    (o,) = _src().parse_aod(ASIN, html).offers
    assert (o.list_price, o.pix_discount) == (5000.0, 0.0)


def test_pix_and_coupon_are_kept_raw_and_do_not_stack_in_pv():
    # Amazon applies the larger of the Pix promo and a coupon, never both. The
    # scraper keeps both raw (the 12x total needs the coupon even when Pix is
    # larger); normalize applies the no-stack rule.
    html = ("<div id='aod-asin-title-text'>Galaxy S25 Ultra 256GB Preto</div>"
            "<div id='aod-offer'><div id='aod-offer-heading'>Seminovo - Excelente</div>"
            "<input name='submit.addToCart' aria-label='Adicionar ao carrinho do "
            "vendedor Loja X e preço R$ 4.200,00'>"
            "<span class='apex-pricetopay-accessibility-label'>R$ 3.990,00</span>"
            "<span id='promoMessage1'>à vista no Pix ou NuPay (5% off)</span>"
            "<div id='aod-offer-promotion-1'>Aplicar cupom: Economize R$ 200,00</div>"
            "</div>").encode()
    src = _src()
    (o,) = src.combine(None, src.parse_aod(ASIN, html))
    assert (o.pix_discount, o.coupon) == (210.0, 200.0)
    assert finalize(o, 0.0088).pv == 3990.0       # 4200 - max(210, 200), not both
    assert card_total(o) == 4000.0                # 12x: only the coupon counts
    assert o.promos == ["à vista no Pix ou NuPay (5% off)",
                        "Aplicar cupom: Economize R$ 200,00"]


def test_amazon_own_offer_uses_its_merchant_id():
    # Amazon.com.br's AOD offer has no seller link; it must key like the dp
    # (merchantID A1ZZFT5FULY4LN) or dp-only runs re-alert under another id
    html = ("<div id='aod-asin-title-text'>Galaxy S25 Ultra 256GB Preto</div>"
            "<div id='aod-offer'><div id='aod-offer-heading'>Seminovo - Excelente</div>"
            "<input name='submit.addToCart' aria-label='Adicionar ao carrinho do "
            "vendedor Amazon.com.br e preço R$ 4.500,00'>"
            "<div id='aod-offer-soldBy'><div class='a-col-right'>"
            "<span class='a-size-small'>Amazon.com.br</span></div></div></div>").encode()
    (o,) = _src().parse_aod(ASIN, html).offers
    assert o.listing_id == f"{ASIN}:A1ZZFT5FULY4LN:excelente"


def test_dp_seller_id_falls_back_to_profile_link():
    html = DP_PIX.read_bytes()
    stripped = html.replace(b'id="merchantID"', b'id="merchantID-gone"')
    assert stripped != html
    o = _src().parse(URL, stripped)
    assert o.listing_id == f"{ASIN}:{CELLULAR}:excelente"


def test_complete_only_when_whole_panel_read(monkeypatch):
    src, _, _ = _fetch_src(monkeypatch, [
        ("aodAjaxMain", _Resp(200, AOD.read_bytes())),
        (f"/dp/{ASIN}", _Resp(200, DP_PIX.read_bytes())),
    ])
    assert src.complete is True and src.degraded == []

    src, _, _ = _fetch_src(monkeypatch, [
        ("aodAjaxMain", _Resp(503, AOD_ERROR_BODY)),
        (f"/dp/{ASIN}", _Resp(200, DP_PIX.read_bytes())),
    ])
    assert src.complete is False and src.degraded       # dp-only fallback

    page1 = AOD.read_bytes().replace(
        b'value="2" id="aod-total-offer-count"', b'value="12" id="aod-total-offer-count"')
    src, _, _ = _fetch_src(monkeypatch, [
        ("pageno=2", _Resp(503, AOD_ERROR_BODY)),
        ("aodAjaxMain", _Resp(200, page1)),
        (f"/dp/{ASIN}", _Resp(200, DP_PIX.read_bytes())),
    ])
    assert src.complete is False                         # page 2 failed


# --- 512GB (second registry entry, ASIN B0FTMWWS8Y) ---------------------------

ASIN_512 = "B0FTMWWS8Y"
URL_512 = f"https://www.amazon.com.br/dp/{ASIN_512}"


def _as_512(raw: bytes) -> bytes:
    """The 256GB captures relabelled as the 512GB Titânio Preto product (no
    real 512GB capture exists; the page layout is the same ASIN family)."""
    out = raw.replace(b"S25 Ultra 5G, 256GB", b"S25 Ultra 5G, 512GB")
    out = out.replace(ASIN.encode(), ASIN_512.encode())
    assert out != raw
    return out


def _src_512():
    return AmazonSeminovosSource(
        {"id": "amazon_seminovos_512", "asin": ASIN_512, "url": URL_512,
         "match": {"storage_gb": 512, "colors": ["preto", "jetblack"]}})


def test_parse_buybox_512():
    o = _src_512().parse(URL_512, _as_512(DP_PIX.read_bytes()))
    assert o is not None
    assert (o.storage_gb, o.color) == (512, "preto")
    assert o.listing_id == f"{ASIN_512}:{CELLULAR}:excelente"
    assert (o.list_price, o.pix_discount) == (4582.47, 229.13)
    assert _src_512()._matches(o)


def test_storage_filter_follows_the_configured_match():
    # the 512 source rejects the 256 page and vice versa
    assert _src_512().parse(URL, DP_PIX.read_bytes()) is None
    assert _src().parse(URL_512, _as_512(DP_PIX.read_bytes())) is None
    # RAM is not storage: "12GB RAM" never satisfies a 12GB/256GB match
    html = b"<html><body><span id='productTitle'>Galaxy S25 Ultra 12GB RAM Preto</span>"
    assert _src().parse(URL, html) is None


def test_aod_512_offers_match_only_the_512_source():
    page = _src_512().parse_aod(ASIN_512, _as_512(AOD.read_bytes()))
    assert len(page.offers) == 3
    assert all(o.storage_gb == 512 and o.listing_id.startswith(ASIN_512)
               for o in page.offers)
    assert all(_src_512()._matches(o) for o in page.offers)
    assert not any(_src()._matches(o) for o in page.offers)


def test_fetch_512_dp_plus_aod(monkeypatch):
    src = _src_512()
    src.pause_range = (0.0, 0.0)
    routes = [("aodAjaxMain", _Resp(200, _as_512(AOD.read_bytes()))),
              (f"/dp/{ASIN_512}", _Resp(200, _as_512(DP_PIX.read_bytes())))]

    def get(url, headers=None, timeout=None):
        return next(r for n, r in routes if n in url)

    monkeypatch.setattr(src.session, "get", get)
    obs = src.fetch()
    assert len(obs) == 3 and src.complete
    cs = next(o for o in obs if o.listing_id == f"{ASIN_512}:{CELLULAR}:excelente")
    assert cs.installments_n == 12 and cs.storage_gb == 512


def test_target_children_follow_the_configured_storage():
    # the variation twister lists every child; each source deep-links its own
    raw = DP_PIX.read_bytes()
    assert _src_512()._target_children(raw) == [ASIN_512]
    assert _src()._target_children(raw) == [ASIN]


# --- promotion messages -------------------------------------------------------

def test_aod_promos_captured_per_offer():
    _, by_id = _aod_offers()
    for o in by_id.values():
        assert o.promos == ["à vista no Pix ou NuPay (5% off)"]


def test_dp_promos_captured_and_merged_onto_pinned():
    src = _src()
    dp = src.parse(URL, DP_PIX.read_bytes())
    assert any("Pix ou NuPay" in p for p in dp.promos)
    page = src.parse_aod(ASIN, AOD.read_bytes())
    combined = {o.listing_id: o for o in src.combine(dp, page)}
    pinned = combined[f"{ASIN}:{CELLULAR}:excelente"]
    assert set(dp.promos) <= set(pinned.promos)
    # the no-promo dp layout has none
    assert src.parse(URL, FIXTURE.read_bytes()).promos == []


def test_dp_coupon_widget_is_a_promo():
    html = (b"<html><body><span id='productTitle'>Galaxy S25 Ultra 256GB Preto"
            b"</span><div id='corePrice_feature_div'><span class='a-price'>"
            b"<span class='a-offscreen'>R$4.500,00</span></span></div>"
            b"<div id='couponsInBuybox_feature_div'>"
            b"Economize   R$ 250,00 com cupom</div></body></html>")
    o = _src().parse(URL, html)
    assert o.coupon == 250.0
    assert o.promos == ["Economize R$ 250,00 com cupom"]   # whitespace squashed
    assert not is_routine_promo(o.promos[0])


def test_routine_promo_detection():
    assert is_routine_promo("à vista no Pix ou NuPay (5% off)")
    assert is_routine_promo("5% off à vista no Pix ou NuPay ou em até 12x de R$ 381,90/mês")
    assert is_routine_promo("à vista no Pix (10% off)")
    assert is_routine_promo("Em até 12x de R$ 381,90 sem juros")
    assert not is_routine_promo("Aplicar cupom: Economize R$ 200,00")
    assert not is_routine_promo("Economize 10% em 12x no cartão Amazon")
    assert is_routine_promo("Frete GRÁTIS")                 # no discount at all
    assert is_routine_promo("Oferta 90 dias de Amazon Music grátis incluso")
    assert is_routine_promo("Economize R$ 229,13 no Pix")   # Pix-only, moves with price
    assert not is_routine_promo("R$ 900 off. Insira o código SMART900")
    assert is_routine_promo("")


def test_parse_listing_id():
    assert parse_listing_id(f"{ASIN}:{CELLULAR}:excelente") == (ASIN, CELLULAR)
    assert parse_listing_id(f"{ASIN}:loja-x:bom") == (ASIN, "loja-x")
    assert parse_listing_id("B0FTMQQ7L2") == ("B0FTMQQ7L2", "")


# --- conditional Amazon-shown promos never feed the coupon -----------------------

def _aod_with_promo(promo: str) -> bytes:
    return ("<div id='aod-asin-title-text'>Galaxy S25 Ultra 256GB Preto</div>"
            "<div id='aod-offer'><div id='aod-offer-heading'>Seminovo - Excelente</div>"
            "<input name='submit.addToCart' aria-label='Adicionar ao carrinho do "
            "vendedor Loja X e preço R$ 4.000,00'>"
            f"<div id='aod-offer-promotion-0'>{promo}</div></div>").encode()


def test_card_first_purchase_coupon_is_announced_not_subtracted():
    promo = "Aplicar cupom de R$ 200 na 1ª compra com cartão Amazon"
    (o,) = _src().parse_aod(ASIN, _aod_with_promo(promo)).offers
    assert o.coupon == 0.0
    assert o.promos == [promo]                    # still reaches the 🏷️ alert
    assert not is_routine_promo(promo)


def test_prime_only_coupon_still_counts():
    (o,) = _src().parse_aod(ASIN, _aod_with_promo(
        "Aplicar cupom: Economize R$ 100,00 para membros Prime")).offers
    assert o.coupon == 100.0


def test_conditional_promo_detection():
    for text in ("Economize R$ 50 com cupom pagando com Cartão Amazon",
                 "cupom de 10% no cartão de crédito Itaú",
                 "Aplicar cupom R$ 30 pagando com NuPay",
                 "Cupom R$ 40 para clientes Nubank",
                 "R$ 20 off com cupom na primeira compra",
                 "Economize R$ 25 com cupom pelo App",
                 "cupom de R$ 15 válido no aplicativo"):
        assert is_conditional_promo(text), text
    for text in ("Aplicar cupom: Economize R$ 200,00",
                 "Cupom de R$ 100 para membros Prime",
                 "Economize 10% em 12x no cartão de crédito",   # any card is fine
                 ""):
        assert not is_conditional_promo(text), text


def test_dp_conditional_coupon_widget_is_not_subtracted():
    html = ("<html><body><span id='productTitle'>Galaxy S25 Ultra 256GB Preto"
            "</span><div id='corePrice_feature_div'><span class='a-price'>"
            "<span class='a-offscreen'>R$4.500,00</span></span></div>"
            "<div id='couponsInBuybox_feature_div'>Economize R$ 250,00 com cupom "
            "na 1ª compra</div></body></html>").encode()
    o = _src().parse(URL, html)
    assert o.coupon == 0.0
    assert o.promos == ["Economize R$ 250,00 com cupom na 1ª compra"]


def test_promo_id_attached_to_its_message():
    _, by_id = _aod_offers()
    for o in by_id.values():
        assert o.promo_ids == {"à vista no Pix ou NuPay (5% off)": "A3UQ0YQ0ADY6VN"}


# --- review round 2: card price unknown under a Pix promo (C4) ------------------

def _strip_card_price(raw: bytes) -> bytes:
    out = re.sub(rb'<div class="[^"]*twister-plus-buying-options-price-data[^"]*">.*?</div>',
                 b"", raw, flags=re.S)
    out = re.sub(rb"total parcelado", b"total xxxxxxxxx", out, flags=re.I)
    assert out != raw
    return out


def test_dp_without_card_price_rebuilds_it_from_the_12x_plan():
    src = _src()
    o = src.parse(URL, _strip_card_price(DP_PIX.read_bytes()))
    assert o.list_price == 4582.80                  # 12 x 381,90: never the Pix 4353,34
    assert o.pix_discount == round(4582.80 - 4353.34, 2)
    assert any("card price missing" in n for n in src.notes)


def test_dp_without_card_price_or_plan_rebuilds_it_from_the_pix_pct():
    raw = _strip_card_price(DP_PIX.read_bytes())
    raw = re.sub(rb"(\d+)x\s*(?:de\s*)?R\$", b"x R$", raw)   # no parsable plan either
    o = _src().parse(URL, raw)
    assert o.list_price == 4582.47                  # ceil(4353.34 / 0.95)
    assert o.installments_n is None


def test_aod_without_card_prices_rebuilds_them_from_the_pix_pct():
    src = _src()
    raw = AOD.read_bytes().replace("e preço R$".encode(), "e por R$".encode())
    assert raw != AOD.read_bytes()
    page = src.parse_aod(ASIN, raw)
    prices = sorted(o.list_price for o in page.offers)
    assert prices == [4582.47, 4582.47, 4839.0, 5099.0][-len(prices):]
    assert 4353.34 not in prices                    # never the Pix price
    assert page.complete and any("rebuilt" in n for n in src.notes)


def test_aod_offer_whose_card_price_cant_be_rebuilt_is_dropped():
    html = ("<div id='aod-asin-title-text'>Galaxy S25 Ultra 256GB Preto</div>"
            "<div id='aod-offer'><div id='aod-offer-heading'>Seminovo - Excelente</div>"
            "<span class='apex-pricetopay-accessibility-label'>R$ 3.200,00</span>"
            "<span id='promoMessage1'>à vista no Pix ou NuPay (20% off)</span>"
            "<input name='submit.addToCart'></div>").encode()
    src = _src()
    page = src.parse_aod(ASIN, html)
    assert page.offers == [] and page.complete is False   # never marks others sold out
    assert any("without card price" in n for n in src.notes)


# --- AOD for another product / storage, or without a title (C12) ----------------

def test_sibling_storage_or_titleless_panel_is_never_complete(monkeypatch):
    assert _src().parse_aod(ASIN, _as_512(AOD.read_bytes())).complete is False
    no_title = AOD.read_bytes().replace(b'id="aod-asin-title-text"', b'id="gone"')
    assert no_title != AOD.read_bytes()
    assert _src().parse_aod(ASIN, no_title).complete is False
    src, obs, _ = _fetch_src(monkeypatch, [
        ("aodAjaxMain", _Resp(200, _as_512(AOD.read_bytes()))),
        (f"/dp/{ASIN}", _Resp(200, DP_PIX.read_bytes())),
    ])
    assert src.complete is False and src.degraded   # no false sold-out marks
    assert len(obs) == 1                            # just the dp buybox


# --- real pushback vs an empty listing (C3) --------------------------------------

def test_blocked_only_on_real_pushback(monkeypatch):
    empty_panel = b"<html><body><div id='aod-container'></div></body></html>"
    src, _, _ = _fetch_src(monkeypatch, [
        ("aodAjaxMain", _Resp(200, empty_panel)),
        (f"/dp/{ASIN}", _Resp(200, DP_PIX.read_bytes())),
    ])
    assert src.degraded and src.blocked is False    # sold-out listing: no backoff
    src, _, _ = _fetch_src(monkeypatch, [
        ("aodAjaxMain", _Resp(503, AOD_ERROR_BODY)),
        (f"/dp/{ASIN}", _Resp(200, DP_PIX.read_bytes())),
    ])
    assert src.blocked is True
    src, _, _ = _fetch_src(monkeypatch, [
        ("aodAjaxMain", _Resp(200, AOD.read_bytes())),
        (f"/dp/{ASIN}", _Resp(200, CAPTCHA_BODY)),
    ])
    assert src.blocked is True
    src, _, _ = _fetch_src(monkeypatch, [
        ("aodAjaxMain", _Resp(200, AOD.read_bytes())),
        (f"/dp/{ASIN}", _Resp(200, DP_PIX.read_bytes())),
    ])
    assert src.blocked is False and src.degraded == []


# --- promo items: split, de-duplicated, hidden text dropped, right ids (C11/C13/C14)

def _aod_block(promo_html: str) -> bytes:
    return ("<div id='aod-asin-title-text'>Galaxy S25 Ultra 256GB Preto</div>"
            "<div id='aod-offer'><div id='aod-offer-heading'>Seminovo - Excelente</div>"
            "<input name='submit.addToCart' aria-label='Adicionar ao carrinho do "
            "vendedor Loja X e preço R$ 4.000,00'>"
            "<span class='apex-pricetopay-accessibility-label'>R$ 3.800,00</span>"
            f"{promo_html}</div>").encode()


PIX_ITEM = ("<span data-csa-c-item-id='amzn1.asin.X:amzn1.promotion.AAAAAAAAAA'>"
            "<span id='promoMessage1'>à vista no Pix ou NuPay (5% off)</span></span>")


def test_pix_line_and_coupon_in_one_block_are_separate_messages():
    html = _aod_block("<div id='aod-offer-promotion-0'>" + PIX_ITEM +
                      "<label id='couponText1'>Aplicar cupom: Economize R$ 200,00</label>"
                      "</div>")
    (o,) = _src().parse_aod(ASIN, html).offers
    assert o.promos == ["à vista no Pix ou NuPay (5% off)",
                        "Aplicar cupom: Economize R$ 200,00"]
    assert o.coupon == 200.0 and o.pix_discount == 200.0     # NuPay no longer voids it
    assert not is_routine_promo(o.promos[1])
    # C14: the coupon has no id of its own and must not borrow the Pix item's
    assert o.promo_ids == {"à vista no Pix ou NuPay (5% off)": "AAAAAAAAAA"}


def test_one_promotion_is_one_message():
    html = _aod_block(
        "<div id='aod-offer-promotion-0'>"
        "<span data-csa-c-item-id='amzn1.asin.X:amzn1.promotion.BBBBBBBBBB'>"
        "<span class='a-text-bold'>R$10</span> <span id='promoMessage2'>off. Insira o "
        "código CUPOMX na finalização da compra.</span> <a>Termos</a>"
        "<span class='aok-hidden'>Cupom salvo!</span></span></div>")
    (o,) = _src().parse_aod(ASIN, html).offers
    assert o.promos == ["R$10 off. Insira o código CUPOMX na finalização da compra. Termos"]
    assert o.promo_ids == {o.promos[0]: "BBBBBBBBBB"}
    assert o.coupon == 10.0                                  # "Insira o código" counts
    plain = _aod_block("<div id='aod-offer-promotion-0'><span id='promoMessage2'>"
                       "Economize 5% em 2 item(ns) selecionado(s)</span> "
                       "<a>Comprar itens</a> | <a>Termos</a></div>")
    (o,) = _src().parse_aod(ASIN, plain).offers
    assert o.promos == ["Economize 5% em 2 item(ns) selecionado(s)"]   # no duplicate


def test_conditions_after_the_display_cut_still_count():
    long = "Aplicar cupom: Economize R$ 300,00 " + "x" * 220 + " na 1ª compra"
    (o,) = _src().parse_aod(ASIN, _aod_block(
        f"<div id='aod-offer-promotion-0'>{long}</div>")).offers
    assert o.coupon == 0.0 and len(o.promos[0]) == 200


def test_payment_method_and_app_coupons_are_conditional():
    for text in ("Aplicar cupom R$ 200 pagando com Pix",
                 "Economize R$ 200,00 no pagamento via Pix",
                 "Aplicar cupom de R$ 300 no boleto",
                 "Cupom de R$ 150 para pagamento à vista",
                 "Economize R$ 250 com cupom exclusivo do aplicativo",
                 "5% off no Pix com o cupom PIX5"):
        assert is_conditional_promo(text), text
        (o,) = _src().parse_aod(ASIN, _aod_block(
            f"<div id='aod-offer-promotion-0'>{text}</div>")).offers
        assert o.coupon == 0.0, text


# --- duplicate listings keep the lowest 12x total (C15) -------------------------

def test_duplicate_listing_keeps_the_lowest_12x_total_and_the_plan():
    from src.models import Observation
    from src.sources.amazon import AodPage
    base = dict(source_id="amazon_seminovos", seller="S", venue="amazon",
                listing_id=f"{ASIN}:S:excelente", url="u", storage_gb=256, color="preto",
                list_price=3640.0, pix_discount=182.0)
    aod_copy = Observation(**base)
    dp_copy = Observation(**base, coupon=150.0, installments_n=12, installment_value=303.34)
    (o,) = _src().combine(dp_copy, AodPage(pinned=None, offers=[aod_copy]))
    assert (o.coupon, o.installments_n) == (150.0, 12)
    assert card_total(o) == 3490.0
    # the dropped copy's plan survives on the kept one (same card price)
    cheap = Observation(**base)
    planned = Observation(**{**base, "pix_discount": 100.0}, installments_n=12,
                          installment_value=303.34)
    (o,) = _src().combine(None, AodPage(pinned=None, offers=[planned, cheap]))
    assert o.pix_discount == 182.0 and o.installments_n == 12


# --- new-unit listings (market study, Oct 2026) ------------------------------------

AOD_NEW = FIX / "amazon_aod_new.html"   # B0DSYJCY45 offers panel, captured 2026-10-23


def _src_new(**cfg):
    return AmazonSeminovosSource({
        "id": "amazon_novo_256_preto", "asin": "B0DSYJCY45", "default_grade": "Novo",
        "match": {"storage_gb": 256, "colors": ["preto", "jetblack"]}, **cfg})


def test_new_unit_panel_parses_novo_as_rank_5():
    src = _src_new()
    page = src.parse_aod("B0DSYJCY45", AOD_NEW.read_bytes())
    assert page.complete and page.total == 5
    offers = {o.listing_id: finalize(o, 0.0088) for o in src.combine(None, page)}
    assert len(offers) == 5                          # Magalu's twin offer collapsed
    amz = offers["B0DSYJCY45:A1ZZFT5FULY4LN:novo"]
    assert (amz.seller, amz.grade_raw, amz.grade_rank) == ("Amazon.com.br", "Novo", 5)
    assert (amz.list_price, amz.pix_discount) == (6043.49, 604.35)   # 10% Pix
    casas = offers["B0DSYJCY45:A3CSZ2DZGY36JR:novo"]
    assert casas.coupon == 0.0                       # COMPRANOAPP: 1st purchase + app
    assert casas.promo_ids                          # its terms can be looked up


def test_new_unit_dp_without_condition_uses_default_grade():
    html = ("<html><body><span id='productTitle'>Celular Samsung Galaxy S25 Ultra 5G, "
            "256GB, 12GB RAM, Titânio Preto</span>"
            "<div class='twister-plus-buying-options-price-data'>"
            '{"desktop_buybox_group_1":[{"priceAmount":6043.49}]}</div>'
            "<span id='apex-pricetopay-accessibility-label'>R$ 5.439,14</span>"
            "<div id='promotionMessageInsideBuyBox_feature_div'>10% off à vista no Pix "
            "ou NuPay ou em até 12x de R$ 503,67/mês</div>"
            "<div id='promoPriceBlockMessage_feature_div'>Oferta 90 dias de Amazon Music "
            "grátis incluso</div><input id='merchantID' value='A1ZZFT5FULY4LN'>"
            "<div id='availability'>Em estoque</div>"
            "<span>em até 12x de R$ 503,67 sem juros</span></body></html>").encode()
    o = finalize(_src_new().parse("https://www.amazon.com.br/dp/B0DSYJCY45", html), 0.0088)
    assert (o.grade_raw, o.grade_rank) == ("Novo", 5)
    assert (o.list_price, o.pix_discount) == (6043.49, 604.35)
    assert (o.installments_n, o.installment_value) == (12, 503.67)
    assert o.listing_id == "B0DSYJCY45:A1ZZFT5FULY4LN:novo"
    assert all(is_routine_promo(p) for p in o.promos)   # Pix line + a freebie
    assert _src().parse(URL, html).grade_raw is None   # seminovo sources: no default


def test_used_like_new_on_a_new_listing_is_alertable():
    html = ("<div id='aod-asin-title-text'>Galaxy S25 Ultra 256GB Titânio Preto</div>"
            "<div id='aod-offer'><div id='aod-offer-heading'>Usado - Como novo</div>"
            "<input name='submit.addToCart' aria-label='Adicionar ao carrinho do "
            "vendedor Amazon.com.br e preço R$ 4.633,00'></div>"
            "<div id='aod-offer'><div id='aod-offer-heading'>Usado - Bom</div>"
            "<input name='submit.addToCart' aria-label='Adicionar ao carrinho do "
            "vendedor Loja e preço R$ 4.100,00'></div>").encode()
    like_new, bom = (finalize(o, 0.0088) for o in
                     _src_new().parse_aod("B0DSYJCY45", html).offers)
    assert (like_new.grade_raw, like_new.grade_rank) == ("Como novo", 4)
    assert bom.grade_rank == 2                      # below the alert rank


def test_jet_black_titles_match():
    from src.sources.amazon import _find_color
    assert _find_color("Galaxy S25 Ultra 512GB Titânio Jet Black") == "jetblack"
    assert _find_color("Galaxy S25 Ultra 512GB Titânio JetBlack") == "jetblack"


def test_product_code_on_a_new_listing_counts_in_the_12x_total():
    html = ("<div id='aod-asin-title-text'>Galaxy S25 Ultra 256GB Titânio Preto</div>"
            "<div id='aod-offer'><div id='aod-offer-heading'>Novo</div>"
            "<input name='submit.addToCart' aria-label='Adicionar ao carrinho do "
            "vendedor Amazon.com.br e preço R$ 4.443,00'>"
            "<div id='aod-offer-promotion-0'>R$ 900 off. Insira o código SMART900 na "
            "finalização da compra.</div></div>").encode()
    (o,) = _src_new().parse_aod("B0DSYJCY45", html).offers
    assert o.coupon == 900.0 and card_total(o) == 3543.0


def test_bank_wallet_and_multi_unit_coupons_are_conditional():   # A1
    for text in ("Economize R$ 300 com cartões Santander",
                 "Cupom de R$ 300 exclusivo para clientes Itaú",
                 "Economize R$ 200 com Mercado Pago",
                 "Economize R$ 250 com o cupom do Banco Inter",
                 "Cupom R$ 100 pagando com PicPay",
                 "Economize R$ 150 com cupom C6 Bank",
                 "Economize R$ 300 ao comprar 2 itens",
                 "Leve 2 e economize R$ 400 com cupom",
                 "Cupom de 10% na compra de 3 unidades",
                 "Economize 5% em 2 item(ns) selecionado(s)"):
        assert is_conditional_promo(text), text
        (o,) = _src().parse_aod(ASIN, _aod_block(
            f"<div id='aod-offer-promotion-0'>{text}</div>")).offers
        assert o.coupon == 0.0, text
    for text in ("Aplicar cupom: Economize R$ 200,00",
                 "Economize 10% em 12x no cartão de crédito",
                 "Cupom de R$ 100 para membros Prime",
                 "Compre 1 e economize R$ 50 com cupom"):
        assert not is_conditional_promo(text), text


# --- CI rate limiting: pacing, AOD-first, retry (Oct 2026) ----------------------

from src.sources import amazon as amazon_mod   # noqa: E402


def test_pacer_spaces_every_request_and_honours_a_deferred_wait():
    t = {"now": 0.0}
    sleeps = []

    def sleep(s):
        sleeps.append(round(s, 2))
        t["now"] += s

    pacer = amazon_mod.Pacer(gap=10, jitter=0.3, clock=lambda: t["now"], sleep=sleep,
                             uniform=lambda a, b: 1.0)
    pacer.wait()                       # first request: no wait
    t["now"] += 3
    pacer.wait()                       # 3 s later: waits the other 7
    pacer.defer(45)
    pacer.wait()                       # the AOD retry: >= 45 s after the last
    pacer.wait()                       # back to the normal gap
    assert sleeps == [7.0, 45.0, 10.0] and pacer.count == 4
    jittery = amazon_mod.Pacer(gap=10, jitter=0.3, clock=lambda: 0.0, sleep=sleeps.append,
                               uniform=lambda a, b: b)
    jittery.wait()
    jittery.wait()
    assert sleeps[-1] == 13.0          # 10 s + 30%


def _routed_session(monkeypatch, routes, calls, warm=True):
    session = amazon_mod.amazon_session()
    if warm:
        session.cookies.set("session-id", "1", domain=".amazon.com.br")

    def get(url, headers=None, timeout=None):
        calls.append(url)
        for needle, resp in routes:
            if needle in url:
                return resp() if callable(resp) else resp
        raise AssertionError(f"unexpected URL {url}")
    monkeypatch.setattr(session, "get", get)
    return session


def _kinds(calls):
    return ["aod" if "aodAjaxMain" in u else "dp" for u in calls]


def test_aod_first_dp_every_4th_poll_and_plan_cached(monkeypatch):
    calls = []
    _routed_session(monkeypatch, [("aodAjaxMain", _Resp(200, AOD.read_bytes())),
                                  (f"/dp/{ASIN}", _Resp(200, DP_PIX.read_bytes()))], calls)
    kinds = []
    for _ in range(5):
        calls.clear()
        obs = _src().fetch()           # a new source object each poll, like run.poll
        kinds.append(_kinds(calls))
        cs = next(o for o in obs if o.listing_id == f"{ASIN}:{CELLULAR}:excelente")
        assert (cs.installments_n, cs.installment_value) == (12, 381.90)   # cached plan
        assert len(obs) == 3                                               # dp never stored
    assert kinds == [["dp", "aod"], ["aod"], ["aod"], ["aod"], ["dp", "aod"]]
    assert amazon_mod.PACER.count == 7                                     # all paced


def test_cached_plan_only_lends_itself_at_an_unchanged_price(monkeypatch):
    calls = []
    aod = {"body": AOD.read_bytes()}
    _routed_session(monkeypatch, [("aodAjaxMain", lambda: _Resp(200, aod["body"])),
                                  (f"/dp/{ASIN}", _Resp(200, DP_PIX.read_bytes()))], calls)
    _src().fetch()
    aod["body"] = AOD.read_bytes().replace(b"4.582,47", b"4.499,00")
    assert aod["body"] != AOD.read_bytes()
    obs = _src().fetch()               # buybox price moved: the old plan is stale
    cs = next(o for o in obs if o.listing_id == f"{ASIN}:{CELLULAR}:excelente")
    assert cs.list_price == 4499.0 and cs.installments_n is None


def test_dp_every_poll_while_it_shows_a_coupon(monkeypatch):
    calls = []
    dp = re.sub(rb'(<div id="couponsInBuybox_feature_div"[^>]*>)',
                rb"\1Economize R$ 100,00 com cupom", DP_PIX.read_bytes(), count=1)
    assert dp != DP_PIX.read_bytes()
    _routed_session(monkeypatch, [("aodAjaxMain", _Resp(200, AOD.read_bytes())),
                                  (f"/dp/{ASIN}", _Resp(200, dp))], calls)
    for _ in range(3):
        calls.clear()
        _src().fetch()
        assert _kinds(calls) == ["dp", "aod"]      # the coupon moves the 12x total


def test_cold_session_fetches_the_dp_first(monkeypatch):
    calls = []
    _routed_session(monkeypatch, [("aodAjaxMain", _Resp(200, AOD.read_bytes())),
                                  (f"/dp/{ASIN}", _Resp(200, DP_PIX.read_bytes()))],
                    calls, warm=False)
    amazon_mod._FETCHES["amazon_seminovos"] = 1     # not a dp_every turn
    _src().fetch()
    assert _kinds(calls) == ["dp", "aod"]


def test_blocked_aod_is_retried_once_after_a_long_wait(monkeypatch):
    calls, sleeps = [], []
    monkeypatch.setattr(amazon_mod, "PACER", amazon_mod.Pacer(
        gap=0, clock=lambda: 0.0, sleep=sleeps.append))
    answers = [_Resp(503, AOD_ERROR_BODY), _Resp(200, AOD.read_bytes())]
    _routed_session(monkeypatch, [("aodAjaxMain", lambda: answers.pop(0)),
                                  (f"/dp/{ASIN}", _Resp(200, DP_PIX.read_bytes()))], calls)
    amazon_mod._FETCHES["amazon_seminovos"] = 1
    src = _src()
    obs = src.fetch()
    assert _kinds(calls) == ["aod", "aod"] and sleeps == [amazon_mod.AOD_RETRY_WAIT_S]
    assert len(obs) == 3 and src.block_level is None and src.aod_ok == 1


def test_aod_blocked_twice_falls_back_to_the_dp_as_soft_trouble(monkeypatch):
    calls = []
    _routed_session(monkeypatch, [("aodAjaxMain", _Resp(503, AOD_ERROR_BODY)),
                                  (f"/dp/{ASIN}", _Resp(200, DP_PIX.read_bytes()))], calls)
    amazon_mod._FETCHES["amazon_seminovos"] = 1
    src = _src()
    (o,) = src.fetch()
    assert _kinds(calls) == ["aod", "aod", "dp"]
    assert o.listing_id == f"{ASIN}:{CELLULAR}:excelente" and o.installments_n == 12
    assert (src.block_level, src.blocked, src.aod_ok, src.aod_tried) == ("soft", True, 0, 1)


def test_empty_listing_is_not_retried_and_not_trouble(monkeypatch):
    calls = []
    empty = b"<html><body><div id='aod-container'></div></body></html>"
    _routed_session(monkeypatch, [("aodAjaxMain", _Resp(200, empty)),
                                  (f"/dp/{ASIN}", _Resp(200, DP_PIX.read_bytes()))], calls)
    amazon_mod._FETCHES["amazon_seminovos"] = 1
    src = _src()
    src.fetch()
    assert _kinds(calls) == ["aod", "dp"] and src.block_level is None


def test_dp_captcha_is_hard_trouble(monkeypatch):
    calls = []
    _routed_session(monkeypatch, [("aodAjaxMain", _Resp(200, AOD.read_bytes())),
                                  (f"/dp/{ASIN}", _Resp(200, CAPTCHA_BODY))], calls)
    src = _src()
    assert len(src.fetch()) == 3 and src.block_level == "hard"


def test_promo_terms_go_through_the_pacer(monkeypatch):
    from src.sources.amazon import promo_terms

    class S:
        def get(self, url, headers=None, timeout=None):
            r = _Resp(200, b'{"tncSectionContentMap": {"TNC_CONTENT": "At\\u00e9 R$ 50"}}')
            r.json = lambda: {"tncSectionContentMap": {"TNC_CONTENT": "Até R$ 50"}}
            return r
    before = amazon_mod.PACER.count
    assert promo_terms(S(), "A3N43RG5YNRVDU") == "Até R$ 50"
    assert amazon_mod.PACER.count == before + 1
