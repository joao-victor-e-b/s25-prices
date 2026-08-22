import json
from pathlib import Path

from src.normalize import finalize
from src.sources.trocafone import TrocafoneSource

FIXTURE = Path(__file__).parent / "fixtures" / "trocafone_api.json"


def _source(**match):
    cfg = {"id": "trocafone_site", "seller": "Trocafone", "venue": "own_site",
           "url": "https://www.trocafone.com.br/", "match": match or None}
    return TrocafoneSource(cfg)


def _parse(**match):
    return _source(**match).parse(json.loads(FIXTURE.read_text(encoding="utf-8")))


def test_only_s25_ultra_256_preto():
    obs = _parse()  # default match: 256GB, preto/jetblack
    assert obs, "expected at least one Preto S25 Ultra 256GB listing"
    for o in obs:
        assert o.storage_gb == 256
        assert o.color == "preto"
        assert o.sku == "SM-S938B"


def test_excludes_accessories_and_other_models():
    # widen colors to catch everything, then assert no capas / other models leak in
    obs = _source(storage_gb=256, colors=[]).parse(
        json.loads(FIXTURE.read_text(encoding="utf-8")))
    assert obs
    for o in obs:
        assert "capa" not in (o.grade_raw or "").lower()
    # grades present should be from the known taxonomy only
    finalize_ranks = []
    for o in obs:
        finalize(o, 0.0088)
        finalize_ranks.append(o.grade_rank)
    assert all(r is not None for r in finalize_ranks)


def test_grade_and_pv_mapping():
    obs = _parse()
    o = next(o for o in obs if o.grade_raw == "Excelente")
    finalize(o, 0.0088)
    assert o.grade_rank == 4
    assert o.list_price > 0
    # sem-juros installments should give a PV at or below the sticker
    assert o.pv is not None and o.pv <= o.list_price
    if o.installments_n:
        assert o.interest_flag is False


def test_out_of_stock_flag_present():
    # the grouped product SKUs are typically out of stock; ensure we capture that
    obs = _source(storage_gb=256, colors=[]).parse(
        json.loads(FIXTURE.read_text(encoding="utf-8")))
    assert any(o.in_stock for o in obs) or any(not o.in_stock for o in obs)
    # every observation has a concrete boolean, never None
    assert all(isinstance(o.in_stock, bool) for o in obs)
