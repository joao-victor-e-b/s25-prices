"""Alerting — CallMeBot (WhatsApp) primary + ntfy.sh fallback (spec §7).

Price alerts lead with what the user pays: "12x de R$ X (total R$ Y)", the card
total after confirmed coupons, then the coupon behind it and the Pix price.
Every R$ amount goes through ``money()``/``safe_text()`` (see ``money``).

Delivery is accounted per WhatsApp-sized batch: every sender returns
``delivered`` (indices of the items whose batch reached at least one channel)
so callers mark exactly those as sent — one failed batch no longer re-sends
the ones that got through, and a missing secret on one channel is logged as a
failed send instead of crashing the poll after the other channel delivered.
"""
from __future__ import annotations

import re
import sys
import urllib.parse

import requests

from . import config
from .normalize import card_total, pix_price
from .triggers import Alert

_CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"
_NTFY_BASE = "https://ntfy.sh"
_MAX_CHARS = 500  # keep WhatsApp body small (spec §7)


def brl(value: float | None) -> str:
    """Format a number as Brazilian currency digits: 4399.9 -> '4.399,90'."""
    if value is None:
        return "-"
    s = f"{value:,.2f}"                       # 4,399.90
    return s.replace(",", "X").replace(".", ",").replace("X", ".")  # 4.399,90


def money(value: float | None) -> str:
    """'R$ 4.399,90' — note the space after R$.

    CallMeBot's whatsapp.php runs the text through a PHP preg_replace, where a
    '$' immediately followed by a digit is a capture-group backreference and
    gets deleted. Keeping a space after 'R$' avoids that (and matches BR style).
    """
    return f"R$ {brl(value)}"


def safe_text(text: str | None) -> str:
    """Scraped free text (aggregator titles like 'R$150 OFF') can carry the same
    '$<digit>' CallMeBot eats; insert the space money() would have."""
    return re.sub(r"\$(?=\d)", "$ ", text or "")


# Headline per trigger; the others show just the priority dot + product.
_HEADLINES = {"instant_buy": "COMPRE", "aggressive": "BOM PREÇO",
              "year_low": "ABAIXO DO MÍNIMO DE 2026"}


def _product(o) -> str:
    storage = f"{o.storage_gb}GB" if o.storage_gb else ""
    return " ".join(x for x in ("S25 Ultra", storage, o.color or "") if x)


def twelve_x(o) -> tuple[float, float] | None:
    """(installment, total) of the 12x plan after confirmed coupons. Amazon's
    own installment is used when nothing was subtracted (its rounding:
    12x R$ 381,90 for R$ 4.582,47); otherwise total / 12."""
    total = card_total(o)
    if total is None:
        return None
    if not o.coupon and o.installments_n == 12 and o.installment_value:
        return o.installment_value, total
    return round(total / 12, 2), total


def coupon_line(o) -> str:
    """'c/ cupom CODE −R$ Z (validado)' / 'cupom Amazon −R$ Z', or ''."""
    if not o.coupon:
        return ""
    if o.coupon_code:
        return f"c/ cupom {safe_text(o.coupon_code)} −{money(o.coupon)} (validado)"
    return f"cupom Amazon −{money(o.coupon)}"


def format_alert(a: Alert) -> str:
    """Render one alert: headline, 12x plan + total, coupon, Pix price,
    seller · grade, note, link."""
    o = a.obs
    label = _HEADLINES.get(a.trigger)
    header = (f"{a.priority} {label} — {_product(o)}" if label
              else f"{a.priority} {_product(o)}")

    lines = [header]
    plan = twelve_x(o)
    if plan is not None:
        per, total = plan
        lines.append(f"12x de {money(per)} (total {money(total)})")
    lines.append(coupon_line(o))
    pix = pix_price(o)
    if pix is not None:
        lines.append(f"Pix à vista {money(pix)}")
    if o.cashback and o.portal:
        lines.append(f"cashback {safe_text(o.portal)} −{money(o.cashback)}")

    grade = o.grade_raw or f"rank {o.grade_rank}"
    bat = f"bat {o.battery_band}" if o.battery_band else ""
    lines.append(" · ".join(x for x in (safe_text(o.seller), grade, bat) if x))
    if a.note:
        lines.append(f"[{safe_text(a.note)}]")
    lines.append(o.url)
    return "\n".join(x for x in lines if x)


