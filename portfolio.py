"""CSV-compatible portfolio calculations extracted from the supplied Streamlit application.

No I/O or web requests in this module. Currency conversion occurs in market.py.
"""
from __future__ import annotations
from datetime import date, datetime
from io import StringIO
import uuid
import numpy as np
import pandas as pd

INPUT_COLS = [
    'trade_date','asset','asset_type','side','quantity','price_trade_ccy',
    'trade_ccy','charges_trade_ccy','stt_trade_ccy','fx_to_base','ticker',
    'last_price_trade_ccy','last_price_base'
]
NUMERIC_COLS = ['quantity','price_trade_ccy','charges_trade_ccy','stt_trade_ccy',
                'fx_to_base','last_price_trade_ccy','last_price_base']
STR_COLS = ['asset','asset_type','side','trade_ccy','ticker']


def read_trades(csv_text: str) -> pd.DataFrame:
    if not csv_text.strip():
        return pd.DataFrame(columns=INPUT_COLS+['trade_id'])
    d = pd.read_csv(StringIO(csv_text), sep=';', dtype={'ticker':'string','asset':'string'},
                    encoding='utf-8-sig', keep_default_na=False)
    required = set(INPUT_COLS[:10])
    missing = required - set(d.columns)
    if missing:
        raise ValueError('Missing CSV columns: '+', '.join(sorted(missing)))
    for col in INPUT_COLS:
        if col not in d.columns:
            d[col] = '' if col in STR_COLS else 0.0
    for col in STR_COLS:
        d[col] = d[col].fillna('').astype(str).str.strip()
    d['side'] = d['side'].str.upper()
    d['trade_ccy'] = d['trade_ccy'].str.upper()
    for col in NUMERIC_COLS:
        d[col] = pd.to_numeric(d[col].astype(str).str.replace(',','.',regex=False), errors='coerce').fillna(0.0)
    dt = pd.to_datetime(d['trade_date'], errors='coerce')
    if dt.isna().any():
        raise ValueError('The CSV contains invalid trade dates.')
    d['trade_date'] = dt
    if (~d.side.isin(['BUY','SELL'])).any():
        raise ValueError('Trade side must be BUY or SELL.')
    if (d.quantity <= 0).any() or (d.price_trade_ccy < 0).any():
        raise ValueError('Quantity must be positive and prices nonnegative.')
    if (d[['charges_trade_ccy','stt_trade_ccy']] < 0).any().any():
        raise ValueError('Fees and tax cannot be negative.')
    if (d.fx_to_base <= 0).any():
        raise ValueError('FX to base must be positive.')
    if (d.asset == '').any() or (d.trade_ccy == '').any():
        raise ValueError('Every trade needs an asset and currency.')
    if (d.asset.str.len()>120).any() or (d.ticker.str.len()>60).any() or (d.trade_ccy.str.len()>4).any():
        raise ValueError('Asset, ticker, or trade-currency field is too long.')
    if 'trade_id' not in d.columns:
        d['trade_id'] = [uuid.uuid4().hex for _ in range(len(d))]
    else:
        d['trade_id'] = d.trade_id.astype(str)
        if (d.trade_id == '').any() or d.trade_id.duplicated().any():
            d['trade_id'] = [uuid.uuid4().hex for _ in range(len(d))]
    return d[INPUT_COLS+['trade_id']].copy()


def to_csv(d: pd.DataFrame, include_ids: bool = True) -> str:
    cols = INPUT_COLS + (['trade_id'] if include_ids else [])
    df = d.copy()
    if df.empty:
        return pd.DataFrame(columns=cols).to_csv(index=False, sep=';')
    df['trade_date'] = pd.to_datetime(df.trade_date).dt.strftime('%Y-%m-%d %H:%M')
    return df[cols].to_csv(index=False, sep=';', float_format='%.10g')


