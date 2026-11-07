"""Compare three research-only swing strategies. No live trading."""
import json
import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
from bot import history, indicators, settings

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "site" / "strategy_comparison.json"
STRATEGIES = ("A", "B", "C")


def simulate(df, strategy, cost_pct=0.004, atr_multiple=3.0):
    x = indicators(df).dropna(subset=["sma200", "atr14"]).copy()
    if len(x) < 40:
        return None

    cash, shares = 1.0, 0.0
    entry_cost = 0.0
    peak = 0.0
    transactions = 0
    completed = 0
    winners = 0
    invested_days = 0
    equity = [1.0]
    dates = [x.index[0]]
    per_side = cost_pct / 2

    for i in range(1, len(x)):
        prior = x.iloc[i - 1]
        today = x.iloc[i]
        op = float(today["Open"])
        close = float(today["Close"])

        if not np.isfinite(op) or op <= 0:
            equity.append(cash + shares * close)
            dates.append(x.index[i])
            continue

        if shares:
            exit_trend = (
                bool(prior["exit_signal"])
                if strategy == "A"
                else bool(prior["Close"] < prior["sma200"])
            )
            exit_stop = (
                strategy == "C"
                and float(prior["Close"]) < peak - atr_multiple * float(prior["atr14"])
            )

            if exit_trend or exit_stop:
                proceeds = shares * op * (1 - per_side)
                winners += int(proceeds > entry_cost)
                completed += 1
                transactions += 1
                cash, shares = proceeds, 0.0
                peak = 0.0

        if not shares and bool(prior["entry_signal"]):
            entry_cost = cash
            shares = cash * (1 - per_side) / op
            cash = 0.0
            peak = op
            transactions += 1

        if shares:
            invested_days += 1
            peak = max(peak, close)

        equity.append(cash + shares * close)
        dates.append(x.index[i])

    curve = pd.Series(equity, index=dates)
    years = max((dates[-1] - dates[0]).days / 365.25, 0.01)
    dd = curve / curve.cummax() - 1

    return {
        "total_return_pct": round((curve.iloc[-1] - 1) * 100, 2),
        "cagr_pct": round((curve.iloc[-1] ** (1 / years) - 1) * 100, 2),
        "max_drawdown_pct": round(dd.min() * 100, 2),
        "buyhold_return_pct": round(
            (x["Close"].iloc[-1] / x["Close"].iloc[0] - 1) * 100, 2
        ),
        "transactions": transactions,
        "completed_trades": completed,
        "win_rate_pct": round(100 * winners / completed, 1) if completed else None,
        "time_in_market_pct": round(100 * invested_days / (len(x) - 1), 1),
        "start": str(dates[0].date()),
        "end": str(dates[-1].date()),
    }


def main():
    cfg = settings()
    results = []
    missing = []
    cost = float(cfg.get("round_trip_cost_pct", 0.004))
    atr_multiple = float(cfg.get("stop_atr_multiple", 3.0))

    for ticker in cfg["tickers"]:
        print(f"Testing {ticker}", flush=True)
        df = history(ticker, years=5)
        if df.empty:
            missing.append(ticker)
            continue

        for strategy in STRATEGIES:
            stats = simulate(df, strategy, cost, atr_multiple)
            if stats is not None:
                results.append({
                    "ticker": ticker,
                    "strategy": strategy,
                    **stats,
                })

    if not results:
        raise SystemExit("No successful strategy tests")

    payload = {
        "generated_utc": dt.datetime.now(
            dt.timezone.utc
        ).isoformat(timespec="seconds"),
        "method": "Individual instrument tests, not a portfolio",
        "strategies": {
            "A": "Current SMA50/SMA200 exit",
            "B": "SMA200 exit",
            "C": "SMA200 plus close-based ATR trailing exit",
        },
        "cost_pct": cost,
        "atr_multiple": atr_multiple,
        "results": results,
        "missing_tickers": missing,
        "limitations": [
            "Current ticker universe; survivorship and selection bias",
            "No FX conversion, tax, dividends paid in cash or portfolio allocation",
            "Signals evaluated at close and executed at next open",
            "ATR stop checked at close, not an intraday protective stop",
            "No out-of-sample validation",
        ],
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(payload, indent=2, allow_nan=False),
        encoding="utf-8",
    )

    table = pd.DataFrame(results)
    summary = table.groupby("strategy").agg(
        mean_cagr=("cagr_pct", "mean"),
        median_cagr=("cagr_pct", "median"),
        mean_drawdown=("max_drawdown_pct", "mean"),
        mean_transactions=("transactions", "mean"),
    )
    print("\nSTRATEGY COMPARISON\n")
    print(summary.round(2).to_string())
    print(f"\nSaved {OUT}")


if __name__ == "__main__":
    main()
