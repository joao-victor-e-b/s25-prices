"""Amazon checkout codes validated in a logged-in cart (src/coupon_checks.yaml).

Why this exists: the codes coupons.py collects from aggregators are guesses.
Prime/app/card locks, caps, minimum orders and — above all — whether Amazon
accepts a code for a third-party refurb seller only show up in a logged-in
cart. The user's alert thresholds are written in the 12x total, so a false
discount there could fire a false "COMPRE". A code may therefore lower that
total only after someone saw Amazon take it off: a check recorded in
coupon_checks.yaml by Claude in Chrome or by hand.

A check is scoped to the ASIN + seller it was tried on (seller "*" = it worked
for every seller tried) and expires after MAX_AGE_DAYS, because Amazon codes
die without notice. ``best_validated`` turns the checks into R$ off:

- per code, the latest check that applies to this ASIN + seller decides (on
  the same day a seller-specific check beats "*", then file order); it must
  say ``works`` and be at most ``max_age_days`` old — and no newer check
  anywhere may have called the code "inválido" (dead codes die everywhere);
- an aggregator listing of the code vetoes it when it expired (``until``)
  after the cart test, or was seen inactive since the test (``seen``: the
  listing's last_seen date, so an old listing never overrides a newer test);
- the aggregator's parsed terms (pct/cap/min/abs) price the code at today's
  card price only when the check recorded both ``cart_total`` and
  ``discount``, those terms reproduce that discount, a percentage has a cap
  (aggregators drop caps; the cart doesn't lie), and the price is not below
  the tested one unless the terms state a minimum;
- otherwise the recorded ``discount`` applies, but only while the price is
  not below the tested ``cart_total`` (a minimum order could stop it).

The file is edited by hand, so loading never raises: a broken file or entry
(including one saved in a non-UTF-8 encoding, or dated in the future) is
reported on stderr and skipped, and the poll keeps watching.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from .coupons import CouponCode

CHECKS_PATH = Path(__file__).resolve().parent / "coupon_checks.yaml"
MAX_AGE_DAYS = 7      # a cart check older than this no longer counts
ANY_SELLER = "*"
_PRICE_SLACK = 1.0    # R$ rounding tolerance when comparing cart amounts


@dataclass
class Check:
    """One cart test, as written in coupon_checks.yaml. ``order`` is the
    entry's position in the file (later = newer on the same date)."""
    code: str
    works: bool
    checked: date
    asin: str
    seller_id: str
    cart_total: float | None = None
    discount: float | None = None
    message: str = ""
    order: int = 0


def _to_date(v) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v).strip()[:10])


def _to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "yes", "sim", "1"):
        return True
    if s in ("false", "no", "nao", "não", "0"):
        return False
    raise ValueError(f"works must be true/false, got {v!r}")


def _opt_money(v) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, str):  # "4.582,47" or "4582.47"
        v = v.replace("R$", "").strip()
        v = v.replace(".", "").replace(",", ".") if "," in v else v
    return round(float(v), 2)


def _parse(entry, order: int) -> Check:
    if not isinstance(entry, dict):
        raise ValueError("not a mapping")
    for key in ("code", "works", "checked", "asin", "seller_id"):
        if entry.get(key) in (None, ""):
            raise ValueError(f"missing {key}")
    seller = str(entry["seller_id"]).strip()
    return Check(
        code=str(entry["code"]).strip().upper(),
        works=_to_bool(entry["works"]),
        checked=_to_date(entry["checked"]),
        asin=str(entry["asin"]).strip().upper(),
        seller_id=seller if seller == ANY_SELLER else seller.upper(),
        cart_total=_opt_money(entry.get("cart_total")),
        discount=_opt_money(entry.get("discount")),
        message=str(entry.get("message") or ""),
        order=order,
    )