def ensure_valid_trade(item: dict) -> dict:
    val = {k: item.get(k) for k in INPUT_COLS}
    for field in STR_COLS:
        val[field] = str(val[field] or '').strip()
    val['side'] = val['side'].upper()
    val['trade_ccy'] = val['trade_ccy'].upper()
    for field in NUMERIC_COLS:
        try:
            val[field] = float(val[field] if val[field] not in (None,'') else (1 if field=='fx_to_base' else 0))
        except (TypeError, ValueError):
            raise ValueError(f'Invalid value for {field}')
        if not np.isfinite(val[field]):
            raise ValueError(f'Invalid value for {field}')
    try:
        val['trade_date'] = pd.Timestamp(val['trade_date']).strftime('%Y-%m-%d %H:%M')
    except Exception:
        raise ValueError('Invalid date')
    if val['side'] not in ['BUY','SELL']:
        raise ValueError('Side must be BUY or SELL')
    if not val['asset'] or not val['trade_ccy'] or val['quantity'] <= 0 or val['price_trade_ccy'] < 0:
        raise ValueError('Asset, currency, positive quantity and nonnegative price are required')
    if len(val['asset'])>120 or len(val['ticker'])>60 or len(val['trade_ccy'])>4:
        raise ValueError('Asset, ticker, or currency field is too long')
    if val['charges_trade_ccy'] < 0 or val['stt_trade_ccy'] < 0 or val['fx_to_base'] <= 0:
        raise ValueError('Charges and STT must be nonnegative; FX must be positive')
    return val


def derived(d: pd.DataFrame, fx_by_trade: np.ndarray | None = None) -> pd.DataFrame:
    """Two different FIFO unit costs: base economic (with STT) and tax (without STT)."""
    out = d.copy()
    if out.empty:
        return out
    n = out.quantity.to_numpy(dtype=float)
    price = out.price_trade_ccy.to_numpy(dtype=float)
    fees = out.charges_trade_ccy.to_numpy(dtype=float)
    stt = out.stt_trade_ccy.to_numpy(dtype=float)
    fx = fx_by_trade if fx_by_trade is not None else out.fx_to_base.to_numpy(dtype=float)
    buy = (out.side == 'BUY').to_numpy()
    gross = n * price
    out['unit_trade'] = (gross + np.where(buy,fees,-fees))/n
    out['unit_base'] = (gross + np.where(buy,fees+stt,-fees-stt))*fx/n
    out['unit_base_no_stt'] = (gross + np.where(buy,fees,-fees))*fx/n
    return out


def fifo(d: pd.DataFrame, unit_col: str, start: date | None = None,
         end: date | None = None) -> tuple[dict, list[dict], list[str]]:
    """FIFO matching as in supplied script: BUY first for identical timestamps, then row order.

    Realized is aggregated by asset within the selected sale-date range.
    """
    if d.empty:
        return {},[],[]
    temp = d.copy()
    temp['_side_rank'] = np.where(temp.side == 'BUY',0,1)
    temp['_row'] = np.arange(len(temp))
    temp = temp.sort_values(['trade_date','_side_rank','_row'],kind='stable')
    positions, realized, warnings = {},[],[]
    for asset,g in temp.groupby('asset',sort=False):
        lots=[]; total_realized=total_buy=total_sell=sold=0.0
        for row in g.itertuples(index=False):
            n=float(row.quantity); unit=float(getattr(row,unit_col)); dt=row.trade_date.date()
            if row.side == 'BUY':
                lots.append([n,unit]); continue
            left=n
            while left > 1e-9 and lots:
                lot=lots[0]; qty=min(left,lot[0]); left-=qty; lot[0]-=qty
                if (start is None or dt>=start) and (end is None or dt<=end):
                    total_realized += (unit-lot[1])*qty
                    total_buy += lot[1]*qty
                    total_sell += unit*qty
                    sold += qty
                if lot[0] <= 1e-9: lots.pop(0)
            if left>1e-6:
                warnings.append(f'{asset}: sell exceeds earlier buys by {left:.6g} units (unmatched amount excluded from FIFO P/L).')
        rem=sum(lot[0] for lot in lots)
        cost=sum(lot[0]*lot[1] for lot in lots)
        positions[asset]={'quantity':rem,'cost':cost,'avg':cost/rem if rem else 0}
        if sold>1e-9:
            realized.append({'asset':asset,'quantity_sold':sold,'buy_value':total_buy,
                             'sell_value':total_sell,'realized':total_realized,
                             'currency':str(g.trade_ccy.iloc[0])})
    return positions,realized,warnings


