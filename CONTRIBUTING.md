# Contributing

Thanks for looking. This is a research project, so the bar for a change is "can it be tested offline, and does it say what it does **not** prove".

## Ground rules

- **English only** in code, comments, UI text, docs and commit messages.
- **No secrets, ever.** Not in code, tests, fixtures or screenshots. Use obviously fake values in tests.
- **Paper trading stays paper trading.** Do not add a code path that can place a real order as a side effect of something else. Going live is a deliberate, separate change.
- **Match the surrounding code**: naming, comment density, idiom. The `freqtrade*/` directories duplicate helper files on purpose (each container is built from its own directory); a test keeps the copies identical.

## Tests

Every `backtest/test_*.py` is self-contained: fake bots, fake exchanges, synthetic data, no network and no containers. Each prints `ALL ... CHECKS PASSED`.

```bash
pip install -r backend/requirements.txt pyyaml
python backtest/test_p26_ranker.py                          # one suite
for t in backtest/test_*.py; do python "$t" | tail -1; done  # all of them
```

A change comes with a test that would fail without it. If you touch a number that a doc quotes, update the doc.

## Adding a study (the house style)

1. Write the hypothesis and the **pass thresholds before the run**, in the module docstring.
2. Fix everything that could be tuned: one primary configuration decides, the rest of a grid is context.
3. Use real costs, a locked period for looking and a later out-of-sample period that decides, a placebo (random picks or shuffled labels), and a no-look-ahead test (changing future data must not change a feature).
4. Compute the verdict in code (`edge candidate` / `unproven` / `no edge` / `insufficient n`), never by hand. Replay code never assigns `edge`.
5. Add it as a card on the Replay page, write the report to `backtest/results/`, and journal the result in [AGENTS.md](./AGENTS.md), including what it does **not** prove.

## Commits

Small, with a message that says why. The engineering journal in `AGENTS.md` records every non-trivial change: what, why, how it was verified and what was not.
