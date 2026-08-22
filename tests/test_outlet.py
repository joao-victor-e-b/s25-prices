from pathlib import Path

from src.normalize import finalize
from src.sources.outletdocelular import OutletDoCelularSource

FIX = Path(__file__).parent / "fixtures"
CAT = FIX / "outlet_cat.html"
PROD = FIX / "outlet_s25.html"
PROD_URL = "https://www.outletdocelular.com.br/celular-samsung/samsung-galaxy-s25-ultra-256gb"


def _src(**match):
    return OutletDoCelularSource(
        {"id": "outletdocelular", "seller": "Outlet do Celular",
         "venue": "own_site", "url": "x", "match": match or None})


def test_product_urls_from_category():
    urls = _src().product_urls(CAT.read_bytes())
    assert PROD_URL in urls
    # the category self-link must not be treated as a product
    assert all("/celular-samsung/s25-ultra" != u.split(".br")[-1] for u in urls)


def test_parse_product_fields():
    o = _src().parse_product(PROD_URL, PROD.read_bytes())
    finalize(o, 0.0088)
    assert o.storage_gb == 256
    assert o.color == "cinza"
    assert o.grade_raw == "Como Novo - Grade A"
    assert o.grade_rank == 4                      # Grade A -> tier 1 -> rank 4
    assert o.in_stock is True
    assert o.list_price == 5324.0
    assert o.pix_discount == 532.4                # 10% Pix discount
    assert o.pv == 4791.6                         # PV rests on the Pix price


def test_default_match_excludes_cinza():
    # target colors are preto/jetblack; the current Cinza unit must be filtered
    o = _src().parse_product(PROD_URL, PROD.read_bytes())
    assert _src()._matches(o) is False


def test_grade_letters_map_to_tiers():
    from src.normalize import grade_rank
    seller = "Outlet do Celular"
    assert grade_rank(seller, "Como Novo - Grade A") == 4
    assert grade_rank(seller, "Usado com cuidado - Grade B") == 3
    assert grade_rank(seller, "Tenho minhas marcas de uso - Grade C") == 2
