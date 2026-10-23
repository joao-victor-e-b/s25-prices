"""Configuration + secret loading.

Secrets come from environment variables. For local runs we also load a
gitignored ``.env`` at the repo root so you don't have to export them by hand.
In GitHub Actions the env vars are injected from repository secrets and no
``.env`` file exists — that's fine, ``load_env`` is a no-op then.
"""
from __future__ import annotations

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path | None = None) -> None:
    """Populate os.environ from a .env file if present. Existing env wins."""
    env_path = path or (_REPO_ROOT / ".env")
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # Don't clobber values already provided by the real environment.
        os.environ.setdefault(key, value)


def get(name: str, default: str | None = None, required: bool = False) -> str | None:
    load_env()
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(
            f"Missing required config '{name}'. Set it in .env (local) or as a "
            f"GitHub Actions secret (cloud)."
        )
    return value


# Which watcher this process is: 'cloud' (the GitHub Actions loop, default) or
# 'local' (the PC watcher, scripts/local_watcher.ps1). Every poll writes it to
# run_log.host; a cloud poll stands by while a local one is active (run.py).
ROLES = ("cloud", "local")


def role(value: str | None = None) -> str:
    """The watcher role: ``value`` (the --role flag) if given, else S25_ROLE,
    else 'cloud'. Raises ValueError on anything but ROLES: a typo must not
    make the PC watcher log itself as 'cloud' (the cloud one would never
    stand by for it, and both would alert)."""
    raw = value or get("S25_ROLE") or "cloud"
    r = raw.strip().lower()
    if r not in ROLES:
        raise ValueError(f"S25_ROLE / --role must be one of {', '.join(ROLES)}, not {raw!r}")
    return r


# Default DB location: data/prices.db committed to the repo (spec §5).
DB_PATH = Path(os.environ.get("S25_DB_PATH", _REPO_ROOT / "data" / "prices.db"))
REGISTRY_PATH = _REPO_ROOT / "src" / "registry.yaml"
