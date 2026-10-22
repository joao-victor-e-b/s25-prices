import re

from src.models import Observation
from src.notify import format_alert, money
from src.triggers import Alert


def _alert():
    o = Observation(
        source_id="trocafy_site", seller="Trocafy", venue="own_site",
        listing_id="3233",
        url="https://www.trocafy.com.br/galaxy-s25-ultra-256gb-titanio-preto-p3233",
        storage_gb=256, color="Titânio Preto", grade_raw="Sou Como Novo",
        grade_rank=4, list_price=4481.90, pix_discount=448.19,
        installments_n=10, installment_value=448.19, interest_flag=False,
        in_stock=True, pv=4033.71,
    )
    return Alert("primary", "🟡", o)


def test_money_has_space_after_symbol():
    assert money(4033.71) == "R$ 4.033,71"


def test_no_dollar_followed_by_digit():
    # CallMeBot/PHP preg_replace eats "$<digit>" as a backreference. Guard it.
    msg = format_alert(_alert())
    assert not re.search(r"\$\d", msg), f"found $<digit> in: {msg!r}"


def test_format_contains_expected_lines():
    msg = format_alert(_alert())
    assert "PV R$ 4.033,71" in msg
    assert "10x s/juros de R$ 448,19" in msg
    assert "Trocafy · Sou Como Novo" in msg
    assert msg.strip().endswith("p3233")


def test_spread_note_has_no_dollar_digit():
    a = _alert()
    a.note = "spread R$412 vs own_site"           # old-style note text
    assert not re.search(r"\$\d", format_alert(a))
