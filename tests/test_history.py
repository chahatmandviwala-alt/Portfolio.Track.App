from datetime import date
import portfolio,market
from test_portfolio import make_rows

def test_all_history_ranges_with_stubbed_market(monkeypatch):
    d=make_rows()
    monkeypatch.setattr(market,'get_prices',lambda tickers,start,end,force=False:({t:{start.isoformat():100.0,end.isoformat():120.0} for t in tickers},{}))
    monkeypatch.setattr(market,'get_fx_series',lambda ccys,base,start,end,force=False:({c:{start.isoformat():10.0} for c in ccys},{}))
    for span in ['1M','3M','6M','1Y','3Y','MAX']:
        r=market.portfolio_history(d,'SEK',span)
        assert r['range']==span
        assert r['points']
        assert len(r['points'])<=922
        assert r['points'][-1]['date']==date.today().isoformat()
        assert r['points'][-1]['value']>=0
