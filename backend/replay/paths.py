"""
The only way replay code is allowed to write: replay_path(run_id, name) / cache_path(name). Anything that resolves outside REPLAY_DIR raises.
`assert_not_live` is a second, independent guard used by the writers: it refuses the live files by name.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
LIVE_NAMES = {"signals.jsonl", "signals_forward.jsonl", "signals_audit.json", "signals_state.json", "signals_candles.json", "allocations.jsonl", "weights.json",
              "allocator_state.json", "allocator_latest.json", "context_latest.json", "context_snapshots.jsonl", "portfolio_brain_latest.json",
              "runtime_settings.json", "kronos_signals.jsonl"}
LIVE_DIR_NAMES = {"allocator", "bots", "kronos"}


EXT, EXT_UNC = "\\\\?\\", "\\\\?\\UNC\\"


class ReplayPathError(Exception):
    pass


def root() -> Path:
    return Path(os.environ.get("REPLAY_DIR", "/data/replay")).resolve()


def _resolved(path: Path) -> Path:
    """resolve(), minus the Windows extended-length prefix: while another thread is creating a parent directory, resolve() can return the prefixed form of the same path."""
    s = str(path.resolve())
    if s.startswith(EXT_UNC):
        return Path("\\\\" + s[len(EXT_UNC):])  # \\?\UNC\host\share -> \\host\share
    if s.startswith(EXT):
        return Path(s[len(EXT):])
    return Path(s)


def _inside(path: Path, base: Path) -> bool:
    try:
        _resolved(path).relative_to(_resolved(base))
        return True
    except ValueError:
        return False


def assert_not_live(path: Path) -> Path:
    p = Path(path)
    if p.name in LIVE_NAMES or any(part in LIVE_DIR_NAMES for part in p.resolve().parts[-3:-1]):
        raise ReplayPathError(f"{p} is a live file/dir; replay never writes there")
    return p


def replay_path(run_id: str, name: str = "") -> Path:
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
        raise ReplayPathError(f"bad run id {run_id!r}")
    if name and (Path(name).is_absolute() or ".." in Path(name).parts or "\\" in name):
        raise ReplayPathError(f"bad name {name!r}")
    base = root()
    p = (base / run_id / name) if name else (base / run_id)
    if not _inside(p, base):
        raise ReplayPathError(f"{p} is outside {base}")
    return assert_not_live(p)


def cache_path(name: str) -> Path:
    if Path(name).is_absolute() or ".." in Path(name).parts or "\\" in name:
        raise ReplayPathError(f"bad cache name {name!r}")
    base = root()
    p = base / "cache" / name
    if not _inside(p, base):
        raise ReplayPathError(f"{p} is outside {base}")
    return assert_not_live(p)


def root_file(name: str) -> Path:
    """A file directly in REPLAY_DIR (e.g. live_reference.json), still inside the sandbox."""
    if Path(name).is_absolute() or ".." in Path(name).parts or "/" in name or "\\" in name:
        raise ReplayPathError(f"bad name {name!r}")
    return assert_not_live(root() / name)


def ensure_parent(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
