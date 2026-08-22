from pathlib import Path

from src.sources.trocafy import TrocafySource

FIXTURE = Path(__file__).parent / "fixtures" / "trocafy_cat.html"


def _source(**match):
    cfg = {"id": "trocafy_site", "seller": "Trocafy", "venue": "own_site",
           "url": "https://www.trocafy.com.br/", "match": match or None}
    return TrocafySource(cfg)


def test_parses_target_256gb_preto():
    src = _source()  # default match: 256GB, preto/jetblack
    obs = src.parse(FIXTURE.read_bytes())
    # fixture has exactly two 256GB Titânio Preto listings
    assert len(obs) == 2
    slugs = {o.listing_id for o in obs}
    assert slugs == {"3233", "3037"}


def test_como_novo_fields():
    src = _source()
    o = next(o for o in src.parse(FIXTURE.read_bytes()) if o.listing_id == "3233")
    assert o.storage_gb == 256
    assert "preto" in o.color.lower()
    assert o.grade_raw == "Sou Como Novo"
    assert o.sku == "SM-S938B"
    assert o.in_stock is True
    assert o.installments_n == 10
    assert abs(o.installment_value - 448.19) < 0.01
    assert o.interest_flag is False
    # nominal sticker = 10 * 448.19 = 4481.90; PIX = 4033.71 -> discount 448.19
    assert abs(o.list_price - 4481.90) < 0.01
    assert abs(o.pix_discount - 448.19) < 0.01


def test_pv_after_normalize_uses_pix_path():
    from src.normalize import finalize
    src = _source()
    o = next(o for o in src.parse(FIXTURE.read_bytes()) if o.listing_id == "3233")
    finalize(o, 0.0088)
    assert o.grade_rank == 4                       # "Sou Como Novo"
    assert abs(o.pv - 4033.71) < 0.01              # PIX beats discounted 10x


def test_marcas_de_uso_is_manual_review():
    src = _source()
    o = next(o for o in src.parse(FIXTURE.read_bytes()) if o.listing_id == "3037")
    from src.normalize import finalize
    finalize(o, 0.0088)
    assert o.grade_rank == 2
    assert o.manual_review is True


def test_widening_colors_returns_more():
    src = _source(storage_gb=256, colors=[])  # any color, still 256GB
    obs = src.parse(FIXTURE.read_bytes())
    assert len(obs) >= 5  # cinza, prata, azul, prata-titanio, preto x2 ...
