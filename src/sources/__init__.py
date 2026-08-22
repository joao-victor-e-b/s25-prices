"""Source registry loader.

Maps a registry entry to a concrete Source subclass by importing
``src.sources.<module>`` and reading its ``SOURCE`` attribute. Returns None
for sources whose scraper isn't implemented yet (Phase 2) so the orchestrator
can skip them cleanly.
"""
from __future__ import annotations

import importlib

from .base import Source


def load_source(cfg: dict) -> Source | None:
    module_name = cfg.get("module")
    if not module_name:
        return None
    try:
        mod = importlib.import_module(f"src.sources.{module_name}")
    except ModuleNotFoundError:
        return None
    cls = getattr(mod, "SOURCE", None)
    if cls is None:
        return None
    return cls(cfg)
