"""Portfolio Track — FastAPI server for the mobile-first investment tracker."""
from __future__ import annotations
import csv
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date,datetime,timedelta,timezone
from io import StringIO
import hashlib,base64,hmac,os,re,secrets,threading
from pathlib import Path
import uuid
from fastapi import FastAPI,Request,Depends,HTTPException,UploadFile,File,Query
from fastapi.responses import HTMLResponse,JSONResponse,StreamingResponse,RedirectResponse,FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import update
from sqlalchemy.orm import Session as DBSession
import numpy as np
import pandas as pd
import requests

import portfolio as engine
import market
from storage import Session,User,Portfolio,init_db,get_portfolio,touch

BASE=Path(__file__).parent
SESSION_SECRET=os.getenv('SESSION_SECRET','local-dev-only-not-for-production-change-this')
app=FastAPI(title='Track portfolio API',docs_url=None,redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET,
                   same_site='lax',https_only=os.getenv('APP_ENV')=='production',max_age=60*60*24*14)
app.mount('/static',StaticFiles(directory=str(BASE/'static')),name='static')

@app.on_event('startup')
def startup():
    if os.getenv('APP_ENV')=='production' and SESSION_SECRET.startswith('local-dev-only'):
        raise RuntimeError('Production requires a strong SESSION_SECRET environment variable')
    init_db()


def session_user(request:Request)->str:
    user=request.session.get('user')
    if not user:raise HTTPException(401,'Sign in to access your portfolio')
    return user


def _check_origin(request:Request):
    """Protect cookie-authenticated mutating endpoints against cross-origin CSRF."""
    origin=request.headers.get('origin')
    host=request.headers.get('host','')
    if origin:
        from urllib.parse import urlparse
        source=urlparse(origin)
        if source.netloc!=host or source.scheme not in ('http','https'):
            raise HTTPException(403,'Cross-origin write refused')
    else:
        ref=request.headers.get('referer','')
        if ref:
            from urllib.parse import urlparse
            if urlparse(ref).netloc!=host:raise HTTPException(403,'Cross-origin write refused')


def db_dep():
    with Session() as session:yield session


def get_owned(user:str,db:DBSession)->Portfolio:
    return get_portfolio(db,user)


@dataclass
class Ledger:
    trades: pd.DataFrame
    tickers: tuple[str, ...] | None = None


_ledger_lock = threading.Lock()
_ledger_cache: OrderedDict[tuple[str, str], Ledger] = OrderedDict()


def _store_ledger(user: str, csv_text: str, ledger: Ledger) -> None:
    if len(csv_text) > 1_000_000:
        return
    key = (user, csv_text)
    with _ledger_lock:
        _ledger_cache[key] = ledger
        _ledger_cache.move_to_end(key)
        while len(_ledger_cache) > 8:
            _ledger_cache.popitem(last=False)


def ledger_for(p: Portfolio) -> Ledger:
    if len(p.csv_text) > 1_000_000:
        return Ledger(engine.read_trades(p.csv_text))
    key = (p.user_key, p.csv_text)
    with _ledger_lock:
        hit = _ledger_cache.get(key)
        if hit is not None:
            _ledger_cache.move_to_end(key)
            return hit
    ledger = Ledger(engine.read_trades(p.csv_text))
    _store_ledger(p.user_key, p.csv_text, ledger)
    return ledger


def data_for(p: Portfolio) -> pd.DataFrame:
    return ledger_for(p).trades.copy()


def quote_tickers(ledger: Ledger) -> tuple[str, ...]:
    if ledger.tickers is None:
        trades = ledger.trades
        positions, _, _ = engine.fifo(trades, 'price_trade_ccy')
        symbols = set()
        for asset, rows in trades[trades.ticker.ne('')].groupby('asset', sort=False):
            if positions[asset]['quantity'] > 1e-6:
                symbols.add(str(rows.sort_values('trade_date').iloc[-1].ticker))
        ledger.tickers = tuple(sorted(symbols))
    return ledger.tickers


def save_data(db:DBSession,p:Portfolio,d:pd.DataFrame):
    p.csv_text=engine.to_csv(d,True)
    touch(p);db.commit()
    _store_ledger(p.user_key, p.csv_text, Ledger(d.copy()))


