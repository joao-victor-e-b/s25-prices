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
| 🔍 **Oferta não visível do servidor** | the product page's "Comparar outras N ofertas a partir de R$ X (+R$ Y de envio)" line puts the cheapest offer (X + Y) at ≤ **R$ 3.919**, and more than R$ 1 below every offer we know for that ASIN: the ones this poll attributed (any seller, grade or stock state) plus those stored in stock in the last 2 h (`triggers.UNSEEN_KNOWN_HOURS`) — so the buybox, an offer that just rotated out of it, or a known Aceitável unit the panel named a poll ago never re-alerts as 🔍. Seller and condition are unknown, so it says "confira" and names the level it would reach (🔴 ≤ 3.500 / 🟠 ≤ 3.800 / 🟡 ≤ 3.919). See [CI limitation](#ci-limitation-the-offers-panel) |
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
- 🔍 has the same ladder, per ASIN (`alerts_sent.listing_id = unseen:<ASIN>`,
  metric = X + Y): a rise never alerts, a R$ 50 drop or a stronger level
  re-fires, and it starts fresh only once a stored reading shows an offer
  *left* (`store.last_offer_departure`, the analogue of a sell-out): the
  product page lost its line (only the buybox was left), or the line's count
  fell with nothing ≤ R$ 3.919 left. A price bouncing across R$ 3.919 at the
  same count (3.915 → 3.925 → 3.915) is a reprice and alerts once. Also
  recorded only after delivery; `--dry-run` prints it and records nothing.

Example:

```
🔴 COMPRE — S25 Ultra 512GB preto
12x de R$ 291,67 (total R$ 3.500,00)
c/ cupom CUPOMX −R$ 200,00 (validado)
Pix à vista R$ 3.500,00
Assurant Ecophone · Excelente
https://www.amazon.com.br/dp/B0FTMWWS8Y?smid=A3SPPFNXUZXE4P

🔍 Amazon 512GB preto: oferta a partir de R$ 3.713,22 (12x ≈ R$ 309,44) — vendedor/condição não visíveis do servidor, confira
nível 🟠 BOM PREÇO · R$ 3.700,00 + R$ 13,22 de envio · 4 ofertas, 2 identificadas (menor R$ 4.149,00)
https://www.amazon.com.br/dp/B0FTMWWS8Y
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
  (512GB JetBlack), with `every: 3` (every 3rd iteration, so a poll fetches
  at most 6 ASINs) and `default_grade: Novo` (their product page names no
  condition). Each poll reads the product page (buybox, 12x plan, promos and
  the "Comparar outras N ofertas" line) and tries the "Comparar outras
  ofertas" panel once, storing one row per seller + grade (`listing_id =
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

## CI limitation: the offers panel

From GitHub Actions IPs Amazon answers the All Offers panel with HTTP 503
most of the time — two CI diagnostic runs (Oct 2026) got it for **every**
variant: `aodAjaxMain`, `/gp/aod/ajax`, full Chrome-141 client hints +
`sec-fetch` cors headers, cold/warm/mobile sessions, `/gp/offer-listing`,
repeated retries; the live watcher gets it on and off — while `/dp/` product
pages always load. `/dp/<ASIN>?smid=<seller>&psc=1` does not switch the
buybox from CI either, so per-seller pages are no way round. On a poll
without the panel, then, only the **buybox** seller can be named (locally,
from a home IP, the panel usually works and every seller is stored).

What still loads is the product page's other-offers line
(`#aod-ingress-link`):

> Comparar outras 3 ofertas a partir de R$4.582,47 · +R$ 13,22 de envio

`amazon.parse_ingress` reads it on every product page into
`models.OfferSummary`: `offers_total` (every offer on the ASIN, **buybox
included**), `min_card_price` (the lowest *card* price among them),
`min_shipping` (`+R$ X de envio`; GRÁTIS = 0) and `scope` (`ALL`, or `NEW` on
the new-unit pages: the link's `condition=`, or its `dp_olp_<SCOPE>_mbc` ref
token when it has none; anything but ALL/NEW/USED is stored as NULL). It is
not an observation — no seller, no grade — so it never drives 🔴/🟠/🟡.

