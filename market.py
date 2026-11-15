"""Historical price / FX provider with persistent database cache.

Yahoo Finance is not an SLA-backed data source: invalid / delisted tickers may be missing.
Do not represent estimated historical points as verified market prices.
"""
from __future__ import annotations
from datetime import date,datetime,timedelta,timezone
from concurrent.futures import ThreadPoolExecutor,as_completed
import json
import numpy as np
import pandas as pd
import requests
from storage import Session, MarketCache

HEADERS={'User-Agent':'Mozilla/5.0 (compatible; PortfolioTracker/1.0)','Accept':'application/json'}


def _get_cached(key:str,start:date,end:date,ttl_hours:float=18)->dict|None:
    with Session() as db:
        item=db.get(MarketCache,key)
        if not item: return None
        fetched=item.fetched_at
        if fetched.tzinfo is None:fetched=fetched.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc)-fetched).total_seconds()>ttl_hours*3600:return None
        if not(item.start_date <= start.isoformat() and item.end_date >= end.isoformat()):return None
        try:return json.loads(item.csv_text)
        except Exception:return None


def _put_cached(key:str,start:date,end:date,rows:dict):
    with Session() as db:
        item=db.get(MarketCache,key)
        if item is None:
            item=MarketCache(cache_key=key);db.add(item)
        item.start_date=start.isoformat();item.end_date=end.isoformat();item.csv_text=json.dumps(rows,separators=(',',':'))
        item.fetched_at=datetime.now(timezone.utc)
        db.commit()


def _yahoo_chart(ticker: str, params: dict) -> dict:
    """Try both Yahoo chart hosts; neither is a guaranteed market-data feed."""
    last_error = None
    for host in ('query2.finance.yahoo.com', 'query1.finance.yahoo.com'):
        try:
            url = f'https://{host}/v8/finance/chart/{requests.utils.quote(ticker, safe="")}'
            response = requests.get(url, headers=HEADERS, params=params, timeout=10)
            response.raise_for_status()
            payload = response.json().get('chart') or {}
            results = payload.get('result') or []
            if results:
                return results[0]
            last_error = ValueError('No chart data returned')
        except (requests.RequestException, ValueError, KeyError) as exc:
            last_error = exc
    raise ValueError(f'Yahoo quote unavailable: {type(last_error).__name__}')


def _positive_price(value) -> float | None:
    """Validate prices, including the comma-formatted figures returned by NSE."""
    try:
        price = float(str(value).replace(',', ''))
    except (TypeError, ValueError):
        return None
    return round(price, 8) if np.isfinite(price) and price > 0 else None


def _fetch_nse_quote(ticker: str) -> dict:
    """Get the last traded NSE price in INR from the exchange's equity quote API.

    NSE's public website often requires a browser-like cookie warm-up, and may
    reject requests from hosted servers. Callers always have a Yahoo fallback.
    """
    symbol = ticker[:-3].upper()  # Strip the .NS Yahoo suffix
    if not symbol or not all(c.isalnum() or c in '-&_' for c in symbol):
        raise ValueError('Invalid NSE ticker')
    page = f'https://www.nseindia.com/get-quotes/equity?symbol={requests.utils.quote(symbol)}'
    headers = {
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': page,
    }
    with requests.Session() as session:
        session.headers.update(headers)
        # Obtain NSE session cookies before requesting the API.
        session.get('https://www.nseindia.com/', timeout=(2, 4)).raise_for_status()
        response = session.get('https://www.nseindia.com/api/quote-equity',
                               params={'symbol': symbol}, timeout=(2, 4))
        response.raise_for_status()
        data = response.json()
    returned_symbol = str((data.get('info') or {}).get('symbol') or symbol)
    if returned_symbol.upper() != symbol:
        raise ValueError('NSE returned a different symbol')
    price = _positive_price((data.get('priceInfo') or {}).get('lastPrice'))
    if price is None:
        raise ValueError('NSE returned no valid last traded price')
    return {'price': price, 'source': 'NSE India',
            'as_of': str((data.get('metadata') or {}).get('lastUpdateTime') or '')}


