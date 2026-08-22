"""Alerting — CallMeBot (WhatsApp) primary + ntfy.sh fallback (spec §7)."""
from __future__ import annotations

import urllib.parse

import requests

from . import config
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


def format_alert(a: Alert) -> str:
    """Render one alert in the spec §7 layout."""
    o = a.obs
    color = o.color or "?"
    storage = f"{o.storage_gb}GB" if o.storage_gb else ""
    header = f"{a.priority} S25 Ultra {storage} {color}".strip()

    price_bits = [f"PV R${brl(o.pv)}"]
    detail = []
    if o.list_price:
        detail.append(f"list {brl(o.list_price)}")
    if o.cashback and o.portal:
        pct = round(o.cashback / o.list_price * 100) if o.list_price else 0
        detail.append(f"-{pct}% {o.portal}")
    if detail:
        price_bits.append("(" + ", ".join(detail) + ")")
    price_line = " ".join(price_bits)

    grade = o.grade_raw or f"rank {o.grade_rank}"
    bat = f"bat {o.battery_band}" if o.battery_band else ""
    line3 = " · ".join(x for x in (o.seller, grade, bat) if x)

    line4 = ""
    if o.installments_n and o.installment_value:
        juros = "s/juros" if o.interest_flag is False else "c/juros"
        line4 = f"{o.installments_n}x {juros} de R${brl(o.installment_value)}"

    note = f"[{a.note}]" if a.note else ""
    lines = [header, price_line, line3, line4, note, o.url]
    return "\n".join(x for x in lines if x)


def _batch(alerts: list[Alert]) -> list[str]:
    """Pack alerts into WhatsApp-sized messages (spec §7: batch, don't burst)."""
    messages: list[str] = []
    current = ""
    for a in alerts:
        block = format_alert(a)
        if current and len(current) + len(block) + 2 > _MAX_CHARS:
            messages.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}" if current else block
    if current:
        messages.append(current)
    return messages


def send_whatsapp(message: str) -> bool:
    params = {
        "phone": config.get("CALLMEBOT_PHONE", required=True),
        "text": message,
        "apikey": config.get("CALLMEBOT_APIKEY", required=True),
    }
    url = _CALLMEBOT_URL + "?" + urllib.parse.urlencode(params)
    try:
        r = requests.get(url, timeout=20)
        return r.status_code == 200
    except requests.RequestException:
        return False


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

    Returns a small report so the caller can log delivery success.
    """
    if not alerts:
        return {"sent": 0, "wa_ok": True, "ntfy_ok": True}

    wa_ok = True
    ntfy_ok = True
    for msg in _batch(alerts):
        if not send_whatsapp(msg):
            wa_ok = False
        if not send_ntfy(msg, title="S25 Ultra deal"):
            ntfy_ok = False
    return {"sent": len(alerts), "wa_ok": wa_ok, "ntfy_ok": ntfy_ok}


def send_heartbeat(best_pv: float | None, sources_polled: int,
                   scrape_failures: int) -> dict:
    """Daily 09:00 one-liner making silent scraper decay visible (spec §7)."""
    best = f"R${brl(best_pv)}" if best_pv is not None else "sem oferta elegível"
    msg = (f"📊 S25 heartbeat — melhor PV: {best} · "
           f"{sources_polled} fontes · {scrape_failures} falhas")
    return {
        "wa_ok": send_whatsapp(msg),
        "ntfy_ok": send_ntfy(msg, title="S25 heartbeat"),
        "message": msg,
    }


def send_test() -> dict:
    """Fire a single test message through both channels (build order §10.1)."""
    msg = "✅ S25 monitor: canal de alerta funcionando (teste)."
    return {
        "wa_ok": send_whatsapp(msg),
        "ntfy_ok": send_ntfy(msg, title="S25 test"),
        "message": msg,
    }
