"""Cart-validated codes (src/coupon_checks.py): what may lower the 12x total."""
from datetime import date
from pathlib import Path

from src import coupon_checks as CC
from src.coupons import CouponCode

REAL_CHECKS_PATH = Path(__file__).resolve().parent.parent / "src" / "coupon_checks.yaml"

TODAY = date(2026, 10, 23)
ASIN = "B0FTMQQ7L2"
SELLER = "A2TO0KKQTM1BAB"
OTHER = "A1708Z2OZTF5YE"


def _check(**kw):
    base = dict(code="CUPOMX", works=True, checked="2026-10-22", asin=ASIN,
                seller_id=SELLER, cart_total=4582.47, discount=200.0, message="ok")
    base.update(kw)
    return base


def _best(checks, card=4582.47, codes=None, seller=SELLER, asin=ASIN, **kw):
    loaded = [CC._parse(c, i) for i, c in enumerate(checks)]
    return CC.best_validated(asin, seller, card, codes, TODAY, checks=loaded, **kw)


# --- loading ----------------------------------------------------------------

def test_missing_and_empty_files_load_as_no_checks(tmp_path):
    assert CC.load_checks(tmp_path / "nope.yaml") == []
    p = tmp_path / "c.yaml"
    p.write_text("# header only\nchecks: []\n", encoding="utf-8")
    assert CC.load_checks(p) == []
    p.write_text("", encoding="utf-8")
    assert CC.load_checks(p) == []


def test_load_parses_entries(write_checks):
    write_checks([_check(code="cupomx", seller_id="*", checked="2026-10-20"),
                  _check(works=False, discount=0.0, cart_total="4.582,47")])
    a, b = CC.load_checks()                       # default path (isolated by conftest)
    assert (a.code, a.seller_id, a.checked) == ("CUPOMX", "*", date(2026, 10, 20))
    assert a.works is True and a.discount == 200.0 and a.order == 0
    assert b.works is False and b.cart_total == 4582.47 and b.order == 1