def _fetch_yahoo_latest(ticker: str) -> dict:
    """Use recent intraday candles and live metadata, NOT a daily-history close."""
    info = _yahoo_chart(ticker, {'range': '5d', 'interval': '5m',
                                 'includePrePost': 'false'})
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
        try: as_of = datetime.fromtimestamp(int(stamp), timezone.utc).isoformat()
        except (TypeError, ValueError, OSError): as_of = ''
    else:
        timestamps = info.get('timestamp') or []
        as_of = datetime.fromtimestamp(timestamps[-1], timezone.utc).isoformat() if timestamps else ''
    return {'price': price, 'source': 'Yahoo Finance intraday', 'as_of': as_of}


def get_latest_prices(tickers: list[str]) -> tuple[dict, dict]:
    """On-demand latest traded quotes; returns (ticker -> quote, ticker -> error).

    NSE prices are read from NSE first, with a Yahoo 5-minute fallback. Other
    exchanges use Yahoo intraday. Do not overwrite a stored LTP with an old
    historical daily close or a transaction-price estimate on failure.
    """
    names = list(dict.fromkeys(t.strip() for t in tickers if t and t.strip()))
    quotes, errors = {}, {}

    def fetch(ticker):
        causes = []
        if ticker.upper().endswith('.NS'):
            try: return _fetch_nse_quote(ticker)
            except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                causes.append(f'NSE India ({type(exc).__name__})')
        try: return _fetch_yahoo_latest(ticker)
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            causes.append(f'Yahoo Finance ({type(exc).__name__})')
        raise ValueError(' and '.join(causes) + ' unavailable')

    if not names:
        return quotes, errors
    with ThreadPoolExecutor(max_workers=min(6, len(names))) as pool:
        futures = {pool.submit(fetch, ticker): ticker for ticker in names}
        for future in as_completed(futures):
            ticker = futures[future]
            try: quotes[ticker] = future.result()
            except Exception as exc: errors[ticker] = str(exc)
    return quotes, errors


def _fetch_chart(ticker:str,start:date,end:date)->dict:
    # end + 2d to include the requested last day, even across markets/timezones.
    start_epoch=int(pd.Timestamp(start, tz='UTC').timestamp())
    end_epoch=int((pd.Timestamp(end,tz='UTC')+pd.Timedelta(days=2)).timestamp())
    try:
        info=_yahoo_chart(ticker,{'period1':start_epoch,'period2':end_epoch,
                                  'interval':'1d','events':'history'})
    except ValueError:
        return {}
    ts=info.get('timestamp') or []
    closes=(info.get('indicators',{}).get('quote') or [{}])[0].get('close') or []
    # Convert timestamps to exchange-local trading dates to avoid the UTC day shifting in Asia.
    utc_offset=int(info.get('meta',{}).get('gmtoffset',0) or 0)
    output={}
    for t,v in zip(ts,closes):
        if v is None:continue
        x=float(v)
        if np.isfinite(x) and x>0:
            dt=datetime.fromtimestamp(int(t)+utc_offset,tz=timezone.utc).date().isoformat()
            output[dt]=round(x,8)
    return output


def get_prices(tickers:list[str],start:date,end:date,force:bool=False)->tuple[dict,dict]:
    """Fetch distinct ticker daily closes concurrently; returns (prices, errors)."""
    names=list(dict.fromkeys(x for x in tickers if x))
    prices={};errors={};missing=[]
    for ticker in names:
        cached=None if force else _get_cached('price:'+ticker,start,end,ttl_hours=18)
        if cached is not None:prices[ticker]=cached
        else:missing.append(ticker)
    if not missing:return prices,errors
    with ThreadPoolExecutor(max_workers=min(12,len(missing))) as ex:
        futures={ex.submit(_fetch_chart,t,start,end):t for t in missing}
        for fut in as_completed(futures):
            t=futures[fut]
            try:
                result=fut.result()
                if not result:errors[t]='No historical quotes available'
                else:
                    prices[t]=result
                    _put_cached('price:'+t,start,end,result)
            except Exception as e:
                errors[t]=f'Historical data unavailable ({type(e).__name__})'
    return prices,errors


