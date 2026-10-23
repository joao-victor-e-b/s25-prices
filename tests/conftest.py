"""Shared test isolation.

src/coupon_checks.yaml is edited by hand (and by Claude in Chrome) while the
suite may run, so no test reads the real file implicitly: by default every
test sees a missing checks file. Tests that need checks write their own (see
``write_checks``).

The Amazon scraper keeps process-wide state (request pacer, shared session):
every test starts from a fresh copy with the pacer's waits turned off (tests
that check pacing build their own Pacer).
"""
from pathlib import Path

import pytest
import yaml

from src import coupon_checks
from src.sources import amazon


@pytest.fixture(autouse=True)
def _isolated_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(coupon_checks, "CHECKS_PATH", tmp_path / "coupon_checks.yaml")


@pytest.fixture(autouse=True)
def _fresh_amazon_state(monkeypatch):
    amazon.reset_state()
    monkeypatch.setattr(amazon, "PACER", amazon.Pacer(gap=0, sleep=lambda s: None))
    yield
    amazon.reset_state()


@pytest.fixture
def write_checks(tmp_path):
    """write_checks([{...}, ...]) -> path of the (default) checks file."""
    def _write(entries) -> Path:
        path = coupon_checks.CHECKS_PATH
        path.write_text(yaml.safe_dump({"checks": entries}, allow_unicode=True),
                        encoding="utf-8")
        return path
    return _write