def _read_text(p: Path) -> str:
    """The file as text: UTF-8 (BOM or not); a file saved as cp1252 (PowerShell
    5.1's Set-Content default) is still read, with a warning."""
    raw = p.read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        print(f"[coupon-checks] {p.name} is not UTF-8; read as cp1252", file=sys.stderr)
        return raw.decode("cp1252", errors="replace")


def load_checks(path: str | Path | None = None, today: date | None = None) -> list[Check]:
    """Every well-formed check in the file ([] when it's missing or broken).
    Checks dated after ``today`` (+1 day of timezone slack) are dropped: a typo
    in the date would otherwise outrank real checks and outlive MAX_AGE_DAYS."""
    p = Path(path) if path else CHECKS_PATH
    if not p.exists():
        return []
    try:
        data = yaml.safe_load(_read_text(p)) or {}
    except (OSError, ValueError, yaml.YAMLError) as e:
        print(f"[coupon-checks] {p.name} unreadable, ignored: {e}", file=sys.stderr)
        return []
    entries = data.get("checks") if isinstance(data, dict) else None
    if not entries:
        return []
    if not isinstance(entries, list):
        print(f"[coupon-checks] {p.name}: 'checks' is not a list, ignored", file=sys.stderr)
        return []
    limit = (today or datetime.now(timezone.utc).date()) + timedelta(days=1)
    out = []
    for i, entry in enumerate(entries):
        try:
            chk = _parse(entry, i)
        except (TypeError, ValueError) as e:
            print(f"[coupon-checks] entry {i + 1} ignored: {e}", file=sys.stderr)
            continue
        if chk.checked > limit:
            print(f"[coupon-checks] entry {i + 1} ignored: checked {chk.checked} is in "
                  "the future", file=sys.stderr)
            continue
        out.append(chk)
    return out


def _applies(c: Check, asin: str, seller_id: str) -> bool:
    return c.asin == asin and c.seller_id in (seller_id, ANY_SELLER)


def _newer(a: Check, b: Check) -> bool:
    return ((a.checked, a.seller_id != ANY_SELLER, a.order)
            > (b.checked, b.seller_id != ANY_SELLER, b.order))


def latest_checks(checks: list[Check], asin: str, seller_id: str) -> dict[str, Check]:
    """The deciding check per code for this ASIN + seller."""
    asin, seller_id = (asin or "").upper(), (seller_id or "").upper()
    latest: dict[str, Check] = {}
    for c in checks:
        if not _applies(c, asin, seller_id):
            continue
        cur = latest.get(c.code)
        if cur is None or _newer(c, cur):
            latest[c.code] = c
    return latest


def _terms_disagree(parsed: CouponCode, c: Check) -> bool:
    """Do the aggregator's parsed terms fail to reproduce (or lack) the cart
    result? Without both numbers there is nothing to confirm them against."""
    if c.discount is None or c.cart_total is None:
        return True
    return abs(parsed.value_at(c.cart_total) - c.discount) > _PRICE_SLACK


def _vetoed(parsed: CouponCode, c: Check, today: date, seen: date | None) -> bool:
    """Has the aggregator called the code over *after* the cart test? An
    ``until`` between the test and today, or an inactive listing seen on or
    after the test day (``seen`` None = this run's scan, i.e. now)."""
    try:
        until = date.fromisoformat(parsed.until[:10]) if parsed.until else None
    except ValueError:
        until = None
    if until is not None and c.checked <= until < today:
        return True
    return not parsed.active and (seen is None or seen >= c.checked)


def _recorded_value(c: Check, card_price: float) -> float | None:
    """The discount the cart showed, while the price is not below what was
    tested (a cheaper order might miss a minimum the check never saw)."""
    if not c.discount or c.discount <= 0 or c.cart_total is None:
        return None
    if card_price < c.cart_total - _PRICE_SLACK:
        return None
    return round(min(c.discount, card_price), 2)


