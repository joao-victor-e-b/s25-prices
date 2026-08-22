"""Base class + shared HTTP helper for source scrapers.

Concrete scrapers (Trocafy, Trocafone, Outlet do Celular, ...) land in Phase 2,
one module per source_id. Each subclass implements ``fetch`` and returns a list
of raw ``Observation`` objects (grade_rank/pv left None; the orchestrator
normalizes them). Every scraper must be defensive: a failure raises, and the
orchestrator counts it toward the heartbeat rather than crashing the run.
"""
from __future__ import annotations

import requests

from ..models import Observation

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


def http_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": _UA,
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    })
    return s


class Source:
    """One retailer listing for SM-S938B 256GB."""

    source_id: str = ""
    seller: str = ""
    venue: str = "own_site"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.source_id = cfg["id"]
        self.seller = cfg.get("seller", self.seller)
        self.venue = cfg.get("venue", self.venue)
        self.url = cfg.get("url", "")
        self.session = http_session()

    def fetch(self) -> list[Observation]:
        raise NotImplementedError
