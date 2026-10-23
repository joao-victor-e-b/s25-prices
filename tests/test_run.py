"""run.poll / run.heartbeat wiring with a fake Amazon source (no network):
registry, validated coupons, delivery-gated dedup, new-promotion alerts."""
import copy
import itertools
import re
from datetime import datetime, timedelta, timezone

import pytest

from src import config, notify, run, store
from src.coupons import CouponCode
from src.models import Observation

ASIN = "B0FTMQQ7L2"
SELLER = "A2TO0KKQTM1BAB"
LID = f"{ASIN}:{SELLER}:excelente"
TODAY = "2026-10-23"                 # the date Env pins poll time to
PIX_LINE = "à vista no Pix ou NuPay (5% off)"
COUPON_LINE = "Aplicar cupom de R$ 300 Termos"


def _offer(**kw):
    base = dict(source_id="amazon_seminovos", seller="CellularStore Oficial",
                venue="amazon", listing_id=LID, url=f"https://www.amazon.com.br/dp/{ASIN}",
                storage_gb=256, color="preto", grade_raw="Excelente",
                list_price=3950.0, pix_discount=197.5, installments_n=12,
                installment_value=329.17, in_stock=True, promos=[PIX_LINE])
    base.update(kw)
    return Observation(**base)


class FakeSource:
    def __init__(self, offers=(), degraded=None, error=None, summary=None):
        self.offers, self.degraded, self.error = list(offers), degraded or [], error
        self.complete = False
        self.summary = summary          # the dp's offers line to report
        self.offer_summary = None

    def fetch(self):
        if self.error:
            raise self.error
        self.offer_summary = copy.deepcopy(self.summary)
        return [copy.deepcopy(o) for o in self.offers]


class Env:
    def __init__(self, monkeypatch, db):
        self.db = db
        self.source = FakeSource()
        self.alerts, self.promo_blocks = [], []
        self.delivered = True
        start = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
        self._ticks = (start + timedelta(minutes=4 * i) for i in itertools.count())
        monkeypatch.setattr(config, "DB_PATH", db)
        monkeypatch.setattr(store, "now_iso",
                            lambda: next(self._ticks).isoformat(timespec="seconds"))
        monkeypatch.setattr(run, "load_registry", lambda include_disabled=False: [
            {"id": "amazon_seminovos", "venue": "amazon", "tier": 1}])
        monkeypatch.setattr(run, "load_source", lambda cfg: self.source)
        monkeypatch.setattr(notify, "notify", self._notify)
        monkeypatch.setattr(notify, "notify_offer_promos", self._notify_promos)
        monkeypatch.setattr(notify, "notify_unseen", self._notify_unseen)
        self.unseen = []

    def _report(self, n):
        return {"sent": n, "wa_ok": False, "ntfy_ok": self.delivered,
                "delivered": list(range(n)) if self.delivered else []}

    def _notify(self, alerts):
        self.alerts.extend(alerts)
        return self._report(len(alerts))

    def _notify_promos(self, blocks):
        self.promo_blocks.extend(blocks)
        return self._report(len(blocks))

    def _notify_unseen(self, alerts):
        self.unseen.extend(alerts)
        return self._report(len(alerts))

    def poll(self, *offers, **kw):
        if offers:
            self.source.offers = list(offers)
        kw.setdefault("scan_promos", False)
        return run.poll(cdi=0.0088, **kw)

    def rows(self, sql, *args):
        conn = store.connect(self.db)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()


@pytest.fixture
def env(monkeypatch, tmp_path):
    return Env(monkeypatch, tmp_path / "prices.db")


def _check(**kw):
    base = dict(code="CUPOMX", works=True, checked=TODAY, asin=ASIN, seller_id=SELLER,
                cart_total=3950.0, discount=200.0, message="Cupom aplicado")
    base.update(kw)
    return base


# --- registry --------------------------------------------------------------------

