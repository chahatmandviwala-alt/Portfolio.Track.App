"""Isolated tests for NSE close-vs-last-trade semantics and chart preloading.

No internet or real Supabase connection. Run:
    python -m pytest tests/test_performance.py tests/test_official_close.py -q
"""
import csv
import importlib
import io
import sys
import zipfile
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest


@pytest.fixture
def modules(monkeypatch, tmp_path):
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{tmp_path / "nse_test.sqlite"}')
    monkeypatch.setenv('NSE_OFFICIAL_CLOSE', '1')
    monkeypatch.setenv('QUOTE_CACHE_SECONDS', '30')
    for name in ('market', 'storage'):
        sys.modules.pop(name, None)
    import market, storage
    storage.init_db()
    yield market, storage
    storage.engine.dispose()
    for name in ('market', 'storage'):
        sys.modules.pop(name, None)


def make_zip(day, rows):
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=['BizDt', 'TckrSymb', 'SctySrs', 'ClsPric', 'LastPric'])
    writer.writeheader()
    for symbol, series, close, last in rows:
        writer.writerow({'BizDt': day.isoformat(), 'TckrSymb': symbol, 'SctySrs': series,
                         'ClsPric': str(close), 'LastPric': str(last)})
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as archive:
        archive.writestr('BhavCopy_NSE_CM.csv', buffer.getvalue())
    return out.getvalue()


def test_bhavcopy_uses_official_close_not_last_traded_price(modules):
    market, _ = modules
    day = date(2026, 11, 13)
    content = make_zip(day, [('CAPLIPOINT', 'BE', 2300, 2200),
                             ('CAPLIPOINT', 'EQ', 2388.70, 2375.00),
                             ('OTHER', 'EQ', 600, 590)])
    prices = market._parse_nse_bhavcopy(content, day)
    assert prices['CAPLIPOINT'] == 2388.70
    assert prices['OTHER'] == 600
    assert prices['CAPLIPOINT'] != 2375.00


def test_bhavcopy_wrong_date_fails_closed(modules):
    market, _ = modules
    content = make_zip(date(2026, 11, 12), [('CAPLIPOINT', 'EQ', 2388.70, 2375.00)])
    with pytest.raises(ValueError):
        market._parse_nse_bhavcopy(content, date(2026, 11, 13))


def test_eod_mode_and_market_open_window(modules):
    market, _ = modules
    def ist(ts):
        return datetime.fromisoformat(ts).replace(tzinfo=ZoneInfo('Asia/Kolkata'))
    assert not market._nse_trading_window(ist('2026-11-15T12:00:00'))  # Sunday
    assert market._nse_report_dates(ist('2026-11-15T12:00:00'))[0] == date(2026, 11, 13)
    assert market._nse_trading_window(ist('2026-11-16T11:00:00'))
    assert not market._nse_trading_window(ist('2026-11-16T19:00:00'))
    assert market._nse_report_dates(ist('2026-11-16T08:00:00'))[0] == date(2026, 11, 13)


def test_eod_quote_beats_yahoo_ltp_and_no_quote_requests(modules, monkeypatch):
    market, _ = modules
    monkeypatch.setattr(market, '_nse_trading_window', lambda: False)
    monkeypatch.setattr(market, '_load_latest_nse_eod',
                        lambda: (date(2026, 11, 13), {'CAPLIPOINT': 2388.70, 'ABC': 121}))
    def do_not_call_yahoo(_):
        raise AssertionError('Should use one shared official file, not ticker-by-ticker Yahoo')
    monkeypatch.setattr(market, '_fetch_yahoo_latest', do_not_call_yahoo)
    quotes, errors = market.get_latest_prices(['CAPLIPOINT.NS', 'ABC.NS'])
    assert not errors
    assert quotes['CAPLIPOINT.NS']['price'] == 2388.7
    assert 'official closing' in quotes['CAPLIPOINT.NS']['source'].lower()
    assert quotes['CAPLIPOINT.NS']['as_of'] == '2026-11-13'


def test_live_mode_does_not_use_previous_official_close(modules, monkeypatch):
    market, _ = modules
    monkeypatch.setattr(market, '_nse_trading_window', lambda: True)
    monkeypatch.setattr(market, '_load_latest_nse_eod',
                        lambda: (_ for _ in ()).throw(AssertionError('Must not read yesterday close during live session')))
    monkeypatch.setattr(market, '_fetch_yahoo_latest',
                        lambda ticker: {'price': 2375., 'source': 'Yahoo Finance intraday', 'as_of': '2026-11-16'})
    quotes, errors = market.get_latest_prices(['CAPLIPOINT.NS'])
    assert quotes['CAPLIPOINT.NS']['price'] == 2375.
    assert not errors


