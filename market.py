"""Portfolio Track market-data service.

Drop-in replacement for the original market.py: preserves public function signatures,
calculation methods, warning semantics and market_cache table layout.

Optimizations:
- Batch Yahoo latest quotes via /v7/finance/quote (with cached crumb) in a single HTTP
  call for all tickers, with automatic high-concurrency /v8/finance/chart fallback.
- Non-blocking NSE EOD close racing so slow/blocked cloud-IP NSE requests never stall
  portfolio refreshes beyond a tight time budget.
- Skip query1 host retries on definitive 400/404/422 client errors.
- Batch cache queries/UPSERTs, reuse overlapping historic date ranges, skip closed
  zero-quantity assets in windowed histories, bounded RAM caches, and zero-wait RAM
  history peek for /api/refresh.
"""
from __future__ import annotations

import csv
import io
import zipfile
import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, time as daytime

import numpy as np
import pandas as pd
import requests
from sqlalchemy import select
import portfolio
import cash as cash_engine
from storage import MarketCache, Session, engine

_LOG = logging.getLogger('uvicorn.error')
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; PortfolioTracker/1.0)', 'Accept': 'application/json'}
_BROWSER_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
_HIST_REFRESH_HOURS = max(1., float(os.getenv('HISTORICAL_REFRESH_HOURS', '12')))
_FX_REFRESH_HOURS = max(1., float(os.getenv('FX_REFRESH_HOURS', '12')))
_NSE_OFFICIAL_CLOSE = os.getenv('NSE_OFFICIAL_CLOSE', '1').strip().lower() not in ('0', 'false', 'no')
_NSE_EOD_COOLDOWN = max(120, int(os.getenv('NSE_EOD_COOLDOWN_SECONDS', '900')))
_NSE_EOD_RACE_BUDGET = max(0.5, min(4.0, float(os.getenv('NSE_EOD_WAIT_SECONDS', '1.6'))))
_QUOTE_TTL = max(0, int(os.getenv('QUOTE_CACHE_SECONDS', '30')))
_NSE_COOLDOWN = max(60, int(os.getenv('NSE_COOLDOWN_SECONDS', '1800')))
_MAX_WORKERS = max(8, min(24, int(os.getenv('MARKET_FETCH_WORKERS', '20'))))
# Yahoo-first is much faster when NSE blocks cloud IPs. Set PRICE_SOURCE_PRIORITY=nse
# to restore NSE-first behavior (at the expense of slower cloud refreshes).
_PREFER_NSE = os.getenv('PRICE_SOURCE_PRIORITY', 'yahoo').strip().lower() == 'nse'

@dataclass
class _CacheRecord:
    start: date
    end: date
    fetched: datetime
    values: dict
    _ord_cache: tuple[np.ndarray, np.ndarray] | None = None

_EPOCH_ORDINAL = date(1970, 1, 1).toordinal()
_DAY_NS = 86_400_000_000_000
_cache_lock = threading.RLock()
_local_rows: OrderedDict[str, tuple[float, _CacheRecord | None]] = OrderedDict()
_quote_rows: OrderedDict[str, tuple[float, dict]] = OrderedDict()
_chart_rows: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
_fifo_bundle_cache: OrderedDict[str, object] = OrderedDict()
_cash_bundle_cache: OrderedDict[str, object] = OrderedDict()
_nse_block_until = 0.0
_nse_eod_block_until = 0.0
_nse_eod_missing_dates: dict[str, float] = {}
_yahoo_crumb_lock = threading.Lock()
_yahoo_crumb_state: tuple[float, str, dict] | None = None
_yahoo_crumb_block_until = 0.0
_prefer_query1_until = 0.0
_http_local = threading.local()
# Separate pools so live quotes and FX never queue behind bulk Yahoo chart fetches or warm-ups.
_io_pool = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix='market-io')
_fx_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='market-fx')
_quote_pool = ThreadPoolExecutor(max_workers=min(16, _MAX_WORKERS), thread_name_prefix='market-quote')
_bg_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='market-bg')
_bg_io_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix='market-bg-io')
_coord_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix='market-coord')
_pending_io: dict[tuple, Future] = {}
_pending_charts: dict[tuple, Future] = {}
_warmed_keys: OrderedDict[tuple, float] = OrderedDict()


def _submit_io(key, function, *args, pool: ThreadPoolExecutor | None = None):
    target_pool = pool or _io_pool

    def forget(future):
        with _cache_lock:
            if _pending_io.get(key) is future:
                del _pending_io[key]

    with _cache_lock:
        future = _pending_io.get(key)
        if future is None:
            future = target_pool.submit(function, *args)
            _pending_io[key] = future
            future.add_done_callback(forget)
        return future


def _http() -> requests.Session:
    """Per-thread persistent HTTP connections; requests.Session is not thread-safe."""
    session = getattr(_http_local, 'session', None)
    if session is None:
        session = requests.Session()
        session.headers.update(HEADERS)
        adapter = requests.adapters.HTTPAdapter(pool_connections=24, pool_maxsize=24)
        session.mount('https://', adapter)
        _http_local.session = session
    return session


def _negative_cache_ttl(key: str, today: date) -> float:
    if key.startswith('nse:close:') and len(key) == 18:
        try:
            d = date(int(key[10:14]), int(key[14:16]), int(key[16:18]))
            if d < today - timedelta(days=2):
                return 3600.0
            return 180.0
        except ValueError:
            pass
    return 60.0


def _read_cached(keys: list[str]) -> dict[str, _CacheRecord]:
    """One SQL query for *all* ticker/FX cache rows; bounded RAM cache between calls."""
    if not keys:
        return {}
    now = time.monotonic()
    found, missing = {}, []
    with _cache_lock:
        for key in dict.fromkeys(keys):
            hit = _local_rows.get(key)
            if hit and hit[0] > now:
                if hit[1] is not None:
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
        today = date.today()
        with _cache_lock:
            for key in missing:
                item = found.get(key)
                ttl = 3600.0 if item is not None else _negative_cache_ttl(key, today)
                _local_rows[key] = (now + ttl, item)
                _local_rows.move_to_end(key)
            while len(_local_rows) > 450:
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
            _local_rows[key] = (now + 3600.0, item)
            _local_rows.move_to_end(key)
        while len(_local_rows) > 450:
            _local_rows.popitem(last=False)


def _record_ord_arrays(record: _CacheRecord | None) -> tuple[np.ndarray, np.ndarray]:
    """Return sorted (ordinal_days, float_values) arrays cached on _CacheRecord."""
    if record is None or not record.values:
        return np.empty(0, dtype=np.int32), np.empty(0, dtype=np.float64)
    cached = record._ord_cache
    if cached is not None:
        return cached
    items = sorted(record.values.items())
    ords = np.fromiter((date.fromisoformat(k).toordinal() for k, _ in items), dtype=np.int32, count=len(items))
    vals = np.fromiter((float(v) for _, v in items), dtype=np.float64, count=len(items))
    pair = (ords, vals)
    record._ord_cache = pair
    return pair


def _align_ord_series(ords: np.ndarray, vals: np.ndarray, target_ords: np.ndarray,
                      bfill: bool = False,
                      override: tuple[int, float] | None = None) -> np.ndarray:
    """Fast O(N log K) equivalent of `_daily_frame(...).reindex(days)` (+ optional `.bfill()`)."""
    n = len(target_ords)
    if override is not None:
        ov_ord, ov_val = override
        if len(ords) == 0:
            ords = np.array([ov_ord], dtype=np.int32)
            vals = np.array([ov_val], dtype=np.float64)
        else:
            ins = int(np.searchsorted(ords, ov_ord))
            if ins < len(ords) and int(ords[ins]) == ov_ord:
                vals = vals.copy()
                vals[ins] = ov_val
            else:
                ords = np.insert(ords, ins, ov_ord)
                vals = np.insert(vals, ins, ov_val)
    if len(ords) == 0:
        return np.full(n, np.nan, dtype=np.float64)
    pos = np.searchsorted(ords, target_ords, side='right') - 1
    out = np.where(pos >= 0, vals[np.maximum(pos, 0)], np.nan)
    if bfill and pos[0] < 0 and int(ords[0]) <= int(target_ords[-1]):
        out[pos < 0] = vals[0]
    return out


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
        windows.append((max(start, end - timedelta(days=10)), end))
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


def _yahoo_hosts() -> tuple[str, str]:
    if time.monotonic() < _prefer_query1_until:
        return ('query1.finance.yahoo.com', 'query2.finance.yahoo.com')
    return ('query2.finance.yahoo.com', 'query1.finance.yahoo.com')


def _note_yahoo_host_fail(host: str) -> None:
    global _prefer_query1_until
    if host.startswith('query2.'):
        _prefer_query1_until = time.monotonic() + 120.0
    else:
        _prefer_query1_until = 0.0


def _yahoo_chart(ticker: str, params: dict, timeout: tuple[float, float] = (1.6, 3.2)) -> dict:
    """Try public Yahoo chart hosts with bounded timeouts and fast failure on 4xx."""
    last_error = None
    for host in _yahoo_hosts():
        try:
            url = f'https://{host}/v8/finance/chart/{requests.utils.quote(ticker, safe="")}'
            response = _http().get(url, params=params, timeout=timeout)
            if response.status_code == 429:
                raise ValueError('Yahoo Finance rate limited requests (HTTP 429)')
            if response.status_code in (400, 404, 422):
                # Invalid/delisted ticker won't exist on the secondary host either.
                raise ValueError(f'Yahoo ticker not found (HTTP {response.status_code})')
            response.raise_for_status()
            payload = response.json().get('chart') or {}
            results = payload.get('result') or []
            if results:
                return results[0]
            last_error = ValueError('No chart data returned')
            break
        except requests.RequestException as exc:
            last_error = exc
            _note_yahoo_host_fail(host)
        except (ValueError, KeyError) as exc:
            last_error = exc
            msg = str(exc).lower()
            if 'rate limited' in msg or 'not found' in msg:
                break
    raise ValueError(f'Yahoo quote unavailable: {type(last_error).__name__}')


def _positive_price(value) -> float | None:
    try:
        price = float(str(value).replace(',', ''))
    except (TypeError, ValueError):
        return None
    return round(price, 8) if np.isfinite(price) and price > 0 else None


def _get_yahoo_crumb() -> tuple[str, dict] | None:
    """Acquire and cache a Yahoo Finance session cookie + crumb for batch v7 quotes."""
    global _yahoo_crumb_state, _yahoo_crumb_block_until
    now = time.monotonic()
    with _yahoo_crumb_lock:
        if _yahoo_crumb_state and _yahoo_crumb_state[0] > now:
            return _yahoo_crumb_state[1], _yahoo_crumb_state[2]
        if now < _yahoo_crumb_block_until:
            return None
        hard_block = False
        try:
            sess = requests.Session()
            sess.headers.update({'User-Agent': _BROWSER_UA, 'Accept': '*/*'})
            # fc.yahoo.com returns 404 while setting the required A3 cookie.
            sess.get('https://fc.yahoo.com', timeout=(1.2, 2.0), allow_redirects=True)
            for host in _yahoo_hosts():
                try:
                    resp = sess.get(f'https://{host}/v1/test/getcrumb', timeout=(1.2, 2.0))
                except requests.RequestException:
                    _note_yahoo_host_fail(host)
                    continue
                if resp.status_code == 200:
                    crumb = (resp.text or '').strip()
                    if crumb and len(crumb) < 64 and '<' not in crumb:
                        cookies = sess.cookies.get_dict()
                        _yahoo_crumb_state = (time.monotonic() + 3600, crumb, cookies)
                        return crumb, cookies
                if resp.status_code in (401, 403):
                    hard_block = True
                    break
                if resp.status_code == 429:
                    break
        except requests.RequestException:
            pass
        _yahoo_crumb_block_until = time.monotonic() + (600.0 if hard_block else 15.0)
        return None