def test_registry_is_amazon_only_with_256_and_512():
    enabled = run.load_registry()
    assert [c["id"] for c in enabled] == [
        "amazon_seminovos", "amazon_seminovos_512", "amazon_novo_256_preto",
        "amazon_novo_256_jetblack", "amazon_novo_512_preto", "amazon_novo_512_jetblack"]
    assert all(c["venue"] == "amazon" and c["module"] == "amazon" for c in enabled)
    by_id = {c["id"]: c for c in enabled}
    assert by_id["amazon_seminovos"]["asin"] == "B0FTMQQ7L2"
    assert by_id["amazon_seminovos"]["match"]["storage_gb"] == 256
    c512 = by_id["amazon_seminovos_512"]
    assert c512["asin"] == "B0FTMWWS8Y" and c512["url"].endswith("/dp/B0FTMWWS8Y")
    assert c512["match"] == {"storage_gb": 512, "colors": ["preto", "jetblack"]}
    assert c512["tier"] == 1
    everything = run.load_registry(include_disabled=True)
    assert len(everything) > len(enabled)          # configs kept, just disabled
    assert all(c.get("enabled") is False for c in everything if c["venue"] != "amazon")


# --- validated coupons -------------------------------------------------------------

def test_validated_code_lowers_the_12x_total_and_fires(env, write_checks):
    write_checks([_check()])
    r = env.poll(_offer())
    (a,) = env.alerts
    assert a.trigger == "aggressive" and a.metric == 3750.0
    assert (a.obs.coupon, a.obs.coupon_code) == (200.0, "CUPOMX")
    assert "c/ cupom CUPOMX −R$ 200,00 (validado)" in notify.format_alert(a)
    (row,) = env.rows("SELECT coupon, coupon_code, pv FROM observations")
    assert (row["coupon"], row["coupon_code"]) == (200.0, "CUPOMX")
    assert row["pv"] < 3750.0                      # 12x of 3750 discounted at CDI
    (sent,) = env.rows("SELECT trigger, pv FROM alerts_sent")
    assert (sent["trigger"], sent["pv"]) == ("aggressive", 3750.0)
    assert r["alerts"] == 1 and r["amazon_trouble"] is False


def test_failed_or_other_seller_checks_change_nothing(env, write_checks):
    write_checks([_check(works=False, discount=0.0), _check(code="OTHER", seller_id="X")])
    env.poll(_offer())
    assert env.alerts == []                        # 3950 > 3800
    (row,) = env.rows("SELECT coupon, coupon_code FROM observations")
    assert (row["coupon"], row["coupon_code"]) == (0, None)


def test_bigger_amazon_coupon_wins_over_a_validated_code(env, write_checks):
    write_checks([_check()])
    env.poll(_offer(coupon=300.0))
    (a,) = env.alerts
    assert (a.obs.coupon, a.obs.coupon_code) == (300.0, None)   # Amazon-shown
    assert a.metric == 3650.0


def test_inactive_aggregator_listing_vetoes_the_code(env, write_checks):
    write_checks([_check()])
    conn = store.connect(env.db)
    store.init_db(conn)
    store.upsert_coupon(conn, CouponCode(source="cuponomia", code="CUPOMX",
                                         abs_off=200.0, active=False),
                        "2026-10-23T11:00:00+00:00")
    conn.commit()
    conn.close()
    env.poll(_offer())
    assert env.alerts == []


# --- delivery-gated dedup + unchanged-row skipping ---------------------------------

def test_alert_retried_until_delivered_even_when_the_row_is_skipped(env):
    o = _offer(list_price=3700.0, pix_discount=185.0)
    env.delivered = False
    r1 = env.poll(o)
    assert len(env.alerts) == 1 and env.rows("SELECT * FROM alerts_sent") == []
    env.delivered = True
    r2 = env.poll()                                # identical snapshot 4 min later
    assert r2["stored"] == 0 and r2["observations"] == 1   # not stored, still evaluated
    assert len(env.alerts) == 2
    assert len(env.rows("SELECT * FROM alerts_sent")) == 1
    env.poll()
    assert len(env.alerts) == 2                    # delivered once -> deduped
    assert r1["stored"] == 1


