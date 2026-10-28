"""
P25-E: LightGBM v2 study (Binance archive, hourly features). Offline: a fake archive / exchange API serving synthetic hourly zip files, temp REPLAY_DIR.
Covers: archive listing and filtering, resumable download, header / microsecond handling, no look-ahead in the hourly features, walk-forward end to end, verdict conditions (incl. the stress-slippage one), wiring.
"""
import io
import json
import os
import re
import sys
import tempfile
import threading
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
failures: list[str] = []
UTC = timezone.utc


def check(label, cond):
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


tmp = tempfile.mkdtemp()
os.environ["REPLAY_DIR"] = tmp
from backend.replay import carry, lgbm_hourly as lh, lgbm_study as lg, momentum as mo  # noqa: E402

HOUR = 3600 * 1000
N_SYM = 16
T0 = datetime(2022, 10, 1, tzinfo=UTC)
T1 = datetime(2025, 3, 1, tzinfo=UTC)          # exclusive: last archived month is 2025-02
NOW = datetime(2025, 3, 20, tzinfo=UTC)


class Resp:
    def __init__(self, code=200, content=b"", text="", js=None):
        self.status_code, self.content, self.text, self._js = code, content, text, js

    def json(self):
        return self._js


def hours(a, b):
    n = int((b - a).total_seconds() // 3600)
    return [int((a + timedelta(hours=h)).timestamp() * 1000) for h in range(n)]


def make_series(i):
    rng = np.random.default_rng(300 + i)
    n = len(hours(T0, T1))
    r = (i - N_SYM / 2) * 0.00001 + 0.0008 * rng.standard_normal(n)
    px = 100.0 * np.cumprod(1 + r)
    qv = 1e6 * (N_SYM - i % 5) * (1 + 0.1 * rng.random(n))
    tb = qv * (0.5 + 0.05 * rng.standard_normal(n))
    return px, qv, tb


SERIES = {f"S{i}USDT": make_series(i) for i in range(N_SYM)}
SERIES["DEADUSDT"] = make_series(40)
SERIES["BTCUSDT"] = make_series(9)
DEAD_END = datetime(2024, 1, 1, tzinfo=UTC)
calls = {"vision": 0}
live = {f"S{i}USDT" for i in range(N_SYM)} | {"BTCUSDT"}


def month_rows(sym, ym, micro=False, header=False):
    px, qv, tb = SERIES[sym]
    ts_all = hours(T0, T1)
    y, m = map(int, ym.split("-"))
    a = datetime(y, m, 1, tzinfo=UTC)
    b = datetime(y + (m == 12), 1 if m == 12 else m + 1, 1, tzinfo=UTC)
    if sym == "DEADUSDT" and a >= DEAD_END:
        return None
    lines = ["open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore"] if header else []
    for j, t in enumerate(ts_all):
        if a.timestamp() * 1000 <= t < b.timestamp() * 1000:
            o = px[j]
            c = px[j + 1] if j + 1 < len(px) else o
            tt = t * 1000 if micro else t
            lines.append(f"{tt},{o},{o * 1.002},{o * 0.998},{c},1,{tt + 3599999},{qv[j]},1000,1,{tb[j]},0")
    return "\n".join(lines)


def zip_of(text):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.csv", text)
    return buf.getvalue()


def all_months(sym):
    ms = lh.months_between(T0, T1 - timedelta(days=1))
    return [m for m in ms if month_rows(sym, m) is not None]


def fake_get(url, params=None, timeout=None, **kw):
    params = params or {}
    if url == lh.S3:
        prefix = params.get("prefix", "")
        if prefix == lh.KLINE_DIR:
            names = sorted(SERIES) + ["S1USDT_240628", "FOOBUSD"]
            return Resp(200, text="<ListBucketResult><IsTruncated>false</IsTruncated>" + "".join(f"<CommonPrefixes><Prefix>{lh.KLINE_DIR}{n}/</Prefix></CommonPrefixes>" for n in names) + "</ListBucketResult>")
        m = re.match(re.escape(lh.KLINE_DIR) + r"([A-Z0-9_]+)/1h/$", prefix)
        if m:
            return Resp(200, text="<ListBucketResult><IsTruncated>false</IsTruncated>" + "".join(f"<Contents><Key>{prefix}{m.group(1)}-1h-{ym}.zip</Key></Contents><Contents><Key>{prefix}{m.group(1)}-1h-{ym}.zip.CHECKSUM</Key></Contents>" for ym in all_months(m.group(1))) + "</ListBucketResult>")
    if url.startswith(lh.VISION):
        calls["vision"] += 1
        m = re.search(r"/klines/([A-Z0-9]+)/1h/[A-Z0-9]+-1h-(\d{4}-\d{2})\.zip$", url)
        if m:
            sym, ym = m.groups()
            txt = month_rows(sym, ym, micro=(sym == "S3USDT" and ym == "2024-05"), header=(sym == "S2USDT"))
            return Resp(200, zip_of(txt)) if txt is not None else Resp(404)
        m = re.search(r"/fundingRate/([A-Z0-9]+)/[A-Z0-9]+-fundingRate-(\d{4}-\d{2})\.zip$", url)
        if m:
            sym, ym = m.groups()
            if month_rows(sym, ym) is None:
                return Resp(404)
            y, mo_ = map(int, ym.split("-"))
            rows = ["calc_time,funding_interval_hours,last_funding_rate"] + [f"{int(datetime(y, mo_, d, h, tzinfo=UTC).timestamp() * 1000)},8,0.0001" for d in range(1, 28) for h in (0, 8, 16)]
            return Resp(200, zip_of("\n".join(rows)))
    if url.endswith("/fapi/v1/exchangeInfo"):
        return Resp(200, js={"symbols": [{"symbol": s, "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"} for s in sorted(live)]})
    if url.endswith("/fapi/v1/klines") or url.endswith("/fapi/v1/fundingRate"):
        return Resp(200, js=[])
    return Resp(404)


# ------------------------------------------------------------ listing and download
check("LISTING: USDT perpetuals only, quarterly (_YYMMDD) and BUSD contracts dropped", lh.list_symbols(fake_get) == sorted(SERIES))
check("LISTING: months come from the archive keys, checksum files ignored", lh.list_months(fake_get, "DEADUSDT")[-1] == "2023-12" and lh.list_months(fake_get, "S0USDT")[0] == "2022-10")
check("LISTING: only contracts that trade now are fetched from the exchange API", lh.trading_symbols(fake_get) == live)
lh.SINCE = "2022-10-01"
res = lh.fetch_all(fake_get, NOW, pace=0, sleep=lambda s: None)
check("FETCH: every listed contract kept, no errors", res["kept"] == len(SERIES) and res["errors"] == 0 and not res["stopped"])
n_calls = calls["vision"]
res2 = lh.fetch_all(fake_get, NOW, pace=0, sleep=lambda s: None)
check("FETCH: a second run downloads nothing new (cached, resumable)", calls["vision"] == n_calls and res2["errors"] == 0)
k, f = lh._read_sym("S2USDT")
check("PARSE: header row skipped, rows sorted and unique, 8 columns", k.shape[1] == 8 and np.all(np.diff(k[:, 0]) == HOUR) and k[0, 0] == int(T0.timestamp() * 1000))
k3, _ = lh._read_sym("S3USDT")
check("PARSE: microsecond timestamps are normalised to milliseconds", np.all(np.diff(k3[:, 0]) == HOUR))
check("PARSE: funding read from the third column", f.shape[1] == 2 and abs(f[0, 1] - 0.0001) < 1e-12)
stop = threading.Event()
stop.set()
check("FETCH: a stop request ends the run without writing the symbol list", lh.fetch_all(fake_get, NOW, stop=stop, pace=0, sleep=lambda s: None)["stopped"])

# ------------------------------------------------------------ matrices and features
lh.UNIVERSE_N, lh.QUINTILE = 12, 3
start = datetime(2023, 1, 1, tzinfo=UTC)
days, syms, M, Od, Td, X, F = lh.build(start, NOW - timedelta(days=1), 12)
check("BUILD: the delisted contract is in the universe while it traded", "DEADUSDT" in syms)
S, D = len(syms), len(days) - 1
check("BUILD: hourly matrices [S, 24 (D+1)], daily opens [S, D+1], returns [S, D]", M["o"].shape == (S, 24 * (D + 1)) and Od.shape == (S, D + 1) and X.shape == (S, D) and F.shape == (S, D))
i0 = syms.index("S0USDT")
check("BUILD: daily return is open to open minus funding (3 x 0.01% a day)", abs(X[i0, 400] - (Od[i0, 401] / Od[i0, 400] - 1.0 - 0.0003)) < 1e-9)
iD = syms.index("DEADUSDT")
check("BUILD: after delisting the contract has no prices (held flat by the book)", np.isnan(Od[iD, D - 5]) and not np.isnan(Od[iD, 300]))
btc = None
k0 = next(i for i, d in enumerate(days) if d >= carry.day_start(start))
k = k0 + 200
uni = mo.universe_at(Td, Od, k, 12)
fe = lh.feature_day(M, Od, F, k, uni, btc)
check("FEATURES: one row per universe coin, one column per named feature", fe.shape == (len(uni), len(lh.FEATURES)) and len(lh.FEATURES) == 39)
h0 = 24 * k
j = lh.FEATURES.index("ret24")
ii = uni[0]
check("FEATURES: ret24 is the open-to-open return over the last 24 hours", abs(fe[0, j] - (M["o"][ii, h0] / M["o"][ii, h0 - 24] - 1.0)) < 1e-6)
M2 = {n: a.copy() for n, a in M.items()}
for n in ("hi", "lo", "qv", "n", "tb"):
    M2[n][:, h0:] *= 7.0
M2["o"][:, h0 + 1:] *= 3.0
Od2, F2 = Od.copy(), F.copy()
Od2[:, k + 1:] *= 3.0
F2[:, k:] += 0.01
check("NO LOOK-AHEAD: changing every bar from the decision hour on and every later price / funding does not change the features", np.allclose(fe, lh.feature_day(M2, Od2, F2, k, uni, btc), equal_nan=True))
M3 = {n: a.copy() for n, a in M.items()}
M3["qv"][:, h0 - 1] *= 9.0
check("FEATURES: the last completed hourly bar does reach the features (surge ratio moves)", not np.allclose(fe, lh.feature_day(M3, Od, F, k, uni, btc), equal_nan=True))

# ------------------------------------------------------------ verdict
ok = {"days": 800, "apr_pct": 25.0, "max_dd_pct": 12.0, "ci95_daily_apr_pct": [5.0, 45.0]}
icg = {"mean_ic": 0.05}
check("VERDICT: all met -> edge candidate", lh.verdict(ok, {"apr_pct": 5.0}, 99.0, 250, icg, 10.0, 8.0)["verdict"] == "edge candidate")
v = lh.verdict(ok, {"apr_pct": 5.0}, 99.0, 250, icg, 10.0, -4.0)
check("VERDICT: costs kill it (stress slippage negative) -> not an edge candidate", v["verdict"] == "unproven" and any("0.30%" in x for x in v["failed"]))
check("VERDICT: missing baseline beat / IC / placebo are still enforced", len(lh.verdict(ok, {"apr_pct": 5.0}, 60.0, 250, {"mean_ic": 0.0}, 90.0, 8.0)["failed"]) == 3)
check("VERDICT: too few rebalances -> insufficient n", lh.verdict(ok, {"apr_pct": 5.0}, 99.0, 5, icg, 10.0, 8.0)["verdict"] == "insufficient n")

# ------------------------------------------------------------ end to end
lh.PLACEBO_SEEDS = 3
lh.PARAMS = {**lh.PARAMS, "n_estimators": 60, "n_jobs": 2, "min_child_samples": 50}
rep = lh.run_study("lgbh-test", {"since": "2023-01-01"}, NOW)
check("STUDY: report with verdict, IC, placebo, both baselines, BTC, 4 configurations", rep["type"] == "lgbm_hourly_study" and rep["verdict"] and len(rep["results"]) == 4 and {"mom14_hold3", "rev3_hold3", "btc_hold"} <= set(rep["benchmarks"]) and rep["placebo"]["seeds"] == 3)
check("STUDY: on persistent drifts the model ranks coins (OOS rank IC > 0.1) and earns net of costs", rep["rank_ic"]["oos"].get("mean_ic", 0) > 0.1 and rep["results"][rep["primary"]]["oos"]["net_pct_of_sleeve"] > 0)
check("STUDY: stress slippage never earns more than the base slippage", rep["results"]["h3_slip0.3"]["oos"]["net_pct_of_sleeve"] < rep["results"]["h3_slip0.1"]["oos"]["net_pct_of_sleeve"])
rob = rep["robustness"]
check("ROBUSTNESS: concentration, yearly, clipped, ex-breaks, legs and the lookback grid are reported", {"concentration_pct_of_sleeve", "apr_pct_by_year", "daily_coin_return_clipped_at_50pct", "without_coins_with_a_100pct_day", "legs_gross_of_the_other_leg", "momentum_lookback_sensitivity_hold3"} <= set(rob) and set(rob["momentum_lookback_sensitivity_hold3"]) == {"mom3", "mom7", "mom14", "mom28", "mom56"})
check("ROBUSTNESS: best-day concentration is consistent (total without the best 20 days is below the total)", rob["concentration_pct_of_sleeve"]["total_without_best_20_days"] < rob["concentration_pct_of_sleeve"]["total"])
vc = lh.verdict(ok, {"apr_pct": 5.0}, 99.0, 250, icg, 10.0, 8.0, 3.0)
check("VERDICT: a result that dies when daily returns are clipped at 50% is not an edge candidate", vc["verdict"] == "unproven" and any("clipped" in x for x in vc["failed"]))
lh.write_outputs("lgbh-test", {}, rep)
d_ = Path(tmp) / "lgbh-test"
md = (d_ / "report.md").read_text(encoding="utf-8")
check("OUTPUT: report.json, report.md, equity.csv written; no internal series in the JSON", all((d_ / f_).is_file() for f_ in ("report.json", "report.md", "equity.csv")) and "_sims" not in json.loads((d_ / "report.json").read_text(encoding="utf-8")))
check("OUTPUT: the markdown is titled v2 and carries verdict, IC, placebo, benchmarks, thresholds, limits", "study v2" in md and all(x in md for x in ("Verdict", "Rank IC", "Placebo", "Benchmarks", "Pass thresholds", "Limits", "Robustness")))

# ------------------------------------------------------------ wiring
src = lambda p: (REPO / p).read_text(encoding="utf-8")   # noqa: E731
check("WIRING: route, runner kind and UI card exist", "/api/replay/lgbm-hourly/study" in src("backend/app.py") and "lgbm_hourly_study" in src("backend/replay/runner.py") and "hRun" in src("frontend/replay.html"))

print("\nALL P25 LGBM HOURLY CHECKS PASSED" if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
