"""Research-only DKK portfolio backtest; does not place brokerage orders."""
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf
from bot import history, indicators, settings

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'site' / 'portfolio_backtest_v3.json'
INITIAL_DKK = 300_000.0
MAX_POSITIONS = 10
MAX_ENTRY_FRACTION = 0.10
SPLIT_DATE = '2025-01-01'


def currency_for(ticker):
    for suffix, ccy in (('.CO', 'DKK'), ('.DE', 'EUR'), ('.PA', 'EUR'),
                        ('.SW', 'CHF'), ('.T', 'JPY'), ('.KS', 'KRW'),
                        ('.AX', 'AUD'), ('.L', 'GBP')):
        if ticker.endswith(suffix):
            return ccy
    return 'USD'  # Assumes unsuffixed tickers are US-listed USD instruments


def fx_series(currency, index):
    if currency == 'DKK':
        return pd.Series(1.0, index=index)
    # Yahoo: DKK=X means DKK per USD; KRW=X means KRW per USD.
    usd_dkk = yf.Ticker('DKK=X').history(period='6y', auto_adjust=True)['Close']
    if usd_dkk.empty:
        raise RuntimeError('Missing USD/DKK exchange-rate history')
    usd_dkk.index = usd_dkk.index.tz_localize(None).normalize() if usd_dkk.index.tz is not None else usd_dkk.index.normalize()
    if currency == 'USD':
        conversion = usd_dkk
    else:
        units_per_usd = yf.Ticker(f'{currency}=X').history(period='6y', auto_adjust=True)['Close']
        if units_per_usd.empty:
            raise RuntimeError(f'Missing USD/{currency} exchange-rate history')
        units_per_usd.index = units_per_usd.index.tz_localize(None).normalize() if units_per_usd.index.tz is not None else units_per_usd.index.normalize()
        aligned = pd.concat([usd_dkk.rename('dkk'), units_per_usd.rename('units')], axis=1).sort_index().ffill(limit=5)
        conversion = aligned['dkk'] / aligned['units']
    # Only use known earlier FX observations; never backfill future rates.
    return conversion.shift(1).reindex(conversion.index.union(index)).sort_index().ffill(limit=5).reindex(index)


def load_data(tickers):
    stocks, missing = {}, {}
    for ticker in tickers:
        try:
            raw = history(ticker, years=5)
            if raw is None or raw.empty:
                raise ValueError('No price history')
            x = indicators(raw).copy()
            x.index = pd.DatetimeIndex(x.index).tz_localize(None).normalize()
            x = x[~x.index.duplicated(keep='last')].sort_index()
            x['rank_momentum_20d'] = x['Close'].pct_change(20)
            x = x.dropna(subset=['sma200'])
            if len(x) < 100:
                raise ValueError('Insufficient data after SMA200 warmup')
            stocks[ticker] = x
            print(f'Loaded {ticker}', flush=True)
        except Exception as exc:
            missing[ticker] = str(exc)
            print(f'Skipped {ticker}: {exc}', flush=True)
    if not stocks:
        raise RuntimeError('No stock price histories loaded')
    return stocks, missing