def test_eod_bhavcopy_fetched_once_and_persisted(modules, monkeypatch):
    market, storage = modules
    monkeypatch.setattr(market, '_nse_report_dates', lambda: [date(2026, 11, 13)])
    content = make_zip(date(2026,11,13), [('CAPLIPOINT','EQ',2388.70,2375.00)])
    calls = []
    def fetch(day):
        calls.append(day)
        return market._parse_nse_bhavcopy(content, day)
    monkeypatch.setattr(market, '_download_nse_bhavcopy', fetch)
    first = market._load_latest_nse_eod()
    assert first[1]['CAPLIPOINT'] == 2388.70
    assert calls == [date(2026, 11, 13)]
    market._local_rows.clear()  # simulate RAM cache eviction / process restart
    assert market._load_latest_nse_eod() == first
    assert calls == [date(2026, 11, 13)]  # read Supabase cache, not network
    with storage.Session() as db:
        cached = db.get(storage.MarketCache, 'nse:close:20261113')
        assert cached is not None and '2388.7' in cached.csv_text


def test_official_snapshot_overlays_only_matching_day(modules, monkeypatch):
    market, _ = modules
    market._write_cached({'nse:close:20261113': market._CacheRecord(
        date(2026, 11, 13), date(2026, 11, 13), datetime.now(timezone.utc),
        {'CAPLIPOINT': 2388.70})})
    monkeypatch.setattr(market, '_nse_report_dates', lambda: [date(2026, 11, 13)])
    prices = {'CAPLIPOINT.NS': {'2026-11-12': 2386.30, '2026-11-13': 2375.00},
              'AAPL': {'2026-11-13': 270.0}}
    result = market._overlay_official_nse_history(prices, date(2026, 11, 15))
    assert result['CAPLIPOINT.NS']['2026-11-13'] == 2388.70
    assert result['CAPLIPOINT.NS']['2026-11-12'] == 2386.30
    assert result['AAPL']['2026-11-13'] == 270.0


def test_combined_price_and_fx_cache_lookup_avoids_separate_selects(modules, monkeypatch):
    market, storage = modules
    market._write_cached({
        'price:ABC': market._CacheRecord(date(2026,1,1), date(2026,12,30), datetime.now(timezone.utc), {'2026-11-13':10.}),
        'fx:USD:SEK': market._CacheRecord(date(2026,1,1), date(2026,12,30), datetime.now(timezone.utc), {'2026-11-13':9.}),
    })
    market._local_rows.clear()
    from sqlalchemy import event
    statements = []
    def count(conn, cursor, stmt, params, context, executemany):
        if 'market_cache' in stmt.lower() and stmt.lstrip().lower().startswith('select'):
            statements.append(stmt)
    event.listen(storage.engine, 'before_cursor_execute', count)
    try:
        # Testing the combined prefetch directly avoids coupling this test to
        # the full portfolio engine's expected input-frame columns.
        keys = ['price:ABC', 'fx:USD:SEK']
        market._read_cached(keys)
        market.get_prices(['ABC'], date(2026, 11, 1), date(2026, 11, 15))
        market.get_fx_series(['USD'], 'SEK', date(2026, 11, 1), date(2026, 11, 15))
        assert len(statements) == 1
    finally:
        event.remove(storage.engine, 'before_cursor_execute', count)


def test_full_csv_fallback_uses_close_price(modules):
    market, _ = modules
    payload = ('SYMBOL,SERIES,DATE1,PREV_CLOSE,LAST_PRICE,CLOSE_PRICE\n'
               'CAPLIPOINT,EQ,13-Nov-2026,2386.30,2375.00,2388.70\n'
               'CAPLIPOINT,BE,13-Nov-2026,2386.30,2200,2200\n').encode()
    prices = market._parse_nse_full_csv(payload, date(2026, 11, 13))
    assert prices['CAPLIPOINT'] == 2388.70


def test_exchange_down_fallback_is_clearly_labeled(modules, monkeypatch):
    market, _ = modules
    monkeypatch.setattr(market, '_nse_trading_window', lambda: False)
    monkeypatch.setattr(market, '_load_latest_nse_eod', lambda: (None, {}))
    monkeypatch.setattr(market, '_fetch_yahoo_latest',
                        lambda ticker: {'price': 2375.0,
                                        'source': 'Yahoo Finance intraday (not NSE official close)',
                                        'as_of': '2026-11-13'})
    prices, errors = market.get_latest_prices(['CAPLIPOINT.NS'])
    assert not errors
    assert prices['CAPLIPOINT.NS']['price'] == 2375.0
    assert 'not NSE official close' in prices['CAPLIPOINT.NS']['source']
