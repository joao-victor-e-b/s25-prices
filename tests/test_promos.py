from pathlib import Path

from src.promos import parse_ddg

FIXTURE = Path(__file__).parent / "fixtures" / "ddg_promos.html"


def _promos():
    return parse_ddg(FIXTURE.read_text(encoding="utf-8"))


def test_parses_deal_posts():
    promos = _promos()
    # 2 real /oferta/ deals; the ad and the /blog/ comparison are excluded
    assert len(promos) == 2
    urls = {p.url for p in promos}
    assert all("/oferta/" in u for u in urls)


def test_extracts_price_from_title():
    p = next(p for p in _promos() if "999001" in p.url)
    assert p.price == 4039.00
    assert p.source == "promobit.com.br"
    assert "S25 Ultra" in p.title


def test_skips_ads_and_reviews():
    promos = _promos()
    for p in promos:
        assert "y.js" not in p.url          # no DDG ad redirects
        assert "/blog/" not in p.url        # no review/comparison pages


def test_unwraps_ddg_redirect():
    # real target URL, not the duckduckgo.com/l/ wrapper
    for p in _promos():
        assert p.url.startswith("https://www.promobit.com.br/")
        assert "duckduckgo.com" not in p.url