def pw_hash(pw:str,salt:bytes)->str:
    return base64.b64encode(hashlib.pbkdf2_hmac('sha256',pw.encode(),salt,240000)).decode()


class Login(BaseModel):
    username:str=Field(min_length=2,max_length=55)
    password:str=Field(min_length=8,max_length=256)

@app.get('/',response_class=HTMLResponse)
def page():return FileResponse(BASE/'static'/'index.html')

@app.api_route('/health', methods=['GET', 'HEAD'])
def health():
    return JSONResponse(content={'ok': True}, status_code=200)

@app.get('/api/me')
def me(request:Request,db:DBSession=Depends(db_dep)):
    user=request.session.get('user')
    if not user:return {'authenticated':False,'google_enabled':bool(os.getenv('GOOGLE_CLIENT_ID') and os.getenv('GOOGLE_CLIENT_SECRET'))}
    rec=db.get(User,user)
    p=get_portfolio(db,user)
    return {'authenticated':True,'name':rec.name if rec else user.rsplit(':',1)[-1],
            'google':user.startswith('google:'),'base_currency':p.base_currency,
            'google_enabled':bool(os.getenv('GOOGLE_CLIENT_ID') and os.getenv('GOOGLE_CLIENT_SECRET'))}

@app.post('/api/register')
def register(data:Login,request:Request,db:DBSession=Depends(db_dep)):
    _check_origin(request)
    username=data.username.lower().strip()
    if not re.fullmatch(r'[a-z0-9_-]{2,55}',username):raise HTTPException(400,'Username may contain only letters, numbers, - and _.')
    key='local:'+username
    if db.get(User,key):raise HTTPException(409,'This username is already taken')
    salt=secrets.token_bytes(16)
    db.add(User(key=key,password_salt=base64.b64encode(salt).decode(),
                password_hash=pw_hash(data.password,salt),name=username))
    db.commit()
    request.session.clear();request.session['user']=key
    return {'ok':True}

@app.post('/api/login')
def login(data:Login,request:Request,db:DBSession=Depends(db_dep)):
    _check_origin(request)
    rec=db.get(User,'local:'+data.username.lower().strip())
    if not rec or not rec.password_salt:raise HTTPException(401,'Invalid username or password')
    salt=base64.b64decode(rec.password_salt)
    if not hmac.compare_digest(pw_hash(data.password,salt),rec.password_hash):
        raise HTTPException(401,'Invalid username or password')
    request.session.clear();request.session['user']=rec.key
    return {'ok':True}

@app.post('/api/logout')
def logout(request:Request):
    _check_origin(request);request.session.clear();return {'ok':True}

@app.get('/auth/google')
async def google_login(request:Request):
    client_id=os.getenv('GOOGLE_CLIENT_ID'); client_secret=os.getenv('GOOGLE_CLIENT_SECRET')
    if not client_id or not client_secret:raise HTTPException(503,'Google sign-in not configured')
    from authlib.integrations.starlette_client import OAuth
    oauth=OAuth()
    oauth.register(name='google',client_id=client_id,client_secret=client_secret,
                   server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
                   client_kwargs={'scope':'openid email profile'})
    callback=str(request.url_for('google_callback'))
    configured=os.getenv('PUBLIC_BASE_URL','').rstrip('/')
    if configured:callback=configured+'/auth/google/callback'
    return await oauth.google.authorize_redirect(request,callback)

@app.get('/auth/google/callback',name='google_callback')
async def google_callback(request:Request,db:DBSession=Depends(db_dep)):
    from authlib.integrations.starlette_client import OAuth
    oauth=OAuth()
    oauth.register(name='google',client_id=os.getenv('GOOGLE_CLIENT_ID'),
                   client_secret=os.getenv('GOOGLE_CLIENT_SECRET'),
                   server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
                   client_kwargs={'scope':'openid email profile'})
    try:
        token=await oauth.google.authorize_access_token(request)
        info=token.get('userinfo')
        if not info or not info.get('sub') or not info.get('email_verified'):
            raise ValueError('Verified Google identity not returned')
    except Exception:
        raise HTTPException(401,'Google authentication failed')
    key='google:'+str(info['sub'])
    name=str(info.get('given_name') or info.get('name') or 'Investor')[:150]
    rec=db.get(User,key)
    if rec is None:db.add(User(key=key,name=name));db.commit()
    request.session.clear();request.session['user']=key
    return RedirectResponse('/',status_code=303)

