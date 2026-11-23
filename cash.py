"""CSV-compatible multi-currency cash & FX calculations extracted from ForexTracker.

No I/O or web requests in this module. Preserves ForexTracker's exact semicolon-delimited
cash_txns.csv schema and average-cost accounting model (with same-day Buy/Credit
processed before Sell/Debit).
"""
from __future__ import annotations

from datetime import date
from io import StringIO
import uuid
import numpy as np
import pandas as pd

CASH_INPUT_COLS = [
    'date', 'foreign_ccy', 'txn_type', 'foreign_amount',
    'fx_rate', 'fee_foreign', 'memo',
]
CASH_NUMERIC_COLS = ['foreign_amount', 'fx_rate', 'fee_foreign']
CASH_STR_COLS = ['foreign_ccy', 'txn_type', 'memo']

_TXN_CANONICAL = {
    'BUY': 'Buy',
    'FX BUY': 'Buy',
    'CREDIT': 'Credit',
    'SELL': 'Sell',
    'FX SELL': 'Sell',
    'DEBIT': 'Debit',
}

CURRENCY_NAMES = {
    'USD': 'US dollar',
    'EUR': 'Euro',
    'CAD': 'Canadian dollar',
    'GBP': 'British pound',
    'GBX': 'British pence',
    'INR': 'Indian rupee',
    'SEK': 'Swedish krona',
    'NOK': 'Norwegian krone',
    'DKK': 'Danish krone',
    'CNY': 'Chinese yuan',
    'JPY': 'Japanese yen',
    'AUD': 'Australian dollar',
    'KRW': 'South Korean won',
    'CHF': 'Swiss franc',
    'NZD': 'New Zealand dollar',
    'SGD': 'Singapore dollar',
    'HKD': 'Hong Kong dollar',
    'PLN': 'Polish zloty',
    'BRL': 'Brazilian real',
    'MXN': 'Mexican peso',
    'ZAR': 'South African rand',
}


def read_cash_txns(csv_text: str) -> pd.DataFrame:
    """Parse semicolon-delimited cash_txns.csv (compatible with ForexTracker)."""
    if not csv_text or not csv_text.strip():
        return pd.DataFrame(columns=CASH_INPUT_COLS + ['txn_id'])
    d = pd.read_csv(
        StringIO(csv_text),
        sep=';',
        dtype={'foreign_ccy': 'string', 'txn_type': 'string', 'memo': 'string'},
        encoding='utf-8-sig',
        keep_default_na=False,
    )
    required = {'date', 'foreign_ccy', 'txn_type', 'foreign_amount', 'fx_rate'}
    missing = required - set(d.columns)
    if missing:
        raise ValueError('Missing cash CSV columns: ' + ', '.join(sorted(missing)))
    for col in CASH_INPUT_COLS:
        if col not in d.columns:
            d[col] = '' if col in CASH_STR_COLS else 0.0
    for col in CASH_STR_COLS:
        d[col] = d[col].fillna('').astype(str).str.strip()
    d['foreign_ccy'] = d['foreign_ccy'].str.upper()
    mapped = d['txn_type'].str.upper().map(_TXN_CANONICAL)
    if mapped.isna().any():
        bad = sorted(set(d.loc[mapped.isna(), 'txn_type'].tolist()))
        raise ValueError('Cash transaction type must be Buy, Sell, Debit, or Credit (got: ' + ', '.join(bad) + ').')
    d['txn_type'] = mapped
    for col in CASH_NUMERIC_COLS:
        d[col] = (
            d[col].astype(str).str.replace(',', '.', regex=False)
            .pipe(pd.to_numeric, errors='coerce')
            .fillna(0.0)
        )
    dt = pd.to_datetime(d['date'], errors='coerce')
    if dt.isna().any():
        raise ValueError('The cash CSV contains invalid transaction dates.')
    d['date'] = dt
    if (d['foreign_ccy'] == '').any() or (d['foreign_ccy'].str.len() > 4).any():
        raise ValueError('Every cash transaction needs a valid 3-4 letter currency code.')
    if (d['foreign_amount'].abs() <= 0).any():
        raise ValueError('Cash transaction amount must be non-zero.')
    if (d['fx_rate'] <= 0).any():
        raise ValueError('Cash transaction FX rate must be positive.')
    if (d['fee_foreign'] < 0).any():
        raise ValueError('Cash exchange fee cannot be negative.')
    if (d['memo'].str.len() > 200).any():
        raise ValueError('Memo field is too long (max 200 characters).')
    inflow = d['txn_type'].isin(['Buy', 'Credit'])
    d['foreign_amount'] = np.where(inflow, d['foreign_amount'].abs(), -d['foreign_amount'].abs())

    if 'txn_id' not in d.columns:
        d['txn_id'] = [uuid.uuid4().hex for _ in range(len(d))]
    else:
        d['txn_id'] = d['txn_id'].astype(str)
        if (d['txn_id'] == '').any() or d['txn_id'].duplicated().any():
            d['txn_id'] = [uuid.uuid4().hex for _ in range(len(d))]
    return d[CASH_INPUT_COLS + ['txn_id']].copy()


