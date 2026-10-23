# S25 Ultra Refurb Monitor

Watches **amazon.com.br** for a **Galaxy S25 Ultra (SM-S938B)** in **Titânio
Preto/JetBlack, 256GB or 512GB, Excelente condition or better** — refurbished
(Seminovos) and new-unit listings — and pushes a
WhatsApp alert (CallMeBot, with an ntfy.sh mirror) when the price the user
would actually pay — the **12x card total after confirmed coupons** — gets low
enough. Price history lives in a committed SQLite DB.

Originally built to the consolidated spec (`s25monitorbuildspecv2.md`) for five
retailers; since Oct 2026 it runs in **Amazon-only mode** (see below).

## What it alerts on

| alert | when |
|---|---|
| 🔴 **COMPRE** | 12x total ≤ **R$ 3.500** (`triggers.BUY_NOW_12X`) |
| 🟠 **BOM PREÇO** | 12x total ≤ **R$ 3.800** (`triggers.GOOD_12X`) |
| 🟡 **ABAIXO DO MÍNIMO DE 2026** | 12x total ≤ **R$ 3.919** (`triggers.YEAR_LOW_12X`) — beats the 2026 low (Assurant 256GB, 12x R$ 326,62, Oct 10–19) |
| 🏷️ Nova promoção Amazon | Amazon starts showing a discount message on an eligible offer whose 12x total is ≤ R$ 4.300, or that the message's stated amount would take to ≤ R$ 3.919 (never the everyday "à vista no Pix ou NuPay (5% off)" line) — once per offer + message, one alert per ASIN for all sellers showing it |
| 🎟️ Cupom Amazon | an aggregator lists a new checkout code worth ≥ R$ 100 at the current price — marked **"não validado"** |
| 💸 Cashback | an Amazon cashback portal turns on |
| 🟢 restock | an eligible offer whose 12x total is ≤ R$ 4.300 comes back in stock (folded into the 🔴/🟠 alert when it returns at one of those prices) |

- **Eligible** = in stock and grade rank ≥ 4 (`triggers.ALERT_MIN_RANK`):
  Amazon's *Premium*, *Excelente*, *Usado - Como novo* (4) and *Novo* (5)
  count; *Muito bom*, *Bom* and *Aceitável* don't.
- **12x total** (`normalize.card_total`) = card price − confirmed coupon. The
  Pix/NuPay promo never enters it (it only applies to a one-off Pix payment).
  The alert shows `12x de R$ X (total R$ Y)`, the coupon behind it, and the
  Pix price (card − the larger of Pix promo and coupon: Amazon never stacks
  them).
- **Dedup**: 🔴/🟠/🟡 are one ladder per listing. A new price alert needs a
  12x total **R$ 50** below the lowest one already sent for the listing (since
  it was last sold out), or a stronger level than any sent (🟡 → 🟠 → 🔴); a
  rise never alerts. A listing that sold out and came back starts fresh. The
  old multi-store triggers (grade upgrade, venue spread, new low, Trocafy
  review) never apply to Amazon offers.
  Delivery is counted per WhatsApp-sized batch: an alert is marked sent only
  once its batch reached at least one channel.

Example:

```
🔴 COMPRE — S25 Ultra 512GB preto
12x de R$ 291,67 (total R$ 3.500,00)
c/ cupom CUPOMX −R$ 200,00 (validado)
Pix à vista R$ 3.500,00
Assurant Ecophone · Excelente
https://www.amazon.com.br/dp/B0FTMWWS8Y?smid=A3SPPFNXUZXE4P
```

## Coupons: only confirmed ones lower the 12x total

Two kinds of coupon count:

1. **Amazon-displayed** coupons on the offer itself (AOD promo rows, the buybox
   coupon widget, "R$ 900 off. Insira o código SMART900" codes) → stored as
   `coupon` with `coupon_code = NULL` ("cupom Amazon −R$ Z"); a percentage
   respects its stated cap ("até R$ 100") and a minimum above the price voids
   it. A conditional one — a specific card (Cartão Amazon, a bank/brand),
   NuPay/Nubank, Pix, boleto or à vista only, first purchase, app — is
   announced (🏷️) but never subtracted; Prime-only counts.
2. **Codes validated in a logged-in cart** and recorded in
   [`src/coupon_checks.yaml`](src/coupon_checks.yaml) → `coupon_code` names the
   code ("c/ cupom CODE −R$ Z (validado)").

Codes found on aggregators (Promobit, Pelando, Cuponomia, Méliuz — see
`src/coupons.py`) are **never** subtracted on their own: Prime/app/card locks,
caps, minimum orders and seller eligibility only show up in a cart. They're
stored (`coupon_codes`), pinged once as "não validado — peça ao Claude para
testar no carrinho" (codes with a stated expiry date or listed by 2+ sites
first: undated single-site listings are mostly stale), and the daily heartbeat
suggests the best untested ones. A code whose latest cart check failed is
neither pinged nor suggested: for 90 days when Amazon called it "inválido",
14 days for "não pode ser aplicado à esta compra".