def dashboard_payload(p: Portfolio, ledger: Ledger,
                      preloaded_fx: tuple[dict[str, float], list[str]] | None = None) -> dict:
    d = ledger.trades
    ccys=d.trade_ccy.unique().tolist() if not d.empty else []
    if preloaded_fx is not None:
        fx, fxwarns = dict(preloaded_fx[0]), list(preloaded_fx[1])
    else:
        fx,fxwarns=market.spot_rates(ccys,p.base_currency) if ccys else ({},[])
    for ccy in ccys:
        if ccy not in fx:
            prev=d.loc[(d.trade_ccy==ccy)&(d.fx_to_base>0)].sort_values('trade_date')
            if not prev.empty:
                fx[ccy]=float(prev.iloc[-1].fx_to_base)
                fxwarns.append(f'Current FX unavailable for {ccy} → {p.base_currency}; using last recorded trade FX as an estimate.')
    out=engine.summarize(d,p.base_currency,fx)
    if not d.empty:
        _, realized_rows, _ = engine.fifo(engine.derived(d), 'unit_base')
        out['realized_total'] = round(sum(r['realized'] for r in realized_rows), 2)
    else:
        out['realized_total'] = 0.0
    out['warnings']=fxwarns+out['warnings']
    out['base_currency']=p.base_currency
    out['trades_count']=len(d)
    out['version']=p.version
    out['history_revision'] = market.history_revision(d, p.base_currency)
    ledger.tickers = tuple(sorted({h['ticker'] for h in out['holdings'] if h['ticker']}))
    return out

@app.get('/api/dashboard')
def dashboard(request: Request, user: str = Depends(session_user), db: DBSession = Depends(db_dep)):
    p = get_owned(user, db)
    db.commit()
    return dashboard_payload(p, ledger_for(p))


@app.get('/api/history')
def history(span: str = Query('1M'), user: str = Depends(session_user), db: DBSession = Depends(db_dep)):
    p = get_owned(user, db)
    db.commit()
    d = ledger_for(p).trades
    try:
        revision = market.history_revision(d, p.base_currency)
        result = market.portfolio_history(d, p.base_currency, span, revision=revision, owner=user)
        return {**result, 'history_revision': revision}
    except ValueError as exc:
        raise HTTPException(400, str(exc))

@app.get('/api/trades')
def trades(user:str=Depends(session_user),db:DBSession=Depends(db_dep)):
    d=data_for(get_owned(user,db)).sort_values('trade_date',ascending=False)
    out=[]
    for _,r in d.iterrows():
        obj={c:r[c] for c in engine.INPUT_COLS+['trade_id']}
        obj['trade_date']=r.trade_date.strftime('%Y-%m-%d %H:%M')
        out.append(obj)
    return {'rows':out}

class TradeInput(BaseModel):
    trade_date:str
    asset:str
    asset_type:str='Stock'
    side:str='BUY'
    quantity:float
    price_trade_ccy:float
    trade_ccy:str='USD'
    charges_trade_ccy:float=0
    stt_trade_ccy:float=0
    fx_to_base:float=1
    ticker:str=''
    last_price_trade_ccy:float=0
    last_price_base:float=0
    auto_fx:bool=False


def _fx_on_date(ccy:str,base:str,date_val:date)->float:
    if ccy==base:return 1.0
    s,e=date_val-timedelta(days=8),date_val+timedelta(days=1)
    data,errors=market.get_fx_series([ccy],base,s,e)
    rates=data.get(ccy,{})
    valid=[(day,r) for day,r in rates.items() if day<=date_val.isoformat()]
    if not valid:raise HTTPException(503,f'FX unavailable for {ccy} → {base} on {date_val}; supply manual FX')
    return sorted(valid)[-1][1]


def _validate_oversell(d:pd.DataFrame,asset:str,qty:float):
    owned=d.loc[d.asset==asset].assign(sgn=lambda x:np.where(x.side=='BUY',1,-1))
    available=float((owned.quantity*owned.sgn).sum())
    if qty>available+1e-5:raise HTTPException(422,f'Insufficient {asset} units; available {available:.6g}')