def to_cash_csv(d: pd.DataFrame, include_ids: bool = True) -> str:
    cols = CASH_INPUT_COLS + (['txn_id'] if include_ids else [])
    df = d.copy()
    if df.empty:
        return pd.DataFrame(columns=cols).to_csv(index=False, sep=';')
    df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
    return df[cols].to_csv(index=False, sep=';', float_format='%.10g')


def ensure_valid_cash_txn(item: dict) -> dict:
    val = {k: item.get(k) for k in CASH_INPUT_COLS}
    val['foreign_ccy'] = str(val['foreign_ccy'] or '').strip().upper()
    raw_type = str(val['txn_type'] or '').strip().upper()
    if raw_type not in _TXN_CANONICAL:
        raise ValueError('Transaction type must be Buy, Sell, Debit, or Credit')
    val['txn_type'] = _TXN_CANONICAL[raw_type]
    val['memo'] = str(val['memo'] or '').strip()
    if len(val['memo']) > 200:
        raise ValueError('Memo is too long (max 200 characters)')
    if not val['foreign_ccy'] or len(val['foreign_ccy']) > 4:
        raise ValueError('Enter a valid currency code (e.g. USD, EUR, SEK)')
    for field in CASH_NUMERIC_COLS:
        default = 1.0 if field == 'fx_rate' else 0.0
        try:
            num = float(val[field] if val[field] not in (None, '') else default)
        except (TypeError, ValueError):
            raise ValueError(f'Invalid value for {field}')
        if not np.isfinite(num):
            raise ValueError(f'Invalid value for {field}')
        val[field] = num
    try:
        val['date'] = pd.Timestamp(val['date']).strftime('%Y-%m-%d')
    except Exception:
        raise ValueError('Invalid transaction date')
    if abs(val['foreign_amount']) <= 0:
        raise ValueError('Amount must be greater than zero')
    if val['fx_rate'] <= 0:
        raise ValueError('FX rate must be positive')
    if val['fee_foreign'] < 0:
        raise ValueError('Exchange fee cannot be negative')
    amt = abs(val['foreign_amount'])
    val['foreign_amount'] = amt if val['txn_type'] in ('Buy', 'Credit') else -amt
    return val


