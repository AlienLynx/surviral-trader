"""
P17-C: control group (freqtrade-control-long / freqtrade-control-short).

Checks the properties that make a control a control: hard dry-run, no keys,
pinned image, strategy files identical to Freqtrade's own sample_strategy.py
except for the documented differences, config identical to the nearest live
analogue (Main / Shorts) except name/port, registered read-only, and kept out
of the payout split. No Freqtrade install needed (AST / text / hash only).
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8").replace("\r\n", "\n")


# ---------------------------------------------------------------- fixture
PRISTINE = REPO / "backtest/fixtures/sample_strategy_2026.8.py"
pristine = read(PRISTINE)
check("vendored template is the exact 2026.8 sample_strategy.py (sha256)",
      hashlib.sha256(pristine.encode()).hexdigest()
      == "18d48d64b955dc9bd1b43f59115c16f2002a881c62c2851ceca64569a6f8651c")

LONG_PATH = REPO / "freqtrade-control-long/user_data/strategies/ControlLongStrategy.py"
SHORT_PATH = REPO / "freqtrade-control-short/user_data/strategies/ControlShortStrategy.py"


def strip_header(text: str) -> str:
    idx = text.index("# pragma pylint: disable=missing-docstring")
    return text[idx:]


# ------------------------------------------------- long: only rename + header
long_body = strip_header(read(LONG_PATH))
check("control-long == template with ONLY the class renamed (header aside)",
      long_body.replace("class ControlLongStrategy(IStrategy):", "class SampleStrategy(IStrategy):") == pristine)


# -------------------- short: reverse the documented differences -> template
def revert_short(text: str) -> str:
    text = text.replace("class ControlShortStrategy(IStrategy):", "class SampleStrategy(IStrategy):")
    text = text.replace("    can_short: bool = True", "    can_short: bool = False")
    out, in_block = [], False
    for line in text.splitlines(keepends=True):
        if line.startswith("        # [P17-C DISABLED"):
            in_block = True
            continue
        if in_block:
            if line.startswith("        # "):
                out.append("        " + line[len("        # "):])
                if line.strip() == "# ] = 1":
                    in_block = False
                continue
            in_block = False
        out.append(line)
    return "".join(out)


short_body = strip_header(read(SHORT_PATH))
check("control-short == template after reverting ONLY the 4 documented differences",
      revert_short(short_body) == pristine)


def class_attr(path: Path, name: str):
    tree = ast.parse(read(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for stmt in node.body:
                if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                    target = stmt.targets[0] if isinstance(stmt, ast.Assign) else stmt.target
                    if isinstance(target, ast.Name) and target.id == name:
                        return ast.literal_eval(stmt.value)
    return None


def string_constants(path: Path) -> set:
    return {n.value for n in ast.walk(ast.parse(read(path))) if isinstance(n, ast.Constant) and isinstance(n.value, str)}


check("control-long can_short is False", class_attr(LONG_PATH, "can_short") is False)
check("control-short can_short is True", class_attr(SHORT_PATH, "can_short") is True)
check("control-long still writes long entry/exit and (ignored) short columns like the template",
      {"enter_long", "exit_long", "enter_short", "exit_short"} <= string_constants(LONG_PATH))
sc = string_constants(SHORT_PATH)
check("control-short writes ONLY short entry/exit columns (long leg disabled)",
      {"enter_short", "exit_short"} <= sc and "enter_long" not in sc and "exit_long" not in sc)
check("control-short defines no leverage() override (Freqtrade default 1.0 = 1x)",
      "def leverage" not in read(SHORT_PATH) and "def leverage" not in read(LONG_PATH))
check("control-long class name", "class ControlLongStrategy(IStrategy)" in read(LONG_PATH))
check("control-short class name", "class ControlShortStrategy(IStrategy)" in read(SHORT_PATH))
for path in (LONG_PATH, SHORT_PATH):
    head = read(path).split("# pragma pylint")[0]
    check(f"{path.name}: header documents the differences", "Differences from the template" in head)

# ------------------------------------------------------------------ configs
def load(p: str) -> dict:
    return json.loads(read(REPO / p))


main_cfg, shorts_cfg = load("freqtrade/user_data/config.json"), load("freqtrade-shorts/user_data/config.json")
cl, cs = load("freqtrade-control-long/user_data/config.json"), load("freqtrade-control-short/user_data/config.json")

for name, cfg in (("control-long", cl), ("control-short", cs)):
    check(f"{name}: dry_run is true in config.json", cfg["dry_run"] is True)
    check(f"{name}: no exchange key/secret in config", cfg["exchange"]["key"] == "" and cfg["exchange"]["secret"] == "")
    check(f"{name}: wallet $500, stake unlimited, max_open_trades 10",
          cfg["dry_run_wallet"] == 500 and cfg["stake_amount"] == "unlimited" and cfg["max_open_trades"] == 10)


def normalise(cfg: dict) -> dict:
    c = json.loads(json.dumps(cfg))
    c.pop("bot_name", None)
    c["api_server"]["CORS_origins"] = []
    return c


check("control-long config == Main config except bot_name / CORS", normalise(cl) == normalise(main_cfg))
check("control-short config == Shorts config except bot_name / CORS", normalise(cs) == normalise(shorts_cfg))
check("control-long is spot", cl["trading_mode"] == "spot")
check("control-short is futures + isolated", cs["trading_mode"] == "futures" and cs["margin_mode"] == "isolated")

# ------------------------------------------------------------------- compose
compose = yaml.safe_load(read(REPO / "docker-compose.yml"))
services = compose["services"]
for svc in ("freqtrade-control-long", "freqtrade-control-short"):
    env = services[svc].get("environment", [])
    env_names = {e.split("=", 1)[0] for e in env}
    check(f"{svc}: NO FREQTRADE__DRY_RUN env override", "FREQTRADE__DRY_RUN" not in env_names)
    check(f"{svc}: NO exchange key/secret env", not any(n.startswith("FREQTRADE__EXCHANGE") for n in env_names))
    check(f"{svc}: NO max_open_trades env override (config.json is authoritative)",
          "FREQTRADE__MAX_OPEN_TRADES" not in env_names)
    check(f"{svc}: no LLM / second-opinion keys passed", not any("API_KEY" in n and "FREQTRADE" not in n for n in env_names))
    check(f"{svc}: own named db volume",
          services[svc]["volumes"] == [f"{svc}-db:/freqtrade/user_data/db"] and f"{svc}-db" in compose["volumes"])
    cmd = " ".join(str(services[svc]["command"]).split())
    check(f"{svc}: command uses its own strategy + persistent logfile",
          "--strategy Control" in cmd and "--logfile /freqtrade/user_data/db/freqtrade.log" in cmd)

host_ports = [p.split(":")[0] for s in services.values() for p in s.get("ports", [])]
check("all host ports in docker-compose.yml are unique", len(host_ports) == len(set(host_ports)))
check("control ports are 8180 / 8190",
      services["freqtrade-control-long"]["ports"] == ["8180:8080"] and services["freqtrade-control-short"]["ports"] == ["8190:8080"])

for d in ("freqtrade-control-long", "freqtrade-control-short"):
    dockerfile = read(REPO / d / "Dockerfile")
    check(f"{d}: image pinned to freqtradeorg/freqtrade:2026.8 (not :stable)",
          re.search(r"^FROM freqtradeorg/freqtrade:2026\.8$", dockerfile, re.M) is not None and ":stable" not in re.sub(r"#.*", "", dockerfile))

# ---------------------------------------------------------------- registration
from backend.engine import freqtrade_client, payout_state  # noqa: E402

check("control_long / control_short registered in freqtrade_client.BOTS",
      "control_long" in freqtrade_client.BOTS and "control_short" in freqtrade_client.BOTS)
check("BOTS urls point at the control services on the internal port",
      freqtrade_client.BOTS["control_long"]["url"] == "http://freqtrade-control-long:8080"
      and freqtrade_client.BOTS["control_short"]["url"] == "http://freqtrade-control-short:8080")
check("control bots are NOT in payout_state.SPLIT_BOTS",
      not {"control_long", "control_short"} & set(payout_state.SPLIT_BOTS))
check("freqtrade_client stays read-only (no POST except the login token call)",
      len(re.findall(r"requests\.(post|put|delete|patch)", read(REPO / "backend/engine/freqtrade_client.py"))) == 1)

if failures:
    print(f"\n{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("\nALL P17-C CONTROL GROUP CHECKS PASSED")