### Validation workflow

1. A 🎟️ ping (or the heartbeat's "🎟️ testar X") names a code.
2. Ask Claude (Claude in Chrome, logged into your Amazon account) — or do it
   yourself — to put the offer in the cart and apply the code. Don't buy.
3. Append the result to `src/coupon_checks.yaml` and push. The running watcher
   pulls every 30 min and re-reads the file every poll.

```yaml
checks:
  - code: CUPOMSEMPRE
    works: false
    checked: 2026-10-23          # date of the test
    asin: B0FTMQQ7L2             # 256GB; the 512GB is B0FTMWWS8Y
    seller_id: A2TO0KKQTM1BAB    # from the listing (?smid=...), or "*" if it worked for every seller tried
    cart_total: 4582.47          # item price in the cart when tested
    discount: 0.0                # R$ Amazon actually took off
    message: "texto exato que a Amazon mostrou"
```

Rules (`src/coupon_checks.py`):

- Append, don't edit: per code, the **latest** check for that ASIN + seller
  decides (on the same day a seller-specific check beats `"*"`). A newer
  "inválido" anywhere kills the code everywhere.
- A check counts for **7 days** (`MAX_AGE_DAYS`) and only with `works: true`.
  Always record `cart_total` and `discount`: without them nothing counts.
- Value: the recorded `discount`, while the price is not below `cart_total`
  (a cheaper order might miss a minimum). The aggregator's parsed terms
  (%/cap/minimum) price the code at today's card price instead only when they
  reproduce your `discount` at `cart_total`, a percentage has a stated cap,
  and — below the tested price — the terms state a minimum. An aggregator
  listing that expired or went inactive *after* your test vetoes it, however
  old the listing.
- A validated code replaces an Amazon-displayed coupon only when it's bigger
  (they don't stack).
- A malformed or future-dated entry is skipped with a warning on stderr; a
  file saved as cp1252 (PowerShell 5.1) is still read. It never stops the
  watcher. Quote the star: `seller_id: "*"`.

## Amazon: offers, Pix and promotions

- **Six products, every seller.** Seminovos: `amazon_seminovos` (ASIN
  `B0FTMQQ7L2`, 256GB) and `amazon_seminovos_512` (`B0FTMWWS8Y`, 512GB),
  Titânio Preto, polled every iteration. New units (a new black 256GB sold at
  R$ 3.543 with SMART900 in Oct 2026, and "Amazon Quase Novo — Usado - Como
  novo" units sit on the new listings): `B0DSYJCY45` (256GB Preto),
  `B0F1HDQSQG` (256GB JetBlack), `B0DSXZ4FZT` (512GB Preto), `B0DTMCLTKV`
  (512GB JetBlack), with `every: 2` (every 2nd iteration, so a poll fetches
  at most 6 ASINs) and `default_grade: Novo` (their product page names no
  condition). Each poll reads the product page and the "Comparar outras
  ofertas" panel and stores one row per seller + grade (`listing_id =
  asin:seller_id:grade`); the cheapest is often *not* the buybox. A seller
  that vanishes from a fully-read panel is stored as sold out, so its return
  can alert again; a panel for another storage, without a title, or with an
  unpriced offer never counts as fully read.
- **Card vs Pix.** `list_price` is the card price (what 12x sem juros totals);
  `pix_discount` is the "à vista no Pix ou NuPay (5% off)" promo; `coupon` the
  confirmed coupon. All stored raw; `normalize` applies Amazon's no-stack rule
  per payment path (PV takes the cheaper of Pix and the 12x plan at CDI).
  When the page hides the card price under a Pix promo, the shown (Pix) price
  is never used as the card price: it is rebuilt from the 12x plan (n x
  installment) or the promo % (rounded up), else the offer is dropped — each
  case is logged as a partial fetch.
- **Promotion messages** are read one promotion item at a time (hidden "cupom
  salvo" text dropped) and recorded in `offer_promos` (first/last seen); a new
  one that offers a discount — not the Pix line, a Pix/à vista-only price, a
  plan or a freebie — triggers 🏷️, with the start of its public terms
  (`/promotion/tnc/<id>`) when Amazon exposes the promotion id.
- **Cashback.** All Amazon cashback portals have been at 0% since Apr 2026;
  the coupon sweep records their rate and pings 💸 when one turns on.

## Amazon-only mode

Every non-Amazon source in `src/registry.yaml` carries `enabled: false` (its
config is kept). Disabled entries are skipped by the poll and not counted by
the heartbeat. To revive one, delete the line or set it to `true`.

Playwright is no longer installed (`requirements.txt`): only disabled sources
(Trocafy's fallback, Americanas) and a best-effort promo check use it. To
re-enable one of those sources, `pip install "playwright>=1.40,<2"` and add a
`python -m playwright install --with-deps chromium` step to the watch
workflow.

## Cadence: the watcher loop

GitHub's cron fires every 5 minutes at best and often runs 10–30 minutes late,
so polling is a loop inside one long job instead (the repo is public, so
Actions minutes are free):

```bash
python -m src.run --loop --minutes 345 --interval 240 --coupon-every 1800 --git-sync
```

- polls every **~4 min** (240 s ± 20% jitter) for 5h45, stopping before the
  budget runs out; `every: N` sources only on every Nth poll;
- when Amazon pushes back (captcha, HTTP 503/429, an error page, a source that
  raised) the sleep doubles, up to 20 min, and resets after a clean poll — an
  empty (sold-out) listing is not pushback;
- the coupon/cashback sweep runs on the first poll and then every 30 min; the
  DuckDuckGo deal scan is off in loop mode; CDI is fetched once per loop;
- `--git-sync` pulls before the first poll (a queued run's checkout predates
  the previous watcher's final snapshot), then commits + pushes
  `data/prices.db` every 30 min (a failed sync is logged, never fatal) and
  once more on exit, with both stop signals ignored: the normal retrying sync
  within 3 min when the time budget ran out, one fast attempt after
  SIGINT/SIGTERM or a crash. Each sync: `git add` → commit "data: price
  snapshot <UTC>" (skipped when nothing changed) → `git pull --rebase`
  (retried 3×) → `git push` (`src/gitsync.py`). If two snapshots of the binary
  DB conflict, neither wins: our rows are merged into a copy of upstream's DB
  (append-only tables unioned on their primary key, coupon_codes/offer_promos
  rows newer-wins), and only then does the job move onto upstream and commit
  the merge — a failed merge moves nothing, so no watcher's rows or dedup
  state are lost;
- an unchanged snapshot is stored at most once an hour per listing (any
  price/stock/coupon change is stored immediately), so the DB stays small.

`.github/workflows/poll.yml` (**watch**) checks out the branch tip and starts
the loop with `exec` (so the runner's stop signals reach Python) on a `*/30`
cron; scheduled and watch-mode runs share the `s25-watch` concurrency group:
the next run waits in line and starts as soon as the current one ends. Manual
runs: `mode` = `watch` | `dry` (one poll, no messages) | `test` (test alert);
dry and test runs get their own group, so they run at once and never displace
the watcher. `.github/workflows/heartbeat.yml` sends the
daily 09:00 BRT heartbeat: best eligible offer as "12x de R$ X (total R$ Y)"
(seller, storage, coupon) and Pix price, the best untested codes worth
≥ R$ 100, and the last 24h of polls/failures from `run_log`. The heartbeat
job fails (exit 1) when neither channel delivered, so a dead secret or relay
shows up as a red run and a GitHub email.

## Layout

```
.github/workflows/poll.yml       watch: the ~6h polling loop (commits the DB)
.github/workflows/heartbeat.yml  daily heartbeat (read-only)
src/
  sources/                   one module per source (amazon.py is the live one)
  registry.yaml              source config — add a seller = edit this file
  models.py                  Observation dataclass (maps to schema)
  normalize.py               grade ranks, 12x card total, Pix price, PV
  coupon_checks.py           cart-validated codes (coupon_checks.yaml)
  coupons.py                 aggregator codes + cashback portals (informational)
  triggers.py                12x thresholds, restock, ... + dedup
  notify.py                  CallMeBot + ntfy message formats
  store.py                   SQLite schema, migrations, snapshot skipping
  gitsync.py                 commit/pull/push of the DB from inside the loop
  run.py                     orchestrator / CLI / watcher loop
data/prices.db               committed price history (written only by the watch job)
tests/                       unit tests (no network)
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

For experiments, point `S25_DB_PATH` at a copy of the DB so the committed one
(owned by the watch job) stays untouched.

### Secrets

Credentials live in a **gitignored `.env`** locally and in **GitHub Actions
repository secrets** in the cloud. Never commit them (the repo is public).

| var | meaning |
|---|---|
| `CALLMEBOT_PHONE`  | WhatsApp number registered with CallMeBot |
| `CALLMEBOT_APIKEY` | CallMeBot API key |
| `NTFY_TOPIC`       | long random ntfy.sh topic (public if guessed) |

If the CallMeBot key leaks, send `Stop` to the bot and re-activate to rotate.
Subscribe to CallMeBot outage notifications so you learn about relay downtime.

## CLI

```bash
python -m src.run --init-db          # create schema (and add new columns to an old DB)
python -m src.run --test-alert       # send one test message (both channels)
python -m src.run --poll             # one poll cycle (+ DDG promos + coupon sweep)
python -m src.run --poll --dry-run   # evaluate + print alerts, send nothing
python -m src.run --loop --minutes 345 --interval 240 --coupon-every 1800 --git-sync
python -m src.run --loop --minutes 10 --interval 60 --dry-run   # local loop, sends nothing
python -m src.run --heartbeat        # daily summary
python -m src.run --poll --enable-new-low   # after ~30 days of history
```

## Deploy

1. Settings → Secrets and variables → Actions → add the three secrets above.
2. The watch workflow (`contents: write`, it pushes DB snapshots) keeps one
   loop running around the clock; the heartbeat workflow reports daily at
   09:00 BRT.