def test_amazon_trouble_flag(env):
    env.source = FakeSource([_offer()], degraded=["aod: HTTP 503"])
    r = env.poll()
    assert r["amazon_trouble"] is True and r["partial"] == 1
    env.source = FakeSource(error=RuntimeError("captcha"))
    r = env.poll()
    assert r["amazon_trouble"] is True and r["failures"] == 1
    env.source = FakeSource([_offer()])
    assert env.poll()["amazon_trouble"] is False


# --- new-promotion alerts -------------------------------------------------------------

def test_new_promo_alerts_once(env):
    env.poll(_offer(promos=[PIX_LINE, COUPON_LINE]))
    (block,) = env.promo_blocks
    assert block.startswith("🏷️ Nova promoção Amazon: Aplicar cupom de R$ 300 Termos — "
                            "CellularStore Oficial 256GB")
    assert not re.search(r"\$\d", block)
    promos = env.rows("SELECT message FROM offer_promos WHERE asin=? AND seller_id=?",
                      ASIN, SELLER)
    assert {r["message"] for r in promos} == {PIX_LINE, COUPON_LINE}
    (sent,) = env.rows("SELECT listing_id, trigger FROM alerts_sent")
    assert sent["trigger"] == "offer_promo"
    assert re.fullmatch(rf"promo:{ASIN}:{SELLER}:[0-9a-f]{{10}}", sent["listing_id"])
    env.poll()
    assert len(env.promo_blocks) == 1              # same message: no repeat
    env.poll(_offer(promos=[PIX_LINE, COUPON_LINE, "Economize 10% com Prime"]))
    assert len(env.promo_blocks) == 2


def test_routine_pix_promo_never_alerts(env):
    env.poll(_offer(promos=[PIX_LINE, "Em até 12x de R$ 329,17 sem juros"]))
    assert env.promo_blocks == []
    assert len(env.rows("SELECT * FROM offer_promos")) == 2   # still recorded


def test_promo_dry_run_writes_nothing(env, capsys):
    env.poll(_offer(promos=[COUPON_LINE]), dry_run=True)
    assert env.promo_blocks == []
    assert "new-promotion message(s) would send" in capsys.readouterr().out
    assert env.rows("SELECT * FROM offer_promos") == []
    assert env.rows("SELECT * FROM alerts_sent") == []
    env.poll()
    assert len(env.promo_blocks) == 1              # the dry run didn't consume it


def test_promo_undelivered_is_retried(env):
    env.delivered = False
    env.poll(_offer(promos=[COUPON_LINE]))
    env.delivered = True
    env.poll()
    assert len(env.promo_blocks) == 2
    env.poll()
    assert len(env.promo_blocks) == 2


def test_promo_ignored_on_ineligible_offers(env):
    env.poll(_offer(listing_id=f"{ASIN}:{SELLER}:bom", grade_raw="Bom",
                    promos=[COUPON_LINE]),
             _offer(listing_id=f"{ASIN}:X:excelente", in_stock=False,
                    promos=[COUPON_LINE]))
    assert env.promo_blocks == []


# --- heartbeat ----------------------------------------------------------------------

NOW = "2026-10-23T12:00:00+00:00"


def _hb_conn(tmp_path):
    conn = store.connect(tmp_path / "hb.db")
    store.init_db(conn)
    store.insert_observation(conn, _offer(
        seller="Assurant Ecophone", listing_id=f"{ASIN}:A3SPPFNXUZXE4P:excelente",
        list_price=3919.0, pix_discount=195.95, installments_n=None,
        installment_value=None, grade_rank=4, ts="2026-10-23T11:00:00+00:00"))
    store.insert_observation(conn, _offer(   # cheaper but Bom: not eligible
        listing_id=f"{ASIN}:X:bom", grade_raw="Bom", grade_rank=2, list_price=3500.0,
        ts="2026-10-23T11:00:00+00:00"))
    for c in (CouponCode(source="cuponomia", code="OIPRIME15", pct=15.0, cap=200.0,
                         min_order=800.0, flags=["prime"]),
              CouponCode(source="cuponomia", code="CUPOMSEMPRE", abs_off=150.0,
                         min_order=1499.0),
              CouponCode(source="promobit", code="CUPOMPRIME10", pct=10.0, cap=20.0)):
        store.upsert_coupon(conn, c, NOW)
    return conn


