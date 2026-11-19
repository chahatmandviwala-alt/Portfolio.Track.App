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
import csv
import io
import zipfile
import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, time as daytime

import numpy as np
import pandas as pd
import requests
from sqlalchemy import select
from storage import MarketCache, Session, engine

_LOG = logging.getLogger('uvicorn.error')
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; PortfolioTracker/1.0)', 'Accept': 'application/json'}
_HIST_REFRESH_HOURS = max(1., float(os.getenv('HISTORICAL_REFRESH_HOURS', '12')))
_FX_REFRESH_HOURS = max(1., float(os.getenv('FX_REFRESH_HOURS', '12')))
_NSE_OFFICIAL_CLOSE = os.getenv('NSE_OFFICIAL_CLOSE', '1').strip().lower() not in ('0', 'false', 'no')
_NSE_EOD_COOLDOWN = max(120, int(os.getenv('NSE_EOD_COOLDOWN_SECONDS', '900')))
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
_local_rows: OrderedDict[str, tuple[float, _CacheRecord | None]] = OrderedDict()
_quote_rows: OrderedDict[str, tuple[float, dict]] = OrderedDict()
_chart_rows: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
_nse_block_until = 0.0
_nse_eod_block_until = 0.0
_nse_eod_missing_dates: dict[str, float] = {}
_http_local = threading.local()
# A process-wide pool reuses provider connections across requests and caps upstream concurrency.
_io_pool = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix='market-io')
_pending_io: dict[tuple, Future] = {}
_pending_charts: dict[tuple, Future] = {}


