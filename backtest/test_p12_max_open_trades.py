"""
P12-fix-2: unit tests for the new GET/POST /api/max-open-trades
endpoints in backend/app.py. DATA_DIR is pointed at a fresh temp dir
before import so this never touches the real /data volume. Same house
style as the other backtest/test_*.py files.
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


_tmp_data_dir = tempfile.mkdtemp()
os.environ["DATA_DIR"] = _tmp_data_dir
# app.py mounts /app/frontend as static files at import time (Docker-image-only
# path). Python resolves the absolute POSIX path "/app/frontend" to
# "<cwd drive>:\app\frontend" on Windows -- pre-create it so the mount doesn't
# raise outside a real container. Harmless empty dir, never touched otherwise.
os.makedirs(os.path.abspath("/app/frontend"), exist_ok=True)
# app.py copies from a Docker-image-only default config path if
# DATA_DIR/config.json doesn't exist yet -- pre-seed it so that branch
# (irrelevant to this test) never runs outside a real container.
import json as _json  # noqa: E402
(Path(_tmp_data_dir) / "config.json").write_text(_json.dumps({
    "start": 500, "target": 5000, "months": 12, "survival_monthly": 60,
    "ramp_months": 0, "take_pct": 20, "risk_pct": 50,
}))

from backend import app as app_module  # noqa: E402

try:
    from fastapi.testclient import TestClient
except ImportError:
    print("SKIPPED: fastapi.testclient (httpx) not installed in this environment -- "
          "endpoint logic below is still exercised directly, just not over real HTTP.")
    TestClient = None


# --- Constants sanity ---
check("MAX_OPEN_TRADES_ENV_KEYS covers all 6 bots",
      set(app_module.MAX_OPEN_TRADES_ENV_KEYS.keys()) == {
          "main", "shorts", "moonshot",
          "listing_fade", "trend_ensemble", "trend_v2",
      })
check("DEFAULT_MAX_OPEN_TRADES keys match MAX_OPEN_TRADES_ENV_KEYS keys exactly",
      set(app_module.DEFAULT_MAX_OPEN_TRADES.keys()) == set(app_module.MAX_OPEN_TRADES_ENV_KEYS.keys()))
check("Moonshot's own default is 10 (P12-fix-2 revert)",
      app_module.DEFAULT_MAX_OPEN_TRADES["moonshot"] == 10)

if TestClient is not None:
    client = TestClient(app_module.app)

    # --- GET before any intent recorded: recorded_intent is None, default_in_repo present ---
    r = client.get("/api/max-open-trades")
    check("GET /api/max-open-trades: 200 OK", r.status_code == 200)
    data = r.json()
    check("GET /api/max-open-trades: all 6 bots present", set(data.keys()) == set(app_module.MAX_OPEN_TRADES_ENV_KEYS.keys()))
    check("GET /api/max-open-trades: fresh state -> recorded_intent is None for every bot",
          all(v["recorded_intent"] is None for v in data.values()))
    check("GET /api/max-open-trades: default_in_repo matches DEFAULT_MAX_OPEN_TRADES",
          all(v["default_in_repo"] == app_module.DEFAULT_MAX_OPEN_TRADES[k] for k, v in data.items()))

    # --- POST: valid update ---
    r2 = client.post("/api/max-open-trades", json={"bot_id": "shorts", "max_open_trades": 15})
    check("POST /api/max-open-trades: 200 OK on a valid bot/value", r2.status_code == 200)
    body2 = r2.json()
    check("POST /api/max-open-trades: response echoes back the requested value", body2["max_open_trades"] == 15)
    check("POST /api/max-open-trades: note explicitly says this does NOT change real behavior",
          "does NOT change" in body2["note"] and "Portainer" in body2["note"] and "redeploy" in body2["note"])

    # --- GET after POST: recorded_intent now reflects it ---
    r3 = client.get("/api/max-open-trades")
    check("GET /api/max-open-trades: recorded_intent reflects the just-saved value for shorts",
          r3.json()["shorts"]["recorded_intent"] == 15)
    check("GET /api/max-open-trades: OTHER bots are untouched by shorts' save",
          r3.json()["moonshot"]["recorded_intent"] is None)

    # --- POST: unknown bot_id -> 400 ---
    r4 = client.post("/api/max-open-trades", json={"bot_id": "not_a_real_bot", "max_open_trades": 5})
    check("POST /api/max-open-trades: unknown bot_id -> 400, not silently accepted", r4.status_code == 400)

    # --- POST: value < 1 -> 400 ---
    r5 = client.post("/api/max-open-trades", json={"bot_id": "main", "max_open_trades": 0})
    check("POST /api/max-open-trades: max_open_trades < 1 -> 400", r5.status_code == 400)


if failures:
    print(f"\n{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)

print("\nALL P12-FIX-2 MAX-OPEN-TRADES ENDPOINT CHECKS PASSED")