def test_heartbeat_message(tmp_path):
    msg = run._heartbeat_message(_hb_conn(tmp_path), NOW)
    lines = msg.split("\n")
    assert lines[0] == ("📊 S25 Amazon — 12x de R$ 326,58 (total R$ 3.919,00) · "
                        "Pix R$ 3.723,05")
    assert lines[1] == "Assurant Ecophone · 256GB · Excelente"
    # the best untested code first, the next ones to batch-test in parentheses
    assert lines[2].startswith("🎟️ testar OIPRIME15: até −R$ 200,00")
    assert lines[2].endswith("(não validado)")
    assert lines[3].startswith("24h: 0 polls") and lines[3].endswith("· 6 fontes")


def test_heartbeat_skips_codes_already_tried(tmp_path, write_checks):
    write_checks([_check(code="OIPRIME15", works=False, discount=0.0,
                         seller_id="A3SPPFNXUZXE4P", checked="2026-10-22")])
    msg = run._heartbeat_message(_hb_conn(tmp_path), NOW)
    assert "testar CUPOMSEMPRE: até −R$ 150,00 (não validado)" in msg


def test_heartbeat_hint_must_beat_the_current_coupon(tmp_path):
    conn = _hb_conn(tmp_path)
    conn.execute("UPDATE observations SET coupon=250, coupon_code='CUPOMX' "
                 "WHERE seller='Assurant Ecophone'")
    msg = run._heartbeat_message(conn, NOW)
    assert "(total R$ 3.669,00)" in msg and "CUPOMX −R$ 250,00 (validado)" in msg
    assert "testar" not in msg


def test_heartbeat_sends(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "hb.db")
    _hb_conn(tmp_path).commit()
    sent = []
    monkeypatch.setattr(notify, "send_whatsapp", lambda m: sent.append(m) or True)
    monkeypatch.setattr(notify, "send_ntfy", lambda m, title=None: True)
    r = run.heartbeat()
    assert r["wa_ok"] and sent and sent[0].startswith("📊 S25 Amazon")


# --- review round 2 --------------------------------------------------------------

def test_empty_listing_is_not_amazon_trouble(env):   # C3
    env.source = FakeSource([_offer()], degraded=["aod: no offers in panel"])
    env.source.blocked = False
    r = env.poll()
    assert r["partial"] == 1 and r["amazon_trouble"] is False
    env.source.blocked = True
    assert env.poll()["amazon_trouble"] is True


def test_every_n_sources_are_staggered(env, monkeypatch):
    calls = []

    class Counting(FakeSource):
        def __init__(self, name):
            super().__init__([])
            self.name = name

        def fetch(self):
            calls.append(self.name)
            return []

    sources = {"semi": Counting("semi"), "novo": Counting("novo")}
    monkeypatch.setattr(run, "load_registry", lambda include_disabled=False: [
        {"id": "semi", "venue": "amazon", "tier": 1},
        {"id": "novo", "venue": "amazon", "tier": 1, "every": 2}])
    monkeypatch.setattr(run, "load_source", lambda cfg: sources[cfg["id"]])
    for i in range(4):
        run.poll(cdi=0.0088, scan_promos=False, iteration=i)
    assert calls == ["semi", "novo", "semi", "semi", "novo", "semi"]
    calls.clear()
    run.poll(cdi=0.0088, scan_promos=False)        # a one-off poll covers everything
    assert calls == ["semi", "novo"]


def test_expired_code_stays_vetoed_after_the_listing_ages_out(env, write_checks):   # C5
    write_checks([_check(code="SEMINOVO200", checked="2026-10-21", cart_total=3950.0)])
    conn = store.connect(env.db)
    store.init_db(conn)
    store.upsert_coupon(conn, CouponCode(source="promobit", code="SEMINOVO200",
                                         abs_off=200.0, until="2026-10-22"),
                        "2026-10-15T12:00:00+00:00")   # last seen long ago
    conn.commit()
    conn.close()
    env.poll(_offer())
    assert env.alerts == []                           # 3950: the expired code is out