def _invalidate_yahoo_crumb(cooldown: float = 600.0) -> None:
    global _yahoo_crumb_state, _yahoo_crumb_block_until
    with _yahoo_crumb_lock:
        _yahoo_crumb_state = None
        _yahoo_crumb_block_until = time.monotonic() + cooldown


def _fetch_yahoo_batch(tickers: list[str]) -> dict[str, dict]:
    """Fetch up to ~100 quotes per single HTTP request via Yahoo v7 quote endpoint.

    Returns only verified positive quotes; any missed tickers seamlessly fall back
    to the concurrent /v8/finance/chart/ path.
    """
    if len(tickers) < 2:
        return {}
    auth = _get_yahoo_crumb()
    if not auth:
        return {}
    crumb, cookies = auth
    canonical = {t.upper(): t for t in tickers}
    results: dict[str, dict] = {}
    headers = {'User-Agent': _BROWSER_UA, 'Accept': 'application/json'}
    chunk_size = 100
    chunks = [tickers[i:i + chunk_size] for i in range(0, len(tickers), chunk_size)]

    def fetch_chunk(batch: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        params = {
            'symbols': ','.join(batch),
            'crumb': crumb,
            'fields': 'symbol,regularMarketPrice,regularMarketTime,currency',
        }
        for host in _yahoo_hosts():
            try:
                resp = _http().get(
                    f'https://{host}/v7/finance/quote',
                    params=params,
                    headers=headers,
                    cookies=cookies,
                    timeout=(1.2, 2.5),
                )
            except requests.RequestException:
                _note_yahoo_host_fail(host)
                continue
            if resp.status_code in (401, 403):
                _invalidate_yahoo_crumb(600.0)
                return {}
            if resp.status_code == 429:
                return {}
            if resp.status_code != 200:
                continue
            try:
                items = ((resp.json().get('quoteResponse') or {}).get('result')) or []
            except (ValueError, KeyError, TypeError):
                return {}
            for item in items:
                sym = str(item.get('symbol') or '').strip().upper()
                ticker = canonical.get(sym)
                if not ticker:
                    continue
                if ticker.upper().endswith('.NS') and str(item.get('currency') or '').upper() != 'INR':
                    continue
                price = _positive_price(item.get('regularMarketPrice'))
                if price is None:
                    continue
                stamp = item.get('regularMarketTime')
                as_of = ''
                if stamp:
                    try:
                        as_of = datetime.fromtimestamp(int(stamp), timezone.utc).isoformat()
                    except (TypeError, ValueError, OSError):
                        as_of = ''
                out[ticker] = {
                    'price': price,
                    'source': 'Yahoo Finance intraday' + (' (not NSE official close)' if ticker.upper().endswith('.NS') else ''),
                    'as_of': as_of,
                }
            return out
        return out

    if len(chunks) == 1:
        results.update(fetch_chunk(chunks[0]))
    else:
        futs = [_quote_pool.submit(fetch_chunk, c) for c in chunks]
        for fut in as_completed(futs):
            try:
                results.update(fut.result())
            except Exception:
                pass
    return results


# Official NSE *closing* prices are different from the last traded price.
# A single daily exchange file covers all .NS stocks and can be cached across
# portfolios. Only use it when the Indian market is not in its trading session.
_IST = timezone(timedelta(hours=5, minutes=30), name='IST')


def _nse_trading_window(now: datetime | None = None) -> bool:
    """Conservative: use intraday quotes until the close is fully published."""
    now = now or datetime.now(_IST)
    if now.tzinfo is None:
        raise ValueError('An offset-aware IST datetime is required')
    now = now.astimezone(_IST)
    return now.weekday() < 5 and daytime(9, 15) <= now.time() < daytime(17, 0)


def _nse_report_dates(now: datetime | None = None) -> list[date]:
    """Recent candidate NSE reports, never today's before its publication window."""
    now = (now or datetime.now(_IST)).astimezone(_IST)
    target = now.date()
    if now.time() < daytime(17, 0):
        target -= timedelta(days=1)
    out = []
    for offset in range(10):
        day = target - timedelta(days=offset)
        if day.weekday() < 5:
            out.append(day)
        if len(out) == 5:
            break
    return out


def _parse_nse_bhavcopy(content: bytes, report_day: date) -> dict[str, float]:
    """Parse the NSE UDiFF equity bhavcopy; use ClsPric, never LastPric."""
    if not content or len(content) > 5_000_000:
        raise ValueError('Unexpected NSE report size')
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = [i for i in archive.infolist() if i.filename.lower().endswith('.csv') and not i.is_dir()]
        if len(names) != 1 or names[0].file_size > 10_000_000:
            raise ValueError('Unexpected NSE report contents')
        with archive.open(names[0]) as raw:
            reader = csv.DictReader(io.TextIOWrapper(raw, encoding='utf-8-sig', newline=''))
            fields = set(reader.fieldnames or [])
            required = {'TckrSymb', 'SctySrs', 'ClsPric'}
            if not required <= fields or not ({'TradDt', 'BizDt'} & fields):
                raise ValueError('NSE close columns absent from report')
            quotes: dict[str, tuple[int, float]] = {}
            for row in reader:
                symbol = str(row.get('TckrSymb') or '').strip().upper()
                series = str(row.get('SctySrs') or '').strip().upper()
                if not symbol or not series or len(symbol) > 40:
                    continue
                raw_day = str(row.get('TradDt') or row.get('BizDt') or '').strip()
                if not raw_day:
                    continue
                parsed = None
                for fmt in ('%Y-%m-%d', '%d-%b-%Y', '%d/%m/%Y', '%Y%m%d'):
                    try:
                        parsed = datetime.strptime(raw_day[:11] if fmt == '%d-%b-%Y' else raw_day[:10], fmt).date()
                        break
                    except ValueError:
                        pass
                if parsed != report_day:
                    continue
                value = _positive_price(row.get('ClsPric'))
                if value is None:
                    continue
                rank = {'EQ': 0, 'BE': 1, 'BZ': 2, 'SM': 3}.get(series, 9)
                if symbol not in quotes or rank < quotes[symbol][0]:
                    quotes[symbol] = (rank, value)
    if not quotes:
        raise ValueError('NSE report contained no valid official closing prices')
    return {sym: val for sym, (_, val) in quotes.items()}


def _parse_nse_full_csv(content: bytes, report_day: date) -> dict[str, float]:
    """Official NSE full bhavcopy fallback; CLOSE_PRICE != LAST_PRICE."""
    if not content or len(content) > 2_000_000:
        raise ValueError('Unexpected full bhavcopy size')
    text = content.decode('utf-8-sig')
    reader = csv.DictReader(io.StringIO(text, newline=''))
    if not {'SYMBOL', 'SERIES', 'DATE1', 'CLOSE_PRICE'} <= set(reader.fieldnames or []):
        raise ValueError('Full bhavcopy header is invalid')
    prices = {}
    for row in reader:
        symbol = str(row.get('SYMBOL') or '').strip().upper()
        series = str(row.get('SERIES') or '').strip().upper()
        raw_day = str(row.get('DATE1') or '').strip()
        parsed = None
        for fmt in ('%d-%b-%Y', '%d-%b-%y', '%Y-%m-%d'):
            try:
                parsed = datetime.strptime(raw_day, fmt).date()
                break
            except ValueError:
                pass
        if parsed != report_day or not symbol:
            continue
        price = _positive_price(row.get('CLOSE_PRICE'))
        if price is None:
            continue
        rank = {'EQ': 0, 'BE': 1, 'BZ': 2, 'SM': 3}.get(series, 9)
        existing = prices.get(symbol)
        if existing is None or rank < existing[0]:
            prices[symbol] = (rank, price)
    if not prices:
        raise ValueError('No official NSE close prices in full bhavcopy')
    return {sym: value for sym, (_, value) in prices.items()}


def _download_nse_bhavcopy(day: date) -> dict[str, float]:
    """Use compressed UDiFF file, falling back to official full CSV on 404."""
    name = f'BhavCopy_NSE_CM_0_0_0_{day:%Y%m%d}_F_0000.csv.zip'
    url = f'https://nsearchives.nseindia.com/content/cm/{name}'
    headers = {
        'User-Agent': _BROWSER_UA,
        'Accept': 'application/zip,text/csv,*/*',
        'Referer': 'https://www.nseindia.com/',
    }
    response = _http().get(url, headers=headers, timeout=(1.4, 2.5))
    if response.status_code == 404:
        alternate = f'https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{day:%d%m%Y}.csv'
        fallback = _http().get(alternate, headers=headers, timeout=(1.4, 2.5))
        fallback.raise_for_status()
        return _parse_nse_full_csv(fallback.content, day)
    response.raise_for_status()
    return _parse_nse_bhavcopy(response.content, day)


def _cached_nse_eod() -> tuple[date | None, dict[str, float]]:
    """Fast cache-only lookup for the most recent NSE closing snapshot."""
    days = _nse_report_dates()
    keys = {day: 'nse:close:' + day.strftime('%Y%m%d') for day in days}
    cached = _read_cached(list(keys.values()))
    for day in days:
        item = cached.get(keys[day])
        if item and item.values:
            return day, dict(item.values)
    return None, {}


def _load_latest_nse_eod() -> tuple[date | None, dict[str, float]]:
    """Return latest official close for every NSE equity via one shared file."""
    global _nse_eod_block_until
    days = _nse_report_dates()
    now = time.monotonic()
    keys = {day: 'nse:close:' + day.strftime('%Y%m%d') for day in days}
    cached = _read_cached(list(keys.values()))
    attempts = 0
    for day in days:
        key = keys[day]
        item = cached.get(key)
        if item and item.values:
            return day, dict(item.values)
        with _cache_lock:
            if now < _nse_eod_block_until:
                return None, {}
            if now < _nse_eod_missing_dates.get(key, 0):
                continue
        if attempts >= 2:
            break
        attempts += 1
        try:
            begin = time.perf_counter()
            prices = _download_nse_bhavcopy(day)
            _write_cached({key: _CacheRecord(day, day, datetime.now(timezone.utc), prices)})
            _LOG.info('market.nse_official_close seconds=%.2f day=%s symbols=%d',
                      time.perf_counter() - begin, day, len(prices))
            return day, prices
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code in (404, 410):
                with _cache_lock:
                    _nse_eod_missing_dates[key] = now + 1200
                if day == datetime.now(_IST).date():
                    break
                continue
            with _cache_lock:
                _nse_eod_block_until = now + _NSE_EOD_COOLDOWN
            _LOG.warning('NSE official EOD feed unavailable (%s); using marked fallback', type(exc).__name__)
            break
        except (requests.RequestException, ValueError, zipfile.BadZipFile, OSError) as exc:
            with _cache_lock:
                _nse_eod_block_until = now + _NSE_EOD_COOLDOWN
            _LOG.warning('NSE official EOD feed unavailable (%s); using marked fallback', type(exc).__name__)
            break
        except Exception:
            _LOG.exception('NSE report cache failure')
            break
    return None, {}


def _nse_official_quotes(tickers: list[str], report: Future | None = None,
                         timeout: float | None = None) -> dict[str, dict]:
    if not tickers:
        return {}
    if report is None:
        if not _NSE_OFFICIAL_CLOSE or _nse_trading_window():
            return {}
        report = _submit_io(('nse-report', datetime.now(_IST).date()), _load_latest_nse_eod)
    try:
        day, closes = report.result(timeout=timeout)
    except FuturesTimeout:
        return {}
    except Exception:
        _LOG.exception('Unable to retrieve NSE closing prices; using fallback')
        return {}
    if not day:
        return {}
    return {
        ticker: {'price': closes[ticker[:-3].upper()],
                 'source': 'NSE official closing price (EOD)',
                 'as_of': day.isoformat()}
        for ticker in tickers if ticker.upper().endswith('.NS')
        and ticker[:-3].upper() in closes
    }


def _overlay_official_nse_history(prices: dict, end: date) -> dict:
    """Correct the most recent NSE daily close when official data is cached."""
    if not _NSE_OFFICIAL_CLOSE or not any(t.upper().endswith('.NS') for t in prices):
        return prices
    candidate_days = _nse_report_dates()
    snapshot = _read_cached(['nse:close:' + d.strftime('%Y%m%d') for d in candidate_days])
    for d in candidate_days:
        record = snapshot.get('nse:close:' + d.strftime('%Y%m%d'))
        if record and record.values and d <= end:
            for ticker, values in prices.items():
                if ticker.upper().endswith('.NS') and ticker[:-3].upper() in record.values:
                    values[d.isoformat()] = float(record.values[ticker[:-3].upper()])
            break
    return prices


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
        'User-Agent': _BROWSER_UA,
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': page,
    }
    try:
        with requests.Session() as session:
            session.headers.update(headers)
            session.get('https://www.nseindia.com/', timeout=(1.4, 2.4)).raise_for_status()
            response = session.get('https://www.nseindia.com/api/quote-equity',
                                   params={'symbol': symbol}, timeout=(1.4, 2.4))
            response.raise_for_status()
            data = response.json()
    except (requests.RequestException, ValueError):
        with _cache_lock:
            _nse_block_until = time.monotonic() + _NSE_COOLDOWN
        raise
    returned_symbol = str((data.get('info') or {}).get('symbol') or symbol)
    if returned_symbol.upper() != symbol:
        raise ValueError('NSE returned a different symbol')
    price = _positive_price((data.get('priceInfo') or {}).get('lastPrice'))
    if price is None:
        raise ValueError('NSE returned no valid last traded price')
    return {'price': price, 'source': 'NSE India last traded price',
            'as_of': str((data.get('metadata') or {}).get('lastUpdateTime') or '')}


