import re

from src.models import Observation
from src.notify import format_alert, format_heartbeat, money, twelve_x
from src.triggers import Alert

URL = "https://www.amazon.com.br/dp/B0FTMWWS8Y?smid=A2TO0KKQTM1BAB"


def _amazon(**kw):
    base = dict(source_id="amazon_seminovos_512", seller="CellularStore Oficial",
                venue="amazon", listing_id="B0FTMWWS8Y:A2TO0KKQTM1BAB:excelente",
                url=URL, storage_gb=512, color="preto", grade_raw="Excelente",
                grade_rank=4, list_price=3700.0, pix_discount=185.0, in_stock=True)
    base.update(kw)
    return Observation(**base)


def _alert(trigger="instant_buy", priority="🔴", **kw):
    return Alert(trigger, priority, _amazon(**kw))


def test_money_has_space_after_symbol():
    assert money(4033.71) == "R$ 4.033,71"


def test_buy_now_with_validated_code():
    msg = format_alert(_alert(coupon=200.0, coupon_code="CUPOMX"))
    lines = msg.split("\n")
    assert lines[0] == "🔴 COMPRE — S25 Ultra 512GB preto"
    assert lines[1] == "12x de R$ 291,67 (total R$ 3.500,00)"    # (3700-200)/12
    assert lines[2] == "c/ cupom CUPOMX −R$ 200,00 (validado)"
    assert lines[3] == "Pix à vista R$ 3.500,00"                 # 3700 - max(185, 200)
    assert lines[4] == "CellularStore Oficial · Excelente"
    assert lines[-1] == URL


def test_good_price_with_amazon_coupon():
    msg = format_alert(_alert("aggressive", "🟠", list_price=3900.0, coupon=150.0,
                              pix_discount=195.0))
    assert msg.startswith("🟠 BOM PREÇO — S25 Ultra 512GB preto\n")
    assert "12x de R$ 312,50 (total R$ 3.750,00)" in msg
    assert "cupom Amazon −R$ 150,00" in msg
    assert "Pix à vista R$ 3.705,00" in msg                       # Pix 195 beats coupon


def test_uses_amazons_installment_when_nothing_subtracted():
    o = _amazon(list_price=4582.47, pix_discount=229.13, installments_n=12,
                installment_value=381.90)
    assert twelve_x(o) == (381.90, 4582.47)
    msg = format_alert(Alert("aggressive", "🟠", o))
    assert "12x de R$ 381,90 (total R$ 4.582,47)" in msg
    assert "cupom" not in msg
    # a coupon changes the plan, so Amazon's installment no longer applies
    o.coupon = 100.0
    assert twelve_x(o) == (373.54, 4482.47)


def test_other_triggers_have_no_buy_headline():
    a = Alert("restock", "🟢", _amazon(list_price=4582.47), note="voltou ao estoque")
    msg = format_alert(a)
    assert msg.startswith("🟢 S25 Ultra 512GB preto\n")
    assert "[voltou ao estoque]" in msg


def test_no_dollar_followed_by_digit():
    # CallMeBot/PHP preg_replace eats "$<digit>" as a backreference. Guard it.
    for a in (_alert(coupon=200.0, coupon_code="CUPOMX"),
              _alert("aggressive", "🟠", coupon=150.0),
              Alert("venue_spread", "🟢", _amazon(), note="spread R$412 vs own_site")):
        msg = format_alert(a)
        assert not re.search(r"\$\d", msg), f"found $<digit> in: {msg!r}"


def test_heartbeat_format():
    o = _amazon(list_price=4582.47, pix_discount=229.13, installments_n=12,
                installment_value=381.90)
    tot = {"runs": 300, "failures": 0, "partial": 4, "coupon_errors": 1}
    msg = format_heartbeat(o, ("OIPRIME15", 200.0), 2, tot)
    lines = msg.split("\n")
    assert lines[0] == ("📊 S25 Amazon — 12x de R$ 381,90 (total R$ 4.582,47) · "
                        "Pix R$ 4.353,34")
    assert lines[1] == "CellularStore Oficial · 512GB · Excelente"
    assert lines[2] == "🎟️ testar OIPRIME15: até −R$ 200,00 (não validado)"
    assert lines[3] == ("⚠️ 24h: 300 polls · 0 falhas · 4 parciais · 1 erros cupom"
                        " · 2 fontes")
    assert not re.search(r"\$\d", msg)


def test_heartbeat_format_without_offer_or_code():
    tot = {"runs": 0, "failures": 0, "partial": 0, "coupon_errors": 0}
    msg = format_heartbeat(None, None, 2, tot)
    assert msg.split("\n")[0] == "📊 S25 Amazon — sem oferta Excelente+ em estoque (48h)"
    assert "testar" not in msg and "⚠️" not in msg


def test_heartbeat_shows_validated_coupon():
    o = _amazon(coupon=120.0, coupon_code="CUPOMX")
    tot = {"runs": 1, "failures": 0, "partial": 0, "coupon_errors": 0}
    msg = format_heartbeat(o, None, 2, tot)
    assert "12x de R$ 298,33 (total R$ 3.580,00)" in msg
    assert "c/ cupom CUPOMX −R$ 120,00 (validado)" in msg
