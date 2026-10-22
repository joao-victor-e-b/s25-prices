from src.normalize import coupon_from_text
from src.sources.amazon import AmazonSeminovosSource


def test_absolute_coupon():
    assert coupon_from_text("Aplicar cupom: Economize R$ 200,00", 4500) == 200.0
    assert coupon_from_text("R$ 150 de desconto com cupom", 4500) == 150.0


def test_percent_coupon_needs_list_price():
    assert coupon_from_text("Cupom de 10% OFF", 4000) == 400.0
    assert coupon_from_text("Cupom de 10% OFF", None) == 0.0


def test_pix_discount_is_not_a_coupon():
    # Trocafy/Outlet show "à vista no Pix (10% de desconto)" — must NOT count
    assert coupon_from_text("à vista no Pix (10% de desconto)", 4481.90) == 0.0
    assert coupon_from_text("R$ 4.033,71 à vista com desconto Pix", 4481.90) == 0.0


def test_no_coupon_text():
    assert coupon_from_text("10x sem juros de R$ 448,19", 4481.90) == 0.0
    assert coupon_from_text("", 4000) == 0.0


def test_amazon_no_coupon_widget_yields_zero():
    from pathlib import Path
    fx = Path(__file__).parent / "fixtures" / "amazon_asin.html"
    src = AmazonSeminovosSource({"id": "amazon_seminovos", "asin": "B0FTMQQ7L2",
                                 "url": "https://www.amazon.com.br/dp/B0FTMQQ7L2"})
    o = src.parse("https://www.amazon.com.br/dp/B0FTMQQ7L2", fx.read_bytes())
    # fixture's coupon widget is empty -> coupon 0.0 (no false positive)
    assert o.coupon == 0.0


def test_amazon_coupon_widget_parsed():
    # price inside the buybox's apex block (a stray a-price elsewhere is ignored)
    html = (b"<html><body><span id='productTitle'>Galaxy S25 Ultra 256GB Preto"
            b"</span><div id='corePrice_feature_div'><span class='a-price'>"
            b"<span class='a-offscreen'>R$4.500,00</span></span></div>"
            b"<div id='couponsInBuybox_feature_div'>"
            b"Economize R$ 250,00 com cupom</div></body></html>")
    src = AmazonSeminovosSource({"id": "amazon_seminovos", "asin": "X",
                                 "url": "https://www.amazon.com.br/dp/X"})
    o = src.parse("https://www.amazon.com.br/dp/X", html)
    assert o.coupon == 250.0
