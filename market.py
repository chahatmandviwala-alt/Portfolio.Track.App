"""Portfolio Track market-data service.

Drop-in replacement for the original market.py: preserves public function signatures,
calculation methods, warning semantics and market_cache table layout.

Optimizations: batch cache queries/UPSERTs, reuse overlapping historic date ranges,
refresh recent dates instead of expiring all old history, keep small bounded RAM
caches, prevent slow NSE failures from delaying every refresh, and limit Yahoo
fallback timeouts. External quote services are best effort, not real-time feeds.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests
from sqlalchemy import select
from storage import MarketCache, Session, engine

_LOG = logging.getLogger('uvicorn.error')
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; PortfolioTracker/1.0)', 'Accept': 'application/json'}
_HIST_REFRESH_HOURS = max(1., float(os.getenv('HISTORICAL_REFRESH_HOURS', '12')))
_FX_REFRESH_HOURS = max(1., float(os.getenv('FX_REFRESH_HOURS', '12')))
_QUOTE_TTL = max(0, int(os.getenv('QUOTE_CACHE_SECONDS', '30')))
_NSE_COOLDOWN = max(60, int(os.getenv('NSE_COOLDOWN_SECONDS', '1800')))
_MAX_WORKERS = max(2, min(12, int(os.getenv('MARKET_FETCH_WORKERS', '8'))))
# Yahoo-first is much faster when NSE blocks cloud IPs. Set PRICE_SOURCE_PRIORITY=nse
# to restore NSE-first behavior (at the expense of slower cloud refreshes).
_PREFER_NSE = os.getenv('PRICE_SOURCE_PRIORITY', 'yahoo').strip().lower() == 'nse'

@dataclass
class _CacheRecord:
    start: date
    end: date
    fetched: datetime
    values: dict

_cache_lock = threading.RLock()
_local_rows: OrderedDict[str, tuple[float, _CacheRecord]] = OrderedDict()
_quote_rows: OrderedDict[str, tuple[float, dict]] = OrderedDict()
_chart_rows: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
_nse_block_until = 0.0
_http_local = threading.local()


def _http() -> requests.Session:
    """Per-thread persistent HTTP connections; requests.Session is not thread-safe."""
    session = getattr(_http_local, 'session', None)
    if session is None:
        session = requests.Session()
        session.headers.update(HEADERS)
        adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=8)
        session.mount('https://', adapter)
        _http_local.session = session
    return session


def _read_cached(keys: list[str]) -> dict[str, _CacheRecord]:
    """One SQL query for *all* ticker/FX cache rows; short RAM cache between calls."""
    if not keys:
        return {}
    now = time.monotonic()
    found, missing = {}, []
    with _cache_lock:
        for key in dict.fromkeys(keys):
            hit = _local_rows.get(key)
            if hit and hit[0] > now:
                found[key] = hit[1]
                _local_rows.move_to_end(key)
            else:
                missing.append(key)
    if missing:
        with Session() as db:
            records = db.scalars(select(MarketCache).where(MarketCache.cache_key.in_(missing))).all()
            for item in records:
                try:
                    fetched = item.fetched_at
                    if fetched.tzinfo is None:
                        fetched = fetched.replace(tzinfo=timezone.utc)
                    record = _CacheRecord(date.fromisoformat(item.start_date),
                                          date.fromisoformat(item.end_date), fetched,
                                          json.loads(item.csv_text))
                    if not isinstance(record.values, dict):
                        continue
                    found[item.cache_key] = record
                except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
                    continue
        with _cache_lock:
            for key in missing:
                if key in found:
                    _local_rows[key] = (now + 60, found[key])
                    _local_rows.move_to_end(key)
            while len(_local_rows) > 350:
                _local_rows.popitem(last=False)
    return found


def _write_cached(rows: dict[str, _CacheRecord]) -> None:
    """Write an entire batch in one transaction and one UPSERT statement.

    Works on both SQLite for local testing and Supabase PostgreSQL. The table's
    schema remains unchanged. No migration is required.
    """
    if not rows:
        return
    values = [{
        'cache_key': key, 'start_date': item.start.isoformat(),
        'end_date': item.end.isoformat(),
        'csv_text': json.dumps(item.values, separators=(',', ':')),
        'fetched_at': item.fetched,
    } for key, item in rows.items()]
    if engine.dialect.name == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
    elif engine.dialect.name == 'sqlite':
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError('Unsupported market cache database backend')
    stmt = insert(MarketCache).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[MarketCache.cache_key],
        set_={field: getattr(stmt.excluded, field) for field in
              ('start_date', 'end_date', 'csv_text', 'fetched_at')},
    )
    with Session.begin() as db:
        db.execute(stmt)
    with _cache_lock:
        now = time.monotonic()
        for key, item in rows.items():
            _local_rows[key] = (now + 60, item)
            _local_rows.move_to_end(key)
        while len(_local_rows) > 350:
            _local_rows.popitem(last=False)


def _get_cached(key: str, start: date, end: date, ttl_hours: float = 18) -> dict | None:
    """Compatibility helper used by older code/tests."""
    item = _read_cached([key]).get(key)
    if not item or item.start > start or item.end < end:
        return None
    if (datetime.now(timezone.utc) - item.fetched).total_seconds() > ttl_hours * 3600:
        return None
    return dict(item.values)


def _put_cached(key: str, start: date, end: date, rows: dict):
    _write_cached({key: _CacheRecord(start, end, datetime.now(timezone.utc), dict(rows))})


def _fetch_windows(entry: _CacheRecord | None, start: date, end: date,
                   fresh_hours: float, force: bool) -> list[tuple[date, date]]:
    if force or entry is None:
        return [(start, end)]
    # A cached empty result may represent a bad or delisted ticker. Recheck it
    # periodically rather than contacting the provider on *every* chart load.
    if not entry.values:
        age = (datetime.now(timezone.utc) - entry.fetched).total_seconds()
        return [(start, end)] if age > 180 else []
    windows = []
    if start < entry.start:
        windows.append((start, min(end, entry.start - timedelta(days=1))))
    if end > entry.end:
        windows.append((max(start, entry.end + timedelta(days=1)), end))
    age_hours = (datetime.now(timezone.utc) - entry.fetched).total_seconds() / 3600
    if age_hours >= fresh_hours and end >= date.today() - timedelta(days=14):
        # Old closes and old FX rates don't expire; refresh only recent dates.
        windows.append((max(start, end - timedelta(days=10)), end))
    # Coalesce overlap between "extend missing dates" and "refresh recent dates".
    windows = sorted((a, b) for a, b in windows if a <= b)
    combined = []
    for a, b in windows:
        if combined and a <= combined[-1][1] + timedelta(days=1):
            combined[-1] = (combined[-1][0], max(b, combined[-1][1]))
        else:
            combined.append((a, b))
    return combined


def _merge_cache(entry: _CacheRecord | None, windows: list[tuple[date, date]],
                 results: list[dict], stamp: datetime) -> _CacheRecord:
    # The single-row schema stores one contiguous coverage interval. Do not
    # incorrectly claim that a gap between two disjoint requests was fetched.
    # Prefer retaining the new range over inventing missing historical dates.
    if entry is not None and windows and (
        max(b for _, b in windows) < entry.start - timedelta(days=1) or
        min(a for a, _ in windows) > entry.end + timedelta(days=1)
    ):
        entry = None
    values = dict(entry.values) if entry else {}
    for chunk in results:
        values.update(chunk)
    starts = [w[0] for w in windows]
    ends = [w[1] for w in windows]
    if entry is not None:
        starts.append(entry.start)
        ends.append(entry.end)
    return _CacheRecord(min(starts), max(ends), stamp, values)


def _yahoo_chart(ticker: str, params: dict) -> dict:
    """Try both public Yahoo chart hosts with bounded connect/read timeouts."""
    last_error = None
    for host in ('query2.finance.yahoo.com', 'query1.finance.yahoo.com'):
        try:
            url = f'https://{host}/v8/finance/chart/{requests.utils.quote(ticker, safe="")}'
            response = _http().get(url, params=params, timeout=(2.5, 5.5))
            # Trying another host after a 429 usually worsens rate-limiting.
            if response.status_code == 429:
                raise ValueError('Yahoo Finance rate limited requests (HTTP 429)')
            response.raise_for_status()
            payload = response.json().get('chart') or {}
            results = payload.get('result') or []
            if results:
                return results[0]
            last_error = ValueError('No chart data returned')
        except (requests.RequestException, ValueError, KeyError) as exc:
            last_error = exc
            if 'rate limited' in str(exc).lower():
                break
    raise ValueError(f'Yahoo quote unavailable: {type(last_error).__name__}')


def _positive_price(value) -> float | None:
    try:
        price = float(str(value).replace(',', ''))
    except (TypeError, ValueError):
        return None
    return round(price, 8) if np.isfinite(price) and price > 0 else None


def _fetch_nse_quote(ticker: str) -> dict:
    """NSE live quote. A cloud-host block activates a short circuit breaker."""
    global _nse_block_until
    with _cache_lock:
        if time.monotonic() < _nse_block_until:
            raise ValueError('NSE temporarily skipped after provider failures')
    symbol = ticker[:-3].upper()
    if not symbol or not all(c.isalnum() or c in '-&_' for c in symbol):
        raise ValueError('Invalid NSE ticker')
    page = f'https://www.nseindia.com/get-quotes/equity?symbol={requests.utils.quote(symbol)}'
    headers = {
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': page,
    }
    try:
        with requests.Session() as session:
            session.headers.update(headers)
            session.get('https://www.nseindia.com/', timeout=(2, 4)).raise_for_status()
            response = session.get('https://www.nseindia.com/api/quote-equity',
                                   params={'symbol': symbol}, timeout=(2, 4))
            response.raise_for_status()
            data = response.json()
    except (requests.RequestException, ValueError) as exc:
        with _cache_lock:
            _nse_block_until = time.monotonic() + _NSE_COOLDOWN
        raise
    returned_symbol = str((data.get('info') or {}).get('symbol') or symbol)
    if returned_symbol.upper() != symbol:
        raise ValueError('NSE returned a different symbol')
    price = _positive_price((data.get('priceInfo') or {}).get('lastPrice'))
    if price is None:
        raise ValueError('NSE returned no valid last traded price')
    return {'price': price, 'source': 'NSE India',
            'as_of': str((data.get('metadata') or {}).get('lastUpdateTime') or '')}


def _fetch_yahoo_latest(ticker: str) -> dict:
    """Use intraday candles/metadata, NOT a historical daily close."""
    info = _yahoo_chart(ticker, {'range': '5d', 'interval': '5m', 'includePrePost': 'false'})
    meta = info.get('meta') or {}
    if ticker.upper().endswith('.NS') and str(meta.get('currency', '')).upper() != 'INR':
        raise ValueError('Yahoo returned a non-INR quote for an NSE stock')
    price = _positive_price(meta.get('regularMarketPrice'))
    if price is None:
        candles = ((info.get('indicators') or {}).get('quote') or [{}])[0].get('close') or []
        price = next((_positive_price(x) for x in reversed(candles) if _positive_price(x)), None)
    if price is None:
        raise ValueError('Yahoo returned no valid recent quote')
    stamp = meta.get('regularMarketTime')
    if stamp:
        try:
            as_of = datetime.fromtimestamp(int(stamp), timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            as_of = ''
    else:
        timestamps = info.get('timestamp') or []
        as_of = datetime.fromtimestamp(timestamps[-1], timezone.utc).isoformat() if timestamps else ''
    return {'price': price, 'source': 'Yahoo Finance intraday', 'as_of': as_of}


def get_latest_prices(tickers: list[str]) -> tuple[dict, dict]:
    """Latest traded quotes, preserving original (quotes, errors) API.

    Yahoo-first is default on free hosting. NSE is used when Yahoo fails or when
    PRICE_SOURCE_PRIORITY=nse. Store the source and as_of from the provider.
    """
    names = list(dict.fromkeys(t.strip() for t in tickers if t and t.strip()))
    quotes, errors, missing = {}, {}, []
    now = time.monotonic()
    with _cache_lock:
        for ticker in names:
            cached = _quote_rows.get(ticker)
            if cached and cached[0] > now:
                quotes[ticker] = dict(cached[1])
            else:
                missing.append(ticker)

    def fetch(ticker):
        causes = []
        methods = ([_fetch_nse_quote, _fetch_yahoo_latest] if _PREFER_NSE
                   else [_fetch_yahoo_latest, _fetch_nse_quote]) if ticker.upper().endswith('.NS') else [_fetch_yahoo_latest]
        for method in methods:
            try:
                return method(ticker)
            except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                causes.append(f'{method.__name__} ({type(exc).__name__})')
        raise ValueError(' and '.join(causes) + ' unavailable')

    if missing:
        begin = time.perf_counter()
        with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(missing))) as pool:
            futures = {pool.submit(fetch, ticker): ticker for ticker in missing}
            for future in as_completed(futures):
                ticker = futures[future]
                try:
                    quote = future.result()
                    quotes[ticker] = quote
                    if _QUOTE_TTL:
                        with _cache_lock:
                            _quote_rows[ticker] = (time.monotonic() + _QUOTE_TTL, dict(quote))
                            _quote_rows.move_to_end(ticker)
                            while len(_quote_rows) > 200:
                                _quote_rows.popitem(last=False)
                except Exception as exc:
                    errors[ticker] = str(exc)
        _LOG.info('market.quote_refresh seconds=%.2f fetched=%d errors=%d cache_hits=%d',
                  time.perf_counter()-begin, len(missing), len(errors), len(names)-len(missing))
    return quotes, errors


def _fetch_chart(ticker: str, start: date, end: date) -> dict:
    start_epoch = int(pd.Timestamp(start, tz='UTC').timestamp())
    end_epoch = int((pd.Timestamp(end, tz='UTC') + pd.Timedelta(days=2)).timestamp())
    info = _yahoo_chart(ticker, {'period1': start_epoch, 'period2': end_epoch,
                                 'interval': '1d', 'events': 'history'})
    ts = info.get('timestamp') or []
    closes = (info.get('indicators', {}).get('quote') or [{}])[0].get('close') or []
    utc_offset = int(info.get('meta', {}).get('gmtoffset', 0) or 0)
    output = {}
    for t, v in zip(ts, closes):
        if v is None:
            continue
        x = float(v)
        if np.isfinite(x) and x > 0:
            day = datetime.fromtimestamp(int(t) + utc_offset, tz=timezone.utc).date().isoformat()
            output[day] = round(x, 8)
    return output


def _series_with_cache(keys: dict[str, str], start: date, end: date, force: bool,
                       refresh_hours: float, provider) -> tuple[dict, dict]:
    """Generic batch fetch for prices and FX. Fetch only uncovered/recent windows."""
    result, errors = {}, {}
    lookup = _read_cached(list(keys.values()))
    jobs: dict = {}
    for symbol, key in keys.items():
        item = lookup.get(key)
        windows = _fetch_windows(item, start, end, refresh_hours, force)
        if item is not None:
            if item.values:
                result[symbol] = dict(item.values)
            else:
                errors[symbol] = 'No historical data available (cached provider failure)'
        if windows:
            jobs[symbol] = (key, item, windows)
        elif item is None:
            jobs[symbol] = (key, None, [(start, end)])
    if jobs:
        begin = time.perf_counter()
        future_map = {}
        with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, max(1, len(jobs)))) as pool:
            for symbol, (_, _, windows) in jobs.items():
                for a, b in windows:
                    fut = pool.submit(provider, symbol, a, b)
                    future_map[fut] = (symbol, a, b)
            completed: dict[str, list] = {x: [] for x in jobs}
            failed: dict[str, bool] = {x: False for x in jobs}
            for fut in as_completed(future_map):
                symbol, a, b = future_map[fut]
                try:
                    chunk = fut.result()
                    if not isinstance(chunk, dict):
                        raise TypeError('provider did not return a dictionary')
                    # A completely empty response spanning business days is
                    # commonly a transient API failure, not a successful update.
                    # Never mark the whole gap as covered in that situation.
                    if not chunk and jobs[symbol][1] is not None and jobs[symbol][1].values and len(pd.bdate_range(a, b)) > 0:
                        raise ValueError('No refreshed business-day data; retaining prior prices')
                    completed[symbol].append(((a, b), chunk))
                except Exception as exc:
                    failed[symbol] = True
                    errors[symbol] = f'Market data unavailable ({type(exc).__name__})'
        to_save = {}
        stamp = datetime.now(timezone.utc)
        for symbol, (key, entry, windows) in jobs.items():
            parts = completed[symbol]
            if failed[symbol] or len(parts) != len(windows):
                # Briefly negative-cache a completely unavailable symbol to stop
                # outages/invalid tickers from blocking every chart request.
                if entry is None:
                    to_save[key] = _CacheRecord(start, end, stamp, {})
                continue  # Do not mark missing dates covered after network failure.
            parts.sort(key=lambda part: part[0][0])
            updated = _merge_cache(entry, [p[0] for p in parts], [p[1] for p in parts], stamp)
            to_save[key] = updated
            if updated.values:
                result[symbol] = dict(updated.values)
                errors.pop(symbol, None)
            else:
                errors[symbol] = 'No historical quotes available'
        try:
            _write_cached(to_save)
        except Exception:
            # A healthy external response should still be usable for this request.
            _LOG.exception('Could not persist market cache batch')
        _LOG.info('market.historical_fetch seconds=%.2f symbols=%d windows=%d errors=%d',
                  time.perf_counter()-begin, len(jobs), len(future_map), len(errors))
    return result, errors


def get_prices(tickers: list[str], start: date, end: date, force: bool = False) -> tuple[dict, dict]:
    names = list(dict.fromkeys(x for x in tickers if x))
    return _series_with_cache({t: 'price:' + t for t in names}, start, end,
                              force, _HIST_REFRESH_HOURS, _fetch_chart)


def _fetch_fx_series(ccy: str, base: str, start: date, end: date) -> dict:
    if ccy == base:
        return {}
    src = 'GBP' if ccy == 'GBX' else ccy
    dest = 'GBP' if base == 'GBX' else base
    if src == dest:
        factor = (0.01 if ccy == 'GBX' else 1)/(0.01 if base == 'GBX' else 1)
        return {str(d.date()): factor for d in pd.date_range(start, end)}
    url = f'https://api.frankfurter.dev/v1/{start.isoformat()}..{end.isoformat()}'
    response = _http().get(url, params={'base': src, 'symbols': dest}, timeout=(2.5, 7))
    response.raise_for_status()
    raw = response.json().get('rates', {})
    factor = (0.01 if ccy == 'GBX' else 1)/(0.01 if base == 'GBX' else 1)
    return {day: float(values[dest])*factor for day, values in raw.items() if dest in values}


def get_fx_series(currencies: list[str], base: str, start: date, end: date,
                  force=False) -> tuple[dict, dict]:
    names = list(dict.fromkeys(currencies))
    keys = {ccy: f'fx:{ccy}:{base}' for ccy in names if ccy != base}
    output, errors = _series_with_cache(
        keys, start, end, force, _FX_REFRESH_HOURS,
        lambda ccy, a, b: _fetch_fx_series(ccy, base, a, b))
    if base in names:
        output[base] = {start.isoformat(): 1.0}
    return output, errors


def spot_rates(currencies: list[str], base: str) -> tuple[dict[str, float], list[str]]:
    today = date.today()
    series, _ = get_fx_series(currencies, base, today-timedelta(days=8), today)
    rates, warnings = {}, []
    for ccy in currencies:
        if ccy == base:
            rates[ccy] = 1.0
        elif series.get(ccy):
            dates = [d for d in series[ccy] if d <= today.isoformat()]
            if dates:
                rates[ccy] = series[ccy][max(dates)]
            else:
                warnings.append(f'Latest FX unavailable: {ccy} → {base} (not converted).')
        else:
            warnings.append(f'Latest FX unavailable: {ccy} → {base} (not converted).')
    return rates, warnings


def trade_fx_rates(df: pd.DataFrame, base: str, preserve_legacy_sek: bool = False) -> tuple[np.ndarray, list[str]]:
    """Original transaction-date FX accounting, retained for compatibility."""
    if df.empty:
        return np.array([]), []
    if base == 'SEK' and preserve_legacy_sek:
        return df.fx_to_base.to_numpy(dtype=float), []
    start = df.trade_date.min().date() - timedelta(days=6)
    end = min(date.today(), df.trade_date.max().date() + timedelta(days=1))
    currencies = df.trade_ccy.unique().tolist()
    series, errors = get_fx_series(currencies, base, start, end)
    output = np.full(len(df), np.nan)
    warns = []
    by_ccy = {}
    for ccy in currencies:
        if ccy == base:
            by_ccy[ccy] = pd.Series(1.0, index=pd.date_range(start, end))
        elif ccy in series:
            vals = pd.Series(series[ccy], dtype='float64')
            vals.index = pd.to_datetime(vals.index)
            by_ccy[ccy] = vals.reindex(pd.date_range(start, end)).ffill().bfill()
        else:
            warns.append(f'Cannot calculate historical {ccy} → {base} FX; base-cost calculations unavailable.')
    dates = df.trade_date.dt.normalize()
    for ccy, series in by_ccy.items():
        mask = (df.trade_ccy == ccy).to_numpy()
        output[mask] = series.reindex(dates[mask]).to_numpy(dtype=float)
    return output, warns


def portfolio_history(df: pd.DataFrame, base: str, span: str) -> dict:
    """Memoize a calculated chart briefly; cache key covers all user trade data.

    Chart calculations themselves are unchanged (in _calculate_portfolio_history).
    The key is a content hash, NOT an unscoped per-user cache entry: a changed
    transaction or last price automatically invalidates it.
    """
    if span not in ('1M', '3M', '6M', '1Y', '3Y', 'MAX'):
        raise ValueError('Unsupported range')
    try:
        digest = hashlib.blake2b(pd.util.hash_pandas_object(df, index=True).values.tobytes(),
                                 digest_size=16).hexdigest()
    except (TypeError, ValueError):
        return _calculate_portfolio_history(df, base, span)
    key = (base, span, date.today().isoformat(), digest)
    with _cache_lock:
        hit = _chart_rows.get(key)
        if hit and hit[0] > time.monotonic():
            _chart_rows.move_to_end(key)
            return copy.deepcopy(hit[1])
    result = _calculate_portfolio_history(df, base, span)
    with _cache_lock:
        _chart_rows[key] = (time.monotonic() + 120, copy.deepcopy(result))
        while len(_chart_rows) > 30:
            _chart_rows.popitem(last=False)
    return result


def _calculate_portfolio_history(df: pd.DataFrame, base: str, span: str) -> dict:
    """Daily account market value; exact original vectorized valuation model."""
    if span not in ('1M','3M','6M','1Y','3Y','MAX'): raise ValueError('Unsupported range')
    if df.empty: return {'points': [], 'warnings': [], 'estimated_assets': [], 'range': span}
    end = date.today()
    first = df.trade_date.min().date()
    days = {'1M':31,'3M':92,'6M':183,'1Y':366,'3Y':1096,'MAX':999999}[span]
    start = first if span == 'MAX' else max(first, end-timedelta(days=days))
    if start > end: return {'points': [], 'warnings': [], 'estimated_assets': [], 'range': span}
    idx = pd.date_range(start, end, freq='D')
    delta = df[['trade_date','asset','side','quantity']].copy()
    delta['trade_date'] = delta.trade_date.dt.normalize()
    delta['signed_qty'] = delta.quantity * np.where(delta.side == 'BUY',1,-1)
    changes = delta.pivot_table(index='trade_date', columns='asset', values='signed_qty',
                                aggfunc='sum', fill_value=0)
    changes = changes.reindex(pd.date_range(first,end), fill_value=0).cumsum().clip(lower=0).reindex(idx).ffill().fillna(0)
    assets = list(changes.columns)
    meta = {}
    tickers = []
    ccys = []
    for asset,g in df.groupby('asset',sort=False):
        if asset not in changes.columns: continue
        g = g.sort_values('trade_date',kind='stable')
        latest = g[g.ticker.astype(str).str.len()>0]
        ticker = str(latest.iloc[-1].ticker) if not latest.empty else ''
        ccy = str(g.iloc[-1].trade_ccy)
        meta[asset] = {'ticker': ticker, 'ccy': ccy}
        if ticker: tickers.append(ticker)
        ccys.append(ccy)
    market_start = start - timedelta(days=10)
    begin = time.perf_counter()
    prices,price_errors = get_prices(tickers,market_start,end)
    prices_elapsed = time.perf_counter()-begin
    fxs,fx_errors = get_fx_series(ccys,base,market_start,end)
    fx_elapsed = time.perf_counter()-begin-prices_elapsed
    trade_price_by_asset = {}
    for asset,g in df.groupby('asset'):
        purchase = g[['trade_date','price_trade_ccy']].copy()
        purchase['trade_date'] = purchase.trade_date.dt.normalize()
        purchase = purchase.drop_duplicates('trade_date',keep='last').set_index('trade_date')['price_trade_ccy']
        trade_price_by_asset[asset] = purchase
    mat = np.zeros((len(idx),len(assets)),dtype='float64')
    unknown_assets=[];estimated_assets=[];warnings=[]
    for j,asset in enumerate(assets):
        m=meta[asset]
        raw=pd.Series(prices.get(m['ticker'],{}),dtype='float64') if m['ticker'] else pd.Series(dtype='float64')
        if not raw.empty:raw.index=pd.to_datetime(raw.index)
        market_prices=raw.reindex(idx).ffill()
        if not raw.empty:
            market_prices=raw.reindex(raw.index.union(idx)).sort_index().ffill().reindex(idx)
        trade_px=trade_price_by_asset[asset]
        trade_px=trade_px.reindex(trade_px.index.union(idx)).sort_index().ffill().reindex(idx)
        uses_proxy=market_prices.isna() & trade_px.notna() & (changes[asset]>0)
        if uses_proxy.any():estimated_assets.append(asset)
        px=market_prices.fillna(trade_px)
        if (px.isna() & (changes[asset]>0)).any():unknown_assets.append(asset)
        fxdata=fxs.get(m['ccy'],{})
        if m['ccy']==base:
            fx=pd.Series(1.0,index=idx)
        elif fxdata:
            rate=pd.Series(fxdata,dtype='float64');rate.index=pd.to_datetime(rate.index)
            fx=rate.reindex(rate.index.union(idx)).sort_index().ffill().reindex(idx)
            fx=fx.bfill()
        else:
            tx=df[df.asset==asset][['trade_date','fx_to_base']].copy()
            tx['trade_date']=tx.trade_date.dt.normalize()
            tx=tx.drop_duplicates('trade_date',keep='last').set_index('trade_date')['fx_to_base']
            fx=tx.reindex(tx.index.union(idx)).sort_index().ffill().reindex(idx)
            if fx.isna().any():fx=fx.bfill()
            if fx.notna().any():estimated_assets.append(asset)
            else:unknown_assets.append(asset)
        qty=changes[asset].to_numpy(dtype=float)
        mat[:,j]=qty*np.nan_to_num(px.to_numpy(dtype=float),nan=0)*np.nan_to_num(fx.to_numpy(dtype=float),nan=0)
    if price_errors:warnings.append(f'Historical quote errors for {len(price_errors)} ticker(s).')
    if fx_errors:warnings.append(f'Historical FX errors for {len(fx_errors)} currency pair(s); recorded transaction FX used as an estimate where possible.')
    if estimated_assets:warnings.append('Some dates use the last trade price as an estimate where market-history quotes are missing.')
    if unknown_assets:warnings.append('Some assets/dates have no usable quote or FX rate and are omitted from those historical totals.')
    total=mat.sum(axis=1)
    max_points=920
    step=max(1,int(np.ceil(len(idx)/max_points)))
    take=np.arange(0,len(idx),step)
    if take[-1]!=len(idx)-1:take=np.r_[take,len(idx)-1]
    points=[{'date':idx[i].date().isoformat(),'value':round(float(total[i]),2)} for i in take]
    _LOG.info('market.history span=%s total_seconds=%.2f prices_seconds=%.2f fx_seconds=%.2f assets=%d',
              span,time.perf_counter()-begin,prices_elapsed,fx_elapsed,len(assets))
    return {'points':points,'range':span,'warnings':warnings,
            'estimated_assets':sorted(set(estimated_assets)),
            'excluded_assets':sorted(set(unknown_assets)),
            'price_count':len(prices),'source':'Yahoo Finance daily closes + Frankfurter FX; trade-price estimates flagged'}
