"""
P21 Replay: fast historical run of the LLM bots (and later the allocator) over cached Bybit candles.

Isolation is the first design rule: everything is written under REPLAY_DIR (default /data/replay) through paths.replay_path(); the live ledger,
the /allocator volume, the bots' volumes and the live bots' REST/control are never touched (see paths.py and the tests that hash the live files
before and after a run). Replay NEVER assigns the status `edge`.
"""