def _submit_io(key, function, *args):
    def forget(future):
        with _cache_lock:
            if _pending_io.get(key) is future:
                del _pending_io[key]

    with _cache_lock:
        future = _pending_io.get(key)
        if future is None:
            future = _io_pool.submit(function, *args)
            _pending_io[key] = future
            future.add_done_callback(forget)
        return future


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
        with _cache_lock:
            for key in missing:
                # Cache database misses too: a first request often needs both
                # prices and FX and should not perform another SELECT for each.
                item = found.get(key)
                _local_rows[key] = (now + (300 if item is not None else 30), item)
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
            _local_rows[key] = (now + 300, item)
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
    """Parse the NSE UDiFF equity bhavcopy; use ClsPric, never LastPric.

    Restricts archive size and validates column names, security series and
    business date. Malformed reports must not be cached as authoritative.
    """
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
                    # Never insert another day's price into a cached snapshot.
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
    """Use compressed UDiFF file, falling back to official full CSV on 404.

    NSE can reject calls from hosted cloud IPs. Do not substitute an intraday
    value and call it a close when its authoritative reports are unreachable.
    """
    name = f'BhavCopy_NSE_CM_0_0_0_{day:%Y%m%d}_F_0000.csv.zip'
    url = f'https://nsearchives.nseindia.com/content/cm/{name}'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Accept': 'application/zip,text/csv,*/*',
        'Referer': 'https://www.nseindia.com/',
    }
    response = _http().get(url, headers=headers, timeout=(2.0, 4.0))
    if response.status_code == 404:
        # Published alternative equity report (same exchange, not a third party).
        alternate = f'https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{day:%d%m%Y}.csv'
        fallback = _http().get(alternate, headers=headers, timeout=(2.0, 4.0))
        fallback.raise_for_status()
        return _parse_nse_full_csv(fallback.content, day)
    response.raise_for_status()
    return _parse_nse_bhavcopy(response.content, day)


def _load_latest_nse_eod() -> tuple[date | None, dict[str, float]]:
    """Return latest official close for every NSE equity via one shared file.

    DB rows are PUBLIC market data, not a user portfolio. Fail closed to the
    existing quote provider if the exchange blocks cloud traffic.
    """
    global _nse_eod_block_until
    days = _nse_report_dates()
    now = time.monotonic()
    keys = {day: 'nse:close:' + day.strftime('%Y%m%d') for day in days}
    cached = _read_cached(list(keys.values()))
    for day in days:
        key = keys[day]
        item = cached.get(key)
        if item and item.values:
            return day, dict(item.values)
        with _cache_lock:
            if now < _nse_eod_block_until:
                return None, {}
            if now < _nse_eod_missing_dates.get(key, 0):
                # A missing report can be due to a market holiday. Try older.
                continue
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
                # On a normal weekday after close, a not-yet-published
                # report must NOT turn yesterday's close into today's quote.
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
            # Do not allow a transient Supabase cache failure to block prices.
            _LOG.exception('NSE report cache failure')
            break
    return None, {}


def _nse_official_quotes(tickers: list[str], report: Future | None = None) -> dict[str, dict]:
    if not tickers:
        return {}
    if report is None:
        if not _NSE_OFFICIAL_CLOSE or _nse_trading_window():
            return {}
        report = _submit_io(('nse-report', datetime.now(_IST).date()), _load_latest_nse_eod)
    try:
        day, closes = report.result()
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
    """Correct the most recent NSE daily close when official data is cached.

    A chart should not silently mix Yahoo's last-trade value into its official
    closing value for the most recent completed NSE session. Does not fetch a
    new NSE report itself: refresh may already have loaded/cached the snapshot.
    """
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
    return {'price': price, 'source': 'NSE India last traded price',
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
    return {'price': price, 'source': 'Yahoo Finance intraday' + (' (not NSE official close)' if ticker.upper().endswith('.NS') else ''), 'as_of': as_of}


def get_latest_prices(tickers: list[str]) -> tuple[dict, dict]:
    """Latest traded quotes, preserving original (quotes, errors) API.

    Yahoo-first is default on free hosting. NSE is used when Yahoo fails or when
    PRICE_SOURCE_PRIORITY=nse. Store the source and as_of from the provider.
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
        # Non-NSE symbols never depend on the exchange's closing file.
        report = (_submit_io(('nse-report', datetime.now(_IST).date()), _load_latest_nse_eod)
                  if check_close else None)
        waiting = set(indian) if check_close else set()
        futures = {_submit_io(('quote', mode, t), fetch, t): t for t in missing if t not in waiting}
        if report is not None:
            official = _nse_official_quotes(indian, report)
            quotes.update(official)
            for ticker in indian:
                if ticker not in official:
                    futures[_submit_io(('quote', mode, ticker), fetch, ticker)] = ticker
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                quotes[ticker] = future.result()
            except Exception as exc:
                errors[ticker] = str(exc)
        if _QUOTE_TTL:
            with _cache_lock:
                expires = time.monotonic() + _QUOTE_TTL
                for ticker in missing:
                    if ticker in quotes:
                        key = mode + ':' + ticker
                        _quote_rows[key] = (expires, dict(quotes[ticker]))
                        _quote_rows.move_to_end(key)
                while len(_quote_rows) > 200:
                    _quote_rows.popitem(last=False)
        _LOG.info('market.quote_refresh seconds=%.2f requested=%d errors=%d cache_hits=%d',
                  time.perf_counter()-begin, len(names), len(errors), len(names)-len(missing))
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
        for symbol, (key, _, windows) in jobs.items():
            for a, b in windows:
                fut = _submit_io(('series', key, a, b), provider, symbol, a, b)
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


_HISTORY_COLUMNS = ['trade_date', 'asset', 'side', 'quantity', 'ticker', 'trade_ccy',
                    'price_trade_ccy', 'fx_to_base']


def history_revision(df: pd.DataFrame, base: str) -> str:
    """Identify exactly the ledger and official-close inputs used by the daily chart."""
    digest = hashlib.blake2b(digest_size=16)
    digest.update(base.encode())
    digest.update(date.today().isoformat().encode())
    if not df.empty:
        digest.update(pd.util.hash_pandas_object(df[_HISTORY_COLUMNS], index=False).values.tobytes())
        if _NSE_OFFICIAL_CLOSE and df.ticker.str.upper().str.endswith('.NS').any():
            days = [d for d in _nse_report_dates() if d <= date.today()]
            keys = ['nse:close:' + d.strftime('%Y%m%d') for d in days]
            try:
                cached = _read_cached(keys)
            except Exception:
                # Close metadata only affects the chart key. A cache outage must not
                # prevent the dashboard from displaying a stored portfolio valuation.
                _LOG.exception('Could not read NSE close metadata for chart revision')
                digest.update(b'close-metadata-unavailable')
                cached = {}
            for key in keys:
                record = cached.get(key)
                if record and record.values:
                    digest.update((key + record.fetched.isoformat()).encode())
                    break
    return digest.hexdigest()


def portfolio_history(df: pd.DataFrame, base: str, span: str, *, revision=None, owner=None) -> dict:
    if span not in ('1M', '3M', '6M', '1Y', '3Y', 'MAX'):
        raise ValueError('Unsupported range')
    key = (owner, revision or history_revision(df, base), span)
    with _cache_lock:
        hit = _chart_rows.get(key)
        if hit and hit[0] > time.monotonic():
            _chart_rows.move_to_end(key)
            return copy.deepcopy(hit[1])
        pending = _pending_charts.get(key)
        leader = pending is None
        if leader:
            pending = _pending_charts[key] = Future()
    if not leader:
        return copy.deepcopy(pending.result())
    try:
        result = _calculate_portfolio_history(df, base, span)
        with _cache_lock:
            _chart_rows[key] = (time.monotonic() + 300, result)
            while len(_chart_rows) > 30:
                _chart_rows.popitem(last=False)
        pending.set_result(result)
        return copy.deepcopy(result)
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
    # Cumulative balances change only on trade dates. Preserve pre-window balances
    # and clip *after* summation, including portfolios with historical oversells.
    changes = changes.sort_index().cumsum().clip(lower=0).reindex(idx, method='ffill').fillna(0)
    assets = list(changes.columns)
    chronological = df.sort_values('trade_date', kind='stable')
    latest = chronological.drop_duplicates('asset', keep='last').set_index('asset')
    named = chronological[chronological.ticker.ne('')].drop_duplicates('asset', keep='last')
    tickers = named.set_index('asset').ticker.reindex(assets).fillna('').tolist()
    ccys = latest.trade_ccy.reindex(assets).tolist()
    market_start = start - timedelta(days=10)
    begin = time.perf_counter()
    # One Supabase SELECT for both Yahoo-price and FX cache keys. Negative
    # cache entries prevent two redundant queries on the first cold request.
    price_keys = {'price:' + t for t in tickers if t}
    fx_keys = {f'fx:{ccy}:{base}' for ccy in ccys if ccy != base}
    if price_keys or fx_keys:
        _read_cached(sorted(price_keys | fx_keys))
    if price_keys and fx_keys:
        # Network misses for prices and FX are independent: run simultaneously.
        with ThreadPoolExecutor(max_workers=2) as pool:
            pjob = pool.submit(get_prices, tickers, market_start, end)
            fjob = pool.submit(get_fx_series, ccys, base, market_start, end)
            prices, price_errors = pjob.result()
            fxs, fx_errors = fjob.result()
    else:
        prices, price_errors = get_prices(tickers, market_start, end)
        fxs, fx_errors = get_fx_series(ccys, base, market_start, end)
    prices_elapsed = time.perf_counter() - begin
    fx_elapsed = 0.0  # concurrent, included in wall-clock time above
    prices = _overlay_official_nse_history(prices, end)
    # Last row in CSV order on each trade day, as in the original price/FX fallback.
    daily_trades = df.assign(day=delta.trade_date).drop_duplicates(['day', 'asset'], keep='last')
    daily_trades = daily_trades.set_index(['day', 'asset'])
    recorded_prices = _daily_frame(daily_trades.price_trade_ccy.unstack(), idx).reindex(columns=assets)
    market_prices = _daily_frame(pd.DataFrame(prices, dtype='float64'), idx).reindex(columns=tickers)
    market_prices.columns = assets
    positive = changes.gt(0)
    estimated = (market_prices.isna() & recorded_prices.notna() & positive).any()
    prices_used = market_prices.fillna(recorded_prices)
    unknown = (prices_used.isna() & positive).any()

    # Align each market currency once, then gather its series for every asset using it.
    rates = _daily_frame(pd.DataFrame(fxs, dtype='float64'), idx).bfill()
    rates[base] = 1.0
    asset_rates = rates.reindex(columns=ccys).copy()
    asset_rates.columns = assets
    missing_rates = [asset for asset, ccy in zip(assets, ccys) if ccy != base and not fxs.get(ccy)]
    if missing_rates:
        recorded = daily_trades.fx_to_base.unstack().reindex(columns=missing_rates)
        recorded = _daily_frame(recorded, idx).bfill()
        asset_rates[missing_rates] = recorded
        available = recorded.notna().any()
        estimated.loc[missing_rates] |= available
        unknown.loc[missing_rates] |= ~available

    estimated_assets = estimated.index[estimated].tolist()
    unknown_assets = unknown.index[unknown].tolist()
    warnings = []
    # Original summation order and cent rounding are significant near half-cent boundaries.
    mat = np.ascontiguousarray(changes.to_numpy(dtype=float) *
                               np.nan_to_num(prices_used.to_numpy(dtype=float), nan=0) *
                               np.nan_to_num(asset_rates.to_numpy(dtype=float), nan=0))
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
            'price_count':len(prices),'source':'Yahoo Finance daily closes (latest cached NSE official close applied when available) + Frankfurter FX; trade-price estimates flagged'}
