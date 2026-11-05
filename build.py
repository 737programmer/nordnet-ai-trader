"""Generate a static, read-only iPhone dashboard from daily market scans."""
import json, datetime as dt, os
from pathlib import Path
from bot import scan, settings
ROOT=Path(__file__).resolve().parent
OUT=ROOT/'site'/'signals.json'

def main():
    table, _, errors=scan()
    if table.empty:
        raise SystemExit('Scan failed: refusing to replace last valid dashboard with empty data')
    records=json.loads(table.to_json(orient='records'))
    payload={'generated_utc':dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
             'universe_size':len(settings()['tickers']), 'scanned':len(records),
             'missing_tickers':errors, 'signals':records,
             'disclaimer':'Experimental research signals, not financial advice. No orders or Nordnet account connection.'}
    OUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'Wrote {OUT}: {len(records)} instruments, {len(errors)} missing')
if __name__=='__main__': main()