def _fetch_yahoo_latest(ticker: str) -> dict:
    """Fetch compact metadata quote first (~1 KB); fall back to 5d/1d only if needed."""
    info = _yahoo_chart(ticker, {'range': '1d', 'interval': '1d', 'includePrePost': 'false'},
                        timeout=(1.4, 2.6))
    meta = info.get('meta') or {}
    if ticker.upper().endswith('.NS') and str(meta.get('currency', '')).upper() != 'INR':
        raise ValueError('Yahoo returned a non-INR quote for an NSE stock')
    price = _positive_price(meta.get('regularMarketPrice'))
    if price is None:
        candles = ((info.get('indicators') or {}).get('quote') or [{}])[0].get('close') or []
        price = next((_positive_price(x) for x in reversed(candles) if _positive_price(x)), None)
    if price is None:
        info = _yahoo_chart(ticker, {'range': '5d', 'interval': '1d', 'includePrePost': 'false'},
                            timeout=(1.4, 2.6))
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
    return {'price': price, 'source': 'Yahoo Finance intraday' + (' (not NSE official close)' if ticker.upper().endswith('.NS') else ''), 'as_of': as_of}


def get_latest_prices(tickers: list[str]) -> tuple[dict, dict]:
    """Latest traded quotes, preserving original (quotes, errors) API.

    Fast path:
    1. Serve RAM-cached quotes and cached NSE official EOD closes immediately.
    2. Start uncached NSE EOD download in parallel (never blocking Yahoo!).
    3. Fetch all remaining tickers in ONE Yahoo v7 batch HTTP call (when crumb is
       available), falling back to 20-way parallel Yahoo v8 chart calls only for
       any symbols not returned by the batch call.
    4. If NSE EOD finishes within the tight race budget, overlay official NSE closes
       on .NS symbols; otherwise return Yahoo's .NS quotes without stalling.
    """
    names = list(dict.fromkeys(t.strip() for t in tickers if t and t.strip()))
    quotes, errors, missing = {}, {}, []
    mode = 'live' if _nse_trading_window() else 'eod'
    now = time.monotonic()
    with _cache_lock:
        for ticker in names:
            cached = _quote_rows.get(mode + ':' + ticker)
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
        indian = [ticker for ticker in missing if ticker.upper().endswith('.NS')]
        check_close = bool(indian) and _NSE_OFFICIAL_CLOSE and mode == 'eod'
        report = None
        if check_close:
            cached_day, cached_closes = _cached_nse_eod()
            if cached_day and cached_closes:
                for ticker in indian:
                    sym = ticker[:-3].upper()
                    if sym in cached_closes:
                        quotes[ticker] = {
                            'price': cached_closes[sym],
                            'source': 'NSE official closing price (EOD)',
                            'as_of': cached_day.isoformat(),
                        }
                indian = [t for t in indian if t not in quotes]
            elif time.monotonic() >= _nse_eod_block_until:
                # Race NSE EOD in the background alongside Yahoo instead of blocking .NS symbols.
                report = _submit_io(('nse-report', datetime.now(_IST).date()), _load_latest_nse_eod, pool=_quote_pool)

        to_fetch = [t for t in missing if t not in quotes]
        if to_fetch and not _PREFER_NSE:
            batch_hits = _fetch_yahoo_batch(to_fetch)
            quotes.update(batch_hits)
            to_fetch = [t for t in to_fetch if t not in quotes]

        if to_fetch:
            futures = {_submit_io(('quote', mode, t), fetch, t, pool=_quote_pool): t for t in to_fetch}
            for future in as_completed(futures):
                ticker = futures[future]
                try:
                    quotes[ticker] = future.result()
                except Exception as exc:
                    errors[ticker] = str(exc)

        if report is not None and indian:
            # Give NSE EOD at most a short remaining window so a slow cloud host never stalls refresh.
            remaining_budget = max(0.05, _NSE_EOD_RACE_BUDGET - (time.perf_counter() - begin))
            official = _nse_official_quotes(indian, report, timeout=remaining_budget)
            if official:
                quotes.update(official)
                for ticker in official:
                    errors.pop(ticker, None)

        if _QUOTE_TTL:
            with _cache_lock:
                expires = time.monotonic() + _QUOTE_TTL
                for ticker in missing:
                    if ticker in quotes:
                        key = mode + ':' + ticker
                        _quote_rows[key] = (expires, dict(quotes[ticker]))
                        _quote_rows.move_to_end(key)
                while len(_quote_rows) > 250:
                    _quote_rows.popitem(last=False)
        _LOG.info('market.quote_refresh seconds=%.2f requested=%d errors=%d cache_hits=%d',
                  time.perf_counter()-begin, len(names), len(errors), len(names)-len(missing))
    return quotes, errors


def warm_quote_providers(has_nse: bool = True) -> None:
    """Non-blocking warm-up of Yahoo v7 crumb and (when EOD) NSE bhavcopy before user refreshes."""
    def _run():
        try:
            _get_yahoo_crumb()
        except Exception:
            pass
        if has_nse and _NSE_OFFICIAL_CLOSE and not _nse_trading_window():
            try:
                day, closes = _cached_nse_eod()
                if not day and time.monotonic() >= _nse_eod_block_until:
                    _submit_io(('nse-report', datetime.now(_IST).date()), _load_latest_nse_eod, pool=_quote_pool)
            except Exception:
                pass

    now = time.monotonic()
    warm_key = ('quote-warm', bool(has_nse), 'live' if _nse_trading_window() else 'eod')
    with _cache_lock:
        if _warmed_keys.get(warm_key, 0.0) > now:
            return
        _warmed_keys[warm_key] = now + 300.0
        _warmed_keys.move_to_end(warm_key)
        while len(_warmed_keys) > 64:
            _warmed_keys.popitem(last=False)
    _bg_pool.submit(_run)


def _fetch_chart(ticker: str, start: date, end: date) -> dict:
    start_epoch = int(pd.Timestamp(start, tz='UTC').timestamp())
    end_epoch = int((pd.Timestamp(end, tz='UTC') + pd.Timedelta(days=2)).timestamp())
    info = _yahoo_chart(ticker, {'period1': start_epoch, 'period2': end_epoch,
                                 'interval': '1d', 'includeAdjustedClose': 'false'},
                        timeout=(1.8, 4.0))
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


def _series_records_with_windows(
    keys: dict[str, str],
    bounds: dict[str, tuple[date, date, date | None]],
    force: bool,
    refresh_hours: float,
    provider,
    *,
    pool: ThreadPoolExecutor | None = None,
) -> tuple[dict[str, _CacheRecord], dict[str, str]]:
    """Batch fetch per-symbol date windows using cache records directly (zero dict copying).

    `bounds[symbol] = (req_start, req_end, expand_start)`:
      - `req_start..req_end` is checked against cached coverage.
      - When a pre-window fetch is needed and `expand_start < req_start`, the fetch extends
        back to `expand_start` so subsequent wider spans avoid re-fetching the same symbol.
    """
    result: dict[str, _CacheRecord] = {}
    errors: dict[str, str] = {}
    if not keys:
        return result, errors
    lookup = _read_cached(list(keys.values()))
    jobs: dict = {}
    for symbol, key in keys.items():
        req_start, req_end, expand_start = bounds[symbol]
        item = lookup.get(key)
        windows = _fetch_windows(item, req_start, req_end, refresh_hours, force)
        if windows and expand_start is not None and expand_start < req_start:
            if item is None:
                windows = [(expand_start, req_end)]
            elif not force:
                expanded = []
                for a, b in windows:
                    if a == req_start and req_start < item.start:
                        expanded.append((expand_start, b))
                    else:
                        expanded.append((a, b))
                windows = expanded
        if item is not None:
            if item.values:
                result[symbol] = item
            else:
                errors[symbol] = 'No historical data available (cached provider failure)'
        if windows:
            jobs[symbol] = (key, item, windows, req_start, req_end)
        elif item is None:
            fetch_start = expand_start if (expand_start is not None and expand_start < req_start) else req_start
            jobs[symbol] = (key, None, [(fetch_start, req_end)], req_start, req_end)

    if jobs:
        begin = time.perf_counter()
        future_map = {}
        for symbol, (key, _, windows, _, _) in jobs.items():
            for a, b in windows:
                fut = _submit_io(('series', key, a, b), provider, symbol, a, b, pool=pool)
                future_map[fut] = (symbol, a, b)
        completed: dict[str, list] = {x: [] for x in jobs}
        failed: dict[str, bool] = {x: False for x in jobs}
        for fut in as_completed(future_map):
            symbol, a, b = future_map[fut]
            try:
                chunk = fut.result()
                if not isinstance(chunk, dict):
                    raise TypeError('provider did not return a dictionary')
                entry = jobs[symbol][1]
                is_contiguous_pre_window = entry is not None and b == entry.start - timedelta(days=1)
                if not chunk and entry is not None and entry.values and not is_contiguous_pre_window and len(pd.bdate_range(a, b)) > 0:
                    raise ValueError('No refreshed business-day data; retaining prior prices')
                completed[symbol].append(((a, b), chunk))
            except Exception as exc:
                failed[symbol] = True
                errors[symbol] = f'Market data unavailable ({type(exc).__name__})'
        to_save: dict[str, _CacheRecord] = {}
        ram_only: dict[str, _CacheRecord] = {}
        stamp = datetime.now(timezone.utc)
        for symbol, (key, entry, windows, req_start, req_end) in jobs.items():
            parts = completed[symbol]
            if failed[symbol] or len(parts) != len(windows):
                if entry is None:
                    to_save[key] = _CacheRecord(req_start, req_end, stamp, {})
                continue
            parts.sort(key=lambda part: part[0][0])
            updated = _merge_cache(entry, [p[0] for p in parts], [p[1] for p in parts], stamp)
            # Skip redundant DB rewrite when force-refresh returned identical data within the hour.
            if (
                entry is not None
                and updated.start == entry.start
                and updated.end == entry.end
                and updated.values == entry.values
                and (stamp - entry.fetched).total_seconds() < 3600
            ):
                updated._ord_cache = entry._ord_cache
                ram_only[key] = updated
            else:
                if entry is not None and updated.values == entry.values:
                    updated._ord_cache = entry._ord_cache
                to_save[key] = updated
            if updated.values:
                result[symbol] = updated
                errors.pop(symbol, None)
            else:
                errors[symbol] = 'No historical quotes available'
        if ram_only:
            with _cache_lock:
                now_m = time.monotonic()
                for key, item in ram_only.items():
                    _local_rows[key] = (now_m + 3600.0, item)
                    _local_rows.move_to_end(key)
        try:
            _write_cached(to_save)
        except Exception:
            _LOG.exception('Could not persist market cache batch')
        _LOG.info('market.historical_fetch seconds=%.2f symbols=%d windows=%d errors=%d',
                  time.perf_counter()-begin, len(jobs), len(future_map), len(errors))
    return result, errors


