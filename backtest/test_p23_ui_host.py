"""
CORS origins from FREQTRADE_UI_HOST: no host name in the repository, every bot builds its origin list at container start.
Pure checks (the shell script is run with `sh` when one exists); nothing here needs Docker.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
failures: list[str] = []


def check(label, cond):
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


BOTS = ["freqtrade", "freqtrade-shorts", "freqtrade-moonshot", "freqtrade-listing-fade", "freqtrade-trend-ensemble", "freqtrade-trend-v2", "freqtrade-control-long", "freqtrade-control-short"]
PORTS = [8099, 8110, 8120, 8160, 8170, 8180, 8190, 8200]
scripts = {b: (REPO / b / "entrypoint-cors.sh").read_bytes() for b in BOTS}
check("SCRIPT: the same entrypoint-cors.sh in all 8 bot directories (byte-identical, LF only, no CR)", len({hashlib.sha256(v).hexdigest() for v in scripts.values()}) == 1 and all(b"\r" not in v for v in scripts.values()))
cy = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
for b in BOTS:
    s = cy["services"][b]
    check(f"COMPOSE {b}: entrypoint is the script, FREQTRADE_UI_HOST is passed as ${{VAR:-localhost}}, the command (trade ...) is still there",
          s.get("entrypoint") == ["/freqtrade/entrypoint-cors.sh"] and "FREQTRADE_UI_HOST=${FREQTRADE_UI_HOST:-localhost}" in s["environment"] and str(s["command"]).strip().startswith("trade"))
    df = (REPO / b / "Dockerfile").read_text(encoding="utf-8")
    check(f"DOCKERFILE {b}: copies the script and makes it executable, the image pin is unchanged", "COPY entrypoint-cors.sh /freqtrade/entrypoint-cors.sh" in df and "chmod +x /freqtrade/entrypoint-cors.sh" in df
          and [ln for ln in df.splitlines() if ln.startswith("FROM ")] == ["FROM freqtradeorg/freqtrade:2026.8"])
    cfg = json.loads((REPO / b / "user_data" / "config.json").read_text(encoding="utf-8"))
    check(f"CONFIG {b}: CORS_origins is empty in the file (the script supplies it)", cfg["api_server"]["CORS_origins"] == [])
check("COMPOSE: the replay-only shorts_v2 config carries no host name either", json.loads((REPO / "freqtrade-shorts-v2/user_data/config.json").read_text(encoding="utf-8"))["api_server"]["CORS_origins"] == [])
ports_in_compose = sorted(int(p.split(":")[0]) for b in BOTS for p in cy["services"][b].get("ports", []))
check("PORTS: the 8 ports in the script are exactly the ports the bot services publish", ports_in_compose == PORTS and all(str(p) in scripts[BOTS[0]].decode() for p in PORTS))

sh = shutil.which("sh")
if sh:
    def run(host):
        src = scripts[BOTS[0]].decode().replace('exec freqtrade "$@" --config /tmp/cors_origins.json', 'cat /tmp/cors_origins.json; echo ARGS "$@"')
        tmp = Path(tempfile.mkdtemp()) / "s.sh"
        tmp.write_text(src.replace("/tmp/cors_origins.json", (tmp.parent / "o.json").as_posix()), encoding="utf-8", newline="\n")
        env = {**os.environ}
        env.pop("FREQTRADE_UI_HOST", None)
        if host is not None:
            env["FREQTRADE_UI_HOST"] = host
        out = subprocess.run([sh, str(tmp), "trade", "--config", "/x.json"], capture_output=True, text=True, env=env)
        lines = out.stdout.splitlines()
        return json.loads(lines[0]), out.stdout, out.stderr
    j, o, e = run("my-nas.local")
    check("RUN: with FREQTRADE_UI_HOST=my-nas.local the file holds http://my-nas.local:<port> for all 8 ports, as valid JSON under api_server.CORS_origins", j["api_server"]["CORS_origins"] == [f"http://my-nas.local:{p}" for p in PORTS])
    check("RUN: the original arguments are passed on unchanged (the extra --config comes last, so it wins)", "ARGS trade --config /x.json" in o or "ARGS trade --config /x.json".replace("/", "/") in o)
    j, o, e = run(None)
    check("RUN: unset -> localhost", j["api_server"]["CORS_origins"][0] == "http://localhost:8099")
    j, o, e = run('evil", "x": "y')
    check("RUN: a host with quotes / spaces (JSON injection) is refused with a message and replaced by localhost", j["api_server"]["CORS_origins"][0] == "http://localhost:8099" and "not allowed" in e)
    j, o, e = run("192.168.1.20")
    check("RUN: an IP address works", j["api_server"]["CORS_origins"][1] == "http://192.168.1.20:8110")
else:
    print("[SKIP] no sh on this machine: the script run checks were not executed")
src = scripts[BOTS[0]].decode()
check("SCRIPT: ends in `exec freqtrade \"$@\" --config ...` (a later --config wins in Freqtrade's merge) and says why the env var of Freqtrade is not used", 'exec freqtrade "$@" --config /tmp/cors_origins.json' in src and "lower-cased" in src)
out = subprocess.run(["git", "-C", str(REPO), "grep", "-n", "-i", "kateg" + "oriki"], capture_output=True, text=True).stdout
check("NO HOST NAME: nothing tracked in the working tree names the original deployment's host (git history is not rewritten)", out.strip() == "")

if failures:
    print(f"\n{len(failures)} FAILED:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nALL UI-HOST CHECKS PASSED")