@app.post('/api/trades')
def add_trade(item:TradeInput,request:Request,user:str=Depends(session_user),db:DBSession=Depends(db_dep)):
    _check_origin(request)
    p=get_owned(user,db);d=data_for(p)
    item_dict=item.model_dump(exclude={'auto_fx'})
    val=engine.ensure_valid_trade(item_dict)
    trade_day=pd.Timestamp(val['trade_date']).date()
    if trade_day>date.today():raise HTTPException(422,'Trade date cannot be in the future')
    if item.auto_fx:
        val['fx_to_base']=_fx_on_date(val['trade_ccy'],p.base_currency,trade_day)
    if val['side']=='SELL':_validate_oversell(d,val['asset'],val['quantity'])
    if not val['ticker'] and not d.empty:
        prev=d.loc[(d.asset==val['asset'])&(d.ticker.astype(str).str.len()>0)].sort_values('trade_date')
        if not prev.empty:val['ticker']=str(prev.iloc[-1].ticker)
    val['trade_id']=uuid.uuid4().hex
    combined=pd.concat([d,pd.DataFrame([val])],ignore_index=True)
    save_data(db,p,engine.read_trades(engine.to_csv(combined)))
    return {'ok':True}

@app.put('/api/trades/{trade_id}')
def edit_trade(trade_id:str,item:TradeInput,request:Request,user:str=Depends(session_user),db:DBSession=Depends(db_dep)):
    _check_origin(request)
    p=get_owned(user,db);d=data_for(p)
    found=d.index[d.trade_id==trade_id]
    if len(found)!=1:raise HTTPException(404,'Trade not found')
    val=engine.ensure_valid_trade(item.model_dump(exclude={'auto_fx'}))
    if item.auto_fx:val['fx_to_base']=_fx_on_date(val['trade_ccy'],p.base_currency,pd.Timestamp(val['trade_date']).date())
    for key,value in val.items():d.at[found[0],key]=value
    save_data(db,p,engine.read_trades(engine.to_csv(d)))
    return {'ok':True}

@app.delete('/api/trades/{trade_id}')
def remove_trade(trade_id:str,request:Request,user:str=Depends(session_user),db:DBSession=Depends(db_dep)):
    _check_origin(request)
    p=get_owned(user,db);d=data_for(p)
    if not (d.trade_id==trade_id).any():raise HTTPException(404,'Trade not found')
    save_data(db,p,d[d.trade_id!=trade_id]);return {'ok':True}

@app.get('/api/tax')
def tax(start:date=Query(default_factory=lambda:date(date.today().year,1,1)),
        end:date=Query(default_factory=date.today),mode:str='base',
        user:str=Depends(session_user),db:DBSession=Depends(db_dep)):
    if start>end:raise HTTPException(400,'Start must precede end')
    p=get_owned(user,db)
    try:return engine.realized_report(data_for(p),p.base_currency,mode,start,end)
    except ValueError as e:raise HTTPException(400,str(e))

@app.get('/api/export/tax')
def tax_export(start:date,end:date,mode:str='base',user:str=Depends(session_user),db:DBSession=Depends(db_dep)):
    p=get_owned(user,db)
    report=engine.realized_report(data_for(p),p.base_currency,mode,start,end)
    output=StringIO();fieldnames=['asset','quantity_sold','buy_value','sell_value','realized','currency']
    writer=csv.DictWriter(output,fieldnames=fieldnames)
    writer.writeheader()
    for row in report['rows']:
        if mode=='base':row={**row,'currency':p.base_currency}
        writer.writerow(row)
    return StreamingResponse(iter([output.getvalue().encode('utf-8-sig')]),media_type='text/csv',
                             headers={'Content-Disposition':f'attachment; filename="realized_pl_{start}_{end}_{mode}.csv"'})

@app.get('/api/export/trades')
def export(user:str=Depends(session_user),db:DBSession=Depends(db_dep)):
    p=get_owned(user,db)
    b=engine.to_csv(data_for(p),include_ids=False).encode('utf-8-sig')
    return StreamingResponse(iter([b]),media_type='text/csv',
                             headers={'Content-Disposition':'attachment; filename="portfolio_trades.csv"'})