def _fetch_fx_series(ccy:str,base:str,start:date,end:date)->dict:
    if ccy==base:return {}
    src='GBP' if ccy=='GBX' else ccy
    dest='GBP' if base=='GBX' else base
    if src==dest:
        factor=(0.01 if ccy=='GBX' else 1)/(0.01 if base=='GBX' else 1)
        return {str(d.date()):factor for d in pd.date_range(start,end)}
    url=f'https://api.frankfurter.dev/v1/{start.isoformat()}..{end.isoformat()}'
    r=requests.get(url,params={'base':src,'symbols':dest},timeout=22)
    r.raise_for_status()
    raw=r.json().get('rates',{})
    factor=(0.01 if ccy=='GBX' else 1)/(0.01 if base=='GBX' else 1)
    return {day:float(v[dest])*factor for day,v in raw.items() if dest in v}


def get_fx_series(currencies:list[str],base:str,start:date,end:date,force=False)->tuple[dict,dict]:
    output={};errors={};missing=[]
    for ccy in dict.fromkeys(currencies):
        if ccy==base:
            output[ccy]={start.isoformat():1.0}
            continue
        key=f'fx:{ccy}:{base}'
        cached=None if force else _get_cached(key,start,end,ttl_hours=18)
        if cached is not None:output[ccy]=cached
        else:missing.append(ccy)
    if missing:
        with ThreadPoolExecutor(max_workers=min(6,len(missing))) as ex:
            futures={ex.submit(_fetch_fx_series,ccy,base,start,end):ccy for ccy in missing}
            for fut in as_completed(futures):
                ccy=futures[fut]
                try:
                    result=fut.result()
                    if not result:errors[ccy]='No FX data returned'
                    else:
                        output[ccy]=result
                        _put_cached(f'fx:{ccy}:{base}',start,end,result)
                except Exception as e:
                    errors[ccy]=f'FX data unavailable ({type(e).__name__})'
    return output,errors


def spot_rates(currencies:list[str],base:str)->tuple[dict[str,float],list[str]]:
    today=date.today(); start=today-timedelta(days=8)
    rate_map,warnings={},[]
    series,errors=get_fx_series(currencies,base,start,today)
    for ccy in currencies:
        if ccy==base:rate_map[ccy]=1.0;continue
        matches=series.get(ccy) or {}
        if matches:
            rate_map[ccy]=matches[max(matches)]
        else:
            warnings.append(f'Latest FX unavailable: {ccy} → {base} (not converted).')
    return rate_map,warnings


def trade_fx_rates(df:pd.DataFrame,base:str,preserve_legacy_sek:bool=False)->tuple[np.ndarray,list[str]]:
    """Recalculate transaction-date FX when reporting base changes, aligned to trade date.

    Original input fx_to_base is retained if base=SEK (legacy default) to preserve
    its exact original accounting; for other bases use verified historical rates.
    """
    if df.empty:return np.array([]),[]
    if base=='SEK' and preserve_legacy_sek:return df.fx_to_base.to_numpy(dtype=float),[]
    start=df.trade_date.min().date()-timedelta(days=6);end=min(date.today(),df.trade_date.max().date()+timedelta(days=1))
    currencies=df.trade_ccy.unique().tolist()
    series,errors=get_fx_series(currencies,base,start,end)
    output=np.full(len(df),np.nan)
    warns=[]
    by_ccy={}
    for ccy in currencies:
        if ccy==base:by_ccy[ccy]=pd.Series(1.0,index=pd.date_range(start,end))
        elif ccy in series:
            vals=pd.Series(series[ccy],dtype='float64'); vals.index=pd.to_datetime(vals.index)
            by_ccy[ccy]=vals.reindex(pd.date_range(start,end)).ffill().bfill()
        else:warns.append(f'Cannot calculate historical {ccy} → {base} FX; base-cost calculations unavailable.')
    dates=df.trade_date.dt.normalize()
    for ccy,s in by_ccy.items():
        mask=(df.trade_ccy==ccy).to_numpy()
        output[mask]=s.reindex(dates[mask]).to_numpy(dtype=float)
    return output,warns


