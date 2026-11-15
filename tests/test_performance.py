"""Offline tests for the market.py/storage.py replacement.
Run from repository root: python -m pytest tests/test_performance.py -q
No Supabase account, API keys, or external network required.
"""
import importlib
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest


@pytest.fixture
def app_modules(monkeypatch, tmp_path):
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{tmp_path / "portfolio.db"}')
    monkeypatch.setenv('PRICE_SOURCE_PRIORITY', 'yahoo')
    monkeypatch.setenv('QUOTE_CACHE_SECONDS', '30')
    for module in ('market', 'storage'):
        sys.modules.pop(module, None)
    import storage
    import market
    storage.init_db()
    yield market, storage
    storage.engine.dispose()
    for module in ('market', 'storage'):
        sys.modules.pop(module, None)


def test_reuses_overlapping_price_ranges_and_persists(app_modules, monkeypatch):
    market, storage = app_modules
    calls = []

    def fake_chart(ticker, start, end):
        calls.append((ticker, start, end))
        return {d.date().isoformat(): 100.0 for d in pd.date_range(start, end)}

    monkeypatch.setattr(market, '_fetch_chart', fake_chart)
    d0, d1, d2 = date(2025, 1, 1), date(2025, 1, 12), date(2025, 2, 4)
    r1, e1 = market.get_prices(['ACME'], d1, d2)
    assert not e1 and len(calls) == 1
    r2, e2 = market.get_prices(['ACME'], d1, d2)
    assert r1 == r2 and not e2 and len(calls) == 1
    r3, e3 = market.get_prices(['ACME'], d0, d2)
    assert not e3 and len(calls) == 2
    assert calls[1] == ('ACME', d0, d1 - timedelta(days=1))
    assert d0.isoformat() in r3['ACME']

    # A fresh process must reuse the Supabase/SQLite persistent cache.
    market._local_rows.clear()
    r4, e4 = market.get_prices(['ACME'], d0, d2)
    assert not e4 and len(calls) == 2 and r4 == r3

    with storage.Session() as db:
        item = db.get(storage.MarketCache, 'price:ACME')
        assert item and item.start_date == d0.isoformat() and item.end_date == d2.isoformat()


def test_batched_persistent_reads_and_writes(app_modules, monkeypatch):
    market, storage = app_modules
    monkeypatch.setattr(market, '_fetch_chart',
                        lambda ticker, a, b: {a.isoformat(): 50.0})
    items = [f'TICKER{i}' for i in range(8)]
    market.get_prices(items, date(2025, 1, 1), date(2025, 1, 5))
    with storage.Session() as db:
        from sqlalchemy import select
        db_rows = db.scalars(select(storage.MarketCache)).all()
        assert len(db_rows) == 8
    market._local_rows.clear()
    num_queries = []
    from sqlalchemy import event
    def counter(conn, cursor, statement, parameters, context, executemany):
        if 'market_cache' in statement.lower() and statement.lstrip().lower().startswith('select'):
            num_queries.append(statement)
    event.listen(storage.engine, 'before_cursor_execute', counter)
    try:
        market.get_prices(items, date(2025, 1, 1), date(2025, 1, 5))
        assert len(num_queries) == 1  # Rather than eight network DB queries
    finally:
        event.remove(storage.engine, 'before_cursor_execute', counter)


def test_recent_window_refresh_does_not_download_all_history(app_modules, monkeypatch):
    market, storage = app_modules
    today = date.today()
    old = today - timedelta(days=365)
    all_calls = []
    def fake(t, a, b):
        all_calls.append((a,b))
        return {a.isoformat(): 99.0, b.isoformat(): 101.0}
    monkeypatch.setattr(market, '_fetch_chart', fake)
    market.get_prices(['ACME'], old, today)
    assert len(all_calls) == 1
    with storage.Session.begin() as db:
        row = db.get(storage.MarketCache, 'price:ACME')
        row.fetched_at = datetime.now(timezone.utc)-timedelta(hours=36)
    market._local_rows.clear()
    market.get_prices(['ACME'], old, today)
    assert len(all_calls) == 2
    assert (today - all_calls[-1][0]).days <= 10  # fetch only recent window


def test_fx_conversion_and_cache(app_modules, monkeypatch):
    market, _ = app_modules
    calls = []
    def fake_fx(ccy, base, start, end):
        calls.append((ccy, start, end))
        return {d.date().isoformat(): 10.0 for d in pd.date_range(start, end)}
    monkeypatch.setattr(market, '_fetch_fx_series', fake_fx)
    a,b = date(2025, 1, 2),date(2025, 1, 9)
    fx, errors = market.get_fx_series(['USD','SEK','EUR','USD'], 'SEK',a,b)
    assert not errors
    assert fx['SEK'] == {a.isoformat():1.0}
    assert len(calls) == 2
    market.get_fx_series(['USD','SEK','EUR'], 'SEK',a,b)
    assert len(calls) == 2