def test_only_delivered_batches_are_marked_sent(env, monkeypatch):   # C8
    def partial(alerts):
        env.alerts.extend(alerts)
        return {"sent": len(alerts), "wa_ok": False, "ntfy_ok": False, "delivered": [1]}
    monkeypatch.setattr(notify, "notify", partial)
    env.poll(_offer(list_price=3700.0),
             _offer(listing_id=f"{ASIN}:OTHER:excelente", list_price=3600.0))
    rows = env.rows("SELECT listing_id FROM alerts_sent")
    assert [r["listing_id"] for r in rows] == [f"{ASIN}:OTHER:excelente"]


def test_codes_to_try_prefer_corroborated_codes(tmp_path):   # C21
    conn = _hb_conn(tmp_path)
    for i in range(5):
        store.upsert_coupon(conn, CouponCode(source="cuponomia", code=f"LONELY{i}",
                                             abs_off=200.0), NOW)
    store.upsert_coupon(conn, CouponCode(source="meliuz,promobit", code="DATED",
                                         abs_off=150.0, until="2099-12-31"), NOW)
    tried = run._codes_to_try(conn, run._best_amazon_offer(conn, NOW), NOW)
    assert tried[0] == ("DATED", 150.0)


# --- pre-deploy round: new-promotion alerts near the user's range only (A5) -----

CARD_PROMO = "Economize R$ 300 com cupom no Cartão Amazon"


def _seller(lid_seller, total, promos, asin="B0DSYJCY45"):
    return _offer(listing_id=f"{asin}:{lid_seller}:novo", grade_raw="Novo",
                  seller=lid_seller, list_price=total, pix_discount=0.0,
                  installments_n=None, installment_value=None, promos=promos)


def test_far_off_offers_dont_announce_promotions(env):
    env.poll(_seller("S1", 6499.0, [CARD_PROMO]), _seller("S2", 7999.0, [CARD_PROMO]),
             _seller("S3", 8999.0, [CARD_PROMO]))
    assert env.promo_blocks == []
    assert len(env.rows("SELECT * FROM offer_promos")) == 3      # history still kept


def test_a_promo_that_brings_the_total_under_the_2026_low_is_announced(env):
    env.poll(_seller("S1", 4500.0, ["R$ 900 off. Insira o código SMART900"]))
    assert len(env.promo_blocks) == 1                           # 4500 - 900 <= 3919


def test_one_alert_per_message_across_sellers(env):
    msg = "Aplicar cupom de R$ 150"
    env.poll(_seller("S1", 4200.0, [msg]), _seller("S2", 4100.0, [msg]),
             _seller("S3", 4300.0, [msg]))
    (block,) = env.promo_blocks
    assert "— S2 256GB (+2 vendedores)" in block               # cheapest offer shown
    assert len(env.rows("SELECT * FROM alerts_sent WHERE trigger='offer_promo'")) == 3
    env.poll()
    assert len(env.promo_blocks) == 1                           # all three recorded


# --- CI rate limiting: per-poll request stats and pushback levels ---------------

def test_poll_logs_requests_and_aod_ratio(env, capsys):
    from src.sources import amazon as amazon_mod

    class Counting(FakeSource):
        def fetch(self):
            amazon_mod.PACER.wait()
            amazon_mod.PACER.wait()
            self.aod_tried, self.aod_ok = 1, 0
            self.ingress_tried, self.ingress_ok = 1, 1
            self.block_level, self.blocked = None, False   # panel 503, dp fine
            self.degraded = ["aod: amazon aod: HTTP 503"]
            return super().fetch()

    env.source = Counting([_offer()])
    r = env.poll()
    assert (r["requests"], r["aod_ok"], r["aod_tried"]) == (2, 0, 1)
    assert (r["ingress_ok"], r["ingress_tried"]) == (1, 1)
    assert r["amazon_block"] is None and r["amazon_trouble"] is False   # no backoff
    assert "[amazon] requests=2 aod_ok=0/1 ingress=1/1\n" in capsys.readouterr().err
    (row,) = env.rows("SELECT detail FROM run_log")
    assert row["detail"].startswith("aod_ok=0/1 req=2 ingress=1/1 | amazon_seminovos partial:")


