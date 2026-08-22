# S25 Ultra Refurb Monitor

Polls official Brazilian refurbished-device retailers for a **Galaxy S25 Ultra
(SM-S938B, 256GB, Titânio Preto)**, normalizes inconsistent condition grades and
payment terms into one comparable present value (PV), stores a price history, and
pushes a WhatsApp alert when a buy-worthy condition fires.

Built to the consolidated spec (`s25monitorbuildspecv2.md`). Runs on an hourly
GitHub Actions cron. Alerts via CallMeBot (WhatsApp) with an ntfy.sh fallback.

## Status

Core (spec §2–8): storage/schema, grade normalization + PV + cashback layer,
CDI fetch, all 8 triggers + 24h dedup, CallMeBot + ntfy + heartbeat, orchestrator,
GitHub Actions hourly cron. `new_low` stays off until ~30 days of history
(`--enable-new-low`, build order §10.6).

**Sources (live):**

| Source | Method |
|---|---|
| Trocafy | HTTP + Playwright fallback (CDN 403s datacenter IPs) |
| Trocafone | VTEX catalog API |
| Outlet do Celular | Tray category → per-product JSON-LD |
| Amazon Seminovos | direct HTTP, best-effort (raises on captcha) |
| Americanas | Playwright (refurb "Usado:" listings) |
| Promotions | DuckDuckGo scan of deal-aggregators → 🔔 promo alerts (Phase 4b) |

**Blocked/parked:** Magalu & Mercado Livre (fingerprint headless Chromium);
ML OAuth parked pending their auth-loop fix.

## Layout

```
.github/workflows/poll.yml   hourly cron + daily heartbeat
src/
  sources/                   one module per source_id (Phase 2)
  registry.yaml              source config — add a seller = edit this file
  models.py                  Observation dataclass (maps to schema)
  normalize.py               grade ranks §2, PV §3, cashback
  cdi.py                     BCB SGS 4390 monthly CDI
  triggers.py                8 triggers §6 + dedup
  notify.py                  CallMeBot + ntfy §7
  store.py                   SQLite §5
  run.py                     orchestrator / CLI
data/prices.db               committed price history
tests/                       normalize + trigger unit tests
```

## Local setup

```bash
python -m venv .venv && . .venv/Scripts/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env          # then fill in real values (see below)
python -m src.run --init-db   # create data/prices.db
python -m src.run --test-alert
pytest
```

### Secrets

Credentials live in a **gitignored `.env`** locally and in **GitHub Actions
repository secrets** in the cloud. Never commit them; keep the repo private.

| var | meaning |
|---|---|
| `CALLMEBOT_PHONE`  | WhatsApp number registered with CallMeBot |
| `CALLMEBOT_APIKEY` | CallMeBot API key |
| `NTFY_TOPIC`       | long random ntfy.sh topic (public if guessed) |

If the CallMeBot key leaks, send `Stop` to the bot and re-activate to rotate.
Subscribe to CallMeBot outage notifications so you learn about relay downtime.

## CLI

```bash
python -m src.run --init-db          # create schema
python -m src.run --test-alert       # send one test message (both channels)
python -m src.run --poll             # one poll cycle (tier 1-2)
python -m src.run --poll --dry-run   # evaluate + print alerts, send nothing (safe local testing)
python -m src.run --poll --daily     # include tier-3 sources
python -m src.run --heartbeat        # daily summary
python -m src.run --poll --enable-new-low   # after ~30 days of history
```

## Deploy

1. Push to a **private** GitHub repo.
2. Settings → Secrets and variables → Actions → add the three secrets above.
3. The workflow polls hourly at `:17` and sends a heartbeat at 09:00 BRT.
4. The DB is committed back after each run, so history accrues in git.