def recompute_average_cost(trades_df: pd.DataFrame) -> pd.DataFrame:
    """Exact ForexTracker average-cost model per currency.

    Same-date, same-currency rule: Buy/Credit processed BEFORE Sell/Debit so
    same-day inflows update average cost before same-day outflows.
    """
    if trades_df.empty:
        out = trades_df.copy()
        for col in ('base_amount', 'fee_base', 'realized_pl_base', 'running_position',
                    'running_cost_base', 'running_avg_cost', 'sale_proceeds_base', 'sale_cost_base'):
            out[col] = pd.Series(dtype='float64')
        return out

    df = trades_df.copy()
    df['date'] = pd.to_datetime(df['date'], errors='coerce')
    df = df.reset_index(drop=False).rename(columns={'index': '_orig_idx'})
    df['_day'] = df['date'].dt.normalize()

    order_map = {'BUY': 0, 'CREDIT': 0, 'SELL': 1, 'DEBIT': 1}
    df['txn_order'] = df['txn_type'].astype(str).str.upper().map(order_map).fillna(2)
    df = df.sort_values(['_day', 'foreign_ccy', 'txn_order', '_orig_idx'], kind='stable')

    for col in CASH_NUMERIC_COLS:
        if col not in df.columns:
            df[col] = 0.0
        df[col] = (
            df[col].astype(str).str.replace(',', '.', regex=False)
            .pipe(pd.to_numeric, errors='coerce')
            .fillna(0.0)
        )

    df['base_amount'] = df['foreign_amount'] * df['fx_rate']
    df['fee_base'] = df['fee_foreign'] * df['fx_rate']
    df['realized_pl_base'] = 0.0
    df['running_position'] = 0.0
    df['running_cost_base'] = 0.0
    df['running_avg_cost'] = 0.0
    df['sale_proceeds_base'] = 0.0
    df['sale_cost_base'] = 0.0

    positions: dict[str, dict[str, float]] = {}
    for idx, row in df.iterrows():
        ccy = str(row['foreign_ccy']).upper()
        if ccy not in positions:
            positions[ccy] = {'pos': 0.0, 'cost': 0.0}
        pos = positions[ccy]['pos']
        cost = positions[ccy]['cost']

        txn_type = str(row['txn_type']).upper()
        foreign_amount = float(row['foreign_amount'])
        fx_rate = float(row['fx_rate'])
        fee_base = float(row['fee_base'])

        realized_pl = 0.0
        sale_proceeds = 0.0
        sale_cost = 0.0

        if txn_type in ('BUY', 'CREDIT'):
            units = abs(foreign_amount)
            base_amount = units * fx_rate
            cost_increase = base_amount + fee_base
            new_pos = pos + units
            new_cost = cost + cost_increase
        elif txn_type in ('SELL', 'DEBIT'):
            units = abs(foreign_amount)
            avg_cost = (cost / pos) if pos > 0 else 0.0
            sale_cost = avg_cost * units
            sale_proceeds = units * fx_rate - fee_base
            realized_pl = sale_proceeds - sale_cost
            new_pos = pos - units
            new_cost = cost - sale_cost
        else:
            new_pos = pos
            new_cost = cost

        positions[ccy]['pos'] = new_pos
        positions[ccy]['cost'] = new_cost

        df.at[idx, 'realized_pl_base'] = realized_pl
        df.at[idx, 'running_position'] = new_pos
        df.at[idx, 'running_cost_base'] = new_cost
        df.at[idx, 'running_avg_cost'] = (new_cost / new_pos) if abs(new_pos) > 1e-12 else 0.0
        df.at[idx, 'sale_proceeds_base'] = sale_proceeds
        df.at[idx, 'sale_cost_base'] = sale_cost

    return df.drop(columns=['txn_order', '_orig_idx', '_day'])