@app.post('/api/import')
async def do_import(request:Request,file:UploadFile=File(...),user:str=Depends(session_user),db:DBSession=Depends(db_dep)):
    _check_origin(request)
    raw=await file.read(6_000_001)
    if len(raw)>6_000_000:raise HTTPException(413,'CSV limit is 6 MB')
    try:
        d=engine.read_trades(raw.decode('utf-8-sig'))
    except (ValueError,UnicodeDecodeError) as e:
        raise HTTPException(422,str(e))
    p=get_owned(user,db)
    save_data(db,p,d)
    return {'ok':True,'count':len(d)}

class SettingsInput(BaseModel):
    base_currency:str

@app.put('/api/settings')
def settings(item:SettingsInput,request:Request,user:str=Depends(session_user),db:DBSession=Depends(db_dep)):
    _check_origin(request)
    new_base=item.base_currency.upper().strip()
    if not re.fullmatch('[A-Z]{3}',new_base):raise HTTPException(400,'Enter a three-letter currency code')
    p=get_owned(user,db)
    if p.base_currency==new_base:return {'ok':True,'base_currency':new_base}
    d=data_for(p)
    if not d.empty:
        rates,warnings=market.trade_fx_rates(d,new_base)
        if warnings or not np.isfinite(rates).all():
            raise HTTPException(503,'Cannot switch base currency: '+('; '.join(warnings) or 'FX missing'))
        d['fx_to_base']=rates
        p.csv_text=engine.to_csv(d)
    p.base_currency=new_base
    touch(p);db.commit()
    if not d.empty:
        _store_ledger(p.user_key, p.csv_text, Ledger(d))
    return {'ok':True,'base_currency':new_base}

@app.post('/api/refresh')
def refresh(request: Request, user: str = Depends(session_user), db: DBSession = Depends(db_dep),
            include_dashboard: bool = False, include_history: bool = False,
            span: str = Query('1M'), known_history_revision: str = Query('')):
    _check_origin(request)
    p = get_owned(user, db)
    db.commit()
    ledger = ledger_for(p)
    tickers = quote_tickers(ledger)
    # Overlap spot FX lookup with quote fetching when the caller also wants the dashboard.
    ccys = ledger.trades.trade_ccy.unique().tolist() if (include_dashboard and not ledger.trades.empty) else []
    fx_future = (market._coord_pool.submit(market.spot_rates, ccys, p.base_currency)
                 if (include_dashboard and ccys) else None)
    quotes, errors = market.get_latest_prices(list(tickers)) if tickers else ({}, {})
    reply = {'updated': len(quotes), 'requested': len(tickers), 'errors': errors,
             'quotes': {t: {'source': q['source'], 'as_of': q['as_of']} for t, q in quotes.items()}}
    active_ledger = ledger
    if quotes:
        trades = ledger.trades
        fresh = trades.ticker.map({t: q['price'] for t, q in quotes.items()})
        changed = fresh.notna() & fresh.ne(trades.last_price_trade_ccy)
        if changed.any():
            trades = trades.copy()
            trades['last_price_trade_ccy'] = trades.last_price_trade_ccy.astype(float).where(~changed, fresh)
            new_csv = engine.to_csv(trades)
            now_utc = datetime.now(timezone.utc)
            saved = db.execute(update(Portfolio).where(Portfolio.user_key == user, Portfolio.version == p.version)
                               .values(csv_text=new_csv, version=Portfolio.version + 1,
                                       updated_at=now_utc)
                               .execution_options(synchronize_session=False))
            db.commit()
            if saved.rowcount != 1:
                raise HTTPException(409, 'Portfolio changed during refresh. Refresh again for current prices.')
            p.csv_text = new_csv
            p.version += 1
            p.updated_at = now_utc
            active_ledger = Ledger(trades, tickers)
            _store_ledger(user, new_csv, active_ledger)
    preloaded_fx = fx_future.result() if fx_future is not None else None
    if include_dashboard:
        dash = dashboard_payload(p, active_ledger, preloaded_fx=preloaded_fx)
        reply['dashboard'] = dash
        if include_history:
            rev = dash['history_revision']
            if known_history_revision and known_history_revision == rev:
                reply['history_unchanged'] = True
            else:
                # Only attach history inline when already warm in RAM; never stall
                # the price refresh waiting on uncached historical chart downloads.
                cached_hist = market.peek_cached_history(span, revision=rev, owner=user)
                if cached_hist is not None:
                    reply['history'] = {**cached_hist, 'history_revision': rev}
    return reply