def _value(c: Check, card_price: float, codes: dict[str, CouponCode] | None,
           today: date, seen: dict[str, date] | None = None) -> float | None:
    parsed = (codes or {}).get(c.code)
    if parsed is not None:
        if _vetoed(parsed, c, today, (seen or {}).get(c.code)):
            return None  # the aggregator says it's over: don't count it
        trusted = (not _terms_disagree(parsed, c)
                   and not (parsed.pct is not None and parsed.cap is None))
        if trusted:
            below = card_price < c.cart_total - _PRICE_SLACK
            if below and parsed.min_order is None:
                return None  # a minimum the aggregator omitted could block it
            return parsed.value_at(card_price)
    return _recorded_value(c, card_price)


def _newest_per_code(checks: list[Check]) -> dict[str, Check]:
    newest: dict[str, Check] = {}
    for c in checks:
        cur = newest.get(c.code)
        if cur is None or _newer(c, cur):
            newest[c.code] = c
    return newest


def best_validated(asin: str, seller_id: str, card_price: float | None,
                   codes: dict[str, CouponCode] | None, today: date,
                   max_age_days: int = MAX_AGE_DAYS,
                   checks: list[Check] | None = None,
                   seen: dict[str, date] | None = None) -> tuple[str, float] | None:
    """(code, R$ off) of the most valuable validated code for this offer, or
    None. ``codes`` maps code key -> parsed CouponCode (coupon_codes table or
    this run's scan) and ``seen`` code key -> that listing's last_seen date;
    ``checks`` defaults to the file's."""
    if not card_price or card_price <= 0:
        return None
    if checks is None:
        checks = load_checks(today=today)
    checks = [c for c in checks if (c.checked - today).days <= 1]   # no future dates
    newest = _newest_per_code(checks)
    best: tuple[str, float] | None = None
    for code, c in sorted(latest_checks(checks, asin, seller_id).items()):
        if not c.works or (today - c.checked).days > max_age_days:
            continue
        g = newest.get(code)
        if g is not None and g is not c and is_dead(g) and _newer(g, c):
            continue  # Amazon has since called the code itself invalid
        value = _value(c, card_price, codes, today, seen)
        if value is None or value <= 0:
            continue
        if best is None or value > best[1]:
            best = (code, round(value, 2))
    return best


def recently_checked(checks: list[Check], asin: str, seller_id: str, today: date,
                     max_age_days: int = MAX_AGE_DAYS) -> set[str]:
    """Codes already tried on this ASIN + seller within the window, whatever
    the result (no point suggesting them again)."""
    return {code for code, c in latest_checks(checks, asin, seller_id).items()
            if (today - c.checked).days <= max_age_days}


FAILED_QUIET_DAYS = 14   # "não pode ser aplicado à esta compra": may fit later
DEAD_QUIET_DAYS = 90     # "O cupom de desconto inserido é inválido": code is dead


def is_dead(c: Check) -> bool:
    """A failed check whose cart message says the code itself is invalid
    (as opposed to valid but not applicable to this purchase)."""
    msg = c.message.lower()
    return not c.works and ("inválido" in msg or "invalido" in msg)


def failed_codes(checks: list[Check], today: date,
                 days: int = FAILED_QUIET_DAYS,
                 dead_days: int = DEAD_QUIET_DAYS) -> set[str]:
    """Codes whose most recent check anywhere (any ASIN/seller) failed: within
    ``dead_days`` when Amazon called the code "inválido" (dead — Oct 2026: 9/9
    undated aggregator codes were), within ``days`` for other failures ("não
    pode ser aplicado à esta compra", not for this item). Pinging or
    suggesting them again is noise, and cart attempts are scarce (Amazon
    locks code entry for 2h after ~10-15 tries)."""
    latest = _newest_per_code([c for c in checks if (c.checked - today).days <= 1])
    return {code for code, c in latest.items()
            if not c.works
            and (today - c.checked).days <= (dead_days if is_dead(c) else days)}