def run_portfolio(stocks, fx, start, end, cost_pct):
    dates = sorted(set().union(*(set(x.index) for x in stocks.values())))
    dates = [d for d in dates if pd.Timestamp(start) <= d <= pd.Timestamp(end)]
    if len(dates) < 30:
        return None
    cash = INITIAL_DKK
    positions = {}  # ticker -> shares
    last_prices = {}
    transactions = []
    equity_curve = []
    fee = cost_pct / 2

    def mark_equity(day):
        total = cash
        for ticker, shares in positions.items():
            price = last_prices.get(ticker)
            rate = fx[ticker].get(day, np.nan)
            if price is not None and np.isfinite(rate) and rate > 0:
                total += shares * price * rate
            else:
                raise RuntimeError(f'Cannot value open position {ticker} on {day.date()}')
        return total

    # Prior trading-day signals for each individual stock, not calendar yesterday.
    for day in dates:
        available = {}
        for ticker, x in stocks.items():
            if day not in x.index:
                continue
            loc = x.index.get_loc(day)
            if loc < 1:
                continue
            prior, today = x.iloc[loc - 1], x.iloc[loc]
            op, cl = float(today['Open']), float(today['Close'])
            rate = fx[ticker].get(day, np.nan)
            if not all(np.isfinite(v) and v > 0 for v in (op, cl, rate)):
                continue
            available[ticker] = (prior, op, cl, float(rate))
            last_prices[ticker] = float(prior['Close'])

        # Existing holdings can be valued with their most recent close.
        for ticker in positions:
            if ticker not in last_prices:
                raise RuntimeError(f'Missing valuation price for {ticker}')
        sold_today = set()
        for ticker in list(positions):
            if ticker not in available:
                continue
            prior, op, _, rate = available[ticker]
            if pd.notna(prior['sma200']) and float(prior['Close']) < float(prior['sma200']):
                shares = positions.pop(ticker)
                proceeds = shares * op * rate * (1 - fee)
                cash += proceeds
                sold_today.add(ticker)
                transactions.append({'date': str(day.date()), 'ticker': ticker,
                                     'side': 'SELL', 'shares': round(shares, 8),
                                     'value_dkk_after_fee': round(proceeds, 2)})

        # Explicit ranking proxy: highest 20-day momentum among existing entry signals.
        # This is NOT the bot's proprietary/current score (not available here).
        candidates = []
        for ticker, (prior, op, _, rate) in available.items():
            if ticker in positions or ticker in sold_today:
                continue
            signal = prior.get('entry_signal', False)
            momentum = prior.get('rank_momentum_20d', np.nan)
            if pd.notna(signal) and bool(signal) and pd.notna(momentum) and np.isfinite(momentum):
                candidates.append((float(momentum), ticker, op, rate))
        candidates.sort(key=lambda item: (-item[0], item[1]))
        for _, ticker, op, rate in candidates:
            if len(positions) >= MAX_POSITIONS:
                break
            equity_before = mark_equity(day)
            budget = min(cash, MAX_ENTRY_FRACTION * equity_before)
            if budget <= 0:
                break
            shares = budget * (1 - fee) / (op * rate)
            if shares <= 0 or not np.isfinite(shares):
                continue
            cash -= budget
            positions[ticker] = shares
            transactions.append({'date': str(day.date()), 'ticker': ticker,
                                 'side': 'BUY', 'shares': round(shares, 8),
                                 'value_dkk_including_fee': round(budget, 2)})
        for ticker, (_, _, cl, _) in available.items():
            last_prices[ticker] = cl
        equity_curve.append((day, mark_equity(day)))

    curve = pd.Series(dict(equity_curve), dtype=float)
    years = (curve.index[-1] - curve.index[0]).days / 365.25
    drawdown = curve / curve.cummax() - 1
    return {
        'start': str(curve.index[0].date()), 'end': str(curve.index[-1].date()),
        'initial_dkk': INITIAL_DKK, 'final_dkk': round(float(curve.iloc[-1]), 2),
        'total_return_pct': round((curve.iloc[-1] / INITIAL_DKK - 1) * 100, 2),
        'cagr_pct': round(((curve.iloc[-1] / INITIAL_DKK) ** (1 / years) - 1) * 100, 2),
        'max_drawdown_pct': round(float(drawdown.min()) * 100, 2),
        'transactions': len(transactions),
        'open_positions': sorted(positions),
        'equity_monthly': [{'date': str(d.date()), 'dkk': round(float(v), 2)}
                           for d, v in curve.resample('ME').last().items()],
        'trades': transactions,
    }


def main():
    cfg = settings()
    stocks, missing = load_data(cfg['tickers'])
    all_days = pd.DatetimeIndex(sorted(set().union(*(set(x.index) for x in stocks.values()))))
    fx = {}
    for ticker in list(stocks):
        try:
            fx[ticker] = fx_series(currency_for(ticker), all_days)
            if fx[ticker].notna().sum() < 100:
                raise ValueError('Too few currency observations')
        except Exception as exc:
            missing[ticker] = f'FX: {exc}'
            del stocks[ticker]
    if not stocks:
        raise RuntimeError('No stocks with usable FX')
    cost = float(cfg.get('round_trip_cost_pct', 0.004))
    earliest = min(x.index.min() for x in stocks.values())
    latest = max(x.index.max() for x in stocks.values())
    # Capital resets between periods, just as in v2.
    periods = {'development': (str(earliest.date()), '2024-12-31'),
               'evaluation': (SPLIT_DATE, str(latest.date()))}
    results = {}
    for name, (start, end) in periods.items():
        results[name] = run_portfolio(stocks, fx, start, end, cost)
        print(name, results[name]['total_return_pct'] if results[name] else 'No data')
    payload = {
        'version': 3, 'generated_utc': datetime.now(timezone.utc).isoformat(),
        'method': 'Portfolio simulation in DKK; no orders placed',
        'parameters': {'initial_dkk': INITIAL_DKK, 'max_positions': MAX_POSITIONS,
                       'max_entry_fraction': MAX_ENTRY_FRACTION, 'cost_pct_round_trip': cost,
                       'exit': 'Previous close below SMA200; next available open',
                       'entry': 'Existing entry_signal; next available open',
                       'ranking': '20-day price momentum (proxy; NOT existing bot score)'},
        'results': results, 'missing_tickers': missing,
        'limitations': [
            'Current ticker universe creates selection/survivorship bias',
            '2025-2026 was already inspected; not a clean out-of-sample test',
            'Capital resets in each period; positions do not carry across split',
            'FX converted using daily Yahoo indicative closes, not executable broker rates',
            'FX uses previous observed close; time-zone differences and stale FX still matter',
            'Last known close used on non-trading days; stale marks possible',
            'Fractional shares permitted; ignores minimum order sizes and exchange holidays',
            'No taxes, dividends in cash, corporate-action verification or benchmark',
            'No portfolio-level rebalancing; 10% cap applies at entry, not after price moves',
            'Price data may be adjusted; verify Open/Close consistency and actual trade prices',
        ],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding='utf-8')
    print(f'Saved {OUT}')


if __name__ == '__main__':
    main()
