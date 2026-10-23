"""Shared test isolation.

src/coupon_checks.yaml is edited by hand (and by Claude in Chrome) while the
suite may run, so no test reads the real file implicitly: by default every
test sees a missing checks file. Tests that need checks write their own (see
``write_checks``).
"""
from pathlib import Path

import pytest
import yaml

from src import coupon_checks


@pytest.fixture(autouse=True)
def _isolated_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(coupon_checks, "CHECKS_PATH", tmp_path / "coupon_checks.yaml")


@pytest.fixture
def write_checks(tmp_path):
    """write_checks([{...}, ...]) -> path of the (default) checks file."""
    def _write(entries) -> Path:
        path = coupon_checks.CHECKS_PATH
        path.write_text(yaml.safe_dump({"checks": entries}, allow_unicode=True),
                        encoding="utf-8")
        return path
    return _write
