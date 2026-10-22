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
| Amazon Seminovos | product page + All Offers panel: every seller's offer, card vs Pix price (raises only if both are blocked) |
| Americanas | Playwright (refurb "Usado:" listings) |
| Promotions | DuckDuckGo scan of deal-aggregators → 🔔 promo alerts (Phase 4b) |
| Amazon coupons | Promobit / Pelando / Cuponomia / Méliuz codes + Inter / Méliuz / Cuponomia cashback → 🎟️ / 💸 pings (daily) |

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
  coupons.py                 Amazon checkout codes + cashback portals (informational)
  cdi.py                     BCB SGS 4390 monthly CDI
  triggers.py                8 triggers §6 + dedup
  notify.py                  CallMeBot + ntfy §7
  store.py                   SQLite §5
  run.py                     orchestrator / CLI
data/prices.db               committed price history
tests/                       normalize + trigger unit tests
```

## Amazon: offers, Pix and coupons

- **Every seller, not just the buybox.** Amazon lists all refurb sellers on one
  ASIN (`B0FTMQQ7L2`, 256GB Titânio Preto); the cheapest is often *not* the
  buybox (Assurant Ecophone's R$ 3.919 offer in Oct 2026). Each poll reads the
  product page and the "Comparar outras ofertas" panel and stores one row per
  seller + grade (`listing_id = asin:seller_id:grade`). A seller that vanishes
  from a fully-read panel is stored as sold out, so its return fires 🟢 restock.
- **Card vs Pix.** `list_price` is the card price (what 12x sem juros totals);
  `pix_discount` is the "à vista no Pix ou NuPay (5% off)" promo, so PV uses the
  Pix price. Amazon applies the larger of Pix and a coupon, never both.
- **Coupon codes are informational.** Seller clip coupons can't be used on
  seminovos, and Amazon checkout codes are capped (R$ 20–200), often Prime /
  app / card / first-purchase only, one per order, and usually not combinable
  with the Pix promo — eligibility for marketplace sellers can only be checked
  in a logged-in cart. So codes never change PV: the daily sweep stores them
  (`coupon_codes`), pings a new code once when it's worth ≥ R$ 100 at the
  current Amazon price (max 5 per day), and the heartbeat shows
  "c/ cupom X: R$ Y (não verificado)" only when a code beats the Pix price.
- **Cashback.** All Amazon cashback portals are at 0% since Apr 2026; the daily
  sweep records their rate (`cashback_status`) and pings 💸 when one turns on.

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
3. The workflow polls tier 1 every 30 min and runs the full sweep (all tiers,
   promos, coupons) + heartbeat at 09:00 BRT. The heartbeat reports the last
   24h of failures from the `run_log` table.
4. The DB is committed back after each run, so history accrues in git.
