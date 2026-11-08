"""Research-only V4.1 sensitivity grid. No broker orders.
Uses V4 trading rules and existing V3 loaders / V3.1 VT benchmark.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd
import portfolio_backtest_v3 as v3
from portfolio_audit_v31 import vt_benchmark
from bot import settings

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'site' / 'portfolio_sensitivity_v41.json'
INITIAL = 300000.0
MAX_POSITIONS = 10
ENTRY_FRACTION = 0.10
MAX_WEIGHT = 0.15
STOP_LEVELS = [0.08, 0.12, 0.16, 0.20, None]
COOLDOWN_DAYS = [0, 5, 10]  # Trading sessions on that ticker, after a trailing-stop sale
SPLIT = pd.Timestamp('2025-01-01')


def simulate(stocks, fx, start, end, cost, trailing_stop, cooldown_days):
    days = sorted({d for frame in stocks.values() for d in frame.index if pd.Timestamp(start) <= d <= pd.Timestamp(end)})
    if len(days) < 30:
        raise ValueError('Insufficient dates in period')
    cash, positions, last_close, trades, equity = INITIAL, {}, {}, [], []
    last_stop_index = {}  # ticker -> index of its own trading session at stop sale
    fee = cost / 2

    def portfolio_value(day):
        total = cash
        for ticker, pos in positions.items():
            rate = fx[ticker].get(day, np.nan)
            price = last_close.get(ticker, np.nan)
            if not (np.isfinite(rate) and rate > 0 and np.isfinite(price) and price > 0):
                raise RuntimeError(f'Missing valuation for {ticker} at {day}')
            total += pos['shares'] * price * rate
        return total

    for day in days:
        available = {}
        for ticker, frame in stocks.items():
            if day not in frame.index:
                continue
            idx = frame.index.get_loc(day)
            if idx < 1:
                continue
            prior, current = frame.iloc[idx-1], frame.iloc[idx]
            op, cl, rate = float(current['Open']), float(current['Close']), fx[ticker].get(day, np.nan)
            if not all(np.isfinite(x) and x > 0 for x in (op, cl, rate)):
                continue
            available[ticker] = (prior, op, cl, float(rate))
            # Use prior close when valuing positions before today's open.
            last_close[ticker] = float(prior['Close'])

        sold = set()
        # Full exits: decisions based exclusively on previous local trading close.
        for ticker in list(positions):
            if ticker not in available:
                continue
            prior, op, _, rate = available[ticker]
            pos = positions[ticker]
            prev_close = float(prior['Close'])
            # High watermark includes closes observed since the position was opened,
            # and is updated only after today's orders.
            sma_exit = pd.notna(prior['sma200']) and prev_close < float(prior['sma200'])
            stop_exit = trailing_stop is not None and prev_close <= pos['peak_close'] * (1 - trailing_stop)
            if sma_exit or stop_exit:
                proceeds = pos['shares'] * op * rate * (1 - fee)
                cash += proceeds
                trades.append({'date': str(day.date()), 'ticker': ticker, 'side': 'SELL',
                               'reason': 'SMA200' if sma_exit else 'TRAILING_STOP',
                               'shares': round(pos['shares'], 8), 'value_dkk_after_fee': round(proceeds, 2)})
                del positions[ticker]
                sold.add(ticker)
                if stop_exit and not sma_exit:
                    last_stop_index[ticker] = stocks[ticker].index.get_loc(day)

        # Trim at next available open if prior-close portfolio weight exceeded 15%.
        # Check all holdings using the same prior-close valuation snapshot.
        before_trim = portfolio_value(day)
        overweight = []
        for ticker, pos in positions.items():
            if ticker not in available:
                continue
            rate = fx[ticker].get(day, np.nan)
            weight = pos['shares'] * last_close[ticker] * rate / before_trim
            if weight > MAX_WEIGHT + 1e-9:
                overweight.append((ticker, weight))
        for ticker, weight in overweight:
            prior, op, _, rate = available[ticker]
            pos = positions[ticker]
            # Target 15% of prior-close portfolio value; open-price gap may overshoot.
            target_shares = (MAX_WEIGHT * before_trim) / (float(prior['Close']) * rate)
            to_sell = max(0.0, pos['shares'] - target_shares)
            if to_sell > 1e-9:
                proceeds = to_sell * op * rate * (1 - fee)
                cash += proceeds
                pos['shares'] -= to_sell
                trades.append({'date': str(day.date()), 'ticker': ticker, 'side': 'TRIM',
                               'reason': 'WEIGHT_ABOVE_15PCT', 'shares': round(to_sell, 8),
                               'prior_weight_pct': round(weight * 100, 3),
                               'value_dkk_after_fee': round(proceeds, 2)})

        candidates = []
        for ticker, (prior, op, _, rate) in available.items():
            if ticker in positions or ticker in sold:
                continue
            if ticker in last_stop_index and cooldown_days > 0:
                elapsed = stocks[ticker].index.get_loc(day) - last_stop_index[ticker]
                if elapsed <= cooldown_days:
                    continue
            signal = prior.get('entry_signal', False)
            momentum = prior.get('rank_momentum_20d', np.nan)
            if pd.notna(signal) and bool(signal) and pd.notna(momentum) and np.isfinite(momentum):
                candidates.append((float(momentum), ticker, op, rate))
        candidates.sort(key=lambda x: (-x[0], x[1]))
        for _, ticker, op, rate in candidates:
            if len(positions) >= MAX_POSITIONS:
                break
            budget = min(cash, ENTRY_FRACTION * portfolio_value(day))
            if budget <= 0:
                break
            shares = budget * (1 - fee) / (op * rate)
            if not (np.isfinite(shares) and shares > 0):
                continue
            cash -= budget
            positions[ticker] = {'shares': shares, 'peak_close': op}
            trades.append({'date': str(day.date()), 'ticker': ticker, 'side': 'BUY',
                           'reason': 'ENTRY_SIGNAL', 'shares': round(shares, 8),
                           'value_dkk_including_fee': round(budget, 2)})

        for ticker, (_, _, cl, _) in available.items():
            last_close[ticker] = cl
            if ticker in positions:
                positions[ticker]['peak_close'] = max(positions[ticker]['peak_close'], cl)
        equity.append((day, portfolio_value(day)))

    curve = pd.Series(dict(equity), dtype=float)
    years = (curve.index[-1] - curve.index[0]).days / 365.25
    dd = curve / curve.cummax() - 1
    return {'start': str(curve.index[0].date()), 'end': str(curve.index[-1].date()),
            'initial_dkk': INITIAL, 'final_dkk': round(float(curve.iloc[-1]), 2),
            'total_return_pct': round(float((curve.iloc[-1] / INITIAL - 1) * 100), 2),
            'cagr_pct': round(float(((curve.iloc[-1] / INITIAL) ** (1 / years) - 1) * 100), 2),
            'max_drawdown_pct': round(float(dd.min() * 100), 2),
            'transactions': len(trades),
            'buy_orders': sum(t['side'] == 'BUY' for t in trades),
            'sell_orders': sum(t['side'] == 'SELL' for t in trades),
            'stop_exits': sum(t['reason'] == 'TRAILING_STOP' for t in trades),
            'weight_trims': sum(t['side'] == 'TRIM' for t in trades),
            'open_positions': sorted(positions),
            'equity_monthly': [{'date': str(d.date()), 'dkk': round(float(v), 2)} for d, v in curve.resample('ME').last().items()],
            'trades': trades}


def main():
    cfg = settings()
    stocks, missing = v3.load_data(cfg['tickers'])
    days = pd.DatetimeIndex(sorted({d for frame in stocks.values() for d in frame.index}))
    fx = {}
    for ticker in list(stocks):
        try:
            fx[ticker] = v3.fx_series(v3.currency_for(ticker), days)
            if fx[ticker].notna().sum() < 100:
                raise ValueError('Insufficient FX data')
        except Exception as exc:
            missing[ticker] = f'FX: {exc}'
            del stocks[ticker]
    if not stocks:
        raise RuntimeError('No stocks with usable FX')
    cost = float(cfg.get('round_trip_cost_pct', 0.004))
    earliest = min(x.index.min() for x in stocks.values())
    latest = max(x.index.max() for x in stocks.values())
    periods = {'development': (str(earliest.date()), '2024-12-31'),
               'evaluation': ('2025-01-01', str(latest.date()))}
    results = {}
    for label, (start, end) in periods.items():
        print(f'Running {label} VT benchmark...', flush=True)
        benchmark = vt_benchmark(start, end, INITIAL)
        runs = []
        for stop in STOP_LEVELS:
            for cooldown in COOLDOWN_DAYS:
                name = f"stop_{int(stop*100)}pct" if stop is not None else 'no_stop'
                print(f'  {label}: {name}, cooldown {cooldown} trading days', flush=True)
                result = simulate(stocks, fx, start, end, cost, stop, cooldown)
                # Store compact summary only, not thousands of trades across 30 runs.
                result.pop('trades', None)
                result.pop('equity_monthly', None)
                result.pop('open_positions', None)
                result['stop_pct'] = None if stop is None else round(stop * 100)
                result['cooldown_trading_days'] = cooldown
                result['vs_vt_final_dkk'] = round(result['final_dkk'] - benchmark['final_dkk'], 2)
                runs.append(result)
        results[label] = {'benchmark_vt_dkk': {k: benchmark[k] for k in
                          ('start', 'end', 'initial_dkk', 'final_dkk', 'total_return_pct', 'cagr_pct', 'max_drawdown_pct')},
                          'scenarios': runs}
    payload = {'version': '4.1', 'generated_utc': datetime.now(timezone.utc).isoformat(),
               'parameters': {'initial_dkk': INITIAL, 'max_positions': MAX_POSITIONS,
                              'entry_fraction': ENTRY_FRACTION, 'max_weight': MAX_WEIGHT,
                              'stop_levels_pct': [None if x is None else int(x*100) for x in STOP_LEVELS],
                              'cooldown_trading_days': COOLDOWN_DAYS,
                              'round_trip_cost_pct': cost,
                              'cooldown_definition': 'After trailing-stop sale, skip next N trading sessions for same ticker; SMA200 sales do not start cooldown',
                              'no_stop_definition': 'SMA200 exits still active; 15% trimming still active'},
               'results': results, 'missing_tickers': missing,
               'limitations': [
                   'Close-based trailing stop executes next available open; cannot guarantee loss cap',
                   '15% trimming is next-open, not an intraday hard limit',
                   'Stop thresholds and cooldowns are explored on already examined historical data; risk of overfitting',
                   'Fractional shares and indicative FX, not actual Nordnet prices or FX spreads',
                   'Current ticker universe causes selection and survivorship bias',
                   'No full corporate-action audit; dividends and transaction costs are approximations',
                   'No-stop scenario still exits on SMA200; not buy-and-hold',
                   'Market time zones approximated using local exchange dates',
                   'Period capital resets independently; no real or automatic trading']}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding='utf-8')
    print(f'Saved {OUT}', flush=True)


if __name__ == '__main__':
    main()