def test_yaml_dates_are_accepted(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("checks:\n  - code: X\n    works: true\n    checked: 2026-10-23\n"
                 "    asin: B0FTMQQ7L2\n    seller_id: '*'\n    cart_total: 4000\n"
                 "    discount: 100\n", encoding="utf-8")
    (c,) = CC.load_checks(p)
    assert c.checked == date(2026, 10, 23) and c.discount == 100.0


def test_broken_file_or_entries_never_raise(tmp_path, capsys):
    p = tmp_path / "c.yaml"
    p.write_text("checks: [unclosed", encoding="utf-8")
    assert CC.load_checks(p) == []
    p.write_text("checks:\n  - code: X\n  - just text\n"
                 "  - {code: Y, works: maybe, checked: 2026-10-23, asin: A, seller_id: S}\n"
                 "  - {code: Z, works: true, checked: 2026-10-23, asin: A, seller_id: S}\n",
                 encoding="utf-8")
    (z,) = CC.load_checks(p)
    assert z.code == "Z"
    assert "ignored" in capsys.readouterr().err


def test_repo_checks_file_is_valid_yaml():
    # whatever entries it holds, the committed file must load (bad entries are
    # skipped one by one, so this guards the file's overall syntax)
    checks = CC.load_checks(REAL_CHECKS_PATH)
    assert isinstance(checks, list)
    assert all(c.code and c.asin and c.seller_id for c in checks)


# --- best_validated -----------------------------------------------------------

def test_recorded_discount_applies_at_the_tested_price():
    assert _best([_check()]) == ("CUPOMX", 200.0)
    assert _best([_check()], card=4700.0) == ("CUPOMX", 200.0)   # dearer: still applies


def test_recorded_discount_not_trusted_below_the_tested_price():
    # a cheaper order might miss a minimum the cart test never saw
    assert _best([_check()], card=4500.0) is None
    assert _best([_check()], card=4581.50) == ("CUPOMX", 200.0)  # within R$1 slack
    assert _best([_check(cart_total=None)]) is None              # can't tell -> no


def test_only_working_recent_checks_count():
    assert _best([_check(works=False, discount=0.0)]) is None
    assert _best([_check(checked="2026-10-16")]) == ("CUPOMX", 200.0)  # 7 days: ok
    assert _best([_check(checked="2026-10-15")]) is None               # 8 days: stale
    assert _best([_check(checked="2026-10-15")], max_age_days=10) == ("CUPOMX", 200.0)


def test_latest_check_wins():
    worked_then_failed = [_check(checked="2026-10-20"),
                          _check(checked="2026-10-22", works=False, discount=0.0)]
    assert _best(worked_then_failed) is None
    failed_then_worked = [_check(checked="2026-10-20", works=False, discount=0.0),
                          _check(checked="2026-10-22")]
    assert _best(failed_then_worked) == ("CUPOMX", 200.0)
    # same day: the later entry in the file wins
    assert _best([_check(), _check(works=False, discount=0.0)]) is None


def test_seller_scope():
    star = _check(seller_id="*")
    assert _best([star], seller=OTHER) == ("CUPOMX", 200.0)      # "*" = every seller
    assert _best([_check()], seller=OTHER) is None               # other seller's test
    assert _best([_check()], asin="B0FTMWWS8Y") is None          # other product
    # same day, a seller-specific failure beats a "*" success
    assert _best([_check(works=False, discount=0.0), star]) is None
    # a newer "*" success beats an older seller-specific failure
    assert _best([_check(checked="2026-10-20", works=False, discount=0.0), star]) == \
        ("CUPOMX", 200.0)


def test_parsed_terms_price_the_code_at_todays_price():
    pct = CouponCode(source="cuponomia", code="CUPOMX", pct=10.0, cap=300.0,
                     min_order=1000.0)
    checks = [_check(cart_total=2000.0, discount=200.0)]       # 10% of 2000: agrees
    assert _best(checks, card=2500.0, codes={"CUPOMX": pct}) == ("CUPOMX", 250.0)
    assert _best(checks, card=4000.0, codes={"CUPOMX": pct}) == ("CUPOMX", 300.0)  # cap
    assert _best(checks, card=900.0, codes={"CUPOMX": pct}) is None   # below min order


def test_terms_that_dont_match_the_cart_fall_back_to_the_recorded_discount():
    # the aggregator lost the cap (10% of R$4.582 = R$458), the cart took R$200
    no_cap = CouponCode(source="pelando", code="CUPOMX", pct=10.0)
    assert _best([_check()], codes={"CUPOMX": no_cap}) == ("CUPOMX", 200.0)
    assert _best([_check()], card=4000.0, codes={"CUPOMX": no_cap}) is None


def test_inactive_or_expired_listing_vetoes_a_validation():
    gone = CouponCode(source="cuponomia", code="CUPOMX", abs_off=200.0, active=False)
    assert _best([_check()], codes={"CUPOMX": gone}) is None
    old = CouponCode(source="promobit", code="CUPOMX", abs_off=200.0, until="2026-10-22")
    assert _best([_check()], codes={"CUPOMX": old}) is None
    live = CouponCode(source="promobit", code="CUPOMX", abs_off=200.0, until="2026-10-23")
    assert _best([_check()], codes={"CUPOMX": live}) == ("CUPOMX", 200.0)


def test_most_valuable_code_wins():
    checks = [_check(code="SMALL", discount=50.0), _check(code="BIG", discount=250.0),
              _check(code="DEAD", works=False, discount=0.0)]
    assert _best(checks) == ("BIG", 250.0)


def test_no_price_no_discount():
    assert _best([_check()], card=None) is None


def test_default_checks_come_from_the_file(write_checks):
    write_checks([_check()])
    assert CC.best_validated(ASIN, SELLER, 4582.47, None, TODAY) == ("CUPOMX", 200.0)


def test_recently_checked():
    loaded = [CC._parse(c, i) for i, c in enumerate([
        _check(code="A", works=False, discount=0.0),
        _check(code="B", checked="2026-10-01"),
        _check(code="C", seller_id="*"),
        _check(code="D", seller_id=OTHER)])]
    assert CC.recently_checked(loaded, ASIN, SELLER, TODAY) == {"A", "C"}


# --- failed codes (pings / suggestions) -----------------------------------------

INVALID = "O cupom de desconto inserido é inválido."
NOT_APPLICABLE = "O cupom de desconto inserido não pode ser aplicado à esta compra."


def _loaded(*entries):
    return [CC._parse(c, i) for i, c in enumerate(entries)]


def test_dead_codes_stay_quiet_for_90_days():
    checks = _loaded(_check(code="DEAD", works=False, discount=0.0, message=INVALID,
                            checked="2026-08-01"))
    assert CC.failed_codes(checks, date(2026, 10, 23)) == {"DEAD"}      # 83 days
    assert CC.failed_codes(checks, date(2026, 11, 1)) == set()          # 92 days


def test_not_applicable_codes_stay_quiet_for_14_days():
    checks = _loaded(_check(code="NA", works=False, discount=0.0,
                            message=NOT_APPLICABLE, checked="2026-10-10"))
    assert CC.failed_codes(checks, date(2026, 10, 23)) == {"NA"}        # 13 days
    assert CC.failed_codes(checks, date(2026, 10, 26)) == set()         # 16 days
    assert CC.failed_codes(checks, date(2026, 10, 26), days=30) == {"NA"}


def test_failed_codes_follow_the_latest_check_anywhere():
    checks = _loaded(
        _check(code="X", works=False, discount=0.0, message=INVALID, checked="2026-10-20"),
        _check(code="X", seller_id="*", asin="B0FTMWWS8Y", checked="2026-10-22"),
        _check(code="Y", works=False, discount=0.0, message=INVALID, checked="2026-10-22",
               seller_id=OTHER))
    assert CC.failed_codes(checks, TODAY) == {"Y"}   # X worked later elsewhere
    assert CC.is_dead(checks[2]) and not CC.is_dead(checks[1])


# --- review round 2 ------------------------------------------------------------

def test_a_code_called_invalid_anywhere_stops_counting_everywhere():   # C16
    checks = _loaded(
        _check(code="X100", asin="B0FTMWWS8Y", seller_id="A3SPPFNXUZXE4P",
               checked="2026-10-20", cart_total=3850.0, discount=100.0),
        _check(code="X100", works=False, discount=0.0, message=INVALID,
               checked="2026-10-22"))
    assert CC.best_validated("B0FTMWWS8Y", "A3SPPFNXUZXE4P", 3880.0, None, TODAY,
                             checks=checks) is None
    # a newer success anywhere brings it back; a non-dead failure stays scoped
    revived = checks + _loaded(_check(code="X100", asin="B0FTMWWS8Y", seller_id="*",
                                      checked="2026-10-23", cart_total=3850.0,
                                      discount=100.0))
    assert CC.best_validated("B0FTMWWS8Y", "A3SPPFNXUZXE4P", 3880.0, None, TODAY,
                             checks=revived) == ("X100", 100.0)
    scoped = _loaded(
        _check(code="Y", asin="B0FTMWWS8Y", seller_id="S2", checked="2026-10-20",
               cart_total=3850.0, discount=100.0),
        _check(code="Y", works=False, discount=0.0, message=NOT_APPLICABLE,
               checked="2026-10-22"))
    assert CC.best_validated("B0FTMWWS8Y", "S2", 3880.0, None, TODAY,
                             checks=scoped) == ("Y", 100.0)


def test_uncapped_or_unconfirmed_terms_are_never_extrapolated():   # C9 / C17
    no_cap = CouponCode(source="cuponomia", code="VCPRIME10", pct=10.0)
    # no recorded discount / cart total: nothing confirms the aggregator's 10%
    assert _best([_check(code="VCPRIME10", discount=None)], card=3900.0,
                 codes={"VCPRIME10": no_cap}) is None
    assert _best([_check(code="VCPRIME10", cart_total=None, discount=20.0)],
                 card=3880.0, codes={"VCPRIME10": no_cap}) is None
    # an uncapped % matching the cart by coincidence is not scaled up either
    agree = [_check(code="VCPRIME10", cart_total=200.0, discount=20.0)]
    assert _best(agree, card=3880.0, codes={"VCPRIME10": no_cap}) == ("VCPRIME10", 20.0)


def test_parsed_terms_below_the_tested_price_need_a_stated_minimum():   # C19
    r100 = CouponCode(source="promobit", code="R100", abs_off=100.0)
    checks = [_check(code="R100", cart_total=4275.78, discount=100.0)]
    assert _best(checks, card=3880.0, codes={"R100": r100}) is None
    with_min = CouponCode(source="promobit", code="R100", abs_off=100.0, min_order=1500.0)
    assert _best(checks, card=3880.0, codes={"R100": with_min}) == ("R100", 100.0)
    assert _best(checks, card=4300.0, codes={"R100": r100}) == ("R100", 100.0)


def test_expiry_veto_outlives_the_listing_and_respects_the_test_date():   # C5
    expired = CouponCode(source="promobit", code="SEMINOVO200", abs_off=200.0,
                         until="2026-10-22")
    checks = [_check(code="SEMINOVO200", checked="2026-10-21", cart_total=3700.0)]
    assert _best(checks, card=3700.0, codes={"SEMINOVO200": expired}) is None
    # a listing that expired *before* the cart test can't veto the newer test
    before = CouponCode(source="promobit", code="SEMINOVO200", abs_off=200.0,
                        until="2026-10-20")
    assert _best(checks, card=3700.0, codes={"SEMINOVO200": before}) == ("SEMINOVO200", 200.0)
    # inactive: only when seen inactive on/after the test day
    gone = CouponCode(source="cuponomia", code="SEMINOVO200", abs_off=200.0, active=False)
    seen_after = {"SEMINOVO200": date(2026, 10, 22)}
    seen_before = {"SEMINOVO200": date(2026, 10, 19)}
    loaded = [CC._parse(c, 0) for c in checks]
    assert CC.best_validated(ASIN, SELLER, 3700.0, {"SEMINOVO200": gone}, TODAY,
                             checks=loaded, seen=seen_after) is None
    assert CC.best_validated(ASIN, SELLER, 3700.0, {"SEMINOVO200": gone}, TODAY,
                             checks=loaded, seen=seen_before) == ("SEMINOVO200", 200.0)


def test_non_utf8_file_still_loads(tmp_path, capsys):   # C18
    p = tmp_path / "c.yaml"
    p.write_bytes(("checks:\n  - code: X\n    works: false\n    checked: 2026-10-22\n"
                   "    asin: B0FTMQQ7L2\n    seller_id: '*'\n"
                   "    message: 'O cupom de desconto inserido é inválido.'\n")
                  .encode("cp1252"))
    (c,) = CC.load_checks(p, today=TODAY)
    assert CC.is_dead(c)
    assert "not UTF-8" in capsys.readouterr().err
    p.write_bytes(b"\xff\xfe\x00garbage: [")
    assert CC.load_checks(p, today=TODAY) == []          # never raises


def test_future_dated_checks_are_ignored(tmp_path, capsys):   # C20
    p = tmp_path / "c.yaml"
    p.write_text("checks:\n"
                 "  - {code: Y, works: true, checked: 2026-11-23, asin: B0FTMQQ7L2,"
                 " seller_id: A2TO0KKQTM1BAB, cart_total: 4000, discount: 100}\n"
                 "  - {code: Y, works: false, checked: 2026-10-22, asin: B0FTMQQ7L2,"
                 " seller_id: A2TO0KKQTM1BAB, message: 'inválido'}\n", encoding="utf-8")
    checks = CC.load_checks(p, today=TODAY)
    assert [c.works for c in checks] == [False]
    assert "in the future" in capsys.readouterr().err
    typo = _loaded(_check(code="Y", checked="2026-11-23", cart_total=4000.0,
                          discount=100.0))
    assert CC.best_validated(ASIN, SELLER, 4100.0, None, TODAY, checks=typo) is None
