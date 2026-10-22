from pathlib import Path

import pytest
import requests

from src.normalize import finalize, grade_rank
from src.sources.amazon import AmazonSeminovosSource, aod_block_reason

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


def test_aod_offer_without_card_price_is_not_discounted():
    # no add-to-cart (no card price) -> shown is taken as card, never guessed
    html = ("<div id='aod-asin-title-text'>Galaxy S25 Ultra 256GB Preto</div>"
            "<div id='aod-offer'><div id='aod-offer-heading'>Seminovo - Aceitável</div>"
            "<span class='apex-pricetopay-accessibility-label'>R$ 3.723,05 com 5 por "
            "cento de desconto</span><span id='promoMessage1'>à vista no Pix (5% off)"
            "</span><div id='aod-offer-soldBy'><a href='/gp/aag/main?seller="
            "A3SPPFNXUZXE4P'>Assurant Ecophone</a></div></div>").encode()
    (o,) = _src().parse_aod(ASIN, html).offers
    assert (o.list_price, o.pix_discount, o.in_stock) == (3723.05, 0.0, False)
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


def test_pix_and_coupon_do_not_stack():
    # Amazon applies the larger of the Pix promo and a coupon, never both
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
    assert (o.pix_discount, o.coupon) == (210.0, 0.0)
    assert finalize(o, 0.0088).pv == 3990.0


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
