from pathlib import Path

from src.normalize import finalize
from src.sources.americanas import AmericanasSource

FIXTURE = Path(__file__).parent / "fixtures" / "americanas_search.html"


def _src(**match):
    return AmericanasSource(
        {"id": "americanas", "venue": "americanas", "url": "x",
         "match": match or None})


def _html():
    return FIXTURE.read_text(encoding="utf-8")


def test_parses_refurb_listing():
    obs = _src(storage_gb=256, colors=[]).parse(_html())  # any color
    assert len(obs) >= 1
    o = obs[0]
    finalize(o, 0.0088)
    assert o.seller == "Trocafone"          # marketplace seller parsed
    assert o.venue == "americanas"
    assert o.storage_gb == 256
    assert o.color == "prata"
    assert o.grade_raw == "Excelente"
    assert o.grade_rank == 4
    assert o.in_stock is True


def test_uses_pix_price_not_fake_de():
    o = _src(storage_gb=256, colors=[]).parse(_html())[0]
    finalize(o, 0.0088)
    # the R$7.999 "de" must be ignored; real price is the Pix one
    assert o.pv == 4334.15
    assert o.list_price < 7999.0
    assert o.installments_n == 8


def test_listing_id_is_stable_slug():
    o = _src(storage_gb=256, colors=[]).parse(_html())[0]
    assert o.listing_id != "p"
    assert "s25-ultra" in o.listing_id


def test_default_match_excludes_prata():
    # target is preto/jetblack; the current Prata listing is filtered by fetch()
    src = _src()  # default match
    o = src.parse(_html())[0]
    assert src._matches(o) is False
