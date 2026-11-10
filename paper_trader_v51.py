"""V5 research-only daily paper portfolio. No broker connection or live orders.
Paper fills use observed CLOSE on the first later scan (not an executable price).
State is public if committed to a public GitHub repository.
"""
import json, math, os
from datetime import datetime, timezone, date
from pathlib import Path
import pandas as pd
import numpy as np
import portfolio_backtest_v3 as v3
from portfolio_backtest_v42 import issuer
from bot import settings

ROOT=Path(__file__).resolve().parent
SITE=ROOT/'site'
STATE=SITE/'paper_v5_state.json'
OUTPUT=SITE/'paper_v5.json'
INITIAL=300000.0
MAX_POSITIONS=10
MAX_WEIGHT=.15
ENTRY_WEIGHT=.10
ROUND_TRIP_COST=.004
MAX_QUOTE_AGE_DAYS=5
HALF_FEE=ROUND_TRIP_COST/2
# Both paper strategies, never real orders
STRATEGIES={'stop20': .20, 'no_stop': None}

def finite(x):
    try:
        x=float(x)
        return x if math.isfinite(x) else None
    except (ValueError,TypeError): return None

def default_book():
    return {'cash_dkk': INITIAL, 'positions':{}, 'pending':[], 'fills':[], 'equity_history':[]}

def load_state():
    if STATE.exists():
        state=json.loads(STATE.read_text())
        if state.get('schema') != 1: raise RuntimeError('Unsupported paper state schema')
        return state
    return {'schema':1, 'created_utc':datetime.now(timezone.utc).isoformat(),
            'books':{key:default_book() for key in STRATEGIES}}

def snapshot(ticker, frame, fx):
    x=frame.iloc[-1]
    day=frame.index[-1]
    price=finite(x['Close'])
    rate=finite(fx[ticker].get(day, np.nan))
    if not price or price<=0 or not rate or rate<=0: return None
    return {'ticker':ticker,'date':str(day.date()),'close':price,'fx_dkk_per_unit':rate,
            'sma200':finite(x.get('sma200')),
            'entry_signal':bool(x.get('entry_signal',False)),
            'momentum20':finite(x.get('rank_momentum_20d'))}

def value(book, quotes):
    total=book['cash_dkk']
    stale=[]
    for t,pos in book['positions'].items():
        q=quotes.get(t)
        if q:
            pos['last_price_dkk']=q['close']*q['fx_dkk_per_unit']
            pos['last_quote_date']=q['date']
        elif 'last_price_dkk' not in pos:
            raise RuntimeError(f'No price to value existing holding {t}')
        else: stale.append(t)
        total+=pos['shares']*pos['last_price_dkk']
    return total,stale

def fill_pending(book, quotes, today):
    """Fill only after signal day, at first subsequently observed CLOSE (paper approximation).
    Orders are canceled if no newer quote exists; never assume a next-day opening fill.
    """
    kept=[]
    for order in book['pending']:
        t=order['ticker']; q=quotes.get(t)
        if not q or q['date']<=order['signal_market_date']:
            kept.append(order); continue
        # Do not simulate fills against quotes that were already historical when
        # the signal was created. Older V5 pending signals use state creation time.
        created_date=order.get('created_utc', book.get('migration_reference_utc', today))[:10]
        if q['date'] < created_date:
            kept.append(order)
            continue
        if (date.fromisoformat(today[:10])-date.fromisoformat(q['date'])).days > MAX_QUOTE_AGE_DAYS:
            kept.append(order)
            continue
        # Revalidate signals on the observed execution close. A stale BUY is canceled.
        if order['action']=='BUY' and not q['entry_signal']:
            continue
        if order['action']=='SELL' and order['reason']=='SMA200' and (
            q['sma200'] is None or q['close'] >= q['sma200']):
            continue
        px=q['close']*q['fx_dkk_per_unit']
        if order['action']=='BUY':
            if t in book['positions'] or len(book['positions'])>=MAX_POSITIONS:
                continue
            if any(issuer(t)==issuer(h) for h in book['positions']): continue
            equity,_=value(book,quotes)
            budget=min(book['cash_dkk'], equity*ENTRY_WEIGHT, equity*MAX_WEIGHT)
            if budget<100: continue
            shares=budget*(1-HALF_FEE)/px
            book['cash_dkk']-=budget
            book['positions'][t]={'shares':shares,'peak_close':q['close'],
                'last_price_dkk':px,'last_quote_date':q['date']}
            amount=budget
        else:
            pos=book['positions'].get(t)
            if not pos: continue
            shares=pos['shares']
            if order['action']=='TRIM':
                equity,_=value(book,quotes)
                target=MAX_WEIGHT*equity/px
                shares=max(0,pos['shares']-target)
                if shares*px<10: continue
            amount=shares*px*(1-HALF_FEE)
            book['cash_dkk']+=amount
            pos['shares']-=shares
            if pos['shares']<1e-9: del book['positions'][t]
        book['fills'].append({'scan_utc':today,'market_date':q['date'],
            'signal_market_date':order['signal_market_date'],
            'signal_created_utc':order.get('created_utc'),
            'fee_dkk':round((budget*HALF_FEE if order['action']=='BUY' else
                             shares*px*HALF_FEE),2),
            'ticker':t,'action':order['action'],'reason':order['reason'],
            'shares':round(shares,8),'paper_value_dkk':round(amount,2),
            'fill_method':'later observed close, hypothetical'})
    book['pending']=kept
    book['fills']=book['fills'][-1000:]

