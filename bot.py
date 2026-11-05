"""Research-only swing scanner. No broker connection or order placement."""
import argparse
import datetime as dt
from pathlib import Path
import numpy as np
import pandas as pd
import yfinance as yf
import yaml

ROOT = Path(__file__).resolve().parent

def settings():
    with open(ROOT / 'config.yaml', encoding='utf-8') as f:
        return yaml.safe_load(f)

def history(ticker, years=5):
    try:
        x = yf.download(ticker, period=f'{years}y', interval='1d', auto_adjust=True,
                        progress=False, threads=False)
        if isinstance(x.columns, pd.MultiIndex):
            x.columns = x.columns.get_level_values(0)
        x = x[['Open', 'High', 'Low', 'Close', 'Volume']].dropna().sort_index()
        return x if len(x) >= 260 else pd.DataFrame()
    except Exception as exc:
        print(f'WARNING {ticker}: {exc}')
        return pd.DataFrame()

def indicators(df):
    x = df.copy()
    c = x['Close']
    x['sma50'] = c.rolling(50).mean()
    x['sma200'] = c.rolling(200).mean()
    x['momentum63'] = c / c.shift(63) - 1
    x['momentum126'] = c / c.shift(126) - 1
    prev = c.shift(1)
    tr = pd.concat([(x.High-x.Low), (x.High-prev).abs(), (x.Low-prev).abs()], axis=1).max(axis=1)
    x['atr14'] = tr.rolling(14).mean()
    x['volatility'] = c.pct_change().rolling(30).std() * np.sqrt(252)
    x['entry_signal'] = ((c > x.sma200) & (x.sma50 > x.sma200) &
                         (x.momentum63 > 0) & (x.momentum126 > 0) &
                         (x.volatility < 0.65))
    x['exit_signal'] = (c < x.sma50) | (c < x.sma200)
    x['score'] = (0.6*x.momentum63 + 0.4*x.momentum126) / x.volatility.replace(0, np.nan)
    return x

def scan():
    cfg = settings()
    rows, data, errors = [], {}, []
    for ticker in cfg['tickers']:
        df = history(ticker)
        if df.empty:
            errors.append(ticker)
            continue
        x = indicators(df)
        data[ticker] = x
        r = x.iloc[-1]
        if pd.isna(r['score']):
            continue
        rows.append(dict(ticker=ticker, date=str(x.index[-1].date()),
                         close=round(float(r.Close), 2),
                         currency='instrument currency (see Yahoo/Nordnet)',
                         signal='BUY CANDIDATE' if bool(r.entry_signal) else ('EXIT / AVOID' if bool(r.exit_signal) else 'WATCH'),
                         score=round(float(r.score), 3),
                         momentum_3m_pct=round(float(r.momentum63)*100, 1),
                         momentum_6m_pct=round(float(r.momentum126)*100, 1),
                         annual_vol_pct=round(float(r.volatility)*100, 1),
                         stop_reference=round(float(r.Close - cfg['stop_atr_multiple']*r.atr14), 2),
                         sma50=round(float(r.sma50), 2),
                         sma200=round(float(r.sma200), 2)))
    table = pd.DataFrame(rows)
    if not table.empty:
        table = table.sort_values(['signal','score'], ascending=[True,False])
    return table, data, errors

def backtest(df, cost_pct=0.004):
    """Long-only, single-instrument test. Signals from previous close; execute next open.
    Fixed 100% allocation while in position. Round-trip cost split across entry/exit.
    Exit conditions evaluated at close; next open execution. No intraday stop simulation.
    """
    x = indicators(df).dropna(subset=['sma200', 'atr14']).copy()
    if len(x) < 40:
        return pd.DataFrame(), {}
    cash, shares = 1.0, 0.0
    per_side = cost_pct/2
    equity = []
    trades = 0
    for i in range(1,len(x)):
        prior, today = x.iloc[i-1], x.iloc[i]
        op = float(today.Open)
        if op <= 0:
            continue
        if shares and bool(prior.exit_signal):
            cash = shares * op * (1-per_side)
            shares = 0.0
            trades += 1
        elif not shares and bool(prior.entry_signal):
            shares = cash * (1-per_side) / op
            cash = 0.0
            trades += 1
        equity.append((x.index[i], cash + shares * float(today.Close)))
    curve = pd.DataFrame(equity, columns=['date','equity']).set_index('date')
    if curve.empty:
        return curve, {}
    dd = curve.equity / curve.equity.cummax() - 1
    years = max((curve.index[-1]-curve.index[0]).days/365.25, 0.01)
    buyhold = float(x.Close.iloc[-1] / x.Close.iloc[0] - 1)
    stats = dict(total_return_pct=round((curve.equity.iloc[-1]-1)*100,2),
                 cagr_pct=round((curve.equity.iloc[-1]**(1/years)-1)*100,2),
                 max_drawdown_pct=round(dd.min()*100,2),
                 buyhold_return_pct=round(buyhold*100,2),
                 transactions=trades,
                 start=str(curve.index[0].date()), end=str(curve.index[-1].date()))
    return curve, stats

def holdings_alerts(data):
    path = ROOT/'holdings.csv'
    h = pd.read_csv(path)
    result=[]
    for _, row in h.iterrows():
        ticker = str(row.ticker).strip()
        if ticker not in data:
            result.append({'ticker':ticker,'alert':'NO DATA','reason':'Add ticker to config.yaml'})
            continue
        r=data[ticker].iloc[-1]
        reasons=[]
        if bool(r.exit_signal): reasons.append('Trend exit signal')
        if pd.notna(row.get('stop_price')) and float(r.Close) <= float(row.stop_price):
            reasons.append('Below user-defined stop (closing-price check)')
        result.append({'ticker':ticker,'alert':'REVIEW SELL' if reasons else 'HOLD / REVIEW',
                       'reason':'; '.join(reasons) or 'No rule-based exit trigger',
                       'last_close':round(float(r.Close),2)})
    return pd.DataFrame(result)

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--backtest', default=None, help='Ticker to backtest, e.g. MSFT')
    args=p.parse_args()
    if args.backtest:
        df=history(args.backtest)
        if df.empty: raise SystemExit('Insufficient data')
        curve, stats=backtest(df, settings()['round_trip_cost_pct'])
        print(stats)
        curve.to_csv(ROOT/'backtest_equity.csv')
        return
    table,data,errors=scan()
    if table.empty: raise SystemExit('No usable market data. Check internet/tickers.')
    table.to_csv(ROOT/'daily_signals.csv',index=False)
    print(table[['ticker','signal','score','close','stop_reference']].to_string(index=False))
    alerts=holdings_alerts(data)
    alerts.to_csv(ROOT/'holdings_alerts.csv',index=False)
    if not alerts.empty: print('\nHOLDINGS\n',alerts.to_string(index=False))
    if errors: print('\nMissing data:',', '.join(errors))
    print('\nSaved daily_signals.csv and holdings_alerts.csv')

if __name__=='__main__':
    main()