def portfolio_history(df:pd.DataFrame,base:str,span:str)->dict:
    """Daily investment-account market value, not time-weighted investment return.

    Asset daily shares calculated by vectorized cumsum; prices and FX are aligned
    to one daily index, then multiplied as 2-D NumPy arrays.
    """
    if span not in ('1M','3M','6M','1Y','3Y','MAX'):raise ValueError('Unsupported range')
    if df.empty:return {'points':[],'warnings':[],'estimated_assets':[],'range':span}
    end=date.today(); first=df.trade_date.min().date()
    days={'1M':31,'3M':92,'6M':183,'1Y':366,'3Y':1096,'MAX':999999}[span]
    start=first if span=='MAX' else max(first,end-timedelta(days=days))
    if start>end:return {'points':[],'warnings':[],'estimated_assets':[],'range':span}
    idx=pd.date_range(start,end,freq='D')
    # If range begins after first trade, initial positions are included via cumsum from the first trade.
    delta=df[['trade_date','asset','side','quantity']].copy()
    delta['trade_date']=delta.trade_date.dt.normalize()
    delta['signed_qty']=delta.quantity * np.where(delta.side=='BUY',1,-1)
    changes=delta.pivot_table(index='trade_date',columns='asset',values='signed_qty',aggfunc='sum',fill_value=0)
    changes=changes.reindex(pd.date_range(first,end),fill_value=0).cumsum().clip(lower=0).reindex(idx).ffill().fillna(0)
    assets=list(changes.columns)
    meta={}
    tickers=[];ccys=[]
    for asset,g in df.groupby('asset',sort=False):
        if asset not in changes.columns:continue
        g=g.sort_values('trade_date',kind='stable')
        latest=g[g.ticker.astype(str).str.len()>0]
        ticker=str(latest.iloc[-1].ticker) if not latest.empty else ''
        ccy=str(g.iloc[-1].trade_ccy)
        meta[asset]={'ticker':ticker,'ccy':ccy}
        if ticker:tickers.append(ticker)
        ccys.append(ccy)
    # price and FX data are requested only for the selected time range (but prices also
    # include one week prior to allow forward-filling across weekends and holidays).
    market_start=start-timedelta(days=10)
    prices,price_errors=get_prices(tickers,market_start,end)
    fxs,fx_errors=get_fx_series(ccys,base,market_start,end)
    # Last observed transaction price, carried forward ONLY as an explicit estimate
    # where an actual historical quote is unavailable.
    trade_price_by_asset={}
    for asset,g in df.groupby('asset'):
        purchase=g[['trade_date','price_trade_ccy']].copy()
        purchase['trade_date']=purchase.trade_date.dt.normalize()
        purchase=purchase.drop_duplicates('trade_date',keep='last').set_index('trade_date')['price_trade_ccy']
        trade_price_by_asset[asset]=purchase
    mat=np.zeros((len(idx),len(assets)),dtype='float64')
    unknown_assets=[];estimated_assets=[];warnings=[]
    for j,asset in enumerate(assets):
        m=meta[asset]
        # Full date price series, last observed actual close forward-filled.
        raw=pd.Series(prices.get(m['ticker'],{}),dtype='float64') if m['ticker'] else pd.Series(dtype='float64')
        if not raw.empty:raw.index=pd.to_datetime(raw.index)
        market_prices=raw.reindex(idx).ffill()  # trailing quotes from days before range need union index
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
            fx=fx.bfill() # weekend before first source date
        else:
            # Use the last transaction's *recorded* FX only as a labeled proxy
            # when the external historical FX series cannot be fetched.
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
    # Sample MAX graph to under ~950 points; always keep last index. No chart reconstruction on tab switch.
    max_points=920
    step=max(1,int(np.ceil(len(idx)/max_points)))
    take=np.arange(0,len(idx),step)
    if take[-1]!=len(idx)-1:take=np.r_[take,len(idx)-1]
    points=[{'date':idx[i].date().isoformat(),'value':round(float(total[i]),2)} for i in take]
    return {'points':points,'range':span,'warnings':warnings,
            'estimated_assets':sorted(set(estimated_assets)),
            'excluded_assets':sorted(set(unknown_assets)),
            'price_count':len(prices),'source':'Yahoo Finance daily closes + Frankfurter FX; trade-price estimates flagged'}