def plan(book, quotes, stop, now):
    equity,stale=value(book,quotes)
    actions=[]; reserved=set()
    seen=book.setdefault('last_signal_dates',{})
    pending_tickers={a['ticker'] for a in book['pending']}
    # Exit and trim signals on the latest known close.
    for t,pos in list(book['positions'].items()):
        q=quotes.get(t)
        if not q or t in pending_tickers or q['date']==seen.get(t): continue
        if (date.fromisoformat(now[:10])-date.fromisoformat(q['date'])).days > MAX_QUOTE_AGE_DAYS: continue
        pos['peak_close']=max(pos['peak_close'],q['close'])
        seen[t]=q['date']
        sma_exit=q['sma200'] is not None and q['close']<q['sma200']
        stop_exit=stop is not None and q['close']<=pos['peak_close']*(1-stop)
        if sma_exit or stop_exit:
            actions.append({'ticker':t,'action':'SELL','reason':'SMA200' if sma_exit else 'TRAILING_STOP',
                'signal_market_date':q['date']})
            reserved.add(t)
        elif pos['shares']*pos['last_price_dkk']/equity>MAX_WEIGHT:
            actions.append({'ticker':t,'action':'TRIM','reason':'WEIGHT_ABOVE_15PCT',
                'signal_market_date':q['date']})
    # Ranking is the SAME 20-day momentum proxy used by V4.2.
    existing_issuers={issuer(t) for t in book['positions']}
    for q in sorted(quotes.values(),key=lambda x: (-(x['momentum20'] if x['momentum20'] is not None else -999),x['ticker'])):
        t=q['ticker']
        if len(book['positions'])+sum(a['action']=='BUY' for a in actions)>=MAX_POSITIONS: break
        if not q['entry_signal'] or q['momentum20'] is None: continue
        if t in pending_tickers or q['date']==seen.get(t): continue
        if (date.fromisoformat(now[:10])-date.fromisoformat(q['date'])).days > MAX_QUOTE_AGE_DAYS: continue
        if issuer(t) in existing_issuers or t in reserved: continue
        if any(a['ticker']==t for a in actions): continue
        actions.append({'ticker':t,'action':'BUY','reason':'ENTRY_SIGNAL',
            'signal_market_date':q['date']})
        seen[t]=q['date']
        existing_issuers.add(issuer(t))
    # Avoid duplicate pending orders; preserve pending until new close arrives.
    for a in actions:
        if a['ticker'] not in pending_tickers:
            book['pending'].append({**a,'created_utc':now})
    return equity,stale,actions

def main():
    cfg=settings()
    stocks,missing=v3.load_data(cfg['tickers'])
    dates=pd.DatetimeIndex(sorted({d for frame in stocks.values() for d in frame.index}))
    fx={}
    for t in list(stocks):
        try:
            fx[t]=v3.fx_series(v3.currency_for(t),dates)
        except Exception as exc:
            missing[t]=str(exc); del stocks[t]
    quotes={}
    for t,frame in stocks.items():
        q=snapshot(t,frame,fx)
        if q: quotes[t]=q
        else: missing[t]='No usable latest quote or FX'
    if not quotes: raise RuntimeError('No usable quotes; refusing to overwrite results')
    state=load_state()
    # Keep the existing V5 state, including all positions, cash, and fill history.
    for book in state['books'].values():
        book.setdefault('migration_reference_utc',state.get('created_utc',datetime.now(timezone.utc).isoformat()))
    now=datetime.now(timezone.utc).isoformat()
    result={'version':'5.1','generated_utc':now,'currency':'DKK',
        'simulation_only':True,'execution':'Signals from latest observed close; paper fills at later observed close, never broker orders',
        'cost_round_trip_pct':ROUND_TRIP_COST*100,'missing_tickers':missing,
        'quote_dates':{t:q['date'] for t,q in quotes.items()},'strategies':{}}
    for key,stop in STRATEGIES.items():
        book=state['books'][key]
        fill_pending(book,quotes,now)
        equity,stale,actions=plan(book,quotes,stop,now)
        today=max(q['date'] for q in quotes.values())
        if not book['equity_history'] or book['equity_history'][-1]['market_date']!=today:
            book['equity_history'].append({'market_date':today,'equity_dkk':round(equity,2)})
        else:
            book['equity_history'][-1]['equity_dkk']=round(equity,2)
        result['strategies'][key]={
            'name':'20% trailing stop' if stop else 'Ingen trailing stop',
            'equity_dkk':round(equity,2),'return_pct':round((equity/INITIAL-1)*100,2),
            'cash_dkk':round(book['cash_dkk'],2),
            'holdings':[{'ticker':t,'shares':round(p['shares'],6),
                'value_dkk':round(p['shares']*p['last_price_dkk'],2),
                'quote_date':p.get('last_quote_date')} for t,p in sorted(book['positions'].items())],
            'new_signals':actions,'hold_signals':[{'ticker':t,'action':'HOLD','signal_market_date':quotes[t]['date']} for t in book['positions'] if t in quotes and not any(a['ticker']==t for a in actions)],'pending':book['pending'],
            'recent_paper_fills':book['fills'][-15:],
            'history':book['equity_history'][-365:],
            'stale_holdings':stale}
    SITE.mkdir(parents=True,exist_ok=True)
    STATE.write_text(json.dumps(state,indent=2,allow_nan=False))
    OUTPUT.write_text(json.dumps(result,indent=2,allow_nan=False))
    print(f'Created {OUTPUT} with {len(quotes)} quotes; no real orders',flush=True)

if __name__=='__main__': main()