def _pack(blocks: list[str]) -> list[tuple[str, list[int]]]:
    """Pack blocks into WhatsApp-sized messages (spec §7: batch, don't burst):
    (message, indices of the blocks it carries)."""
    batches: list[tuple[str, list[int]]] = []
    current, idxs = "", []
    for i, block in enumerate(blocks):
        if current and len(current) + len(block) + 2 > _MAX_CHARS:
            batches.append((current, idxs))
            current, idxs = block, [i]
        else:
            current = f"{current}\n\n{block}" if current else block
            idxs.append(i)
    if current:
        batches.append((current, idxs))
    return batches


def _safe_send(fn, *args, **kwargs) -> bool:
    """A channel call that can't raise (a missing secret is a failed send)."""
    try:
        return bool(fn(*args, **kwargs))
    except Exception as e:
        print(f"[notify] {getattr(fn, '__name__', 'send')}: {type(e).__name__}: {e}",
              file=sys.stderr)
        return False


def _deliver(blocks: list[str], title: str) -> dict:
    """Send blocks batched on both channels (no short-circuit). ``delivered``
    lists the blocks whose batch reached at least one channel."""
    if not blocks:
        return {"sent": 0, "wa_ok": True, "ntfy_ok": True, "delivered": []}
    wa_ok = ntfy_ok = True
    delivered: list[int] = []
    for msg, idxs in _pack(blocks):
        wa = _safe_send(send_whatsapp, msg)
        nt = _safe_send(send_ntfy, msg, title=title)
        wa_ok, ntfy_ok = wa_ok and wa, ntfy_ok and nt
        if wa or nt:
            delivered.extend(idxs)
    return {"sent": len(blocks), "wa_ok": wa_ok, "ntfy_ok": ntfy_ok,
            "delivered": delivered}


def send_whatsapp(message: str) -> bool:
    params = {
        "phone": config.get("CALLMEBOT_PHONE", required=True),
        "text": message,
        "apikey": config.get("CALLMEBOT_APIKEY", required=True),
    }
    url = _CALLMEBOT_URL + "?" + urllib.parse.urlencode(params)
    try:
        r = requests.get(url, timeout=20)
    except requests.RequestException as e:
        # the message can embed the full URL, which carries the apikey
        print(f"[whatsapp] {type(e).__name__}", file=sys.stderr)
        return False
    if r.status_code != 200:
        # CallMeBot explains itself in the body (key invalid, not activated,
        # rate limit); without this a dead channel is invisible in the logs
        body = re.sub(r"\s+", " ", r.text or "")[:200]
        print(f"[whatsapp] HTTP {r.status_code}: {body!r}", file=sys.stderr)
        return False
    return True


def send_ntfy(message: str, title: str | None = None) -> bool:
    topic = config.get("NTFY_TOPIC", required=True)
    headers = {}
    if title:
        # ntfy title header must be latin-1 safe; strip emoji/accents.
        headers["Title"] = title.encode("ascii", "ignore").decode() or "S25 alert"
    try:
        r = requests.post(f"{_NTFY_BASE}/{topic}",
                          data=message.encode("utf-8"),
                          headers=headers, timeout=20)
        return r.status_code in (200, 201)
    except requests.RequestException:
        return False


def notify(alerts: list[Alert]) -> dict:
    """Send all alerts. WhatsApp primary, ntfy always mirrors (spec §7).

    Returns a report: per-channel success for the log, and ``delivered``
    (indices into ``alerts``) for the dedup state.
    """
    return _deliver([format_alert(a) for a in alerts], "S25 Ultra deal")


def format_heartbeat(best, code_hint: list[tuple[str, float]] | tuple[str, float] | None,
                     sources: int, totals) -> str:
    """Daily summary making silent scraper decay visible (spec §7), Amazon
    only: the best eligible offer's 12x total and Pix price, one unvalidated
    code worth trying, and the last 24h of polls/failures (``totals`` is a
    ``store.run_log_totals`` row or a dict with the same keys)."""
    if best is None:
        lines = ["📊 S25 Amazon — sem oferta Excelente+ em estoque (48h)"]
    else:
        per, total = twelve_x(best)
        lines = [f"📊 S25 Amazon — 12x de {money(per)} (total {money(total)}) · "
                 f"Pix {money(pix_price(best))}"]
        storage = f"{best.storage_gb}GB" if best.storage_gb else ""
        who = " · ".join(x for x in (safe_text(best.seller), storage,
                                     best.grade_raw or "") if x)
        coupon = coupon_line(best)
        lines.append(f"{who} · {coupon}" if coupon else who)
    hints = [code_hint] if isinstance(code_hint, tuple) else list(code_hint or [])
    if hints:
        code, value = hints[0]
        more = (f" (+ {', '.join(safe_text(c) for c, _ in hints[1:])})"
                if len(hints) > 1 else "")
        lines.append(f"🎟️ testar {safe_text(code)}: até −{money(value)}{more} (não validado)")
    warn = "⚠️ " if (totals["failures"] or totals["partial"]) else ""
    lines.append(f"{warn}24h: {totals['runs']} polls · {totals['failures']} falhas · "
                 f"{totals['partial']} parciais · {totals['coupon_errors']} erros cupom"
                 f" · {sources} fontes")
    return "\n".join(lines)


