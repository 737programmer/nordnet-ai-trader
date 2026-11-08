"""Research-only V3.1 audit: benchmark, SK Hynix trade check, and no-SK sensitivity.
Run from the repository root. Does not place orders or modify V3 outputs.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd
import yfinance as yf
import portfolio_backtest_v3 as v3

ROOT = Path(__file__).resolve().parent
OLD = ROOT / 'site' / 'portfolio_backtest_v3.json'
OUT = ROOT / 'site' / 'portfolio_audit_v31.json'
EXCLUDE = '000660.KS'
BENCHMARK = 'VT'  # Vanguard Total World Stock ETF, USD-listed; proxy, not a world index itself


def normalize(frame):
    x = frame.copy()
    x.index = pd.DatetimeIndex(x.index).tz_localize(None).normalize()
    return x[~x.index.duplicated(keep='last')].sort_index()


def summary_from_series(curve, initial):
    if curve.empty or len(curve) < 2:
        raise ValueError('Insufficient benchmark observations')
    years = (curve.index[-1] - curve.index[0]).days / 365.25
    dd = curve / curve.cummax() - 1
    return {'start': str(curve.index[0].date()), 'end': str(curve.index[-1].date()),
            'initial_dkk': round(float(initial), 2), 'final_dkk': round(float(curve.iloc[-1]), 2),
            'total_return_pct': round(float((curve.iloc[-1] / initial - 1) * 100), 2),
            'cagr_pct': round(float(((curve.iloc[-1] / initial) ** (1 / years) - 1) * 100), 2),
            'max_drawdown_pct': round(float(dd.min() * 100), 2),
            'equity_monthly': [{'date': str(day.date()), 'dkk': round(float(val), 2)}
                               for day, val in curve.resample('ME').last().items()]}


def vt_benchmark(start, end, initial):
    # auto_adjust=True makes Close dividend-adjusted. Historical adjusted prices
    # are a total-return approximation, not a cash-dividend or tax simulation.
    end_exclusive = str((pd.Timestamp(end) + pd.Timedelta(days=1)).date())
    hist = yf.Ticker(BENCHMARK).history(start=start, end=end_exclusive,
                                        auto_adjust=True, actions=True, repair=True)
    if hist.empty:
        raise RuntimeError('VT history unavailable')
    hist = normalize(hist)
    px = hist['Close'].astype(float).replace([np.inf, -np.inf], np.nan).dropna()
    fx = v3.fx_series('USD', px.index)
    dkk_px = (px * fx).replace([np.inf, -np.inf], np.nan).dropna()
    if len(dkk_px) < 30:
        raise RuntimeError('Insufficient VT prices/FX')
    # Invest at first available adjusted close. Explicitly report this timing.
    curve = initial * dkk_px / float(dkk_px.iloc[0])
    result = summary_from_series(curve, initial)
    result['ticker'] = BENCHMARK
    result['instrument'] = 'Vanguard Total World Stock ETF (USD-listed)'
    result['method'] = 'Dividend-adjusted close in USD converted using lagged indicative DKK/USD; buy at first available close, hold to last close'
    result['first_price_date'] = str(dkk_px.index[0].date())
    result['last_price_date'] = str(dkk_px.index[-1].date())
    return result


def audit_sk_trades(old):
    reports = {}
    rows = []
    for period, result in old.get('results', {}).items():
        trades = result.get('trades', []) if result else []
        buys = [t for t in trades if t['ticker'] == EXCLUDE and t['side'] == 'BUY']
        sells = [t for t in trades if t['ticker'] == EXCLUDE and t['side'] == 'SELL']
        reports[period] = {'buys': buys, 'sells': sells}
        for t in buys + sells:
            rows.append(t)
    if not rows:
        return {'status': 'NO_TRADES_FOUND', 'periods': reports}
    start = str((pd.Timestamp(min(t['date'] for t in rows)) - pd.Timedelta(days=14)).date())
    end = str((pd.Timestamp(max(t['date'] for t in rows)) + pd.Timedelta(days=14)).date())
    end_exclusive = str((pd.Timestamp(end) + pd.Timedelta(days=1)).date())
    tk = yf.Ticker(EXCLUDE)
    raw = tk.history(start=start, end=end_exclusive, auto_adjust=False, actions=True, repair=False)
    adjusted = tk.history(start=start, end=end_exclusive, auto_adjust=True, actions=True, repair=False)
    repaired = tk.history(start=start, end=end_exclusive, auto_adjust=True, actions=True, repair=True)
    if raw.empty or adjusted.empty:
        raise RuntimeError('SK Hynix raw/adjusted price history unavailable')
    raw, adjusted = normalize(raw), normalize(adjusted)
    repaired = normalize(repaired) if not repaired.empty else None
    all_dates = pd.DatetimeIndex(sorted(set(raw.index).union(adjusted.index)))
    fx = v3.fx_series('KRW', all_dates)
    check = []
    for t in sorted(rows, key=lambda x: (x['date'], x['side'])):
        day = pd.Timestamp(t['date'])
        if day not in raw.index or day not in adjusted.index:
            check.append({'date': t['date'], 'side': t['side'], 'status': 'MISSING_PRICE_DAY'})
            continue
        shares = float(t['shares'])
        amount = float(t.get('value_dkk_including_fee', t.get('value_dkk_after_fee')))
        rate = float(fx.get(day, np.nan))
        fee = float(old['parameters']['cost_pct_round_trip']) / 2
        # reverse V3 fee to infer its assumed open in KRW per share
        inferred_open = amount * (1 - fee) / shares / rate if t['side'] == 'BUY' else amount / (1 - fee) / shares / rate
        raw_open = float(raw.loc[day, 'Open'])
        adjusted_open = float(adjusted.loc[day, 'Open'])
        repaired_open = float(repaired.loc[day, 'Open']) if repaired is not None and day in repaired.index else None
        pct_vs_adjusted = (inferred_open / adjusted_open - 1) * 100
        check.append({'date': t['date'], 'side': t['side'], 'shares': shares,
                      'amount_dkk': round(amount, 2), 'dkk_per_krw_lagged': round(rate, 10),
                      'inferred_v3_open_krw': round(inferred_open, 4),
                      'yahoo_unadjusted_open_krw': round(raw_open, 4),
                      'yahoo_adjusted_open_krw': round(adjusted_open, 4),
                      'yahoo_repaired_adjusted_open_krw': round(repaired_open, 4) if repaired_open is not None else None,
                      'difference_vs_adjusted_pct': round(pct_vs_adjusted, 3),
                      'match_adjusted_within_2pct': abs(pct_vs_adjusted) < 2})
    splits = raw.get('Stock Splits', pd.Series(dtype=float))
    splits = splits[splits.notna() & (splits != 0)]
    return {'status': 'CHECKED', 'periods': reports,
            'trade_price_checks': check,
            'reported_splits_in_window': [{'date': str(d.date()), 'ratio': float(v)} for d, v in splits.items()],
            'note': 'Yahoo adjusted and unadjusted prices are not independent verification; compare material discrepancies with an exchange/broker source.'}


def main():
    if not OLD.exists():
        raise FileNotFoundError('Missing site/portfolio_backtest_v3.json; upload V3 artifact to site/ or run V3 first')
    old = json.loads(OLD.read_text(encoding='utf-8'))
    if old.get('version') != 3:
        raise ValueError('Expected V3 results')
    original = old['results']
    stocks, missing = v3.load_data(v3.settings()['tickers'])
    all_days = pd.DatetimeIndex(sorted(set().union(*(set(x.index) for x in stocks.values()))))
    fx = {}
    for ticker in list(stocks):
        try:
            fx[ticker] = v3.fx_series(v3.currency_for(ticker), all_days)
            if fx[ticker].notna().sum() < 100:
                raise ValueError('Too few FX observations')
        except Exception as exc:
            missing[ticker] = f'FX: {exc}'
            del stocks[ticker]
    if EXCLUDE not in stocks:
        raise RuntimeError(f'{EXCLUDE} missing; cannot perform comparable sensitivity test')
    stocks_without = {k: v for k, v in stocks.items() if k != EXCLUDE}
    cost = float(v3.settings().get('round_trip_cost_pct', 0.004))
    output = {'version': '3.1', 'generated_utc': datetime.now(timezone.utc).isoformat(),
              'source_v3_generated_utc': old.get('generated_utc'),
              'source_v3_parameters': old.get('parameters'),
              'method': 'Independent no-SK rerun using V3 algorithm; VT DKK buy-and-hold benchmark; SK trade data checks',
              'results': {}, 'sk_hynix_audit': {}, 'missing_tickers_rerun': missing,
              'warnings': [
                  'Sensitivity is a full portfolio rerun without SK Hynix; not simple profit subtraction.',
                  'Current ticker list introduces survivorship/selection bias.',
                  'Benchmark VT is an ETF proxy, not a literal global stock index; its fees are embedded in its price.',
                  'VT benchmark invests at first available adjusted CLOSE; strategy trades at next OPEN, so execution timing differs.',
                  'Historical adjusted ETF prices approximate reinvested distributions; Danish dividend taxes and brokerage costs are omitted.',
                  'V3 uses fractional shares and price-adjusted opens; verify corporate actions and execution assumptions.',
                  'V3 10% position cap applies at entry only; a winning position may grow far beyond 10%.',
                  'Data downloaded on rerun may differ from saved V3; original V3 results are preserved for comparison.',
                  'Original V3 has no complete daily cash or position-size ledger, so exact exposure history cannot be audited from its JSON.',
                  '2025-2026 has been repeatedly inspected; it is no longer a clean out-of-sample period.',
              ]}
    for period, base in original.items():
        if not base:
            continue
        start, end = base['start'], base['end']
        no_sk = v3.run_portfolio(stocks_without, fx, start, end, cost)
        if no_sk is None:
            raise RuntimeError(f'No-SK test unavailable for {period}')
        bench = vt_benchmark(start, end, float(base['initial_dkk']))
        output['results'][period] = {
            'original_v3': {k: base[k] for k in ('start','end','initial_dkk','final_dkk','total_return_pct','cagr_pct','max_drawdown_pct','transactions')},
            'without_sk_hynix': no_sk,
            'benchmark_vt_dkk': bench,
            'difference_original_minus_without_sk_dkk': round(float(base['final_dkk']) - float(no_sk['final_dkk']), 2),
            'difference_original_minus_benchmark_dkk': round(float(base['final_dkk']) - float(bench['final_dkk']), 2),
        }
        print(f'{period}: original {base["final_dkk"]:,.0f}, no SK {no_sk["final_dkk"]:,.0f}, VT {bench["final_dkk"]:,.0f} DKK', flush=True)
    try:
        output['sk_hynix_audit'] = audit_sk_trades(old)
    except Exception as exc:
        output['sk_hynix_audit'] = {'status': 'ERROR', 'error': str(exc)}
        output['warnings'].append('SK Hynix verification failed: see sk_hynix_audit.error')
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    print('Saved', OUT, flush=True)


if __name__ == '__main__':
    main()