def test_a_raising_amazon_source_is_hard_trouble(env):
    env.source = FakeSource(error=RuntimeError("captcha"))
    r = env.poll()
    assert r["amazon_block"] == "hard"
    (row,) = env.rows("SELECT detail FROM run_log")
    assert row["detail"].startswith("aod_ok=0/0 req=0")


def test_new_unit_listings_poll_every_third_iteration():
    every = {c["id"]: c.get("every", 1) for c in run.load_registry()}
    assert every["amazon_seminovos"] == every["amazon_seminovos_512"] == 1
    assert {every[k] for k in every if k.startswith("amazon_novo")} == {3}


# --- the dp's offers line: offer_summary, 🔍 alerts, heartbeat (Oct 2026) ------------

from pathlib import Path   # noqa: E402

from src.models import OfferSummary   # noqa: E402

FIX = Path(__file__).parent / "fixtures"


def _line(n=3, price=4582.47, shipping=13.22, asin=ASIN, scope="ALL"):
    return OfferSummary(asin=asin, offers_total=n, min_card_price=price,
                        min_shipping=shipping, scope=scope, storage_gb=256, color="preto")


def test_offer_summary_stored_on_change_and_a_rise_is_only_noted(env, capsys):
    env.source = FakeSource([_offer()], summary=_line())
    env.poll()
    env.poll()                                     # unchanged 4 min later: skipped
    env.source.summary = _line(n=4)
    r = env.poll()
    rows = env.rows("SELECT ts, offers_total, min_card_price, min_shipping, scope "
                    "FROM offer_summary ORDER BY ts")
    assert [(x["offers_total"], x["min_card_price"], x["min_shipping"], x["scope"])
            for x in rows] == [(3, 4582.47, 13.22, "ALL"), (4, 4582.47, 13.22, "ALL")]
    assert r["alerts"] == r["unseen_alerts"] == 0 and env.unseen == []   # record only
    detail = env.rows("SELECT detail FROM run_log ORDER BY ts DESC")[0]["detail"]
    assert detail.startswith("aod_ok=0/0 req=0 ingress=0/0 | ")
    assert f"{ASIN} offers 3->4 from 4582.47" in detail
    assert f"[offers] {ASIN} offers 3->4" in capsys.readouterr().out
    env.source.summary = _line(n=2)
    env.poll()                                     # a drop is not noted
    detail = env.rows("SELECT detail FROM run_log ORDER BY ts DESC")[0]["detail"]
    assert "offers" not in detail


def test_unseen_offer_alert_is_sent_and_recorded_once(env):
    # buybox 3.950 (above every threshold), the line says "a partir de 3.700 + 13,22"
    env.source = FakeSource([_offer()], summary=_line(n=4, price=3700.0))
    r = env.poll()
    (a,) = env.unseen
    assert r["unseen_alerts"] == 1 and env.alerts == []
    assert (a.level, a.metric, a.attributed, a.attributed_min) == (
        "aggressive", 3713.22, 1, 3950.0)
    (sent,) = env.rows("SELECT listing_id, trigger, pv FROM alerts_sent")
    assert tuple(sent) == (f"unseen:{ASIN}", "unseen_offer", 3713.22)
    env.poll()
    assert len(env.unseen) == 1                    # deduped by the ladder


def test_unseen_offer_retried_until_delivered(env):
    env.source = FakeSource([_offer()], summary=_line(n=4, price=3700.0))
    env.delivered = False
    env.poll()
    assert env.rows("SELECT * FROM alerts_sent") == []
    env.delivered = True
    env.poll()
    env.poll()
    assert len(env.unseen) == 2 and len(env.rows("SELECT * FROM alerts_sent")) == 1


def test_unseen_offer_dry_run_prints_and_records_nothing(env, capsys):
    env.source = FakeSource([_offer()], summary=_line(n=4, price=3700.0))
    env.poll(dry_run=True)
    out = capsys.readouterr().out
    assert "[dry-run] 1 unseen-offer alert(s) would send:" in out
    assert "🔍 Amazon 256GB preto: oferta a partir de R$ 3.713,22" in out
    assert env.unseen == [] and env.rows("SELECT * FROM alerts_sent") == []
    assert len(env.rows("SELECT * FROM offer_summary")) == 1   # history still kept
    env.poll()
    assert len(env.unseen) == 1                    # the dry run didn't consume it