def summarize_cash(df: pd.DataFrame, base: str, spot_fx: dict[str, float]) -> dict:
    """Summarize active cash balances and unrealized/realized FX P/L in base currency."""
    base_u = base.upper()
    if df.empty:
        return {
            'balances': [],
            'totals': {
                'value': 0.0, 'invested': 0.0, 'pnl': 0.0, 'pnl_pct': 0.0,
                'realized_total': 0.0, 'currencies': 0,
            },
            'warnings': [],
        }
    computed = recompute_average_cost(df)
    all_realized = float(computed['realized_pl_base'].sum())
    latest = computed.groupby('foreign_ccy', as_index=False, sort=False).tail(1)
    realized_by_ccy = computed.groupby('foreign_ccy')['realized_pl_base'].sum().to_dict()

    balances = []
    warnings: list[str] = []
    for _, row in latest.iterrows():
        pos = float(row['running_position'])
        if abs(pos) < 1e-9:
            continue
        ccy = str(row['foreign_ccy']).upper()
        cost_base = float(row['running_cost_base'])
        avg_cost = float(row['running_avg_cost'])
        rate = 1.0 if ccy == base_u else spot_fx.get(ccy)
        if rate is None:
            warnings.append(f'No verified {ccy} → {base_u} FX rate for cash balance; excluded from valuation.')
            val_base = None
            pnl_base = None
            pnl_pct = 0.0
        else:
            val_base = pos * rate
            pnl_base = val_base - cost_base
            pnl_pct = (100.0 * pnl_base / cost_base) if abs(cost_base) > 1e-9 else 0.0
        balances.append({
            'currency': ccy,
            'name': CURRENCY_NAMES.get(ccy, ccy),
            'amount': pos,
            'avg_cost': avg_cost,
            'fx_rate': rate,
            'cost_base': cost_base,
            'value': val_base,
            'pnl': pnl_base,
            'pnl_pct': pnl_pct,
            'realized': float(realized_by_ccy.get(ccy, 0.0)),
            'is_base': ccy == base_u,
        })

    total_val = sum(b['value'] or 0.0 for b in balances)
    total_cost = sum(b['cost_base'] for b in balances if b['value'] is not None)
    total_pnl = total_val - total_cost
    pos_val_sum = sum(max(0.0, b['value'] or 0.0) for b in balances)
    for b in balances:
        v = max(0.0, b['value'] or 0.0)
        b['share'] = (100.0 * v / pos_val_sum) if pos_val_sum > 1e-9 else 0.0

    balances.sort(key=lambda b: b['value'] if b['value'] is not None else -1e18, reverse=True)
    return {
        'balances': balances,
        'totals': {
            'value': total_val,
            'invested': total_cost,
            'pnl': total_pnl,
            'pnl_pct': (100.0 * total_pnl / total_cost) if abs(total_cost) > 1e-9 else 0.0,
            'realized_total': round(all_realized, 2),
            'currencies': len(balances),
        },
        'warnings': list(dict.fromkeys(warnings)),
    }


def realized_cash_report(df: pd.DataFrame, base: str, start: date, end: date) -> list[dict]:
    """Realized FX P/L per currency in [start, end] matching ForexTracker's Realized P/L tab."""
    if df.empty:
        return []
    base_u = base.upper()
    computed = recompute_average_cost(df)
    dates = computed['date'].dt.date
    mask = (dates >= start) & (dates <= end) & computed['txn_type'].isin(['Sell', 'Debit'])
    sub = computed[mask]
    if sub.empty:
        return []
    rows = []
    for ccy, grp in sub.groupby('foreign_ccy', sort=False):
        ccy_u = str(ccy).upper()
        qty = float(grp['foreign_amount'].abs().sum())
        cost = float(grp['sale_cost_base'].sum())
        proceeds = float(grp['sale_proceeds_base'].sum())
        realized = float(grp['realized_pl_base'].sum())
        if ccy_u == base_u and abs(realized) < 1e-6:
            continue
        if qty <= 1e-9:
            continue
        rows.append({
            'asset': ccy_u,
            'asset_name': CURRENCY_NAMES.get(ccy_u, ccy_u),
            'quantity_sold': qty,
            'buy_value': cost,
            'sell_value': proceeds,
            'realized': realized,
            'currency': base_u,
            'foreign_ccy': ccy_u,
            'ticker': '',
            'type': 'Cash',
        })
    rows.sort(key=lambda r: r['realized'], reverse=True)
    return rows