def test_quote_fetch_yahoo_first_and_fallback(app_modules, monkeypatch):
    market, _ = app_modules
    calls = []
    def yahoo(t):
        calls.append(('Yahoo',t))
        if t == 'FAIL.NS':
            raise ValueError('unavailable')
        return {'price':110.0,'source':'Yahoo Finance intraday','as_of':'today'}
    def nse(t):
        calls.append(('NSE',t))
        return {'price':111.0,'source':'NSE India','as_of':'today'}
    monkeypatch.setattr(market, '_fetch_yahoo_latest', yahoo)
    monkeypatch.setattr(market, '_fetch_nse_quote', nse)
    prices, errors = market.get_latest_prices(['OK.NS','FAIL.NS'])
    assert not errors
    assert prices['OK.NS']['price']==110.0
    assert prices['FAIL.NS']['price']==111.0
    assert ('NSE','OK.NS') not in calls
    n = len(calls)
    market.get_latest_prices(['OK.NS','FAIL.NS'])
    assert len(calls) == n  # repeated rapid click uses quote cache


def test_history_values_and_memo_invalidation(app_modules, monkeypatch):
    market, _ = app_modules
    today = date.today()
    first = today - timedelta(days=7)
    count = []
    def fake_prices(*args,**kwargs):
        count.append('prices')
        return {'ABC': {str(d.date()): 12.0 for d in pd.date_range(first,today)}}, {}
    monkeypatch.setattr(market,'get_prices',fake_prices)
    monkeypatch.setattr(market,'get_fx_series',lambda *args,**kwargs: ({'SEK': {}},{}))
    df = pd.DataFrame([{'trade_date':pd.Timestamp(first),'asset':'ABC', 'side':'BUY',
                        'quantity':2., 'ticker':'ABC','trade_ccy':'SEK',
                        'price_trade_ccy':10., 'fx_to_base':1., 'last_price_trade_ccy':12.}])
    result1=market.portfolio_history(df,'SEK','1M')
    assert result1['points'][-1]['value']==24.0
    result2=market.portfolio_history(df,'SEK','1M')
    assert result2==result1 and count==['prices']
    df.loc[0,'quantity']=3.
    result3=market.portfolio_history(df,'SEK','1M')
    assert result3['points'][-1]['value']==36.0 and len(count)==2


def test_cache_does_not_mix_base_currencies(app_modules, monkeypatch):
    market,_=app_modules
    monkeypatch.setattr(market,'_fetch_fx_series', lambda ccy, base,a,b: {a.isoformat(): 5. if base=='SEK' else 9.})
    a,b=date(2025,1,1),date(2025,1,5)
    v1,_=market.get_fx_series(['USD'],'SEK',a,b)
    v2,_=market.get_fx_series(['USD'],'EUR',a,b)
    assert v1['USD']!=v2['USD']


def test_no_real_credentials_or_database_required(app_modules):
    market,storage=app_modules
    assert storage.engine.dialect.name == 'sqlite'
    assert not market.get_prices([],date.today(),date.today())[0]


def test_failed_historical_provider_has_short_negative_cache(app_modules, monkeypatch):
    market, storage = app_modules
    n = []
    def fail(t, a, b):
        n.append(t)
        raise ValueError('simulated Yahoo outage')
    monkeypatch.setattr(market, '_fetch_chart', fail)
    a, b = date(2025, 1, 1), date(2025, 1, 10)
    _, errors1 = market.get_prices(['BAD'], a, b)
    assert 'BAD' in errors1 and n == ['BAD']
    _, errors2 = market.get_prices(['BAD'], a, b)
    assert 'BAD' in errors2 and n == ['BAD']
    with storage.Session() as db:
        row = db.get(storage.MarketCache,'price:BAD')
        assert row is not None and row.csv_text == '{}'


def test_history_cache_not_crossing_fundamental_date_gap(app_modules, monkeypatch):
    market, storage = app_modules
    monkeypatch.setattr(market, '_fetch_chart',
                        lambda t, a, b: {a.isoformat():10.,b.isoformat():20.})
    old_start, old_end = date(2024, 1, 1), date(2024, 1, 20)
    new_start, new_end = date(2025, 1, 1), date(2025, 1, 10)
    market.get_prices(['ACME'], old_start, old_end)
    market.get_prices(['ACME'], new_start, new_end)
    with storage.Session() as db:
        row=db.get(storage.MarketCache,'price:ACME')
        assert row.start_date == new_start.isoformat()
        assert row.end_date == new_end.isoformat()
        assert old_start.isoformat() not in row.csv_text