def _series_with_cache(keys: dict[str, str], start: date, end: date, force: bool,
                       refresh_hours: float, provider,
                       *, pool: ThreadPoolExecutor | None = None) -> tuple[dict, dict]:
    """Generic batch fetch for prices and FX. Fetch only uncovered/recent windows."""
    bounds = {sym: (start, end, None) for sym in keys}
    records, errors = _series_records_with_windows(keys, bounds, force, refresh_hours, provider, pool=pool)
    return {sym: dict(rec.values) for sym, rec in records.items()}, errors


def get_prices(tickers: list[str], start: date, end: date, force: bool = False) -> tuple[dict, dict]:
    names = list(dict.fromkeys(x for x in tickers if x))
    return _series_with_cache({t: 'price:' + t for t in names}, start, end,
                              force, _HIST_REFRESH_HOURS, _fetch_chart, pool=_io_pool)


def _fetch_fx_series(ccy: str, base: str, start: date, end: date) -> dict:
    if ccy == base:
        return {}
    src = 'GBP' if ccy == 'GBX' else ccy
    dest = 'GBP' if base == 'GBX' else base
    if src == dest:
        factor = (0.01 if ccy == 'GBX' else 1)/(0.01 if base == 'GBX' else 1)
        return {str(d.date()): factor for d in pd.date_range(start, end)}
    url = f'https://api.frankfurter.dev/v1/{start.isoformat()}..{end.isoformat()}'
    response = _http().get(url, params={'base': src, 'symbols': dest}, timeout=(1.5, 3.8))
    response.raise_for_status()
    raw = response.json().get('rates', {})
    factor = (0.01 if ccy == 'GBX' else 1)/(0.01 if base == 'GBX' else 1)
    return {day: float(values[dest])*factor for day, values in raw.items() if dest in values}


def _get_fx_records(
    currencies: list[str],
    base: str,
    start: date,
    end: date,
    force: bool = False,
    expand_start: date | None = None,
    pool: ThreadPoolExecutor | None = None,
) -> tuple[dict[str, _CacheRecord], dict[str, str]]:
    names = list(dict.fromkeys(currencies))
    keys = {ccy: f'fx:{ccy}:{base}' for ccy in names if ccy != base}
    bounds = {ccy: (start, end, expand_start) for ccy in keys}
    return _series_records_with_windows(
        keys, bounds, force, _FX_REFRESH_HOURS,
        lambda ccy, a, b: _fetch_fx_series(ccy, base, a, b),
        pool=pool or _fx_pool,
    )


def get_fx_series(currencies: list[str], base: str, start: date, end: date,
                  force=False) -> tuple[dict, dict]:
    names = list(dict.fromkeys(currencies))
    records, errors = _get_fx_records(names, base, start, end, force=force)
    output = {ccy: dict(rec.values) for ccy, rec in records.items()}
    if base in names:
        output[base] = {start.isoformat(): 1.0}
    return output, errors


def spot_rates(currencies: list[str], base: str, force: bool = False) -> tuple[dict[str, float], list[str]]:
    today = date.today()
    today_iso = today.isoformat()
    records, _ = _get_fx_records(currencies, base, today - timedelta(days=8), today, force=force)
    rates, warnings = {}, []
    for ccy in currencies:
        if ccy == base:
            rates[ccy] = 1.0
        else:
            rec = records.get(ccy)
            if rec and rec.values:
                # Fast path using pre-sorted ordinal array if available, else single max scan.
                if rec._ord_cache is not None:
                    ords, vals = rec._ord_cache
                    idx_r = int(np.searchsorted(ords, today.toordinal(), side='right')) - 1
                    if idx_r >= 0:
                        rates[ccy] = float(vals[idx_r])
                    else:
                        warnings.append(f'Latest FX unavailable: {ccy} → {base} (not converted).')
                else:
                    best_day = None
                    for d in rec.values:
                        if d <= today_iso and (best_day is None or d > best_day):
                            best_day = d
                    if best_day is not None:
                        rates[ccy] = rec.values[best_day]
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


def cash_fx_rates(df: pd.DataFrame, base: str) -> tuple[np.ndarray, list[str]]:
    """Historical transaction-date FX rates for cash_txns.csv on base-currency switch."""
    if df.empty:
        return np.array([]), []
    base_u = base.upper()
    start = df['date'].min().date() - timedelta(days=6)
    end = min(date.today(), df['date'].max().date() + timedelta(days=1))
    currencies = df['foreign_ccy'].astype(str).str.upper().unique().tolist()
    series, _ = get_fx_series(currencies, base_u, start, end)
    output = np.full(len(df), np.nan)
    warns = []
    by_ccy = {}
    full_idx = pd.date_range(start, end)
    for ccy in currencies:
        if ccy == base_u:
            by_ccy[ccy] = pd.Series(1.0, index=full_idx)
        elif ccy in series and series[ccy]:
            vals = pd.Series(series[ccy], dtype='float64')
            vals.index = pd.to_datetime(vals.index)
            by_ccy[ccy] = vals.reindex(full_idx).ffill().bfill()
        else:
            warns.append(f'Cannot calculate historical {ccy} → {base_u} FX for cash transactions.')
    dates = pd.to_datetime(df['date']).dt.normalize()
    for ccy, s in by_ccy.items():
        mask = (df['foreign_ccy'].astype(str).str.upper() == ccy).to_numpy()
        output[mask] = s.reindex(dates[mask]).to_numpy(dtype=float)
    return output, warns


_HISTORY_COLUMNS = ['trade_date', 'asset', 'side', 'quantity', 'ticker', 'trade_ccy',
                    'price_trade_ccy', 'charges_trade_ccy', 'stt_trade_ccy', 'fx_to_base']
_CASH_HISTORY_COLUMNS = ['date', 'foreign_ccy', 'txn_type', 'foreign_amount', 'fx_rate', 'fee_foreign']


def compute_inv_hash_bytes(df: pd.DataFrame | None) -> tuple[bytes, bool]:
    """Compute reusable hash bytes for trades history columns and whether any .NS ticker exists."""
    if df is None or df.empty:
        return b'', False
    raw = pd.util.hash_pandas_object(df[_HISTORY_COLUMNS], index=False).values.tobytes()
    has_nse = any(str(t).upper().endswith('.NS') for t in df['ticker'].tolist() if t)
    return raw, has_nse


def compute_cash_hash_bytes(cash_df: pd.DataFrame | None) -> bytes:
    """Compute reusable hash bytes for cash history columns."""
    if cash_df is None or cash_df.empty:
        return b''
    return pd.util.hash_pandas_object(cash_df[_CASH_HISTORY_COLUMNS], index=False).values.tobytes()


def nse_revision_token() -> bytes:
    """Lookup cached official NSE close stamp once for chart revision hashing."""
    if not _NSE_OFFICIAL_CLOSE:
        return b''
    days = [d for d in _nse_report_dates() if d <= date.today()]
    keys = ['nse:close:' + d.strftime('%Y%m%d') for d in days]
    try:
        cached = _read_cached(keys)
    except Exception:
        _LOG.exception('Could not read NSE close metadata for chart revision')
        return b'close-metadata-unavailable'
    for key in keys:
        record = cached.get(key)
        if record and record.values:
            return (key + record.fetched.isoformat()).encode()
    return b''


def history_revision(df: pd.DataFrame, base: str,
                     cash_df: pd.DataFrame | None = None,
                     mode: str = 'investments',
                     *,
                     inv_hash_bytes: bytes | None = None,
                     cash_hash_bytes: bytes | None = None,
                     has_nse: bool | None = None,
                     nse_token: bytes | None = None) -> str:
    """Identify exactly the ledger and official-close inputs used by the daily chart."""
    if mode not in ('investments', 'cash', 'nav'):
        mode = 'investments'
    digest = hashlib.blake2b(digest_size=16)
    digest.update(mode.encode())
    digest.update(base.encode())
    digest.update(date.today().isoformat().encode())
    if mode in ('investments', 'nav') and df is not None and not df.empty:
        digest.update(b'INV:')
        if inv_hash_bytes is None:
            inv_hash_bytes, auto_has_nse = compute_inv_hash_bytes(df)
            if has_nse is None:
                has_nse = auto_has_nse
        digest.update(inv_hash_bytes)
        if _NSE_OFFICIAL_CLOSE:
            if has_nse is None:
                has_nse = any(str(t).upper().endswith('.NS') for t in df['ticker'].tolist() if t)
            if has_nse:
                tok = nse_token if nse_token is not None else nse_revision_token()
                if tok:
                    digest.update(tok)
    if mode in ('cash', 'nav') and cash_df is not None and not cash_df.empty:
        digest.update(b'CASH:')
        if cash_hash_bytes is None:
            cash_hash_bytes = compute_cash_hash_bytes(cash_df)
        digest.update(cash_hash_bytes)
    return digest.hexdigest()


def _shallow_chart_copy(result: dict) -> dict:
    return {
        **result,
        'points': list(result.get('points', ())),
        'warnings': list(result.get('warnings', ())),
        'estimated_assets': list(result.get('estimated_assets', ())),
        'excluded_assets': list(result.get('excluded_assets', ())),
    }


