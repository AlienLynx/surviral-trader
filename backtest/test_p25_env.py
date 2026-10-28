"""
The Portainer import file (.env.example) lists every variable docker-compose.yml reads, with the same default, secrets empty; the docs cover the decision-source variants and the import file.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
failures: list[str] = []


def check(label, cond):
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
want = {}
for m in re.finditer(r"\$\{([A-Z][A-Za-z0-9_]*)(?::-([^}]*))?\}", compose):
    if m.group(1) != "VAR":
        want.setdefault(m.group(1), m.group(2) or "")
example = {}
for line in (REPO / ".env.example").read_text(encoding="utf-8").splitlines():
    if line.strip() and not line.lstrip().startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        example[k.strip()] = v.strip()
check("EXAMPLE: every variable the compose file reads is in .env.example", not [k for k in want if k not in example])
check("EXAMPLE: .env.example has no variable the compose file does not read (no stale names)", not [k for k in example if k not in want])
check("EXAMPLE: every default equals the compose default", not [k for k in want if example.get(k) != want[k]])
secret = [k for k in example if re.search(r"(API_KEY|API_SECRET|PASSWORD|JWT_SECRET)$", k)]
check("EXAMPLE: every key, secret, password and JWT secret is empty (nothing secret in git)", secret and all(example[k] == "" for k in secret))
for k in ("MAIN_DECISION_SOURCE", "SHORTS_DECISION_SOURCE", "MOONSHOT_DECISION_SOURCE", "MAIN_LLM_PROVIDER", "SHORTS_DRY_RUN_WALLET", "PORTFOLIO_BRAIN_MODE", "ALLOCATOR_EXCLUDED_BOTS", "KRONOS_MODE", "FREQTRADE_UI_HOST"):
    check(f"EXAMPLE: {k} is present", k in example)
check("EXAMPLE: the decision-source variants are explained in the file itself", "kronos_llm" in (REPO / ".env.example").read_text(encoding="utf-8") and "llm = the LLM alone" in (REPO / ".env.example").read_text(encoding="utf-8"))
doc = (REPO / "ENVIRONMENT_VARIABLES.md").read_text(encoding="utf-8")
check("DOCS: the three decision-source values are explained with when to use each and how to change them", all(x in doc for x in ("Choosing the decision source", "`kronos_llm` (default)", "`kronos` |", "Update the stack")))
check("DOCS: the import file and the allocator switches are documented", "Import file for Portainer" in doc and "ALLOCATOR_EXCLUDED_BOTS" in doc and "PORTFOLIO_BRAIN_MODE=shadow" in doc)
survival_env = re.search(r"\n  survival-agent:.*?\n  freqtrade:\n", compose, re.S).group(0)
check("COMPOSE: the allocator switches are passed to survival-agent", all(f"{k}=" in survival_env for k in ("ALLOCATOR_EXCLUDED_BOTS", "ALLOCATOR_PROBE", "ALLOCATOR_PROBE_DAYS", "ALLOCATOR_COIN_LIMIT", "MARKET_CONTEXT_MODE")))

print("\nALL P25 ENV CHECKS PASSED" if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