def send_heartbeat(message: str) -> dict:
    """Send the (pre-formatted) heartbeat on both channels."""
    msg = safe_text(message)
    return {
        "wa_ok": _safe_send(send_whatsapp, msg),
        "ntfy_ok": _safe_send(send_ntfy, msg, title="S25 heartbeat"),
        "message": msg,
    }


def format_promo(promo) -> str:
    """One-line promo post (Phase 4b) in the §7 style."""
    price = f" — {money(promo.price)}" if promo.price is not None else ""
    return (f"🔔 Promo S25 Ultra{price}\n{safe_text(promo.title)}\n"
            f"{promo.source}\n{promo.url}")


def _send_blocks(blocks: list[str], title: str) -> dict:
    """Pack pre-formatted blocks into WhatsApp-sized messages and send each on
    both channels (best-effort; see ``_deliver``)."""
    return _deliver(blocks, title)


def notify_promos(promos: list) -> dict:
    """Send promo posts, batched, WhatsApp + ntfy (best-effort)."""
    return _send_blocks([format_promo(pr) for pr in promos], "S25 promo")


def format_coupon(code, value: float, price: float) -> str:
    """A new Amazon checkout code worth trying (``coupons.CouponCode``).
    Informational: it never lowers the 12x total until validated in a cart
    (coupon_checks.yaml), so the ping asks for exactly that."""
    limits = [f for f in code.flags if f in ("prime", "app", "first_purchase", "card")]
    caveat = "não validado — peça ao Claude para testar no carrinho"
    if limits:
        caveat = f"exige {', '.join(limits)} · {caveat}"
    lines = [
        f"🎟️ Cupom Amazon {code.key} — até {money(value)} num pedido de {money(price)}",
        safe_text(code.title),
        caveat,
        code.url or "",
    ]
    return "\n".join(x for x in lines if x)


def format_cashback(s) -> str:
    """An Amazon cashback portal turned on (``coupons.CashbackStatus``)."""
    rate = f" {s.rate_pct:g}%" if s.rate_pct else ""
    lines = [f"💸 Cashback Amazon ativo: {s.portal}{rate}", safe_text(s.detail), s.url]
    return "\n".join(x for x in lines if x)


def notify_coupons(blocks: list[str]) -> dict:
    """Send coupon/cashback blocks (already formatted), batched."""
    return _send_blocks(blocks, "S25 cupom")


def format_offer_promo(message: str, o, terms: str | None = None, others: int = 0) -> str:
    """A promotion message Amazon just started showing on a target offer (its
    cheapest one; ``others`` = more sellers showing it), plus the start of its
    official terms when they could be fetched."""
    storage = f"{o.storage_gb}GB" if o.storage_gb else ""
    more = f"(+{others} vendedor{'es' if others > 1 else ''})" if others else ""
    head = " ".join(x for x in (safe_text(o.seller), storage, more) if x)
    lines = [f"🏷️ Nova promoção Amazon: {safe_text(message)} — {head}"]
    if terms:
        lines.append(f"Termos: {safe_text(terms)}")
    lines.append(o.url)
    return "\n".join(lines)


def notify_offer_promos(blocks: list[str]) -> dict:
    """Send new-promotion blocks (already formatted), batched."""
    return _send_blocks(blocks, "S25 promo Amazon")


def send_test() -> dict:
    """Fire a single test message through both channels (build order §10.1)."""
    msg = "✅ S25 monitor: canal de alerta funcionando (teste)."
    return {
        "wa_ok": _safe_send(send_whatsapp, msg),
        "ntfy_ok": _safe_send(send_ntfy, msg, title="S25 test"),
        "message": msg,
    }
