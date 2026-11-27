"""CSV-compatible portfolio calculations extracted from the supplied Streamlit application.

No I/O or web requests in this module. Currency conversion occurs in market.py.
"""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass
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
        s = d[col]
        if not pd.api.types.is_numeric_dtype(s):
            s = s.astype(str).str.replace(',','.',regex=False)
        d[col] = pd.to_numeric(s, errors='coerce').fillna(0.0)
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
    buy = (out.side.to_numpy() == 'BUY')
    gross = n * price
    out['unit_trade'] = (gross + np.where(buy,fees,-fees))/n
    out['unit_base'] = (gross + np.where(buy,fees+stt,-fees-stt))*fx/n
    out['unit_base_no_stt'] = (gross + np.where(buy,fees,-fees))*fx/n
    return out


def _trade_date_ns(series: pd.Series) -> np.ndarray:
    """Return int64 nanosecond timestamps for fast stable sorting and date comparison."""
    if pd.api.types.is_datetime64_any_dtype(series):
        return series.to_numpy(dtype='datetime64[ns]').view('int64')
    return pd.to_datetime(series).to_numpy(dtype='datetime64[ns]').view('int64')


def _fifo_order_and_arrays(d: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, list, np.ndarray, np.ndarray, list]:
    n_rows = len(d)
    ts_ns = _trade_date_ns(d['trade_date'])
    is_buy = (d['side'].to_numpy() == 'BUY')
    side_rank = np.where(is_buy, 0, 1)
    order = np.lexsort((np.arange(n_rows, dtype=np.int64), side_rank, ts_ns))
    return order, ts_ns, d['asset'].tolist(), is_buy, d['quantity'].to_numpy(dtype=float), d['trade_ccy'].tolist()


