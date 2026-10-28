"""
One catalog of everything the operator may want to download, and the download itself. The bot logs / trades / aggregates are served by the export centre (`/api/export/*`, which
the Downloads page also shows); this module adds every other file the system writes: accounting and portfolio files, the signal ledger and audit, the Kronos files, the live
snapshot and reference of the replay, and the research runs (calibration, replay tests, Kronos evaluations) with their logs.

Safety: only fixed directories are listed (the data dir, the shared allocator dir, the Kronos dir, the root of the replay dir), only plain files with an allowed extension, names
that match a strict pattern, never a dot-file, never anything with a secret-looking name (`.env`, credentials). A path is resolved and must stay inside its directory.
"""
from __future__ import annotations

import io
import json
import os
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,120}$")
ALLOWED_EXT = (".json", ".jsonl", ".md", ".log", ".txt", ".csv")
DENY = re.compile(r"(\.env|secret|credential|password|token|\.key$|\.pem$|\.tmp$|\.lock$)", re.I)
GROUPS = ("Portfolio & accounting", "Portfolio brain & allocator", "Signals & Kronos", "Training & calibration", "Replay tests")


def sources(data_dir: Path | None = None) -> dict[str, Path]:
    return {"data": Path(data_dir or os.environ.get("DATA_DIR", "/data")), "allocator": Path(os.environ.get("ALLOCATOR_DIR") or "/allocator"),
            "kronos": Path(os.environ.get("KRONOS_DIR") or "/kronos"), "replay": Path(os.environ.get("REPLAY_DIR", "/data/replay"))}


def group_of(source: str, name: str) -> str:
    n = name.lower()
    if source == "replay":
        return "Training & calibration"
    if n.startswith(("kronos", "signals", "announcements", "blocked")):
        return "Signals & Kronos"
    if n.startswith(("portfolio_brain", "allocat", "weights", "context_", "market_context")):
        return "Portfolio brain & allocator"
    return "Portfolio & accounting"


def describe(name: str) -> str:
    n = name.lower()
    table = (("balance_history", "daily balance snapshot of every bot"), ("capital", "capital ledger (deposits / withdrawals per bot)"), ("payout_state", "payout baseline and history"),
             ("decisions.log", "accounting cycle decisions"), ("config.json", "goal and payout settings"), ("scorecard", "bot scorecard"), ("runtime_settings", "dashboard switches"),
             ("portfolio_brain_report", "daily portfolio-brain report (readable)"), ("portfolio_brain_snapshots", "portfolio-brain snapshots, one per day"), ("portfolio_brain_latest", "latest portfolio-brain snapshot"),
             ("allocations", "every allocator decision (weights, rules fired)"), ("weights", "weights the bots read"), ("allocator_state", "allocator state"), ("allocator_latest", "latest allocator decision"),
             ("context_snapshots", "hourly market-context snapshots"), ("context_latest", "latest market-context snapshot"), ("signals_audit", "signal audit report"), ("signals_forward", "forward returns of the signal ledger"),
             ("signals_state", "signal ledger state"), ("signals", "signal ledger (every LLM / Kronos decision)"), ("kronos_signals", "live Kronos signals (append-only)"), ("kronos_health", "last Kronos cycle (time, pairs, model)"),
             ("announcements", "every exchange delisting notice read so far (audit ledger)"), ("blocked_pairs", "coins the bots may not open new trades on right now (delisting notices)"), ("kronos_rank", "per-bot Kronos rank the bots read"), ("live_snapshot", "live decisions / trades / restarts used by the calibration"), ("live_reference", "live reference summary used by the replay"))
    for key, text in table:
        if n.startswith(key):
            return text
    return ""


def _ok_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in ALLOWED_EXT and NAME_RE.match(path.name) is not None and not DENY.search(path.name)