def summarize(d: pd.DataFrame, base: str, spot_fx: dict[str,float]) -> dict:
    """Market values use available last-traded prices; never silently infer a missing FX rate."""
    if d.empty:
        return {'holdings':[],'totals':{'value':0,'invested':0,'pnl':0,'pnl_pct':0,'assets':0},'warnings':[]}
    dd=derived(d)
    pb,_,w=fifo(dd,'unit_base')
    pt,_,_=fifo(dd,'unit_trade')
    holdings=[]; warnings=list(dict.fromkeys(w))
    for asset, rec in pb.items():
        if rec['quantity'] <= 1e-6: continue
        rows=d[d.asset==asset]
        row=rows.iloc[-1]  # CSV order; use newest non-empty ticker below
        trade_ccy=str(row.trade_ccy)
        prices=rows[rows.last_price_trade_ccy>0]
        last_price=float(prices.iloc[-1].last_price_trade_ccy) if not prices.empty else None
        latest_ticker=rows[rows.ticker.astype(str).str.len()>0].sort_values('trade_date')
        ticker=str(latest_ticker.iloc[-1].ticker) if not latest_ticker.empty else ''
        rate=spot_fx.get(trade_ccy)
        if rate is None:
            warnings.append(f'No verified {trade_ccy} → {base} FX rate. {asset} excluded from market value.')
        # If no quote: use original app's cost-basis fallback but label clearly.
        est=last_price is None
        trade_cost=pt.get(asset,{}).get('cost',0)
        if est:
            trade_value=trade_cost
            warnings.append(f'{asset}: latest price missing; using trade-currency cost basis as estimate.')
        else:
            trade_value=rec['quantity']*last_price
        base_value=trade_value*rate if rate is not None else None
        # Actual base economic cost FIFO, not using current FX.
        invest=rec['cost']
        gain_trade=trade_value-trade_cost
        holdings.append({'asset':asset,'ticker':ticker,'type':str(row.asset_type),
                         'currency':trade_ccy,'quantity':rec['quantity'],
                         'ltp':last_price, 'avg':pt.get(asset,{}).get('avg',0),
                         'value_trade':trade_value,'invested_trade':trade_cost,
                         'pnl_trade':gain_trade,'pnl_trade_pct':100*gain_trade/trade_cost if trade_cost else 0,
                         'value':base_value,'invested':invest,'pnl':base_value-invest if base_value is not None else None,
                         'estimated':est})
    total=sum(h['value'] or 0 for h in holdings)
    invested=sum(h['invested'] for h in holdings if h['value'] is not None)
    for h in holdings: h['share']=100*(h['value'] or 0)/total if total else 0
    holdings.sort(key=lambda h: h['value'] or 0, reverse=True)
    return {'holdings':holdings,'totals':{'value':total,'invested':invested,
               'pnl':total-invested,'pnl_pct':100*(total-invested)/invested if invested else 0,
               'assets':len(holdings)},'warnings':list(dict.fromkeys(warnings))}


def realized_report(d:pd.DataFrame,base:str,mode:str,start:date,end:date)->dict:
    if mode not in ('base','trade'):raise ValueError('Invalid reporting currency')
    unit='unit_trade' if mode=='trade' else ('unit_base_no_stt' if base.upper()=='INR' else 'unit_base')
    _,rows,warnings=fifo(derived(d),unit,start,end)
    rows.sort(key=lambda r:r['realized'],reverse=True)
    return {'rows':rows,'total':sum(x['realized'] for x in rows) if mode=='base' else None,
            'warnings':list(dict.fromkeys(warnings)), 'mode':mode,'currency':base if mode=='base' else None}
