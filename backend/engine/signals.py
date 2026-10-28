"""
Market signal — price and volatility only, v0.1.

Pulls public candle data from Bybit (no API key required — this is
read-only market data, same as anyone browsing bybit.com sees). Turns
it into a small, plain-language summary that brain.py can reason over.

Deliberately NOT included yet: news/sentiment analysis. Adding it
later means calling brain.ask() on a handful of headlines and merging
the result into this same dict — see AGENTS.md for where that slots
in. Keeping this file to "what actually happened to the price" first
means we can tell whether the agent's decisions make sense before
adding a second, harder-to-verify input.
"""
import logging
import statistics

import requests

log = logging.getLogger("survival-agent.signals")

KLINE_URL = "https://api.bybit.com/v5/market/kline"


def get_signal(symbol: str = "BTCUSDT", hours: int = 24) -> dict:
    """
    Returns a plain summary of the last `hours` of price action:
      - current price
      - percent change over the window
      - a simple volatility measure (how wide the price swung, as a
        percent of its low) -- not a textbook stat, just "how choppy
        has this been"

    Returns {"available": False, ...} if the request fails for any
    reason. Callers must treat that as "no signal this cycle", not as
    permission to guess.
    """
    try:
        r = requests.get(
            KLINE_URL,
            params={"category": "spot", "symbol": symbol, "interval": "60", "limit": hours},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()

        if data.get("retCode") != 0:
            log.warning("Bybit returned an error: %s", data.get("retMsg"))
            return {"available": False, "symbol": symbol, "reason": data.get("retMsg")}

        candles = data["result"]["list"]
        if not candles:
            return {"available": False, "symbol": symbol, "reason": "no candle data returned"}

        closes = [float(c[4]) for c in candles]
        highs = [float(c[2]) for c in candles]
        lows = [float(c[3]) for c in candles]

        current_price = closes[0]
        oldest_price = closes[-1]
        pct_change = ((current_price - oldest_price) / oldest_price) * 100

        period_low = min(lows)
        period_high = max(highs)
        volatility_pct = ((period_high - period_low) / period_low) * 100

        return {
            "available": True,
            "symbol": symbol,
            "window_hours": hours,
            "current_price": round(current_price, 2),
            "pct_change_over_window": round(pct_change, 2),
            "volatility_pct": round(volatility_pct, 2),
            "direction": "up" if pct_change > 0.5 else "down" if pct_change < -0.5 else "flat",
        }

    except Exception as e:
        log.error("Signal fetch failed for %s: %s", symbol, e)
        return {"available": False, "symbol": symbol, "reason": str(e)}
