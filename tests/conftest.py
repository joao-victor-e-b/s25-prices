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

from src import config, coupon_checks
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


@pytest.fixture(autouse=True)
def _default_role(monkeypatch):
    """Every test runs as the cloud watcher unless it says otherwise: neither
    the shell's nor .env's S25_ROLE leaks in (an empty value reads as cloud,
    and load_env never overrides a variable that is set)."""
    monkeypatch.setenv("S25_ROLE", "")


@pytest.fixture(autouse=True)
def _no_real_channels(monkeypatch):
    """No test can reach CallMeBot or ntfy with the real secrets from .env:
    they read empty (tests that need a configured channel set dummies)."""
    for name in ("CALLMEBOT_PHONE", "CALLMEBOT_APIKEY", "NTFY_TOPIC"):
        monkeypatch.setenv(name, "")


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """The default DB (and the loop's lock next to it) live in the test's
    tmp dir, never data/prices.db."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "prices.db")


@pytest.fixture
def write_checks(tmp_path):
    """write_checks([{...}, ...]) -> path of the (default) checks file."""
    def _write(entries) -> Path:
        path = coupon_checks.CHECKS_PATH
        path.write_text(yaml.safe_dump({"checks": entries}, allow_unicode=True),
                        encoding="utf-8")
        return path
    return _write
