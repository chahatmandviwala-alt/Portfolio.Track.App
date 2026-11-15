"""Contract tests for the NSE and Yahoo latest-price refresh sequence."""
from unittest.mock import Mock
import uuid
from fastapi.testclient import TestClient
import market
from app import app


def test_nse_quote_uses_exchange_last_price(monkeypatch):
    seen=[]
    session=Mock()
    session.__enter__=Mock(return_value=session)
    session.__exit__=Mock(return_value=False)
    def get(url, **kwargs):
        seen.append((url, kwargs.get('params')))
        result=Mock()
        result.json.return_value={'info':{'symbol':'RELIANCE'},
                                  'priceInfo':{'lastPrice':'1,456.75'},
                                  'metadata':{'lastUpdateTime':'11-Nov-2026 15:30:00'}}
        return result
    session.get.side_effect=get
    monkeypatch.setattr(market.requests,'Session',lambda:session)
    quote=market._fetch_nse_quote('RELIANCE.NS')
    assert quote['price']==1456.75
    assert quote['source']=='NSE India'
    assert seen[1][1]=={'symbol':'RELIANCE'}


def test_nse_falls_back_to_intraday_if_exchange_unavailable(monkeypatch):
    calls=[]
    def unavailable(ticker):
        calls.append(('nse',ticker))
        raise ValueError('Exchange blocked hosted request')
    def yahoo(ticker):
        calls.append(('yahoo',ticker))
        return {'price':1024.5,'source':'Yahoo Finance intraday','as_of':'2026-11-11T10:00:00+00:00'}
    monkeypatch.setattr(market,'_fetch_nse_quote',unavailable)
    monkeypatch.setattr(market,'_fetch_yahoo_latest',yahoo)
    quotes,errors=market.get_latest_prices(['SBIN.NS','SBIN.NS'])
    assert quotes['SBIN.NS']['price']==1024.5
    assert not errors
    assert calls==[('nse','SBIN.NS'),('yahoo','SBIN.NS')]


def test_unavailable_quote_does_not_fabricate_daily_close(monkeypatch):
    monkeypatch.setattr(market,'_fetch_nse_quote',lambda ticker: (_ for _ in ()).throw(ValueError('blocked')))
    monkeypatch.setattr(market,'_fetch_yahoo_latest',lambda ticker: (_ for _ in ()).throw(ValueError('blocked')))
    quotes,errors=market.get_latest_prices(['SBIN.NS'])
    assert quotes=={}
    assert 'SBIN.NS' in errors


def test_yahoo_intraday_uses_metadata_and_checks_nse_currency(monkeypatch):
    monkeypatch.setattr(market,'_yahoo_chart',lambda ticker,params:{
        'meta':{'regularMarketPrice':125.25,'currency':'INR','regularMarketTime':1794391200},
        'indicators':{'quote':[{'close':[120.0]}]}})
    assert market._fetch_yahoo_latest('SBIN.NS')['price']==125.25
    monkeypatch.setattr(market,'_yahoo_chart',lambda ticker,params:{
        'meta':{'regularMarketPrice':125.25,'currency':'USD'},
        'indicators':{'quote':[{'close':[120.0]}]}})
    try:
        market._fetch_yahoo_latest('SBIN.NS')
        assert False, 'Should reject a non-INR NSE quote'
    except ValueError as e:
        assert 'non-INR' in str(e)


def test_refresh_updates_nse_ltp_and_reports_source(monkeypatch):
    monkeypatch.setattr(market,'spot_rates',lambda ccys,base:({ccy:1 for ccy in ccys},[]))
    with TestClient(app) as client:
        name='nse'+uuid.uuid4().hex[:16]
        assert client.post('/api/register',json={'username':name,'password':'CorrectHorseBattery123'}).status_code==200
        trade={'asset':'Reliance Industries','ticker':'RELIANCE.NS','asset_type':'Stock',
               'side':'BUY','quantity':10,'price_trade_ccy':900,'trade_ccy':'INR',
               'trade_date':'2025-01-10','fx_to_base':1,'auto_fx':False,
               'last_price_trade_ccy':800}
        assert client.post('/api/trades',json=trade).status_code==200
        monkeypatch.setattr(market,'get_latest_prices',lambda tickers:({
            'RELIANCE.NS':{'price':1456.75,'source':'NSE India','as_of':'2026-11-11 15:30'}},{}))
        out=client.post('/api/refresh').json()
        assert out['updated']==1
        assert out['quotes']['RELIANCE.NS']['source']=='NSE India'
        holdings=client.get('/api/dashboard').json()['holdings']
        assert holdings[0]['ltp']==1456.75
        assert holdings[0]['value_trade']==14567.5
