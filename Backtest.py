
"""Historical research backtest for all configured stocks."""
import json
import datetime as dt
from pathlib import Path
import pandas as pd
from bot import settings, history, backtest

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "site" / "backtest.json"

def main():
    cfg = settings()
    results = []
    missing = []

    for ticker in cfg["tickers"]:
        print(f"Testing {ticker}...", flush=True)
        df = history(ticker, years=5)

        if df.empty:
            missing.append(ticker)
            continue

        curve, stats = backtest(
            df,
            cost_pct=cfg.get("round_trip_cost_pct", 0.004)
        )

        if not stats:
            missing.append(ticker)
            continue

        results.append({"ticker": ticker, **stats})

    if not results:
        raise SystemExit("No successful backtests")

    results.sort(
        key=lambda r: r["cagr_pct"],
        reverse=True
    )

    payload = {
        "generated_utc": dt.datetime.now(
            dt.timezone.utc
        ).isoformat(),
        "method": "Individual stock backtests, not portfolio",
        "results": results,
        "missing_tickers": missing
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8"
    )

    print(pd.DataFrame(results)[[
        "ticker",
        "total_return_pct",
        "cagr_pct",
        "max_drawdown_pct",
        "buyhold_return_pct",
        "transactions"
    ]].to_string(index=False))

    print(f"Completed {len(results)} backtests")

if __name__ == "__main__":
    main()
