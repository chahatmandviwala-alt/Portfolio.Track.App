from fastapi.testclient import TestClient
from app import app
import uuid


def test_local_login_trade_lifecycle_and_exports(monkeypatch):
    import market
    monkeypatch.setattr(market,'spot_rates',lambda currencies,base:({c:1 for c in currencies},[]))
    with TestClient(app) as client:
        assert client.get('/api/dashboard').status_code==401
        name='t'+uuid.uuid4().hex[:15]
        assert client.post('/api/register',json={'username':name,'password':'SomeLongPassword123'}).status_code==200
        assert client.get('/api/me').json()['authenticated'] is True
        payload={'asset':'TEST','ticker':'TEST','asset_type':'Stock','side':'BUY','quantity':8,
                 'price_trade_ccy':100,'trade_ccy':'SEK','trade_date':'2025-11-01',
                 'charges_trade_ccy':1,'stt_trade_ccy':0,'fx_to_base':1,'auto_fx':False}
        assert client.post('/api/trades',json=payload).status_code==200
        d=client.get('/api/dashboard').json()
        assert d['totals']['assets']==1
        assert d['holdings'][0]['quantity']==8
        assert client.get('/api/export/trades').status_code==200
        trades=client.get('/api/trades').json()['rows']
        assert len(trades)==1
        payload['quantity']=6
        assert client.put('/api/trades/'+trades[0]['trade_id'],json=payload).status_code==200
        assert client.get('/api/dashboard').json()['holdings'][0]['quantity']==6
        sell={**payload,'side':'SELL','quantity':2,'trade_date':'2026-01-07','price_trade_ccy':130}
        assert client.post('/api/trades',json=sell).status_code==200
        tax=client.get('/api/tax?start=2026-01-01&end=2026-11-11&mode=base').json()
        assert len(tax['rows'])==1 and tax['total']>0
        assert client.get('/api/export/tax?start=2026-01-01&end=2026-11-11&mode=base').status_code==200
        assert client.delete('/api/trades/'+trades[0]['trade_id']).status_code==200
        assert client.post('/api/logout').status_code==200
        assert client.get('/api/dashboard').status_code==401
