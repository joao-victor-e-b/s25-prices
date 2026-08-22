"""CDI monthly rate.

Pulled from BCB SGS series 4390 ("CDI acumulada no mês", in percent). Precision
is not load-bearing (spec §3), so any fetch failure falls back to a hardcoded
value — revisit quarterly.
"""
from __future__ import annotations

import requests

# ~10.5%/yr effective -> ~0.83%/month. Revisit quarterly (spec §3).
DEFAULT_CDI_MONTHLY = 0.0088

_SGS_URL = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.4390/dados/ultimos/1?formato=json"


def get_cdi_monthly() -> float:
    """Return CDI as a monthly decimal (e.g. 0.0088). Never raises."""
    try:
        r = requests.get(_SGS_URL, timeout=15)
        if r.status_code == 200:
            data = r.json()
            if data:
                # SGS returns percent, comma or dot decimal depending on locale.
                raw = str(data[-1]["valor"]).replace(",", ".")
                pct = float(raw)
                if 0 < pct < 10:  # sanity: monthly CDI is a small percent
                    return pct / 100.0
    except (requests.RequestException, ValueError, KeyError, IndexError):
        pass
    return DEFAULT_CDI_MONTHLY