def peek_cached_history(span: str, *, revision: str, owner=None, mode: str = 'investments') -> dict | None:
    """Instant non-blocking RAM cache check used by /api/refresh."""
    if not revision or span not in ('1M', '3M', '6M', '1Y', '3Y', 'MAX'):
        return None
    if mode not in ('investments', 'cash', 'nav'):
        return None
    key = (owner, mode, revision, span)
    with _cache_lock:
        hit = _chart_rows.get(key)
        if hit and hit[0] > time.monotonic():
            _chart_rows.move_to_end(key)
            return _shallow_chart_copy(hit[1])
    return None


def _store_chart_cache(key: tuple, result: dict) -> None:
    with _cache_lock:
        _chart_rows[key] = (time.monotonic() + 600.0, result)
        _chart_rows.move_to_end(key)
        while len(_chart_rows) > 72:
            _chart_rows.popitem(last=False)


def _format_history_reply(
    start_ord: int,
    n_days: int,
    span: str,
    mode: str,
    rebase: bool,
    portfolio_daily: np.ndarray,
    invested_daily: np.ndarray,
    cum_dev: np.ndarray,
    all_realized: float,
    warnings: list[str],
    estimated_assets: list[str],
    excluded_assets: list[str],
    price_count: int,
) -> dict:
    baseline = float(cum_dev[0]) if rebase else 0.0
    dev_series = cum_dev - baseline

    max_points = 920
    step = max(1, int(np.ceil(n_days / max_points)))
    take = np.arange(0, n_days, step)
    if take[-1] != n_days - 1:
        take = np.r_[take, n_days - 1]
    points = [
        {
            'date': date.fromordinal(start_ord + int(i)).isoformat(),
            'value': round(float(dev_series[i]), 2),
            'portfolio_value': round(float(portfolio_daily[i]), 2),
            'invested': round(float(invested_daily[i]), 2),
        }
        for i in take
    ]
    return {
        'points': points,
        'range': span,
        'mode': mode,
        'warnings': warnings,
        'estimated_assets': estimated_assets,
        'excluded_assets': excluded_assets,
        'price_count': price_count,
        'baseline': round(baseline, 2),
        'development': round(float(dev_series[-1]), 2),
        'realized_total': round(all_realized, 2),
        'source': 'Daily development (Unrealized P/L + Realized P/L) using Yahoo Finance daily closes + Frankfurter FX',
    }


def portfolio_history(df: pd.DataFrame, base: str, span: str, *,
                      revision=None, owner=None,
                      cash_df: pd.DataFrame | None = None,
                      mode: str = 'investments',
                      inv_hash_bytes: bytes | None = None,
                      cash_hash_bytes: bytes | None = None,
                      has_nse: bool | None = None,
                      nse_token: bytes | None = None,
                      cash_computed: pd.DataFrame | None = None) -> dict:
    if span not in ('1M', '3M', '6M', '1Y', '3Y', 'MAX'):
        raise ValueError('Unsupported range')
    if mode not in ('investments', 'cash', 'nav'):
        raise ValueError('Unsupported history mode')
    rev = revision or history_revision(
        df, base, cash_df=cash_df, mode=mode,
        inv_hash_bytes=inv_hash_bytes, cash_hash_bytes=cash_hash_bytes,
        has_nse=has_nse, nse_token=nse_token,
    )
    key = (owner, mode, rev, span)
    with _cache_lock:
        hit = _chart_rows.get(key)
        if hit and hit[0] > time.monotonic():
            _chart_rows.move_to_end(key)
            return _shallow_chart_copy(hit[1])
        pending = _pending_charts.get(key)
        leader = pending is None
        if leader:
            pending = _pending_charts[key] = Future()
    if not leader:
        return _shallow_chart_copy(pending.result())
    try:
        sibling_replies: dict[str, dict] = {}
        result = _calculate_history(
            df, cash_df, base, span, mode,
            inv_hash_bytes=inv_hash_bytes,
            cash_hash_bytes=cash_hash_bytes,
            cash_computed=cash_computed,
            sibling_out=sibling_replies if mode == 'nav' else None,
        )
        _store_chart_cache(key, result)
        # In NAV mode, we already computed investment & cash daily series; cache them for instant mode switching.
        if sibling_replies:
            for sib_mode, sib_res in sibling_replies.items():
                sib_rev = history_revision(
                    df, base, cash_df=cash_df, mode=sib_mode,
                    inv_hash_bytes=inv_hash_bytes, cash_hash_bytes=cash_hash_bytes,
                    has_nse=has_nse, nse_token=nse_token,
                )
                _store_chart_cache((owner, sib_mode, sib_rev, span), sib_res)
        pending.set_result(result)
        return _shallow_chart_copy(result)
    except BaseException as exc:
        pending.set_exception(exc)
        raise
    finally:
        with _cache_lock:
            _pending_charts.pop(key, None)


def _daily_frame(frame: pd.DataFrame, days: pd.DatetimeIndex) -> pd.DataFrame:
    """Align all columns at once; retain observations before the visible interval."""
    frame.index = pd.to_datetime(frame.index)
    return frame.reindex(frame.index.union(days)).sort_index().ffill().reindex(days)


@dataclass
class _DailyFifoBundle:
    u_days_ord: np.ndarray            # sorted unique trade day ordinals, shape (U,)
    assets: tuple[str, ...]           # asset names in first-appearance order, len A
    qty_mat: np.ndarray               # forward-filled float64, shape (U, A)
    cost_mat: np.ndarray              # forward-filled float64, shape (U, A)
    cum_realized_u: np.ndarray        # cumulative realized P/L at u_days_ord, shape (U,)
    all_time_realized: float
    tickers: tuple[str, ...]          # latest non-empty ticker per asset (len A)
    ccys: tuple[str, ...]             # latest trade_ccy per asset (len A)
    first_held_ord: tuple[int | None, ...]
    last_held_ord: tuple[int | None, ...]  # None means still held past last trade date
    rec_px: tuple[tuple[np.ndarray, np.ndarray], ...]
    rec_fx: tuple[tuple[np.ndarray, np.ndarray], ...]
    first_trade_date: date


@dataclass
class _DailyCashBundle:
    u_days_ord: np.ndarray            # sorted unique cash day ordinals, shape (U,)
    ccys: tuple[str, ...]             # uppercase currency codes, len C
    pos_mat: np.ndarray               # forward-filled float64, shape (U, C)
    cost_mat: np.ndarray              # forward-filled float64, shape (U, C)
    cum_realized_u: np.ndarray        # cumulative realized P/L at u_days_ord, shape (U,)
    all_time_realized: float
    rec_fx: dict[str, tuple[np.ndarray, np.ndarray]]
    first_cash_date: date


def _ffill_2d(mat: np.ndarray) -> np.ndarray:
    """Vectorized column-wise forward-fill on a 2D float64 array, replacing leading NaNs with 0."""
    u, a = mat.shape
    if u == 0 or a == 0:
        return np.zeros((u, a), dtype=np.float64)
    mask = np.isnan(mat)
    idx_ff = np.where(~mask, np.arange(u, dtype=np.int32)[:, None], 0)
    np.maximum.accumulate(idx_ff, axis=0, out=idx_ff)
    filled = mat[idx_ff, np.arange(a, dtype=np.int32)[None, :]]
    np.nan_to_num(filled, copy=False, nan=0.0)
    return filled