A listing whose only offer is the buybox shows **no** line (single-offer
new-unit pages, Oct 2026). When our ASIN's page loads with a priced buybox
and the slot the line lives in (`#olpLinkWidget_feature_div`) but no line, a
**no-line marker** is recorded instead: `scope = NONE`, 1 offer at the
buybox's card price (0 when it is unavailable). A blocked or captcha page,
another variation's page, a page without the slot or without a buybox price
never writes one; it doesn't count in `ingress=k/m` either. Then:

- `offer_summary` stores lines and markers per ASIN (on change + hourly
  keepalive, like observations; merged append-only by gitsync). On
  2026-10-23 CI read B0FTMQQ7L2 with 3 offers and B0FTMWWS8Y with 4, while
  only 2 sellers were attributable.
- a rising `offers_total` is noted in the poll's `run_log.detail` (`<ASIN>
  offers 2->4 from 4149.00`; a marker compares with any scope, so a line
  appearing next to a lone buybox reads `1->2`), with no alert: a new seller
  says nothing about price by itself;
- 🔍 fires when the line's cheapest offer is within the 2026 low and below
  every known offer (see the alerts table); a marker never fires, and resets
  the 🔍 ladder (the hidden offer left);
- the daily heartbeat adds `Ofertas: 256GB N (menor R$ X) · 512GB N (menor
  R$ Y)` from the latest reading of each seminovo listing (last 48h; a
  marker shows as `1`, so a vanished line isn't reported for two more days;
  new-unit listings — `default_grade: Novo` — are left out).

Each poll therefore costs one product page + one panel try per due ASIN; a
blocked panel is not retried and does not slow the loop down (see Cadence).

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
  budget runs out; `every: N` sources only on every Nth poll (the four
  new-unit ASINs: every 3rd);
- Amazon rate-limits bursts from CI IPs, so every Amazon request in the
  process — all sources and promotion-terms lookups — goes through one pacer
  (`amazon.AMAZON_MIN_GAP_S` = 10 s ± 30% apart) on one shared session. Per
  due ASIN and poll: the product page first (buybox, 12x plan, promos and the
  other-offers line — everything CI can get), then **one** try at the offers
  panel (every seller's card price, when Amazon serves it). A blocked panel
  is not retried: from CI it is always blocked ([CI
  limitation](#ci-limitation-the-offers-panel));
- backoff: only real pushback (a source raised, product page
  blocked/captcha) doubles the sleep, up to 20 min; a poll without it resets
  to the interval. A blocked or empty offers panel next to a good product
  page is not pushback — it is CI's steady state, and backing off on it used
  to stretch every poll to 12–17 min. Each poll logs `[amazon] requests=N
  aod_ok=x/y ingress=k/m` and starts its `run_log.detail` with `aod_ok=x/y
  req=N ingress=k/m` (k of m product pages showed the other-offers line);
  the panel's failures still count as partial fetches there;
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
(seller, storage, coupon) and Pix price, the other-offers line of the
seminovo listings (`Ofertas: 256GB N (menor R$ X) · 512GB N (menor R$ Y)`,
skipped when `offer_summary` has nothing from the last 48h), the best
untested codes worth ≥ R$ 100, and the last 24h of polls/failures from
`run_log`. The heartbeat
job fails (exit 1) when neither channel delivered, so a dead secret or relay
shows up as a red run and a GitHub email.

## Layout

```
.github/workflows/poll.yml       watch: the ~6h polling loop (commits the DB)
.github/workflows/heartbeat.yml  daily heartbeat (read-only)
src/
  sources/                   one module per source (amazon.py is the live one)
  registry.yaml              source config — add a seller = edit this file
  models.py                  Observation (maps to schema) + OfferSummary (offers line)
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
