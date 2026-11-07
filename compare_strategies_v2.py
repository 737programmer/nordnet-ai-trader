"""Research-only comparison of swing strategies. No orders are placed."""
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from bot import history, indicators, settings

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "site" / "strategy_comparison_v2.json"
STRATEGIES = ("A", "B", "C")
SPLIT_DATE = "2025-01-01"


def valid_signal(value):
    return bool(value) if pd.notna(value) else False


def simulate(ind, strategy, start, end, cost_pct=0.004, atr_multiple=2.5):
    """Indicators use earlier history; account is reset for each test window."""
    window = ind.loc[(ind.index >= pd.Timestamp(start)) &
                     (ind.index <= pd.Timestamp(end))].copy()
    if len(window) < 30:
        return None

    cash, shares = 1.0, 0.0
    entry_cost, peak = 0.0, 0.0
    transactions = completed = winners = invested_days = 0
    equity, dates = [1.0], [window.index[0]]
    fee = cost_pct / 2.0

    for i in range(1, len(window)):
        prior = window.iloc[i - 1]
        today = window.iloc[i]
        opening, closing = float(today["Open"]), float(today["Close"])
        sold_today = False

        if np.isfinite(opening) and opening > 0:
            if shares > 0:
                if strategy == "A":
                    trend_exit = valid_signal(prior["exit_signal"])
                else:
                    trend_exit = (pd.notna(prior["sma200"]) and
                                  float(prior["Close"]) < float(prior["sma200"]))

                stop_exit = (strategy == "C" and
                             pd.notna(prior["atr14"]) and
                             float(prior["Close"]) <
                             peak - atr_multiple * float(prior["atr14"]))

                if trend_exit or stop_exit:
                    proceeds = shares * opening * (1 - fee)
                    winners += int(proceeds > entry_cost)
                    completed += 1
                    transactions += 1
                    cash, shares, peak = proceeds, 0.0, 0.0
                    sold_today = True

            # An exit cannot be followed by a new entry on the same day.
            if (shares == 0 and not sold_today and
                    valid_signal(prior["entry_signal"])):
                entry_cost = cash
                shares = cash * (1 - fee) / opening
                cash, peak = 0.0, opening
                transactions += 1

        if shares > 0:
            invested_days += 1
            if np.isfinite(closing):
                peak = max(peak, closing)

        mark = closing if np.isfinite(closing) else opening
        if not np.isfinite(mark):
            mark = float(prior["Close"])
        equity.append(cash + shares * mark)
        dates.append(window.index[i])

    curve = pd.Series(equity, index=pd.DatetimeIndex(dates))
    years = (dates[-1] - dates[0]).days / 365.25
    if years <= 0 or curve.iloc[-1] <= 0:
        return None
    drawdown = curve / curve.cummax() - 1
    buyhold = window["Close"].iloc[-1] / window["Close"].iloc[0] - 1

    return {
        "total_return_pct": round((curve.iloc[-1] - 1) * 100, 2),
        "cagr_pct": round((curve.iloc[-1] ** (1 / years) - 1) * 100, 2),
        "max_drawdown_pct": round(drawdown.min() * 100, 2),
        "buyhold_return_pct": round(buyhold * 100, 2),
        "transactions": transactions,
        "completed_trades": completed,
        "win_rate_pct": round(winners / completed * 100, 1) if completed else None,
        "time_in_market_pct": round(invested_days / (len(window) - 1) * 100, 1),
        "start": str(dates[0].date()),
        "end": str(dates[-1].date()),
    }


def main():
    cfg = settings()
    cost = float(cfg.get("round_trip_cost_pct", 0.004))
    atr_multiple = float(cfg.get("stop_atr_multiple", 2.5))
    results, missing = [], []

    for ticker in cfg["tickers"]:
        print(f"Testing {ticker}", flush=True)
        raw = history(ticker, years=5)
        if raw is None or raw.empty:
            missing.append(ticker)
            continue

        ind = indicators(raw).dropna(subset=["sma200", "atr14"]).copy()
        if ind.empty:
            missing.append(ticker)
            continue

        periods = {
            "development": (str(ind.index[0].date()), "2024-12-31"),
            "evaluation": (SPLIT_DATE, str(ind.index[-1].date())),
        }
        for period, (start, end) in periods.items():
            for strategy in STRATEGIES:
                stats = simulate(ind, strategy, start, end, cost, atr_multiple)
                if stats is not None:
                    results.append({"ticker": ticker, "period": period,
                                    "strategy": strategy, **stats})

    if not results:
        raise SystemExit("No backtests completed")

    payload = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "version": 2,
        "method": "Individual stock tests, not a portfolio",
        "split_date": SPLIT_DATE,
        "cost_pct": cost,
        "atr_multiple": atr_multiple,
        "strategies": {
            "A": "Original exit_signal",
            "B": "Close below SMA200",
            "C": "Close below SMA200 or trailing ATR threshold",
        },
        "results": results,
        "missing_tickers": missing,
        "limitations": [
            "Same current ticker list in both periods; selection bias",
            "Evaluation period was already inspected in earlier backtests",
            "Indicators use history before each period, but capital resets",
            "Signals from previous close; execution at next open",
            "ATR threshold is checked at close, not an intraday stop",
            "No FX, taxes, portfolio allocation or separate cash dividend accounting",
            "Open positions are marked to market; win rate excludes them",
        ],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")

    table = pd.DataFrame(results)
    summary = table.groupby(["period", "strategy"]).agg(
        mean_cagr=("cagr_pct", "mean"),
        median_cagr=("cagr_pct", "median"),
        mean_drawdown=("max_drawdown_pct", "mean"),
        mean_transactions=("transactions", "mean"),
    )
    print("\nSTRATEGY COMPARISON V2\n")
    print(summary.round(2).to_string())
    print(f"\nSaved {OUT}")


if __name__ == "__main__":
    main()