def _entry(source: str, path: Path) -> dict:
    st = path.stat()
    return {"group": group_of(source, path.name), "label": path.name, "description": describe(path.name), "kind": "file", "size": st.st_size, "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "url": f"/api/downloads/file/{source}/{path.name}"}


def list_files(data_dir: Path | None = None) -> list[dict]:
    out = []
    for source, base in sources(data_dir).items():
        if not base.is_dir():
            continue
        for p in sorted(base.iterdir()):
            if _ok_file(p):
                if source == "replay" and p.name not in ("live_snapshot.json", "live_reference.json"):
                    continue
                if source == "allocator" and p.name == "kronos_rank.json":
                    pass
                out.append(_entry(source, p))
    # the same file name can sit in two places (the allocator volume has copies): keep both, the source is in the URL, say where in the label
    seen = {}
    for e in out:
        seen.setdefault(e["label"], []).append(e)
    for label, es in seen.items():
        if len(es) > 1:
            for e in es:
                e["label"] = f"{label} ({e['url'].split('/')[4]})"
    return out


def resolve(source: str, name: str, data_dir: Path | None = None) -> Path | None:
    base = sources(data_dir).get(source)
    if base is None or not NAME_RE.match(name or ""):
        return None
    p = (base / name)
    try:
        if not _ok_file(p) or p.resolve().parent != base.resolve():
            return None
    except OSError:
        return None
    if source == "replay" and name not in ("live_snapshot.json", "live_reference.json"):
        return None
    return p


def research_entries(runs: list[dict], kronos_dir: Path, eval_ids: list[str]) -> list[dict]:
    """Calibration / replay runs (ZIP + log) and Kronos evaluations (ZIP) and the Kronos signal files (ZIP)."""
    out = []
    for r in runs:
        typ = r.get("type", "replay")
        group = "Training & calibration" if typ == "calibration" else "Replay tests"
        kind = {"calibration": "calibration", "exit_variants": "exit variants", "listing_fade_study": "Listing Fade study", "carry_study": "funding carry study", "delist_study": "delisting short study", "momentum_study": "momentum study", "fade_exit_study": "Fade exit study", "lgbm_study": "LightGBM study", "lgbm_hourly_study": "LightGBM v2 study", "lgbm_grid_study": "Ranker robustness grid", "lgbm_premium_study": "Premium features study", "maker_study": "Maker-fill cost model"}.get(typ, "replay")
        out.append({"group": group, "label": f"{kind} {r['run_id']}", "description": ("report + trades + decisions + log" + (f", ${r['usd']}" if r.get("usd") else "")) if r.get("has_report") else "no report (failed or still running)",
                    "kind": "zip", "url": f"/api/replay/runs/{r['run_id']}/download", "log_url": f"/api/replay/runs/{r['run_id']}/log", "modified": None, "size": None})
    for eid in eval_ids:
        out.append({"group": "Training & calibration", "label": f"Kronos evaluation {eid}", "description": "signal quality, engine trades, equity, decisions, report", "kind": "zip", "url": f"/api/replay/kronos/eval/{eid}/download", "modified": None, "size": None})
    if (kronos_dir / "job.json").is_file():
        out.append({"group": "Training & calibration", "label": "Kronos historical signals (job, progress, signals)", "description": "everything the kronos-replay worker wrote", "kind": "zip", "url": "/api/replay/kronos/files.zip", "modified": None, "size": None})
    return out


def kronos_files_zip(kronos_dir: Path) -> bytes | None:
    if not (kronos_dir / "job.json").is_file():
        return None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(kronos_dir.iterdir()):
            if f.is_file() and (f.name in ("job.json", "model.json", "bench_request.json", "bench_result.json") or f.name.startswith(("progress_", "signals_"))) and NAME_RE.match(f.name):
                z.write(f, f.name)
    return buf.getvalue()


def catalog(runs: list[dict], eval_ids: list[str], data_dir: Path | None = None) -> dict:
    srcs = sources(data_dir)
    files = list_files(data_dir) + research_entries(runs, srcs["replay"] / "kronos", eval_ids)
    by_group = {g: [e for e in files if e["group"] == g] for g in GROUPS}
    return {"groups": [{"name": g, "items": by_group[g]} for g in GROUPS if by_group[g]], "n": len(files),
            "note": "Bot logs, trades and the bot ZIP are in the export centre block above; every file here is read-only and secrets are never listed."}


def run_log(run_dir: Path) -> str | None:
    f = run_dir / "log.txt"
    try:
        return f.read_text(encoding="utf-8", errors="replace") if f.is_file() else None
    except OSError:
        return None