def test_heartbeat_offers_line_from_the_latest_rows(tmp_path):
    conn = _hb_conn(tmp_path)
    assert "Ofertas" not in run._heartbeat_message(conn, NOW)    # no rows: no line
    for s, ts in ((_line(), "2026-10-23T09:00:00+00:00"),
                  (_line(n=2, price=4300.0), "2026-10-23T11:00:00+00:00"),   # latest 256
                  (_line(n=4, price=4149.0, shipping=None, asin="B0FTMWWS8Y"),
                   "2026-10-23T11:30:00+00:00"),
                  (_line(n=5, price=5799.0, asin="B0DSYJCY45", scope="NEW"),  # new unit
                   "2026-10-23T11:30:00+00:00"),
                  (_line(n=2, asin="B0DSXZ4FZT"), "2026-10-20T11:30:00+00:00")):  # stale
        s.ts = ts
        store.insert_offer_summary(conn, s)
    lines = run._heartbeat_message(conn, NOW).split("\n")
    assert lines[2] == "Ofertas: 256GB 2 (menor R$ 4.300,00) · 512GB 4 (menor R$ 4.149,00)"
    assert lines[3].startswith("🎟️ testar")


def test_poll_with_the_offers_panel_blocked_is_not_trouble(env, monkeypatch, capsys):
    # the real source end to end: the dp loads, the panel 503s (CI, Oct 2026)
    from src.sources import amazon as amazon_mod
    from tests.test_amazon import AOD_ERROR_BODY, _Resp

    env.source = amazon_mod.AmazonSeminovosSource({
        "id": "amazon_seminovos", "venue": "amazon", "asin": ASIN,
        "url": f"https://www.amazon.com.br/dp/{ASIN}",
        "match": {"storage_gb": 256, "colors": ["preto", "jetblack"]}})
    calls = []

    def get(url, headers=None, timeout=None):
        calls.append(url)
        if "aodAjaxMain" in url:
            return _Resp(503, AOD_ERROR_BODY)
        return _Resp(200, (FIX / "amazon_dp_pix.html").read_bytes())
    monkeypatch.setattr(env.source.session, "get", get)
    r = env.poll()
    assert len(calls) == 2                         # dp + one panel try, no retry
    assert r["amazon_block"] is None and r["amazon_trouble"] is False
    assert (r["aod_ok"], r["aod_tried"], r["ingress_ok"], r["ingress_tried"]) == (0, 1, 1, 1)
    assert r["observations"] == 1 and r["unseen_alerts"] == 0   # the line's min = buybox
    (row,) = env.rows("SELECT detail FROM run_log")
    assert row["detail"].startswith("aod_ok=0/1 req=2 ingress=1/1 | amazon_seminovos partial:")
    (s,) = env.rows("SELECT asin, offers_total, min_card_price, min_shipping, scope "
                    "FROM offer_summary")
    assert tuple(s) == (ASIN, 3, 4582.47, 13.22, "ALL")
    assert "backoff" not in capsys.readouterr().err


# --- review round: known offers, the no-line marker, the heartbeat -----------------

def _no_line(price=4582.47, asin=ASIN, n=1):
    return OfferSummary(asin=asin, offers_total=n, min_card_price=price,
                        scope=OfferSummary.NO_LINE, storage_gb=256, color="preto")


def test_unseen_quiet_for_an_offer_the_last_poll_named(env):
    # Q1: the panel loads — Excelente buybox 4.275,78 + an Aceitável unit at
    # 3.890, which is the line's minimum. Q2: the panel 503s, only the buybox
    # is named, the line is unchanged: that Aceitável unit is no hidden offer.
    buybox = _offer(list_price=4275.78, pix_discount=0.0, installments_n=None,
                    installment_value=None, promos=[])
    aceitavel = _offer(listing_id=f"{ASIN}:A3SPPFNXUZXE4P:aceitavel",
                       seller="Assurant Ecophone", grade_raw="Aceitável",
                       list_price=3890.0, pix_discount=0.0, installments_n=None,
                       installment_value=None, promos=[])
    env.source = FakeSource([buybox, aceitavel], summary=_line(n=4, price=3890.0,
                                                               shipping=0.0))
    r1 = env.poll()
    env.source.offers = [buybox]
    r2 = env.poll()
    assert r1["unseen_alerts"] == r2["unseen_alerts"] == 0 and env.unseen == []
    assert env.alerts == []                       # Aceitável never alerts


