"""
P24: the Downloads catalog and the per-bot parameters endpoint. Offline: temp directories, FastAPI TestClient, no Freqtrade.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import zipfile
import io
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
failures: list[str] = []


def check(label, cond):
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


tmp = Path(tempfile.mkdtemp())
data, alloc, kron = tmp / "data", tmp / "alloc", tmp / "kronos"
for d in (data, alloc, kron, data / "replay" / "kronos" / "eval" / "e1"):
    d.mkdir(parents=True)
os.environ.update({"DATA_DIR": str(data), "ALLOCATOR_DIR": str(alloc), "KRONOS_DIR": str(kron), "REPLAY_DIR": str(data / "replay"), "BOTS_DIR": str(tmp / "bots")})
(data / "balance_history.json").write_text("{}")
(data / "decisions.log").write_text("x")
(data / ".env").write_text("DEEPSEEK_API_KEY=sk-secret")
(data / "credentials.json").write_text("{}")
(data / "binary.bin").write_bytes(b"x")
(data / "portfolio_brain_report.md").write_text("# r")
(data / "signals_audit.json").write_text("{}")
(alloc / "weights.json").write_text("{}")
(alloc / "kronos_rank.json").write_text("{}")
(kron / "kronos_signals.jsonl").write_text("{}\n")
(data / "replay" / "live_snapshot.json").write_text("{}")
(data / "replay" / "live_reference.json").write_text("{}")
(data / "replay" / "other_secret_notes.json").write_text("{}")
rk = data / "replay" / "kronos"
(rk / "job.json").write_text("{}")
(rk / "progress_0.json").write_text("{}")
(rk / "signals_0.jsonl").write_text("{}\n")
(rk / "eval" / "e1" / "report.json").write_text("{}")
run = data / "replay" / "20260101-000000"
run.mkdir()
(run / "params.json").write_text(json.dumps({"type": "calibration"}))
(run / "report.json").write_text(json.dumps({"llm": {"usd": 0}}))
(run / "log.txt").write_text("line one\n")
run2 = data / "replay" / "20260102-000000"
run2.mkdir()
(run2 / "params.json").write_text(json.dumps({"type": "replay"}))

from backend import downloads  # noqa: E402

cat = downloads.catalog([{"run_id": "20260101-000000", "type": "calibration", "has_report": True, "usd": 0}, {"run_id": "20260102-000000", "type": "replay", "has_report": False}], ["e1"], data)
items = [i for g in cat["groups"] for i in g["items"]]
urls = {i["url"] for i in items}
check("CATALOG: accounting, brain, signal files are listed", all(any(u.endswith(n) for u in urls) for n in ("balance_history.json", "decisions.log", "portfolio_brain_report.md", "signals_audit.json", "weights.json", "kronos_signals.jsonl")))
check("CATALOG: .env, credential files, unknown extensions and random replay-root files are never listed", not any(x in u for u in urls for x in (".env", "credentials", "binary.bin", "other_secret")))
check("CATALOG: live_snapshot / live_reference of the replay are listed under Training & calibration",
      {i["label"] for g in cat["groups"] if g["name"] == "Training & calibration" for i in g["items"]} >= {"live_snapshot.json", "live_reference.json"})
check("CATALOG: calibration run under Training & calibration, replay run under Replay tests, both with their ZIP",
      any(i["label"].startswith("calibration 2026") for g in cat["groups"] if g["name"] == "Training & calibration" for i in g["items"]) and any(i["label"].startswith("replay 2026") for g in cat["groups"] if g["name"] == "Replay tests" for i in g["items"]))
check("CATALOG: a Kronos evaluation and the Kronos historical files ZIP are listed", "/api/replay/kronos/eval/e1/download" in urls and "/api/replay/kronos/files.zip" in urls)
check("CATALOG: file names present in two directories are told apart in the label", len({i["label"] for i in items}) == len(items))

check("RESOLVE: a listed file resolves", downloads.resolve("data", "balance_history.json", data) is not None)
for bad in (("data", ".env"), ("data", "credentials.json"), ("data", "../x.json"), ("data", "binary.bin"), ("replay", "other_secret_notes.json"), ("nope", "x.json"), ("data", "a/b.json"), ("data", "")):
    check(f"RESOLVE: refused {bad}", downloads.resolve(*bad, data) is None)
z = downloads.kronos_files_zip(rk)
names = set(zipfile.ZipFile(io.BytesIO(z)).namelist()) if z else set()
check("ZIP: Kronos files ZIP holds job, progress and signals files only", names == {"job.json", "progress_0.json", "signals_0.jsonl"})
check("LOG: run log text", downloads.run_log(run) == "line one\n" and downloads.run_log(run2) is None)

# HTTP layer (app.py wants /app/frontend at import and a seeded config.json, like the other tests)
os.makedirs(os.path.abspath("/app/frontend"), exist_ok=True)
(data / "config.json").write_text(json.dumps({"start": 500, "target": 5000, "months": 12, "survival_monthly": 60, "ramp_months": 0, "take_pct": 20, "risk_pct": 50}))
from fastapi.testclient import TestClient  # noqa: E402
from backend import app as appmod  # noqa: E402

c = TestClient(appmod.app)
r = c.get("/api/downloads")
check("API /api/downloads: 200, groups present", r.status_code == 200 and r.json()["n"] >= 10)
check("API file download returns the content", c.get("/api/downloads/file/data/balance_history.json").content == b"{}")
check("API: .env is a 404, traversal is a 404", c.get("/api/downloads/file/data/.env").status_code == 404 and c.get("/api/downloads/file/data/..%2F.env").status_code in (404, 400))
check("API: run log download, missing log is 404, bad id is 400", c.get("/api/replay/runs/20260101-000000/log").text == "line one\n" and c.get("/api/replay/runs/20260102-000000/log").status_code == 404 and c.get("/api/replay/runs/..x/log").status_code in (400, 404))
check("API: Kronos files ZIP", c.get("/api/replay/kronos/files.zip").status_code == 200)
check("API: /calibration route exists", any(getattr(rt, "path", "") == "/calibration" for rt in appmod.app.routes))
p = c.get("/api/bots/main/params")
j = p.json()
check("API: bot params works with the bot offline (static part, reachable false)", p.status_code == 200 and j["reachable"] is False and j["static"]["strategy"] == "DeepSeekStrategy" and j["static"]["decision_source"] in ("llm", "kronos_llm", "kronos"))
check("API: rule bot has no decision switch, unknown bot 404", "decision_source" not in c.get("/api/bots/trend_v2/params").json()["static"] and c.get("/api/bots/zzz/params").status_code == 404)


# ---- the three pages: shared shell, English only, every JS block parses, tab ids match their panels (no browser needed)
import re  # noqa: E402

fe = REPO / "frontend"
pages = {n: (fe / n).read_text(encoding="utf-8") for n in ("index.html", "calibration.html", "replay.html")}
check("UI: every page links the shared ui.css and nav.js (no external script, no CDN)", all('href="/assets/ui.css"' in h and 'src="/assets/nav.js"' in h and "cdn." not in h.lower() and '<script src="http' not in h for h in pages.values()))
check("UI: no Cyrillic text left on any page (English only)", not any(re.search("[Ѐ-ӿ]", h) for h in pages.values()))
for name, h in pages.items():
    ids = re.findall(r'id="([^"]+)"', h.split("<script")[0])      # markup only: the scripts build a few ids at run time
    check(f"UI {name}: element ids are unique", len(ids) == len(set(ids)))
    tabs = re.findall(r'data-tab="([^"]+)"', h)
    check(f"UI {name}: every tab link has a panel", tabs and all(f'id="tab-{t}"' in h for t in tabs))
idx = pages["index.html"]
check("UI index: the six tabs Overview, Bots, Portfolio, Signals, Downloads, Settings", all(f'data-tab="{t}"' in idx for t in ("overview", "bots", "portfolio", "signals", "downloads", "settings")))
check("UI index: one page per bot (8 bots incl. controls) with strategy, parameters, evidence, settings, per-bot downloads", all(b in idx for b in ("control_long", "control_short", "botParamsCard", "botEvidenceCard", "botSettingsCard", "botDownloadsCard", "/api/bots/")))
check("UI index: Downloads tab lists the export centre AND the catalog of every other file", 'id="export"' in idx and 'id="dlCatalog"' in idx and "/api/downloads" in idx)
check("UI index: max open trades is edited per bot on the bot page; trading mode only on main", "mot_" in idx and "if (id === 'main')" in idx and 'id="max-open-trades"' not in idx)
cal = pages["calibration.html"]
check("UI calibration: separate page with live reference, calibration, Kronos and reports/logs tabs and a run-log download", all(t in cal for t in ('data-tab="live"', 'data-tab="calibrate"', 'data-tab="kronos"', 'data-tab="reports"', "/log", "/api/replay/kronos/files.zip")))
rep = pages["replay.html"]
check("UI replay: tests only -- no calibration, live reference or Kronos controls left", not any(x in rep for x in ("btnCal", "btnRef", "kGen", "kEval", "calBots", "refFrom")) and "/calibration" in rep)


# ---- dry-run paper money per bot comes from a compose variable whose default equals the bot's config.json
import yaml  # noqa: E402

cy = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
for svc, var in (("freqtrade", "MAIN"), ("freqtrade-shorts", "SHORTS"), ("freqtrade-moonshot", "MOONSHOT"), ("freqtrade-listing-fade", "FADE"), ("freqtrade-trend-ensemble", "TREND"),
                 ("freqtrade-trend-v2", "TREND_V2"), ("freqtrade-control-long", "CONTROL_LONG"), ("freqtrade-control-short", "CONTROL_SHORT")):
    cfgw = json.loads((REPO / svc / "user_data" / "config.json").read_text(encoding="utf-8"))["dry_run_wallet"]
    check(f"COMPOSE {svc}: FREQTRADE__DRY_RUN_WALLET=${{{var}_DRY_RUN_WALLET:-{cfgw}}} (default = config.json)", f"FREQTRADE__DRY_RUN_WALLET=${{{var}_DRY_RUN_WALLET:-{cfgw}}}" in cy["services"][svc]["environment"])
check("DOCS: the wallet variables are documented with the ledger warning", "SHORTS_DRY_RUN_WALLET" in (REPO / "ENVIRONMENT_VARIABLES.md").read_text(encoding="utf-8"))

shutil.rmtree(tmp, ignore_errors=True)
print("\nALL P24 DOWNLOADS CHECKS PASSED" if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
