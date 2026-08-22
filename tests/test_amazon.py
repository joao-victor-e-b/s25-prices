from pathlib import Path

import pytest

from src.normalize import finalize, grade_rank
from src.sources.amazon import AmazonSeminovosSource

FIXTURE = Path(__file__).parent / "fixtures" / "amazon_asin.html"
URL = "https://www.amazon.com.br/dp/B0FTMQQ7L2"


def _src(**match):
    return AmazonSeminovosSource(
        {"id": "amazon_seminovos", "asin": "B0FTMQQ7L2", "url": URL,
         "match": match or None})


def test_parse_buybox():
    o = _src().parse(URL, FIXTURE.read_bytes())
    finalize(o, 0.0088)
    assert o.storage_gb == 256
    assert o.color == "preto"
    assert o.grade_raw == "Excelente"
    assert o.grade_rank == 4                       # generic fallback -> rank 4
    assert o.seller == "CellularStore Oficial"     # actual shipper
    assert o.in_stock is True
    assert o.list_price == 4582.47
    assert o.installments_n == 12
    assert abs(o.installment_value - 381.90) < 0.01
    assert o.interest_flag is False
    assert o.pv is not None and o.pv < o.list_price  # 12x sem juros lowers PV


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