def _build_daily_fifo_bundle(df: pd.DataFrame, cache_key: str | None = None) -> _DailyFifoBundle:
    """Fast NumPy daily FIFO replay matching portfolio.py rules, cached per ledger hash."""
    if cache_key is not None:
        with _cache_lock:
            hit = _fifo_bundle_cache.get(cache_key)
            if hit is not None:
                _fifo_bundle_cache.move_to_end(cache_key)
                return hit

    order, ts_ns, assets_list, is_buy, qtys, ccys_list = portfolio._fifo_order_and_arrays(df)
    prices_arr = df['price_trade_ccy'].to_numpy(dtype=float)
    fees_arr = df['charges_trade_ccy'].to_numpy(dtype=float)
    stt_arr = df['stt_trade_ccy'].to_numpy(dtype=float)
    fx_arr = df['fx_to_base'].to_numpy(dtype=float)
    gross = qtys * prices_arr
    unit_base_arr = (gross + np.where(is_buy, fees_arr + stt_arr, -fees_arr - stt_arr)) * fx_arr / qtys
    days_ord_arr = (ts_ns // _DAY_NS + _EPOCH_ORDINAL).astype(np.int32)

    u_days_ord, inv_day = np.unique(days_ord_arr, return_inverse=True)
    n_days = len(u_days_ord)
    first_trade_date = date.fromordinal(int(u_days_ord[0]))

    asset_to_col: dict[str, int] = {}
    asset_names: list[str] = []
    lots_by_col: list[deque] = []

    # First register asset order matching groupby('asset', sort=False) on sorted trades.
    for idx in order:
        a = assets_list[idx]
        if a not in asset_to_col:
            asset_to_col[a] = len(asset_names)
            asset_names.append(a)
            lots_by_col.append(deque())

    n_assets = len(asset_names)
    qty_mat = np.full((n_days, n_assets), np.nan, dtype=np.float64)
    cost_mat = np.full((n_days, n_assets), np.nan, dtype=np.float64)
    realized_by_u = np.zeros(n_days, dtype=np.float64)

    for idx in order:
        col = asset_to_col[assets_list[idx]]
        u_i = int(inv_day[idx])
        n = float(qtys[idx])
        unit = float(unit_base_arr[idx])
        lots = lots_by_col[col]
        if is_buy[idx]:
            lots.append([n, unit])
        else:
            left = n
            realized_delta = 0.0
            while left > 1e-9 and lots:
                lot = lots[0]
                qty = left if left < lot[0] else lot[0]
                left -= qty
                lot[0] -= qty
                realized_delta += (unit - lot[1]) * qty
                if lot[0] <= 1e-9:
                    lots.popleft()
            if realized_delta:
                realized_by_u[u_i] += realized_delta
        qty_mat[u_i, col] = sum(lot[0] for lot in lots)
        cost_mat[u_i, col] = sum(lot[0] * lot[1] for lot in lots)

    qty_mat = _ffill_2d(qty_mat)
    cost_mat = _ffill_2d(cost_mat)
    cum_realized_u = np.cumsum(realized_by_u)
    all_time_realized = float(cum_realized_u[-1]) if n_days else 0.0

    # Latest ticker & trade_ccy per asset (matching chronological stable sort keep='last').
    tickers_list = df['ticker'].tolist()
    latest_ticker = [''] * n_assets
    latest_ccy = [''] * n_assets
    # Traverse in chronological order (order is sorted by trade_date, side_rank, orig_row;
    # for latest trade_ccy / latest non-empty ticker by trade_date stable, sort by ts_ns only).
    chrono_order = np.argsort(ts_ns, kind='stable')
    for idx in chrono_order:
        col = asset_to_col[assets_list[idx]]
        latest_ccy[col] = str(ccys_list[idx])
        tk = str(tickers_list[idx]) if tickers_list[idx] is not None else ''
        if tk:
            latest_ticker[col] = tk

    # Recorded daily prices & FX per asset in CSV order (drop_duplicates(['day', 'asset'], keep='last')).
    rec_px_dicts: list[dict[int, float]] = [{} for _ in range(n_assets)]
    rec_fx_dicts: list[dict[int, float]] = [{} for _ in range(n_assets)]
    for i in range(len(assets_list)):
        col = asset_to_col[assets_list[i]]
        d_ord = int(days_ord_arr[i])
        rec_px_dicts[col][d_ord] = float(prices_arr[i])
        rec_fx_dicts[col][d_ord] = float(fx_arr[i])

    rec_px = []
    rec_fx = []
    for col in range(n_assets):
        px_items = sorted(rec_px_dicts[col].items())
        rec_px.append((
            np.fromiter((k for k, _ in px_items), dtype=np.int32, count=len(px_items)),
            np.fromiter((v for _, v in px_items), dtype=np.float64, count=len(px_items)),
        ))
        fx_items = sorted(rec_fx_dicts[col].items())
        rec_fx.append((
            np.fromiter((k for k, _ in fx_items), dtype=np.int32, count=len(fx_items)),
            np.fromiter((v for _, v in fx_items), dtype=np.float64, count=len(fx_items)),
        ))

    # Exact holding window [first_held_ord, last_held_ord] per asset.
    first_held: list[int | None] = []
    last_held: list[int | None] = []
    for col in range(n_assets):
        pos_idx = np.flatnonzero(qty_mat[:, col] > 1e-9)
        if len(pos_idx) == 0:
            first_held.append(None)
            last_held.append(None)
        else:
            first_held.append(int(u_days_ord[int(pos_idx[0])]))
            if qty_mat[-1, col] > 1e-9:
                last_held.append(None)
            else:
                # First day after last positive day where qty dropped to <= 1e-9.
                last_pos = int(pos_idx[-1])
                close_u = min(last_pos + 1, n_days - 1)
                last_held.append(int(u_days_ord[close_u]))

    bundle = _DailyFifoBundle(
        u_days_ord=u_days_ord,
        assets=tuple(asset_names),
        qty_mat=qty_mat,
        cost_mat=cost_mat,
        cum_realized_u=cum_realized_u,
        all_time_realized=all_time_realized,
        tickers=tuple(latest_ticker),
        ccys=tuple(latest_ccy),
        first_held_ord=tuple(first_held),
        last_held_ord=tuple(last_held),
        rec_px=tuple(rec_px),
        rec_fx=tuple(rec_fx),
        first_trade_date=first_trade_date,
    )
    if cache_key is not None:
        with _cache_lock:
            _fifo_bundle_cache[cache_key] = bundle
            _fifo_bundle_cache.move_to_end(cache_key)
            while len(_fifo_bundle_cache) > 8:
                _fifo_bundle_cache.popitem(last=False)
    return bundle


def _build_daily_cash_bundle(
    cash_df: pd.DataFrame,
    cache_key: str | None = None,
    computed: pd.DataFrame | None = None,
) -> _DailyCashBundle:
    """Fast NumPy daily cash replay matching ForexTracker average-cost rules, cached per hash."""
    if cache_key is not None:
        with _cache_lock:
            hit = _cash_bundle_cache.get(cache_key)
            if hit is not None:
                _cash_bundle_cache.move_to_end(cache_key)
                return hit

    comp = computed if computed is not None else cash_engine.recompute_average_cost(cash_df)
    ts_ns = portfolio._trade_date_ns(comp['date'])
    days_ord_arr = (ts_ns // _DAY_NS + _EPOCH_ORDINAL).astype(np.int32)
    u_days_ord, inv_day = np.unique(days_ord_arr, return_inverse=True)
    n_days = len(u_days_ord)
    first_cash_date = date.fromordinal(int(u_days_ord[0]))

    ccy_list = [str(c).upper() for c in comp['foreign_ccy'].tolist()]
    r_pos = comp['running_position'].to_numpy(dtype=float)
    r_cost = comp['running_cost_base'].to_numpy(dtype=float)
    r_pl = comp['realized_pl_base'].to_numpy(dtype=float)

    ccys_sorted = sorted(set(ccy_list))
    ccy_to_col = {c: i for i, c in enumerate(ccys_sorted)}
    n_ccys = len(ccys_sorted)

    pos_mat = np.full((n_days, n_ccys), np.nan, dtype=np.float64)
    cost_mat = np.full((n_days, n_ccys), np.nan, dtype=np.float64)
    realized_by_u = np.zeros(n_days, dtype=np.float64)

    for i in range(len(ccy_list)):
        u_i = int(inv_day[i])
        col = ccy_to_col[ccy_list[i]]
        pos_mat[u_i, col] = float(r_pos[i])
        cost_mat[u_i, col] = float(r_cost[i])
        realized_by_u[u_i] += float(r_pl[i])

    pos_mat = _ffill_2d(pos_mat)
    cost_mat = _ffill_2d(cost_mat)
    cum_realized_u = np.cumsum(realized_by_u)
    all_time_realized = float(cum_realized_u[-1]) if n_days else 0.0

    # Recorded cash FX in original CSV order (drop_duplicates(['day', 'foreign_ccy'], keep='last')).
    raw_ts_ns = portfolio._trade_date_ns(cash_df['date'])
    raw_days = (raw_ts_ns // _DAY_NS + _EPOCH_ORDINAL).astype(np.int32)
    raw_ccys = [str(c).upper() for c in cash_df['foreign_ccy'].tolist()]
    raw_fx = cash_df['fx_rate'].to_numpy(dtype=float)
    rec_fx_dicts: dict[str, dict[int, float]] = {}
    for i in range(len(raw_ccys)):
        c = raw_ccys[i]
        d_map = rec_fx_dicts.get(c)
        if d_map is None:
            d_map = {}
            rec_fx_dicts[c] = d_map
        d_map[int(raw_days[i])] = float(raw_fx[i])
    rec_fx: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for c, d_map in rec_fx_dicts.items():
        items = sorted(d_map.items())
        rec_fx[c] = (
            np.fromiter((k for k, _ in items), dtype=np.int32, count=len(items)),
            np.fromiter((v for _, v in items), dtype=np.float64, count=len(items)),
        )

    bundle = _DailyCashBundle(
        u_days_ord=u_days_ord,
        ccys=tuple(ccys_sorted),
        pos_mat=pos_mat,
        cost_mat=cost_mat,
        cum_realized_u=cum_realized_u,
        all_time_realized=all_time_realized,
        rec_fx=rec_fx,
        first_cash_date=first_cash_date,
    )
    if cache_key is not None:
        with _cache_lock:
            _cash_bundle_cache[cache_key] = bundle
            _cash_bundle_cache.move_to_end(cache_key)
            while len(_cash_bundle_cache) > 8:
                _cash_bundle_cache.popitem(last=False)
    return bundle


def _daily_fifo_state(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series, float]:
    """Replay trades chronologically with exact portfolio.py FIFO rules on unit_base."""
    bundle = _build_daily_fifo_bundle(df)
    dt_index = pd.DatetimeIndex([pd.Timestamp(date.fromordinal(int(d))) for d in bundle.u_days_ord])
    qty_df = pd.DataFrame(bundle.qty_mat, index=dt_index, columns=list(bundle.assets))
    cost_df = pd.DataFrame(bundle.cost_mat, index=dt_index, columns=list(bundle.assets))
    if len(bundle.u_days_ord):
        r_series = pd.Series(bundle.cum_realized_u, index=dt_index, dtype='float64')
    else:
        r_series = pd.Series(dtype='float64')
    return qty_df, cost_df, r_series, bundle.all_time_realized


def _daily_cash_state(cash_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series, float]:
    """Replay cash_txns with exact ForexTracker average-cost rules."""
    bundle = _build_daily_cash_bundle(cash_df)
    dt_index = pd.DatetimeIndex([pd.Timestamp(date.fromordinal(int(d))) for d in bundle.u_days_ord])
    pos_df = pd.DataFrame(bundle.pos_mat, index=dt_index, columns=list(bundle.ccys))
    cost_df = pd.DataFrame(bundle.cost_mat, index=dt_index, columns=list(bundle.ccys))
    r_series = pd.Series(bundle.cum_realized_u, index=dt_index, dtype='float64')
    return pos_df, cost_df, r_series, bundle.all_time_realized


def _latest_nse_overlay(end: date) -> tuple[int, dict[str, float]] | None:
    """Fast cache-only lookup for the latest applicable NSE official close snapshot."""
    if not _NSE_OFFICIAL_CLOSE:
        return None
    candidate_days = _nse_report_dates()
    snapshot = _read_cached(['nse:close:' + d.strftime('%Y%m%d') for d in candidate_days])
    for d in candidate_days:
        record = snapshot.get('nse:close:' + d.strftime('%Y%m%d'))
        if record and record.values and d <= end:
            return d.toordinal(), record.values
    return None


def _compute_inv_ticker_bounds(
    bundle: _DailyFifoBundle,
    active_col_indices: np.ndarray,
    market_start: date,
    end: date,
    expand_to_full_held: bool,
) -> dict[str, tuple[date, date, date | None]]:
    """Compute minimal per-ticker [req_start, req_end, expand_start] bounds from actual held periods."""
    ticker_bounds: dict[str, tuple[date, date, date | None]] = {}
    for col in active_col_indices:
        tk = bundle.tickers[int(col)]
        if not tk:
            continue
        f_ord = bundle.first_held_ord[int(col)]
        l_ord = bundle.last_held_ord[int(col)]
        held_start = date.fromordinal(f_ord) - timedelta(days=21) if f_ord is not None else market_start
        held_end = min(end, date.fromordinal(l_ord)) if l_ord is not None else end
        req_start = min(held_end, max(market_start, held_start))
        req_end = held_end
        exp_start = held_start if (expand_to_full_held and held_start < req_start) else None
        prev = ticker_bounds.get(tk)
        if prev is None:
            ticker_bounds[tk] = (req_start, req_end, exp_start)
        else:
            p_exp = prev[2]
            if exp_start is not None and p_exp is not None:
                merged_exp = min(exp_start, p_exp)
            else:
                merged_exp = exp_start if exp_start is not None else p_exp
            ticker_bounds[tk] = (min(prev[0], req_start), max(prev[1], req_end), merged_exp)
    return ticker_bounds


def _investments_daily_arrays(
    df: pd.DataFrame,
    base: str,
    target_ords: np.ndarray,
    start: date,
    end: date,
    *,
    bundle: _DailyFifoBundle | None = None,
    expand_to_full_held: bool = False,
    preloaded_fx: tuple[dict[str, _CacheRecord], dict[str, str]] | None = None,
    preloaded_prices: tuple[dict[str, _CacheRecord], dict[str, str]] | None = None,
    fx_expand_start: date | None = None,
) -> dict:
    """Compute daily investment value, invested cost, and cumulative development on target_ords."""
    n = len(target_ords)
    zeros = np.zeros(n, dtype=float)
    if df is None or df.empty:
        return {
            'value': zeros, 'invested': zeros, 'cum_dev': zeros,
            'realized_total': 0.0, 'warnings': [],
            'estimated_assets': [], 'excluded_assets': [], 'price_count': 0,
        }

    if bundle is None:
        bundle = _build_daily_fifo_bundle(df)

    pos = np.searchsorted(bundle.u_days_ord, target_ords, side='right') - 1
    valid = pos >= 0
    safe_pos = np.maximum(pos, 0)
    changes_all = np.where(valid[:, None], bundle.qty_mat[safe_pos], 0.0)
    costs_all = np.where(valid[:, None], bundle.cost_mat[safe_pos], 0.0)
    cum_realized = np.where(valid, bundle.cum_realized_u[safe_pos], 0.0)

    active_idx = np.flatnonzero((changes_all > 1e-9).any(axis=0))
    if len(active_idx) == 0:
        return {
            'value': zeros, 'invested': zeros, 'cum_dev': cum_realized,
            'realized_total': bundle.all_time_realized, 'warnings': [],
            'estimated_assets': [], 'excluded_assets': [], 'price_count': 0,
        }

    qty_arr = changes_all[:, active_idx]
    cost_arr = costs_all[:, active_idx]
    n_act = len(active_idx)
    assets = [bundle.assets[int(c)] for c in active_idx]
    tickers = [bundle.tickers[int(c)] for c in active_idx]
    ccys = [bundle.ccys[int(c)] for c in active_idx]

    market_start = start - timedelta(days=10)
    need_prices = preloaded_prices is None
    need_fx = preloaded_fx is None
    if need_prices or need_fx:
        ticker_bounds = _compute_inv_ticker_bounds(bundle, active_idx, market_start, end, expand_to_full_held) if need_prices else {}
        price_keys = {t: 'price:' + t for t in ticker_bounds}
        fx_keys_list = [f'fx:{c}:{base}' for c in dict.fromkeys(ccys) if c != base] if need_fx else []
        preload_keys = list(price_keys.values()) + fx_keys_list
        if preload_keys:
            _read_cached(preload_keys)
        if need_fx and need_prices and price_keys and fx_keys_list:
            fjob = _coord_pool.submit(_get_fx_records, ccys, base, market_start, end, False, fx_expand_start)
            price_records, price_errors = _series_records_with_windows(
                price_keys, ticker_bounds, False, _HIST_REFRESH_HOURS, _fetch_chart, pool=_io_pool
            )
            fx_records, fx_errors = fjob.result()
        else:
            if need_prices:
                price_records, price_errors = _series_records_with_windows(
                    price_keys, ticker_bounds, False, _HIST_REFRESH_HOURS, _fetch_chart, pool=_io_pool
                )
            else:
                price_records, price_errors = preloaded_prices
            if need_fx:
                fx_records, fx_errors = _get_fx_records(ccys, base, market_start, end, False, fx_expand_start)
            else:
                fx_records, fx_errors = preloaded_fx
    else:
        price_records, price_errors = preloaded_prices
        fx_records, fx_errors = preloaded_fx

    has_ns_price = any(t.upper().endswith('.NS') for t in price_records)
    nse_overlay = _latest_nse_overlay(end) if has_ns_price else None

    # Align market prices & recorded prices directly into (n, n_act) NumPy arrays.
    market_px_mat = np.full((n, n_act), np.nan, dtype=np.float64)
    rec_px_mat = np.full((n, n_act), np.nan, dtype=np.float64)
    aligned_ticker_cache: dict[str, np.ndarray] = {}
    for j in range(n_act):
        col = int(active_idx[j])
        r_ords, r_vals = bundle.rec_px[col]
        rec_px_mat[:, j] = _align_ord_series(r_ords, r_vals, target_ords, bfill=False)
        tk = tickers[j]
        if not tk:
            continue
        cached_col = aligned_ticker_cache.get(tk)
        if cached_col is None:
            rec = price_records.get(tk)
            if rec is not None and rec.values:
                p_ords, p_vals = _record_ord_arrays(rec)
                ov = None
                if nse_overlay is not None and tk.upper().endswith('.NS'):
                    sym = tk[:-3].upper()
                    if sym in nse_overlay[1]:
                        ov = (nse_overlay[0], float(nse_overlay[1][sym]))
                cached_col = _align_ord_series(p_ords, p_vals, target_ords, bfill=False, override=ov)
            else:
                cached_col = np.full(n, np.nan, dtype=np.float64)
            aligned_ticker_cache[tk] = cached_col
        market_px_mat[:, j] = cached_col

    positive = qty_arr > 1e-9
    market_isna = np.isnan(market_px_mat)
    rec_notna = ~np.isnan(rec_px_mat)
    estimated_flags = (market_isna & rec_notna & positive).any(axis=0)
    prices_used = np.where(market_isna, rec_px_mat, market_px_mat)
    prices_notna = ~np.isnan(prices_used)
    unknown_flags = ((~prices_notna) & positive).any(axis=0)

    # Align FX rates per unique currency, then broadcast to active assets.
    aligned_fx: dict[str, np.ndarray] = {base: np.ones(n, dtype=np.float64)}
    for ccy in dict.fromkeys(ccys):
        if ccy == base:
            continue
        rec = fx_records.get(ccy)
        if rec is not None and rec.values:
            f_ords, f_vals = _record_ord_arrays(rec)
            aligned_fx[ccy] = _align_ord_series(f_ords, f_vals, target_ords, bfill=True)

    fx_mat = np.full((n, n_act), np.nan, dtype=np.float64)
    for j in range(n_act):
        ccy = ccys[j]
        col_fx = aligned_fx.get(ccy)
        if col_fx is not None:
            fx_mat[:, j] = col_fx
        else:
            col = int(active_idx[j])
            rf_ords, rf_vals = bundle.rec_fx[col]
            rec_col = _align_ord_series(rf_ords, rf_vals, target_ords, bfill=True)
            fx_mat[:, j] = rec_col
            avail = bool((~np.isnan(rec_col)).any())
            estimated_flags[j] = bool(estimated_flags[j] or avail)
            unknown_flags[j] = bool(unknown_flags[j] or (not avail))

    estimated_assets = [assets[j] for j in range(n_act) if estimated_flags[j]]
    unknown_assets = [assets[j] for j in range(n_act) if unknown_flags[j]]

    valued_mask = positive & prices_notna & (~np.isnan(fx_mat))
    px_arr = np.nan_to_num(prices_used, nan=0.0)
    fx_arr = np.nan_to_num(fx_mat, nan=0.0)
    val_mat = np.where(valued_mask, qty_arr * px_arr * fx_arr, 0.0)
    cost_mat = np.where(valued_mask, cost_arr, 0.0)
    portfolio_daily = val_mat.sum(axis=1)
    invested_daily = cost_mat.sum(axis=1)
    unrealized_daily = (val_mat - cost_mat).sum(axis=1)
    cum_dev = unrealized_daily + cum_realized

    inv_fx_errors = {c: e for c, e in fx_errors.items() if c in ccys}
    warnings = []
    if price_errors:
        warnings.append(f'Historical quote errors for {len(price_errors)} ticker(s).')
    if inv_fx_errors:
        warnings.append(f'Historical FX errors for {len(inv_fx_errors)} currency pair(s); recorded transaction FX used as an estimate where possible.')
    if estimated_assets:
        warnings.append('Some dates use the last trade price as an estimate where market-history quotes are missing.')
    if unknown_assets:
        warnings.append('Some assets/dates have no usable quote or FX rate and are omitted from those historical totals.')

    return {
        'value': portfolio_daily,
        'invested': invested_daily,
        'cum_dev': cum_dev,
        'realized_total': bundle.all_time_realized,
        'warnings': warnings,
        'estimated_assets': sorted(set(estimated_assets)),
        'excluded_assets': sorted(set(unknown_assets)),
        'price_count': sum(1 for rec in price_records.values() if rec.values),
    }


def _cash_daily_arrays(
    cash_df: pd.DataFrame | None,
    base: str,
    target_ords: np.ndarray,
    start: date,
    end: date,
    *,
    bundle: _DailyCashBundle | None = None,
    preloaded_fx: tuple[dict[str, _CacheRecord], dict[str, str]] | None = None,
    fx_expand_start: date | None = None,
) -> dict:
    """Compute daily cash value, invested cost, and cumulative FX development on target_ords."""
    n = len(target_ords)
    zeros = np.zeros(n, dtype=float)
    if cash_df is None or cash_df.empty:
        return {
            'value': zeros, 'invested': zeros, 'cum_dev': zeros,
            'realized_total': 0.0, 'warnings': [],
        }

    base_u = base.upper()
    if bundle is None:
        bundle = _build_daily_cash_bundle(cash_df)

    pos = np.searchsorted(bundle.u_days_ord, target_ords, side='right') - 1
    valid = pos >= 0
    safe_pos = np.maximum(pos, 0)
    pos_all = np.where(valid[:, None], bundle.pos_mat[safe_pos], 0.0)
    cost_all = np.where(valid[:, None], bundle.cost_mat[safe_pos], 0.0)
    cum_realized = np.where(valid, bundle.cum_realized_u[safe_pos], 0.0)

    active_idx = np.flatnonzero((np.abs(pos_all) > 1e-9).any(axis=0))
    if len(active_idx) == 0:
        return {
            'value': zeros, 'invested': zeros, 'cum_dev': cum_realized,
            'realized_total': bundle.all_time_realized, 'warnings': [],
        }

    pos_arr = pos_all[:, active_idx]
    cost_arr = cost_all[:, active_idx]
    active_ccys = [bundle.ccys[int(c)] for c in active_idx]
    n_act = len(active_ccys)

    if preloaded_fx is None:
        market_start = start - timedelta(days=10)
        fx_records, fx_errors = _get_fx_records(active_ccys, base_u, market_start, end, False, fx_expand_start)
    else:
        fx_records, fx_errors = preloaded_fx

    fx_mat = np.full((n, n_act), np.nan, dtype=np.float64)
    for j, ccy in enumerate(active_ccys):
        if ccy == base_u:
            fx_mat[:, j] = 1.0
            continue
        rec = fx_records.get(ccy)
        if rec is not None and rec.values:
            f_ords, f_vals = _record_ord_arrays(rec)
            fx_mat[:, j] = _align_ord_series(f_ords, f_vals, target_ords, bfill=True)
        else:
            rf = bundle.rec_fx.get(ccy)
            if rf is not None:
                fx_mat[:, j] = _align_ord_series(rf[0], rf[1], target_ords, bfill=True)

    active_mask = np.abs(pos_arr) > 1e-9
    valued_mask = active_mask & (~np.isnan(fx_mat))
    fx_clean = np.nan_to_num(fx_mat, nan=0.0)
    val_mat = np.where(valued_mask, pos_arr * fx_clean, 0.0)
    cost_mat = np.where(valued_mask, cost_arr, 0.0)

    value_daily = val_mat.sum(axis=1)
    invested_daily = cost_mat.sum(axis=1)
    unrealized_daily = (val_mat - cost_mat).sum(axis=1)
    cum_dev = unrealized_daily + cum_realized

    cash_fx_errors = {c: e for c, e in fx_errors.items() if c in active_ccys}
    warnings = []
    if cash_fx_errors:
        warnings.append(f'Historical cash FX errors for {len(cash_fx_errors)} currency pair(s); recorded transaction FX used where possible.')

    return {
        'value': value_daily,
        'invested': invested_daily,
        'cum_dev': cum_dev,
        'realized_total': bundle.all_time_realized,
        'warnings': warnings,
    }


def _calculate_history(
    df: pd.DataFrame,
    cash_df: pd.DataFrame | None,
    base: str,
    span: str,
    mode: str,
    *,
    inv_hash_bytes: bytes | None = None,
    cash_hash_bytes: bytes | None = None,
    cash_computed: pd.DataFrame | None = None,
    sibling_out: dict[str, dict] | None = None,
) -> dict:
    """Daily Development = Unrealized P/L + Realized P/L for investments, cash, or NAV."""
    if span not in ('1M', '3M', '6M', '1Y', '3Y', 'MAX'):
        raise ValueError('Unsupported range')
    empty_reply = {
        'points': [], 'warnings': [], 'estimated_assets': [], 'excluded_assets': [],
        'range': span, 'mode': mode, 'baseline': 0.0, 'development': 0.0, 'realized_total': 0.0,
    }
    has_inv = df is not None and not df.empty
    has_cash = cash_df is not None and not cash_df.empty
    if mode == 'investments' and not has_inv:
        return empty_reply
    if mode == 'cash' and not has_cash:
        return empty_reply
    if mode == 'nav' and not has_inv and not has_cash:
        return empty_reply

    begin = time.perf_counter()
    inv_bundle = None
    cash_bundle = None
    if mode in ('investments', 'nav') and has_inv:
        inv_key = hashlib.blake2b(inv_hash_bytes, digest_size=12).hexdigest() if inv_hash_bytes else None
        inv_bundle = _build_daily_fifo_bundle(df, cache_key=inv_key)
    if mode in ('cash', 'nav') and has_cash:
        cash_key = hashlib.blake2b(cash_hash_bytes, digest_size=12).hexdigest() if cash_hash_bytes else None
        cash_bundle = _build_daily_cash_bundle(cash_df, cache_key=cash_key, computed=cash_computed)

    end = date.today()
    first_candidates = []
    if inv_bundle is not None:
        first_candidates.append(inv_bundle.first_trade_date)
    if cash_bundle is not None:
        first_candidates.append(cash_bundle.first_cash_date)
    first = min(first_candidates)
    span_days = {'1M': 31, '3M': 92, '6M': 183, '1Y': 366, '3Y': 1096, 'MAX': 999999}[span]
    start = first if span == 'MAX' else max(first, end - timedelta(days=span_days))
    if start > end:
        return empty_reply

    start_ord = start.toordinal()
    end_ord = end.toordinal()
    target_ords = np.arange(start_ord, end_ord + 1, dtype=np.int32)
    n_days = len(target_ords)
    rebase = (span != 'MAX' and start > first)
    expand_full = (span != '1M')
    fx_expand_start = (first - timedelta(days=10)) if expand_full else None

    if mode == 'investments':
        inv_res = _investments_daily_arrays(
            df, base, target_ords, start, end,
            bundle=inv_bundle, expand_to_full_held=expand_full,
            fx_expand_start=fx_expand_start,
        )
        reply = _format_history_reply(
            start_ord, n_days, span, mode, rebase,
            inv_res['value'], inv_res['invested'], inv_res['cum_dev'],
            inv_res['realized_total'], inv_res['warnings'],
            inv_res['estimated_assets'], inv_res['excluded_assets'], inv_res['price_count'],
        )
    elif mode == 'cash':
        cash_res = _cash_daily_arrays(
            cash_df, base, target_ords, start, end,
            bundle=cash_bundle, fx_expand_start=fx_expand_start,
        )
        reply = _format_history_reply(
            start_ord, n_days, span, mode, rebase,
            cash_res['value'], cash_res['invested'], cash_res['cum_dev'],
            cash_res['realized_total'], cash_res['warnings'],
            [], [], 0,
        )
    else:
        # NAV mode: gather union of active currencies & tickers once, batch-read DB cache once,
        # and run Yahoo prices (_io_pool) and Frankfurter FX (_fx_pool) concurrently!
        all_ccys: list[str] = []
        act_i = np.empty(0, dtype=np.int64)
        if inv_bundle is not None:
            pos_i = np.searchsorted(inv_bundle.u_days_ord, target_ords, side='right') - 1
            valid_i = pos_i >= 0
            ch_i = np.where(valid_i[:, None], inv_bundle.qty_mat[np.maximum(pos_i, 0)], 0.0)
            act_i = np.flatnonzero((ch_i > 1e-9).any(axis=0))
            all_ccys.extend(inv_bundle.ccys[int(c)] for c in act_i)
        if cash_bundle is not None:
            pos_c = np.searchsorted(cash_bundle.u_days_ord, target_ords, side='right') - 1
            valid_c = pos_c >= 0
            ch_c = np.where(valid_c[:, None], cash_bundle.pos_mat[np.maximum(pos_c, 0)], 0.0)
            act_c = np.flatnonzero((np.abs(ch_c) > 1e-9).any(axis=0))
            all_ccys.extend(cash_bundle.ccys[int(c)] for c in act_c)
        all_ccys = list(dict.fromkeys(all_ccys))

        base_u = base.upper()
        market_start = start - timedelta(days=10)
        t_bounds = (
            _compute_inv_ticker_bounds(inv_bundle, act_i, market_start, end, expand_full)
            if (inv_bundle is not None and len(act_i) > 0) else {}
        )
        p_keys = {t: 'price:' + t for t in t_bounds}
        fx_keys_list = [f'fx:{c}:{base_u}' for c in all_ccys if c != base_u]
        if p_keys or fx_keys_list:
            _read_cached(list(p_keys.values()) + fx_keys_list)

        fx_future = (
            _coord_pool.submit(_get_fx_records, all_ccys, base_u, market_start, end, False, fx_expand_start)
            if fx_keys_list else None
        )
        preloaded_prices = (
            _series_records_with_windows(p_keys, t_bounds, False, _HIST_REFRESH_HOURS, _fetch_chart, pool=_io_pool)
            if p_keys else ({}, {})
        )
        preloaded_fx = fx_future.result() if fx_future is not None else ({}, {})

        inv_res = _investments_daily_arrays(
            df if has_inv else pd.DataFrame(), base, target_ords, start, end,
            bundle=inv_bundle, expand_to_full_held=expand_full,
            preloaded_fx=preloaded_fx, preloaded_prices=preloaded_prices,
        )
        cash_res = _cash_daily_arrays(
            cash_df if has_cash else None, base, target_ords, start, end,
            bundle=cash_bundle, preloaded_fx=preloaded_fx,
        )
        portfolio_daily = inv_res['value'] + cash_res['value']
        invested_daily = inv_res['invested'] + cash_res['invested']
        cum_dev = inv_res['cum_dev'] + cash_res['cum_dev']
        all_realized = inv_res['realized_total'] + cash_res['realized_total']
        warnings = list(dict.fromkeys(inv_res['warnings'] + cash_res['warnings']))

        reply = _format_history_reply(
            start_ord, n_days, span, mode, rebase,
            portfolio_daily, invested_daily, cum_dev,
            all_realized, warnings,
            inv_res['estimated_assets'], inv_res['excluded_assets'], inv_res['price_count'],
        )
        # Populate sibling modes when their start date & rebase flag match this NAV window.
        if sibling_out is not None:
            if inv_bundle is not None:
                f_inv = inv_bundle.first_trade_date
                s_inv = f_inv if span == 'MAX' else max(f_inv, end - timedelta(days=span_days))
                r_inv = (span != 'MAX' and s_inv > f_inv)
                if s_inv == start and r_inv == rebase:
                    sibling_out['investments'] = _format_history_reply(
                        start_ord, n_days, span, 'investments', r_inv,
                        inv_res['value'], inv_res['invested'], inv_res['cum_dev'],
                        inv_res['realized_total'], inv_res['warnings'],
                        inv_res['estimated_assets'], inv_res['excluded_assets'], inv_res['price_count'],
                    )
            if cash_bundle is not None:
                f_cash = cash_bundle.first_cash_date
                s_cash = f_cash if span == 'MAX' else max(f_cash, end - timedelta(days=span_days))
                r_cash = (span != 'MAX' and s_cash > f_cash)
                if s_cash == start and r_cash == rebase:
                    sibling_out['cash'] = _format_history_reply(
                        start_ord, n_days, span, 'cash', r_cash,
                        cash_res['value'], cash_res['invested'], cash_res['cum_dev'],
                        cash_res['realized_total'], cash_res['warnings'],
                        [], [], 0,
                    )

    _LOG.info('market.history mode=%s span=%s total_seconds=%.2f points=%d',
              mode, span, time.perf_counter() - begin, len(reply['points']))
    return reply


def warm_history_cache(
    df: pd.DataFrame | None,
    cash_df: pd.DataFrame | None,
    base: str,
    *,
    inv_hash_bytes: bytes | None = None,
    cash_hash_bytes: bytes | None = None,
    cash_computed: pd.DataFrame | None = None,
) -> None:
    """Gentle, deduplicated background warm-up of exact held price & FX history after initial 1M load."""
    has_inv = df is not None and not df.empty
    has_cash = cash_df is not None and not cash_df.empty
    if not has_inv and not has_cash:
        return
    inv_k = hashlib.blake2b(inv_hash_bytes, digest_size=12).hexdigest() if inv_hash_bytes else ''
    cash_k = hashlib.blake2b(cash_hash_bytes, digest_size=12).hexdigest() if cash_hash_bytes else ''
    warm_key = ('hist-warm', base.upper(), inv_k, cash_k, date.today().isoformat())
    now = time.monotonic()
    with _cache_lock:
        if _warmed_keys.get(warm_key, 0.0) > now:
            return
        _warmed_keys[warm_key] = now + 1800.0
        _warmed_keys.move_to_end(warm_key)
        while len(_warmed_keys) > 64:
            _warmed_keys.popitem(last=False)

    def _run():
        try:
            end = date.today()
            first_dates = []
            ccys: list[str] = []
            ticker_bounds: dict[str, tuple[date, date, date | None]] = {}
            if has_inv:
                ib = _build_daily_fifo_bundle(df, cache_key=inv_k or None)
                first_dates.append(ib.first_trade_date)
                all_idx = np.flatnonzero((ib.qty_mat > 1e-9).any(axis=0))
                if len(all_idx):
                    m_start = ib.first_trade_date - timedelta(days=10)
                    ticker_bounds = _compute_inv_ticker_bounds(ib, all_idx, m_start, end, expand_to_full_held=True)
                    ccys.extend(ib.ccys[int(c)] for c in all_idx)
            if has_cash:
                cb = _build_daily_cash_bundle(cash_df, cache_key=cash_k or None, computed=cash_computed)
                first_dates.append(cb.first_cash_date)
                ccys.extend(cb.ccys)
            if not first_dates:
                return
            first_all = min(first_dates)
            fx_start = first_all - timedelta(days=10)
            ccys = list(dict.fromkeys(ccys))
            base_u = base.upper()
            p_keys = {t: 'price:' + t for t in ticker_bounds}
            fx_keys_list = [f'fx:{c}:{base_u}' for c in ccys if c != base_u]
            if p_keys or fx_keys_list:
                _read_cached(list(p_keys.values()) + fx_keys_list)
            f_fut = (
                _coord_pool.submit(_get_fx_records, ccys, base_u, fx_start, end, False, None, _fx_pool)
                if fx_keys_list else None
            )
            if p_keys:
                _series_records_with_windows(p_keys, ticker_bounds, False, _HIST_REFRESH_HOURS, _fetch_chart, pool=_bg_io_pool)
            if f_fut is not None:
                f_fut.result()
        except Exception:
            _LOG.debug('Background history warm-up skipped', exc_info=True)

    _bg_pool.submit(_run)


def _calculate_portfolio_history(df: pd.DataFrame, base: str, span: str) -> dict:
    """Backward-compatible wrapper for investment-only history."""
    return _calculate_history(df, None, base, span, 'investments')