def fifo(d: pd.DataFrame, unit_col: str, start: date | None = None,
         end: date | None = None) -> tuple[dict, list[dict], list[str]]:
    """FIFO matching as in supplied script: BUY first for identical timestamps, then row order.

    Realized is aggregated by asset within the selected sale-date range.
    """
    if d.empty:
        return {},[],[]
    order, ts_ns, assets, is_buy, qtys, ccys = _fifo_order_and_arrays(d)
    units = d[unit_col].to_numpy(dtype=float)
    has_range = start is not None or end is not None
    # Compare by calendar day (floor to day in ns = 86_400_000_000_000 ns) matching row.trade_date.date().
    day_ns = 86_400_000_000_000
    start_day = (pd.Timestamp(start).value // day_ns) if start is not None else None
    end_day = (pd.Timestamp(end).value // day_ns) if end is not None else None

    # Per-asset state: [lots_deque, total_realized, total_buy, total_sell, sold, first_ccy, warns_list]
    states: dict[str, list] = {}
    for idx in order:
        asset = assets[idx]
        st = states.get(asset)
        if st is None:
            st = [deque(), 0.0, 0.0, 0.0, 0.0, str(ccys[idx]), []]
            states[asset] = st
        n = float(qtys[idx])
        unit = float(units[idx])
        if is_buy[idx]:
            st[0].append([n, unit])
            continue
        lots = st[0]
        left = n
        if has_range:
            d_day = int(ts_ns[idx]) // day_ns
            in_range = (start_day is None or d_day >= start_day) and (end_day is None or d_day <= end_day)
        else:
            in_range = True
        while left > 1e-9 and lots:
            lot = lots[0]
            qty = left if left < lot[0] else lot[0]
            left -= qty
            lot[0] -= qty
            if in_range:
                st[1] += (unit - lot[1]) * qty
                st[2] += lot[1] * qty
                st[3] += unit * qty
                st[4] += qty
            if lot[0] <= 1e-9:
                lots.popleft()
        if left > 1e-6:
            st[6].append(f'{asset}: sell exceeds earlier buys by {left:.6g} units (unmatched amount excluded from FIFO P/L).')

    positions, realized, warnings = {}, [], []
    for asset, st in states.items():
        lots, total_realized, total_buy, total_sell, sold, first_ccy, asset_warns = st
        if asset_warns:
            warnings.extend(asset_warns)
        rem = sum(lot[0] for lot in lots)
        cost = sum(lot[0] * lot[1] for lot in lots)
        positions[asset] = {'quantity': rem, 'cost': cost, 'avg': cost / rem if rem else 0}
        if sold > 1e-9:
            realized.append({'asset': asset, 'quantity_sold': sold, 'buy_value': total_buy,
                             'sell_value': total_sell, 'realized': total_realized,
                             'currency': first_ccy})
    return positions, realized, warnings


@dataclass(frozen=True)
class OpenAssetRecord:
    asset: str
    ticker: str
    asset_type: str
    trade_ccy: str
    quantity: float
    invested_base: float
    invested_trade: float
    avg_trade: float
    last_price: float | None


@dataclass(frozen=True)
class SummaryState:
    open_assets: tuple[OpenAssetRecord, ...]
    base_warnings: tuple[str, ...]
    realized_total: float
    open_tickers: tuple[str, ...]
    open_currencies: tuple[str, ...]


def _extract_last_prices(d: pd.DataFrame, target_assets: set[str]) -> dict[str, float]:
    """Single linear scan matching `rows[rows.last_price_trade_ccy > 0].iloc[-1]` per asset."""
    out: dict[str, float] = {}
    if d.empty or not target_assets:
        return out
    assets = d['asset'].tolist()
    prices = d['last_price_trade_ccy'].to_numpy(dtype=float)
    for asset, px in zip(assets, prices):
        if asset in target_assets and px > 0:
            out[asset] = float(px)
    return out


def build_summary_state(d: pd.DataFrame) -> SummaryState:
    """Precompute FX-independent FIFO positions, realized total, and per-asset metadata."""
    if d.empty:
        return SummaryState((), (), 0.0, (), ())
    dd = derived(d)
    pb, rb, w = fifo(dd, 'unit_base')
    pt, _, _ = fifo(dd, 'unit_trade')
    realized_total = round(sum(r['realized'] for r in rb), 2)

    open_names = [asset for asset, rec in pb.items() if rec['quantity'] > 1e-6]
    if not open_names:
        return SummaryState((), tuple(dict.fromkeys(w)), realized_total, (), ())
    open_set = set(open_names)

    # Single linear scan in CSV order to collect last row fields, last positive price, and newest ticker.
    assets = d['asset'].tolist()
    ccys = d['trade_ccy'].tolist()
    types = d['asset_type'].tolist()
    tickers = d['ticker'].tolist()
    prices = d['last_price_trade_ccy'].to_numpy(dtype=float)
    ts_ns = _trade_date_ns(d['trade_date'])

    last_ccy: dict[str, str] = {}
    last_type: dict[str, str] = {}
    last_price: dict[str, float] = {}
    best_ticker: dict[str, tuple[int, str]] = {}

    for i, asset in enumerate(assets):
        if asset not in open_set:
            continue
        last_ccy[asset] = str(ccys[i])
        last_type[asset] = str(types[i])
        px = float(prices[i])
        if px > 0:
            last_price[asset] = px
        tk = str(tickers[i]) if tickers[i] is not None else ''
        if tk:
            t_val = int(ts_ns[i])
            prev = best_ticker.get(asset)
            if prev is None or t_val >= prev[0]:
                best_ticker[asset] = (t_val, tk)

    records: list[OpenAssetRecord] = []
    symbols: set[str] = set()
    open_ccys: list[str] = []
    seen_ccys: set[str] = set()
    for asset in open_names:
        rec = pb[asset]
        t_rec = pt.get(asset)
        tk = best_ticker[asset][1] if asset in best_ticker else ''
        if tk:
            symbols.add(tk)
        t_ccy = last_ccy[asset]
        if t_ccy and t_ccy not in seen_ccys:
            seen_ccys.add(t_ccy)
            open_ccys.append(t_ccy)
        records.append(OpenAssetRecord(
            asset=asset,
            ticker=tk,
            asset_type=last_type[asset],
            trade_ccy=t_ccy,
            quantity=rec['quantity'],
            invested_base=rec['cost'],
            invested_trade=t_rec['cost'] if t_rec else 0,
            avg_trade=t_rec['avg'] if t_rec else 0,
            last_price=last_price.get(asset),
        ))
    return SummaryState(
        open_assets=tuple(records),
        base_warnings=tuple(dict.fromkeys(w)),
        realized_total=realized_total,
        open_tickers=tuple(sorted(symbols)),
        open_currencies=tuple(open_ccys),
    )


def refresh_summary_state_prices(state: SummaryState, d: pd.DataFrame) -> SummaryState:
    """Clone SummaryState with updated last_price_trade_ccy from d without re-running FIFO."""
    if not state.open_assets:
        return state
    target = {rec.asset for rec in state.open_assets}
    latest_px = _extract_last_prices(d, target)
    updated = tuple(
        OpenAssetRecord(
            asset=rec.asset,
            ticker=rec.ticker,
            asset_type=rec.asset_type,
            trade_ccy=rec.trade_ccy,
            quantity=rec.quantity,
            invested_base=rec.invested_base,
            invested_trade=rec.invested_trade,
            avg_trade=rec.avg_trade,
            last_price=latest_px.get(rec.asset),
        )
        for rec in state.open_assets
    )
    return SummaryState(
        open_assets=updated,
        base_warnings=state.base_warnings,
        realized_total=state.realized_total,
        open_tickers=state.open_tickers,
        open_currencies=state.open_currencies,
    )


def summarize_from_state(state: SummaryState, base: str, spot_fx: dict[str,float]) -> dict:
    """Evaluate current holdings and totals from precomputed SummaryState and spot FX."""
    if not state.open_assets and not state.base_warnings:
        return {'holdings':[],'totals':{'value':0,'invested':0,'pnl':0,'pnl_pct':0,'assets':0},'warnings':[]}
    holdings = []
    warnings = list(state.base_warnings)
    for rec in state.open_assets:
        trade_ccy = rec.trade_ccy
        rate = spot_fx.get(trade_ccy)
        if rate is None:
            warnings.append(f'No verified {trade_ccy} → {base} FX rate. {rec.asset} excluded from market value.')
        last_price = rec.last_price
        est = last_price is None
        trade_cost = rec.invested_trade
        if est:
            trade_value = trade_cost
            warnings.append(f'{rec.asset}: latest price missing; using trade-currency cost basis as estimate.')
        else:
            trade_value = rec.quantity * last_price
        base_value = trade_value * rate if rate is not None else None
        invest = rec.invested_base
        gain_trade = trade_value - trade_cost
        holdings.append({'asset':rec.asset,'ticker':rec.ticker,'type':rec.asset_type,
                         'currency':trade_ccy,'quantity':rec.quantity,
                         'ltp':last_price, 'avg':rec.avg_trade,
                         'value_trade':trade_value,'invested_trade':trade_cost,
                         'pnl_trade':gain_trade,'pnl_trade_pct':100*gain_trade/trade_cost if trade_cost else 0,
                         'value':base_value,'invested':invest,'pnl':base_value-invest if base_value is not None else None,
                         'estimated':est})
    total = sum(h['value'] or 0 for h in holdings)
    invested = sum(h['invested'] for h in holdings if h['value'] is not None)
    for h in holdings:
        h['share'] = 100 * (h['value'] or 0) / total if total else 0
    holdings.sort(key=lambda h: h['value'] or 0, reverse=True)
    return {'holdings':holdings,'totals':{'value':total,'invested':invested,
               'pnl':total-invested,'pnl_pct':100*(total-invested)/invested if invested else 0,
               'assets':len(holdings)},'warnings':list(dict.fromkeys(warnings))}


def summarize(d: pd.DataFrame, base: str, spot_fx: dict[str,float]) -> dict:
    """Market values use available last-traded prices; never silently infer a missing FX rate."""
    if d.empty:
        return {'holdings':[],'totals':{'value':0,'invested':0,'pnl':0,'pnl_pct':0,'assets':0},'warnings':[]}
    return summarize_from_state(build_summary_state(d), base, spot_fx)


def realized_report(d:pd.DataFrame,base:str,mode:str,start:date,end:date)->dict:
    if mode not in ('base','trade'):raise ValueError('Invalid reporting currency')
    unit='unit_trade' if mode=='trade' else ('unit_base_no_stt' if base.upper()=='INR' else 'unit_base')
    _,rows,warnings=fifo(derived(d),unit,start,end)
    rows.sort(key=lambda r:r['realized'],reverse=True)
    return {'rows':rows,'total':sum(x['realized'] for x in rows) if mode=='base' else None,
            'warnings':list(dict.fromkeys(warnings)), 'mode':mode,'currency':base if mode=='base' else None}
