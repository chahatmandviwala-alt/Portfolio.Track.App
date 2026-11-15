from datetime import date
import io
import pandas as pd
import pytest
import portfolio


def make_rows():
    cols=portfolio.INPUT_COLS
    defaults={'asset_type':'Stock','trade_ccy':'USD','charges_trade_ccy':0,
              'stt_trade_ccy':0,'fx_to_base':10,'ticker':'ABC','last_price_trade_ccy':120,'last_price_base':1200}
    src=[{'trade_date':'2025-01-10','asset':'ABC','side':'BUY','quantity':10,'price_trade_ccy':100},
         {'trade_date':'2025-02-10','asset':'ABC','side':'BUY','quantity':10,'price_trade_ccy':200},
         {'trade_date':'2025-03-10','asset':'ABC','side':'SELL','quantity':15,'price_trade_ccy':250}]
    return portfolio.read_trades(pd.DataFrame([{**defaults,**r} for r in src])[cols].to_csv(sep=';',index=False))


def test_fifo_matching_and_costs():
    d=make_rows();p=portfolio.derived(d)
    open_,realized,warnings=portfolio.fifo(p,'unit_trade',date(2025,1,1),date(2025,12,31))
    assert not warnings
    assert open_['ABC']['quantity']==5
    assert open_['ABC']['cost']==1000
    assert realized[0]['realized']==15*250-(10*100+5*200)
    assert realized[0]['quantity_sold']==15
    assert realized[0]['buy_value']==2000


def test_report_filter_by_sale_date():
    d=make_rows()
    rep=portfolio.realized_report(d,'USD','trade',date(2025,1,1),date(2025,2,28))
    assert rep['rows']==[]
    rep=portfolio.realized_report(d,'SEK','base',date(2025,3,1),date(2025,3,31))
    assert rep['total']==17500


def test_inr_stt_rule():
    d=make_rows(); d.loc[0,'stt_trade_ccy']=10;d.loc[2,'stt_trade_ccy']=20
    trade=portfolio.realized_report(d,'INR','trade',date(2025,1,1),date(2025,12,31))
    rupee=portfolio.realized_report(d,'INR','base',date(2025,1,1),date(2025,12,31))
    sek=portfolio.realized_report(d,'SEK','base',date(2025,1,1),date(2025,12,31))
    assert trade['rows'][0]['realized']==1750
    assert rupee['total']==17500  # original excludes STT for INR tax
    assert sek['total']!=rupee['total'] # other bases include STT


def test_csv_round_trip_preserves_ids_and_format():
    d=make_rows();v=portfolio.to_csv(d);back=portfolio.read_trades(v)
    assert d.trade_id.tolist()==back.trade_id.tolist()
    export=portfolio.to_csv(d,False)
    assert 'trade_id' not in export.splitlines()[0]
    assert portfolio.read_trades(export).shape[0]==3


def test_bad_csv_rejected():
    with pytest.raises(ValueError,match='Missing CSV columns'):
        portfolio.read_trades('asset;date\nABC;2024-01-01')


def test_summary_profit_in_trade_currency():
    d=make_rows()
    res=portfolio.summarize(d,'USD',{'USD':1.0})
    assert res['totals']['assets']==1
    h=res['holdings'][0]
    assert h['quantity']==5
    assert h['avg']==200
    assert h['pnl_trade']==-400  # 5 units @ 120 less remaining 5 @ 200
    assert h['share']==100
