"""Per-batch delivery accounting (notify._deliver) and the watch workflow's
guarantees — review round 2, C0/C1/C2/C8. No network."""
from pathlib import Path

import yaml

from src import notify
from src.models import Observation
from src.triggers import Alert

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "poll.yml"


def _alert(i):
    o = Observation(source_id="amazon_seminovos", seller="S" * 60, venue="amazon",
                    listing_id=f"L{i}", url="https://www.amazon.com.br/dp/" + "X" * 150,
                    storage_gb=256, color="preto", grade_raw="Excelente", grade_rank=4,
                    list_price=3700.0 + i, in_stock=True)
    return Alert("aggressive", "🟠", o)


def test_each_batch_counts_on_its_own(monkeypatch):
    alerts = [_alert(i) for i in range(3)]
    batches = notify._pack([notify.format_alert(a) for a in alerts])
    assert len(batches) >= 2                         # long blocks: several messages
    first = batches[0][1]
    sent = {"n": 0}

    def wa(msg):                                     # batch 1 only via WhatsApp
        sent["n"] += 1
        return sent["n"] == 1

    def ntfy(msg, title=None):                       # batch 2 only via ntfy
        return sent["n"] == 2
    monkeypatch.setattr(notify, "send_whatsapp", wa)
    monkeypatch.setattr(notify, "send_ntfy", ntfy)
    rep = notify.notify(alerts)
    delivered_batches = [idxs for _, idxs in batches[:2]]
    assert sorted(rep["delivered"]) == sorted(sum(delivered_batches, []))
    assert set(first) <= set(rep["delivered"])
    assert rep["wa_ok"] is False and rep["ntfy_ok"] is False   # per-channel AND, for logs


def test_a_missing_secret_is_a_failed_send_not_a_crash(monkeypatch):
    def ntfy_without_topic(msg, title=None):
        raise RuntimeError("Missing required config 'NTFY_TOPIC'")
    monkeypatch.setattr(notify, "send_whatsapp", lambda msg: True)
    monkeypatch.setattr(notify, "send_ntfy", ntfy_without_topic)
    rep = notify.notify([_alert(0)])
    assert rep["delivered"] == [0] and rep["ntfy_ok"] is False
    rep = notify.notify_coupons(["🎟️ x"])
    assert rep["delivered"] == [0]


def test_nothing_delivered_when_both_channels_fail(monkeypatch):
    monkeypatch.setattr(notify, "send_whatsapp", lambda msg: False)
    monkeypatch.setattr(notify, "send_ntfy", lambda msg, title=None: False)
    assert notify.notify([_alert(0), _alert(1)])["delivered"] == []


def test_watch_workflow_guarantees():
    text = WORKFLOW.read_text(encoding="utf-8")
    wf = yaml.safe_load(text)
    job = wf["jobs"]["watch"]
    checkout = job["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@")
    # C0: the branch tip, not the queued event's SHA; full history for rebase
    assert checkout["with"] == {"ref": "${{ github.ref_name }}", "fetch-depth": 0}
    # C1: python is the step's process, so cancel/timeout signals reach it
    watch = next(s for s in job["steps"] if s.get("name", "").startswith("Watch"))
    assert watch["run"].startswith("exec python -m src.run --loop")
    assert "--git-sync" in watch["run"]
    # C2: only scheduled/watch runs queue behind the watcher
    assert "concurrency" not in wf
    group = job["concurrency"]["group"]
    assert "s25-watch" in group and "s25-manual-" in group and "github.run_id" in group
    assert job["concurrency"]["cancel-in-progress"] is False


def test_ci_installs_no_browser_stack():   # Y2: only disabled sources use Playwright
    reqs = (WORKFLOW.parents[2] / "requirements.txt").read_text(encoding="utf-8")
    assert "playwright" not in reqs.lower()
    assert "playwright" not in WORKFLOW.read_text(encoding="utf-8").lower().replace(
        "no playwright", "")
