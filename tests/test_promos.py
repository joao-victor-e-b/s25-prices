from pathlib import Path

import json

import src.promos as promos_mod
from src.promos import _parse_price, parse_ddg


class _Resp:
    def __init__(self, text, code=200):
        self.text = text
        self.status_code = code


def _promobit_html(status_name=None, status=None):
    d = {"props": {"pageProps": {"serverOffer": {
        "offerStatusName": status_name, "offerStatus": status}}}}
    return (f'<html><script id="__NEXT_DATA__" type="application/json">'
            f'{json.dumps(d)}</script></html>')


def test_promobit_active(monkeypatch):
    monkeypatch.setattr(promos_mod, "_http_get",
                        lambda u: _Resp(_promobit_html(status_name="ACTIVE")))
    assert promos_mod.is_active("https://www.promobit.com.br/oferta/x-1") is True


def test_promobit_finished_by_name(monkeypatch):
    monkeypatch.setattr(promos_mod, "_http_get",
                        lambda u: _Resp(_promobit_html(status_name="FINISHED")))
    assert promos_mod.is_active("https://www.promobit.com.br/oferta/x-2") is False


def test_promobit_finished_by_status_5(monkeypatch):
    monkeypatch.setattr(promos_mod, "_http_get",
                        lambda u: _Resp(_promobit_html(status=5)))
    assert promos_mod.is_active("https://www.promobit.com.br/oferta/x-3") is False


def test_generic_expiry_text(monkeypatch):
    monkeypatch.setattr(promos_mod, "_http_get",
                        lambda u: _Resp("<html>Oferta expirada</html>"))
    assert promos_mod.is_active("https://lipeofertas.com.br/oferta/x") is False


def test_pelando_active_via_render(monkeypatch):
    monkeypatch.setattr("src.render.render", lambda *a, **k: "<html>oferta ativa</html>")
    assert promos_mod.is_active("https://www.pelando.com.br/d/x") is True


def test_pelando_expired_via_render(monkeypatch):
    monkeypatch.setattr("src.render.render", lambda *a, **k: "<html>Oferta expirada</html>")
    assert promos_mod.is_active("https://www.pelando.com.br/d/y") is False


def test_price_parsing_with_and_without_cents():
    assert _parse_price("Por R$ 4.033,71: foo") == 4033.71
    assert _parse_price("Por R$ 4.320: bar") == 4320.0   # thousands, no cents
    assert _parse_price("sem preço aqui") is None

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


def test_rejects_non_ultra_and_siblings():
    urls = {p.url for p in _promos()}
    # plain Galaxy S25 and S25 FE must not match the S25 Ultra target
    assert not any("galaxy-s25-256gb" in u for u in urls)
    assert not any("s25-fe" in u for u in urls)


def test_filters_expired_by_snippet():
    urls = {p.url for p in _promos()}
    # the row whose snippet says "Oferta expirada" is dropped pre-fetch
    assert not any("999005" in u for u in urls)


def test_unwraps_ddg_redirect():
    # real target URL, not the duckduckgo.com/l/ wrapper
    for p in _promos():
        assert p.url.startswith("https://www.promobit.com.br/")
        assert "duckduckgo.com" not in p.url