def test_unseen_quiet_when_the_buybox_rotates_away_from_an_alerted_offer(env):
    # R1: Assurant Excelente 3.900 holds the buybox -> 🟡. R2: CellularStore
    # 4.582,47 holds it; the line still says 3.900 — the offer we just alerted
    assurant = _offer(listing_id=f"{ASIN}:A3SPPFNXUZXE4P:excelente",
                      seller="Assurant Ecophone", list_price=3900.0, pix_discount=0.0,
                      installments_n=None, installment_value=None, promos=[])
    env.source = FakeSource([assurant], summary=_line(n=3, price=3900.0, shipping=0.0))
    env.poll()
    assert [a.trigger for a in env.alerts] == ["year_low"]
    env.source.offers = [_offer(list_price=4582.47, pix_discount=0.0, promos=[])]
    r = env.poll()
    assert r["unseen_alerts"] == 0 and env.unseen == []
    assert len(env.alerts) == 1


def test_line_that_disappears_is_stored_as_a_marker_and_resets_the_ladder(env, capsys):
    env.source = FakeSource([_offer()], summary=_line(n=2, price=3700.0))
    env.poll()
    assert len(env.unseen) == 1                    # 🔍 at 3.713,22
    env.source.summary = _no_line(3950.0)          # sold: only the buybox is left
    r = env.poll()
    assert r["unseen_alerts"] == 0 and len(env.unseen) == 1
    rows = env.rows("SELECT offers_total, min_card_price, scope FROM offer_summary "
                    "ORDER BY ts")
    assert [tuple(x) for x in rows] == [(2, 3700.0, "ALL"), (1, 3950.0, "NONE")]
    # the heartbeat reports the lone buybox, not the vanished line
    conn = store.connect(env.db)
    try:
        assert run._offer_lines(conn, store.now_iso(), [
            {"asin": ASIN, "match": {"storage_gb": 256}}]) == [("256GB", 1, 3950.0)]
    finally:
        conn.close()
    # a blocked page writes nothing (no marker, no line)
    env.source.error = RuntimeError("amazon: captcha / bot wall")
    env.poll()
    assert len(env.rows("SELECT * FROM offer_summary")) == 2
    env.source.error = None
    # the same price coming back is a new offer: it alerts again, and 1 -> 2 is noted
    env.source.summary = _line(n=2, price=3700.0)
    r = env.poll()
    assert r["unseen_alerts"] == 1 and len(env.unseen) == 2
    detail = env.rows("SELECT detail FROM run_log ORDER BY ts DESC")[0]["detail"]
    assert f"{ASIN} offers 1->2 from 3700.00" in detail


def test_heartbeat_skips_new_unit_listings_and_shows_the_marker(tmp_path):
    conn = _hb_conn(tmp_path)
    for s, ts in ((_line(n=3, price=3800.0), "2026-10-23T09:00:00+00:00"),
                  (_no_line(4582.47), "2026-10-23T11:00:00+00:00"),         # line gone
                  (_line(n=4, price=4149.0, shipping=None, asin="B0FTMWWS8Y"),
                   "2026-10-23T11:30:00+00:00"),
                  # new-unit pages: a stray ALL-scoped line and a marker
                  (_line(n=6, price=5999.0, asin="B0DSYJCY45"), "2026-10-23T11:30:00+00:00"),
                  (_no_line(7110.0, asin="B0DSXZ4FZT"), "2026-10-23T11:30:00+00:00")):
        s.ts = ts
        store.insert_offer_summary(conn, s)
    lines = run._heartbeat_message(conn, NOW).split("\n")
    assert lines[2] == "Ofertas: 256GB 1 (menor R$ 4.582,47) · 512GB 4 (menor R$ 4.149,00)"
