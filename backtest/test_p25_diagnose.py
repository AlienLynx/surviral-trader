"""
"Why is it not trading?" -- the per-bot diagnosis. Offline: synthetic bot logs in a temp BOTS_DIR, a stubbed bot REST API, a temp allocator directory.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
failures: list[str] = []


def check(label, cond):
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


tmp = Path(tempfile.mkdtemp())
os.environ["BOTS_DIR"] = str(tmp)
os.environ["DATA_DIR"] = str(tmp)
alloc = tmp / "alloc"
alloc.mkdir()
for b in ("main", "shorts", "listing_fade", "moonshot"):
    (tmp / b).mkdir()

from backend import diagnose  # noqa: E402
from backend.engine import freqtrade_client as ftc  # noqa: E402

now = datetime.now(timezone.utc)
ts = lambda m: (now - timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M:%S,000")  # noqa: E731


def log(bot, rows):
    (tmp / bot / "freqtrade.log").write_text("\n".join(rows) + "\n", encoding="utf-8")


log("main", [f"{ts(i)} - DeepSeekStrategy - INFO - DeepSeek said hold for ETH/USDT -- llm veto of kronos buy: ETH below 4h SMA" for i in range(1, 30)]
    + [f"{ts(40)} - DeepSeekStrategy - INFO - DeepSeek said hold for SOL/USDT -- kronos: no fresh signal for this pair (fail-safe: hold)"]
    + [f"{ts(5)} - freqtrade.wallets - INFO - Stake amount for pair BTC/USDT:USDT is too small (4.95 < 89.0), adjusting to 89.0" for _ in range(25)]
    + [f"{ts(2000)} - DeepSeekStrategy - INFO - DeepSeek said buy for OLD/USDT -- old line outside the window"])
log("shorts", [f"{ts(10)} - DeepSeekShortsStrategy - INFO - DeepSeek said sell_short for NEAR/USDT:USDT -- crowded long" for _ in range(8)]
    + [f"{ts(11)} - DeepSeekShortsStrategy - INFO - confirm_trade_entry: rejecting RLC/USDT:USDT -- funding -0.1463%/8h < -0.0500% threshold" for _ in range(12)]
    + [f"{ts(12)} - DeepSeekShortsStrategy - INFO - confirm_trade_entry: rejecting STG/USDT:USDT -- announcement guard: STG: perp delisting: Delisting of STGUSDT Perpetual Contract" for _ in range(3)]
    + [f"{ts(13)} - freqtrade.rpc.rpc_manager - INFO - Sending rpc message: {{'trade_id': 5, 'type': entry_fill, 'pair': 'ETH/USDT:USDT', 'amount': 1.0}}"])
log("listing_fade", [f"{ts(20)} - ListingFadeStrategy - INFO - ListingFade: rule-based short signal for LONGXIA/USDT:USDT (-86.8% from listing high, close < EMA20)" for _ in range(3)])
log("moonshot", [])

STATE = {"main": "running", "shorts": "paused", "listing_fade": "running", "moonshot": "running"}
STAKES = {"main": [5.0, 5.2, 4.9], "shorts": [25.0]}


def fake_get(bot, path):
    if path.endswith("show_config"):
        return {"state": STATE[bot], "max_open_trades": 3}
    if path.endswith("status"):
        return [{"pair": "X"}] * (3 if bot == "listing_fade" else 1)
    if "trades" in path:
        return {"trades": [{"stake_amount": s} for s in STAKES.get(bot, [])]}
    return None


ftc._authed_get = fake_get
(alloc / "weights.json").write_text(json.dumps({"mode": "enforce", "updated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "weights": {"main": {"weight": 0.1}, "shorts": {"weight": 0.0}, "listing_fade": {"weight": 0.25}, "moonshot": {"weight": 1.0}}}))

m = diagnose.why("main", hours=24, allocator_dir=alloc)
txt = " | ".join(f["text"] for f in m["findings"])
check("MAIN: decisions are counted inside the window only (the 2000-minute-old line is ignored)", m["decisions"] == {"hold": 30} and "buy" not in m["decisions"])
check("MAIN: every Kronos proposal vetoed by the LLM is counted and explained, with the switch that follows Kronos instead", m["vetoes"] == {"buy": 29} and "vetoed 29" in txt and "DECISION_SOURCE=kronos" in txt)
check("MAIN: a hold with no fresh Kronos signal is counted separately", m["no_signal"] == 1)
check("MAIN: entries dropped for a stake below the exchange minimum are counted with the numbers", m["too_small"]["BTC/USDT:USDT"]["count"] == 25 and "below the exchange's minimum order" in txt and "$4.95" in txt)
check("MAIN: the allocator weight 0.1 is shown with the recent median stake", "weight is 0.1" in txt and "10%" in txt and "median $5.00" in txt)
s = diagnose.why("shorts", hours=24, allocator_dir=alloc)
st = " | ".join(f["text"] for f in s["findings"])
check("SHORTS: a paused bot is the first thing said (bad), with what to look at", s["findings"][0]["severity"] == "bad" and "PAUSED" in s["findings"][0]["text"])
check("SHORTS: the allocator weight 0 is named as the pause", "weight is 0" in st)
check("SHORTS: refusals are grouped by cause (funding rule, announcement guard) with counts", s["rejections"] == {"funding rule (the short would pay funding)": 12, "announcement guard (delisting notice)": 3} and "12 entries were refused by the funding rule" in st)
check("SHORTS: filled entries are counted", s["entries_filled"] == 1)
f = diagnose.why("listing_fade", hours=24, allocator_dir=alloc)
ft = " | ".join(x["text"] for x in f["findings"])
check("RULE BOT: no LLM decisions, the rule signals are counted, full slots are reported", "3 rule signals" in ft and "All 3 slots are in use" in ft)
mo = diagnose.why("moonshot", hours=24, allocator_dir=alloc)
check("EMPTY LOG: a bot with no readable log lines says so", any("No log lines" in x["text"] for x in mo["findings"]))
(alloc / "weights.json").write_text(json.dumps({"mode": "enforce", "updated_at": (now - timedelta(hours=40)).strftime("%Y-%m-%dT%H:%M:%SZ"), "weights": {"main": {"weight": 0.0}}}))
old = diagnose.why("main", hours=24, allocator_dir=alloc)
check("STALE WEIGHTS: a weights file older than 36 h is reported as ignored by the bots, not as a pause", any("older than 36 h" in x["text"] for x in old["findings"]) and not any("paused by the allocator" in x["text"] for x in old["findings"]))
check("UNKNOWN BOT: None", diagnose.why("zzz", 24, alloc) is None)

# the HTTP route and the page
os.makedirs(os.path.abspath("/app/frontend"), exist_ok=True)
(tmp / "config.json").write_text(json.dumps({"start": 500, "target": 5000, "months": 12, "survival_monthly": 60, "ramp_months": 0, "take_pct": 20, "risk_pct": 50}))
os.environ["ALLOCATOR_DIR"] = str(alloc)
from fastapi.testclient import TestClient  # noqa: E402
from backend import app as appmod  # noqa: E402

c = TestClient(appmod.app)
r = c.get("/api/bots/shorts/why?hours=24")
check("API: /api/bots/{id}/why returns the findings; an unknown bot is 404", r.status_code == 200 and r.json()["findings"] and c.get("/api/bots/zzz/why").status_code == 404)
html = (REPO / "frontend" / "index.html").read_text(encoding="utf-8")
check("UI: every bot page has the 'Why is it not trading?' card, loaded with the page", 'id="botWhyCard"' in html and "loadBotWhy" in html and "/why?hours=24" in html)

print("\nALL P25 DIAGNOSE CHECKS PASSED" if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
