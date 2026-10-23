import math

from src.models import Observation
from src.normalize import (
    apply_cashback,
    card_total,
    finalize,
    grade_rank,
    is_manual_review,
    pix_price,
    pv,
)


def _offer(**kw):
    base = dict(source_id="s", seller="Trocafy", venue="own_site",
                listing_id="l", url="u", list_price=4399.0)
    base.update(kw)
    return Observation(**base)


# --- grade ranks (spec §2) ---

def test_grade_ranks_per_seller():
    assert grade_rank("Trocafone", "Muito Bom") == 3
    assert grade_rank("Trocafone", "Excelente") == 4
    assert grade_rank("Trocafone", "Bom") == 2
    assert grade_rank("Trocafy", "Sou Como Novo") == 4
    assert grade_rank("Trocafy", "Tenho minhas marcas de uso") == 2
    assert grade_rank("Amazon Seminovos", "Premium") == 4
    assert grade_rank("Outlet do Celular", "tier 2") == 3
    assert grade_rank("Mercado Livre", "recondicionado pela marca") == 4
    assert grade_rank("Mercado Livre", "recondicionado") == 2


def test_muito_bom_not_shadowed_by_bom():
    # "muito bom" must not be captured by the "bom" substring rule.
    assert grade_rank("Trocafone", "Muito Bom") == 3


def test_generic_openbox_is_rank5_any_seller():
    assert grade_rank("Qualquer", "Open Box") == 5
    assert grade_rank("Loja X", "Vitrine") == 5
    assert grade_rank("Trocafone", "Novo Reembalado") == 5


def test_unknown_grade_is_none():
    assert grade_rank("Trocafy", "sei la") is None
    assert grade_rank("Trocafy", None) is None


def test_trocafy_rank2_is_manual_review():
    assert is_manual_review("Trocafy", 2) is True
    assert is_manual_review("Trocafone", 2) is False
    assert is_manual_review("Trocafy", 4) is False


# --- PV (spec §3) ---

def test_pv_pix_only():
    o = _offer(list_price=4000.0, pix_discount=200.0, installments_n=None)
    assert pv(o, 0.0088) == 3800.0


def test_pv_installments_sem_juros_beats_nominal():
    # spec §3: R$3.700 in 10x sem juros prices near R$3.580 PV
    o = _offer(list_price=3700.0, installments_n=10,
               installment_value=370.0, interest_flag=False,
               pix_discount=0.0)
    got = pv(o, 0.0088)
    assert got < 3700.0
    assert 3500.0 < got < 3620.0


def test_pv_takes_cheaper_path():
    # PIX 3600 vs 10x sem juros of 370 (~3540 PV) -> installment wins
    o = _offer(list_price=3600.0, installments_n=10,
               installment_value=370.0, interest_flag=False)
    assert pv(o, 0.0088) < 3600.0


def test_cashback_layer():
    # spec §3: R$4.300 list with 5% cashback -> R$4.085 effective
    o = _offer(list_price=4300.0, installments_n=None)
    apply_cashback(o, {"own_site": [("Meliuz", 0.05)]})
    assert o.portal == "Meliuz"
    assert math.isclose(o.cashback, 215.0)
    assert pv(o, 0.0088) == 4085.0


def test_finalize_sets_rank_and_pv():
    o = _offer(seller="Trocafy", grade_raw="Sou Como Novo",
               list_price=4200.0, installments_n=None)
    finalize(o, 0.0088)
    assert o.grade_rank == 4
    assert o.pv == 4200.0
    assert o.manual_review is False


# --- Amazon: 12x card total and the Pix/coupon no-stack rule ---

def _amz(**kw):
    base = dict(source_id="amazon_seminovos", seller="CellularStore", venue="amazon",
                listing_id="A:S:excelente", url="u", list_price=4000.0)
    base.update(kw)
    return Observation(**base)


def test_card_total_subtracts_only_the_coupon():
    assert card_total(_amz(pix_discount=200.0)) == 4000.0       # Pix never in 12x
    assert card_total(_amz(pix_discount=200.0, coupon=150.0)) == 3850.0
    assert card_total(_amz(list_price=None)) is None


def test_pix_price_takes_the_larger_discount_not_both():
    assert pix_price(_amz(pix_discount=200.0, coupon=150.0)) == 3800.0
    assert pix_price(_amz(pix_discount=200.0, coupon=300.0)) == 3700.0
    assert pix_price(_amz()) == 4000.0


def test_amazon_pv_pix_path_does_not_stack():
    o = _amz(pix_discount=200.0, coupon=150.0)
    assert pv(o, 0.0088) == 3800.0                # not 4000 - 200 - 150


def test_amazon_pv_installments_spread_the_card_total():
    o = _amz(coupon=300.0, installments_n=12, installment_value=333.34)
    expect = sum((3700.0 / 12) / 1.0088 ** k for k in range(1, 13))
    assert pv(o, 0.0088) == round(expect, 2)      # (list - coupon)/12, not the old 333.34
    assert pv(o, 0.0088) < 3700.0


def test_amazon_pv_without_plan_is_the_pix_path():
    assert pv(_amz(pix_discount=200.0), 0.0088) == 3800.0
    assert pv(_amz(list_price=None), 0.0088) is None


def test_other_venues_still_subtract_both():
    # non-Amazon sellers' Pix discount and coupon do stack (unchanged rule)
    o = _offer(list_price=4000.0, pix_discount=200.0, coupon=150.0)
    assert pv(o, 0.0088) == 3650.0


# --- review round 2: coupon caps/minimums; new units ---

def test_coupon_text_respects_caps_and_minimums():
    from src.normalize import coupon_from_text
    assert coupon_from_text("Aplicar cupom de 10% (até R$ 100)", 4000) == 100.0
    assert coupon_from_text("Cupom de 10% limitado a R$ 150", 4000) == 150.0
    assert coupon_from_text("Economize R$ 50 com cupom em compras acima de R$ 5.000",
                            4000) == 0.0
    assert coupon_from_text("Economize R$ 50 com cupom em compras acima de R$ 3.000",
                            4000) == 50.0
    assert coupon_from_text("R$ 900 off. Insira o código SMART900", 4443) == 900.0


def test_new_unit_grades():
    assert grade_rank("Amazon.com.br", "Novo") == 5
    assert grade_rank("Amazon.com.br", "Como novo") == 4      # Usado - Como novo
    assert grade_rank("Loja", "Seminovo") is None              # not a new unit
    assert grade_rank("Loja", "Muito bom") == 3
