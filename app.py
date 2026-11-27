"""Portfolio Track — FastAPI server for the integrated Investments & Cash tracker."""
from __future__ import annotations
import csv
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from io import StringIO
import hashlib, base64, hmac, os, re, secrets, threading
from pathlib import Path
import uuid
from concurrent.futures import Future
from fastapi import FastAPI, Request, Depends, HTTPException, UploadFile, File, Query, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import update, select, func
from sqlalchemy.orm import Session as DBSession
import numpy as np
import pandas as pd

import portfolio as engine
import cash as cash_engine
import market
from storage import (
    Session, User, Portfolio, CashLedger,
    init_db, get_portfolio, get_cash_ledger, touch,
)

BASE = Path(__file__).parent
SESSION_SECRET = os.getenv('SESSION_SECRET', 'local-dev-only-not-for-production-change-this')
app = FastAPI(title='Track portfolio API', docs_url=None, redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    SessionMiddleware, secret_key=SESSION_SECRET,
    same_site='lax', https_only=os.getenv('APP_ENV') == 'production', max_age=60 * 60 * 24 * 14,
)


class CachedStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200 and 'cache-control' not in response.headers:
            response.headers['Cache-Control'] = 'public, max-age=86400'
        return response


app.mount('/static', CachedStaticFiles(directory=str(BASE / 'static')), name='static')


@app.on_event('startup')
def startup():
    if os.getenv('APP_ENV') == 'production' and SESSION_SECRET.startswith('local-dev-only'):
        raise RuntimeError('Production requires a strong SESSION_SECRET environment variable')
    init_db()


def session_user(request: Request) -> str:
    user = request.session.get('user')
    if not user:
        raise HTTPException(401, 'Sign in to access your portfolio')
    return user


def _check_origin(request: Request):
    """Protect cookie-authenticated mutating endpoints against cross-origin CSRF."""
    origin = request.headers.get('origin')
    host = request.headers.get('host', '')
    if origin:
        from urllib.parse import urlparse
        source = urlparse(origin)
        if source.netloc != host or source.scheme not in ('http', 'https'):
            raise HTTPException(403, 'Cross-origin write refused')
    else:
        ref = request.headers.get('referer', '')
        if ref:
            from urllib.parse import urlparse
            if urlparse(ref).netloc != host:
                raise HTTPException(403, 'Cross-origin write refused')


def db_dep():
    with Session() as session:
        yield session


def get_owned(user: str, db: DBSession) -> Portfolio:
    return get_portfolio(db, user)


def get_owned_cash(user: str, db: DBSession) -> CashLedger:
    return get_cash_ledger(db, user)


@dataclass
class PortfolioMeta:
    user_key: str
    base_currency: str
    version: int
    updated_at: datetime | None


@dataclass
class CashMeta:
    user_key: str
    version: int
    updated_at: datetime | None


@dataclass
class Ledger:
    trades: pd.DataFrame
    tickers: tuple[str, ...] | None = None
    summary_state: engine.SummaryState | None = None
    inv_hash_bytes: bytes | None = None
    has_nse: bool | None = None

    def get_summary_state(self) -> engine.SummaryState:
        if self.summary_state is None:
            st = engine.build_summary_state(self.trades)
            self.summary_state = st
            self.tickers = st.open_tickers
        return self.summary_state

    def get_inv_hash(self) -> tuple[bytes, bool]:
        if self.inv_hash_bytes is None or self.has_nse is None:
            raw, has_nse = market.compute_inv_hash_bytes(self.trades)
            self.inv_hash_bytes = raw
            self.has_nse = has_nse
        return self.inv_hash_bytes, self.has_nse


@dataclass
class CashData:
    df: pd.DataFrame
    computed: pd.DataFrame | None = None
    summary_state: cash_engine.CashSummaryState | None = None
    cash_hash_bytes: bytes | None = None

    def get_computed(self) -> pd.DataFrame:
        if self.computed is None:
            self.computed = cash_engine.recompute_average_cost(self.df)
        return self.computed

    def get_summary_state(self) -> cash_engine.CashSummaryState:
        if self.summary_state is None:
            self.summary_state = cash_engine.build_cash_summary_state(self.df, computed=self.get_computed())
        return self.summary_state

    def get_cash_hash(self) -> bytes:
        if self.cash_hash_bytes is None:
            self.cash_hash_bytes = market.compute_cash_hash_bytes(self.df)
        return self.cash_hash_bytes


_ledger_lock = threading.Lock()
_ledger_cache: OrderedDict[tuple[str, str], Ledger] = OrderedDict()
_ledger_meta_cache: OrderedDict[tuple, Ledger] = OrderedDict()
_cash_cache: OrderedDict[tuple[str, str], CashData] = OrderedDict()
_cash_meta_cache: OrderedDict[tuple, CashData] = OrderedDict()
_pending_parse: dict[tuple, Future] = {}


def _meta_stamp(dt: datetime | None) -> str:
    return dt.isoformat() if dt is not None else ''


def _store_ledger(user: str, csv_text: str, ledger: Ledger,
                  version: int | None = None, updated_at: datetime | None = None) -> None:
    csv_len = len(csv_text)
    with _ledger_lock:
        if csv_len <= 1_000_000:
            key = (user, csv_text)
            _ledger_cache[key] = ledger
            _ledger_cache.move_to_end(key)
            while len(_ledger_cache) > 8:
                _ledger_cache.popitem(last=False)
        if version is not None:
            mkey = (user, version, _meta_stamp(updated_at), csv_len)
            _ledger_meta_cache[mkey] = ledger
            _ledger_meta_cache.move_to_end(mkey)
            while len(_ledger_meta_cache) > 8:
                _ledger_meta_cache.popitem(last=False)


def ledger_for(p: Portfolio) -> Ledger:
    csv_text = p.csv_text
    csv_len = len(csv_text)
    mkey = (p.user_key, p.version, _meta_stamp(p.updated_at), csv_len)
    with _ledger_lock:
        hit = _ledger_meta_cache.get(mkey)
        if hit is not None:
            _ledger_meta_cache.move_to_end(mkey)
            return hit
        if csv_len <= 1_000_000:
            ckey = (p.user_key, csv_text)
            hit = _ledger_cache.get(ckey)
            if hit is not None:
                _ledger_cache.move_to_end(ckey)
                _ledger_meta_cache[mkey] = hit
                _ledger_meta_cache.move_to_end(mkey)
                return hit
    ledger = Ledger(engine.read_trades(csv_text))
    _store_ledger(p.user_key, csv_text, ledger, version=p.version, updated_at=p.updated_at)
    return ledger


def data_for(p: Portfolio) -> pd.DataFrame:
    return ledger_for(p).trades.copy()


def _store_cash(user: str, csv_text: str, df_or_data: pd.DataFrame | CashData,
                version: int | None = None, updated_at: datetime | None = None) -> CashData:
    data = df_or_data if isinstance(df_or_data, CashData) else CashData(df_or_data)
    csv_len = len(csv_text)
    with _ledger_lock:
        if csv_len <= 1_000_000:
            key = (user, csv_text)
            _cash_cache[key] = data
            _cash_cache.move_to_end(key)
            while len(_cash_cache) > 8:
                _cash_cache.popitem(last=False)
        if version is not None:
            mkey = (user, version, _meta_stamp(updated_at), csv_len)
            _cash_meta_cache[mkey] = data
            _cash_meta_cache.move_to_end(mkey)
            while len(_cash_meta_cache) > 8:
                _cash_meta_cache.popitem(last=False)
    return data


def cash_obj_for(c: CashLedger) -> CashData:
    csv_text = c.csv_text
    csv_len = len(csv_text)
    mkey = (c.user_key, c.version, _meta_stamp(c.updated_at), csv_len)
    with _ledger_lock:
        hit = _cash_meta_cache.get(mkey)
        if hit is not None:
            _cash_meta_cache.move_to_end(mkey)
            return hit
        if csv_len <= 1_000_000:
            ckey = (c.user_key, csv_text)
            hit = _cash_cache.get(ckey)
            if hit is not None:
                _cash_cache.move_to_end(ckey)
                _cash_meta_cache[mkey] = hit
                _cash_meta_cache.move_to_end(mkey)
                return hit
    data = CashData(cash_engine.read_cash_txns(csv_text))
    return _store_cash(c.user_key, csv_text, data, version=c.version, updated_at=c.updated_at)


def cash_frame_for(c: CashLedger) -> pd.DataFrame:
    return cash_obj_for(c).df


def cash_data_for(c: CashLedger) -> pd.DataFrame:
    return cash_frame_for(c).copy()


def load_portfolio_state(user: str, db: DBSession) -> tuple[PortfolioMeta, Ledger]:
    """Read lightweight metadata first; skip downloading full csv_text on RAM cache hit."""
    row = db.execute(
        select(Portfolio.base_currency, Portfolio.version, Portfolio.updated_at, func.length(Portfolio.csv_text))
        .where(Portfolio.user_key == user)
    ).first()
    if row is None:
        p = get_portfolio(db, user)
        return PortfolioMeta(user, p.base_currency, p.version, p.updated_at), ledger_for(p)
    base_ccy, version, updated_at, csv_len = row[0], int(row[1]), row[2], int(row[3] or 0)
    meta = PortfolioMeta(user, base_ccy, version, updated_at)
    mkey = (user, version, _meta_stamp(updated_at), csv_len)
    parse_key = ('inv', mkey)
    with _ledger_lock:
        hit = _ledger_meta_cache.get(mkey)
        if hit is not None:
            _ledger_meta_cache.move_to_end(mkey)
            return meta, hit
        pending = _pending_parse.get(parse_key)
        leader = pending is None
        if leader:
            pending = _pending_parse[parse_key] = Future()
    if not leader:
        return meta, pending.result()
    try:
        csv_text = db.scalar(select(Portfolio.csv_text).where(Portfolio.user_key == user)) or ''
        ledger = Ledger(engine.read_trades(csv_text))
        _store_ledger(user, csv_text, ledger, version=version, updated_at=updated_at)
        pending.set_result(ledger)
        return meta, ledger
    except BaseException as exc:
        pending.set_exception(exc)
        raise
    finally:
        with _ledger_lock:
            _pending_parse.pop(parse_key, None)


def load_cash_state(user: str, db: DBSession) -> tuple[CashMeta, CashData]:
    """Read lightweight metadata first; skip downloading full cash csv_text on RAM cache hit."""
    row = db.execute(
        select(CashLedger.version, CashLedger.updated_at, func.length(CashLedger.csv_text))
        .where(CashLedger.user_key == user)
    ).first()
    if row is None:
        c = get_cash_ledger(db, user)
        return CashMeta(user, c.version, c.updated_at), cash_obj_for(c)
    version, updated_at, csv_len = int(row[0]), row[1], int(row[2] or 0)
    meta = CashMeta(user, version, updated_at)
    mkey = (user, version, _meta_stamp(updated_at), csv_len)
    parse_key = ('cash', mkey)
    with _ledger_lock:
        hit = _cash_meta_cache.get(mkey)
        if hit is not None:
            _cash_meta_cache.move_to_end(mkey)
            return meta, hit
        pending = _pending_parse.get(parse_key)
        leader = pending is None
        if leader:
            pending = _pending_parse[parse_key] = Future()
    if not leader:
        return meta, pending.result()
    try:
        csv_text = db.scalar(select(CashLedger.csv_text).where(CashLedger.user_key == user)) or ''
        data = CashData(cash_engine.read_cash_txns(csv_text))
        _store_cash(user, csv_text, data, version=version, updated_at=updated_at)
        pending.set_result(data)
        return meta, data
    except BaseException as exc:
        pending.set_exception(exc)
        raise
    finally:
        with _ledger_lock:
            _pending_parse.pop(parse_key, None)


def quote_tickers(ledger: Ledger) -> tuple[str, ...]:
    if ledger.tickers is None:
        ledger.tickers = ledger.get_summary_state().open_tickers
    return ledger.tickers


def save_data(db: DBSession, p: Portfolio, d: pd.DataFrame):
    p.csv_text = engine.to_csv(d, True)
    touch(p)
    db.commit()
    _store_ledger(p.user_key, p.csv_text, Ledger(d.copy()), version=p.version, updated_at=p.updated_at)


def save_cash_data(db: DBSession, c: CashLedger, d: pd.DataFrame):
    c.csv_text = cash_engine.to_cash_csv(d, True)
    touch(c)
    db.commit()
    _store_cash(c.user_key, c.csv_text, d.copy(), version=c.version, updated_at=c.updated_at)


def pw_hash(pw: str, salt: bytes) -> str:
    return base64.b64encode(hashlib.pbkdf2_hmac('sha256', pw.encode(), salt, 240000)).decode()


class Login(BaseModel):
    username: str = Field(min_length=2, max_length=55)
    password: str = Field(min_length=8, max_length=256)


@app.get('/', response_class=HTMLResponse)
def page():
    return FileResponse(BASE / 'static' / 'index.html')


@app.api_route('/health', methods=['GET', 'HEAD'])
def health():
    return JSONResponse(content={'ok': True}, status_code=200)


@app.get('/api/me')
def me(
    request: Request,
    background_tasks: BackgroundTasks,
    include_dashboard: bool = False,
    db: DBSession = Depends(db_dep),
):
    user = request.session.get('user')
    google_enabled = bool(os.getenv('GOOGLE_CLIENT_ID') and os.getenv('GOOGLE_CLIENT_SECRET'))
    if not user:
        return {
            'authenticated': False,
            'google_enabled': google_enabled,
        }
    name = db.scalar(select(User.name).where(User.key == user))
    if include_dashboard:
        p_meta, ledger = load_portfolio_state(user, db)
        c_meta, cash_data = load_cash_state(user, db)
        dash = dashboard_payload(p_meta, ledger, c_meta, cash_data)
        _, has_nse = ledger.get_inv_hash()
        background_tasks.add_task(market.warm_quote_providers, has_nse)
        return {
            'authenticated': True,
            'name': name if name else user.rsplit(':', 1)[-1],
            'google': user.startswith('google:'),
            'base_currency': p_meta.base_currency,
            'google_enabled': google_enabled,
            'dashboard': dash,
        }
    base_ccy = db.scalar(select(Portfolio.base_currency).where(Portfolio.user_key == user))
    if base_ccy is None:
        base_ccy = get_portfolio(db, user).base_currency
    return {
        'authenticated': True,
        'name': name if name else user.rsplit(':', 1)[-1],
        'google': user.startswith('google:'),
        'base_currency': base_ccy,
        'google_enabled': google_enabled,
    }


@app.post('/api/register')
def register(data: Login, request: Request, db: DBSession = Depends(db_dep)):
    _check_origin(request)
    username = data.username.lower().strip()
    if not re.fullmatch(r'[a-z0-9_-]{2,55}', username):
        raise HTTPException(400, 'Username may contain only letters, numbers, - and _.')
    key = 'local:' + username
    if db.get(User, key):
        raise HTTPException(409, 'This username is already taken')
    salt = secrets.token_bytes(16)
    db.add(User(
        key=key,
        password_salt=base64.b64encode(salt).decode(),
        password_hash=pw_hash(data.password, salt),
        name=username,
    ))
    db.commit()
    request.session.clear()
    request.session['user'] = key
    return {'ok': True}


@app.post('/api/login')
def login(data: Login, request: Request, db: DBSession = Depends(db_dep)):
    _check_origin(request)
    rec = db.get(User, 'local:' + data.username.lower().strip())
    if not rec or not rec.password_salt:
        raise HTTPException(401, 'Invalid username or password')
    salt = base64.b64decode(rec.password_salt)
    if not hmac.compare_digest(pw_hash(data.password, salt), rec.password_hash):
        raise HTTPException(401, 'Invalid username or password')
    request.session.clear()
    request.session['user'] = rec.key
    return {'ok': True}


@app.post('/api/logout')
def logout(request: Request):
    _check_origin(request)
    request.session.clear()
    return {'ok': True}


@app.get('/auth/google')
async def google_login(request: Request):
    client_id = os.getenv('GOOGLE_CLIENT_ID')
    client_secret = os.getenv('GOOGLE_CLIENT_SECRET')
    if not client_id or not client_secret:
        raise HTTPException(503, 'Google sign-in not configured')
    from authlib.integrations.starlette_client import OAuth
    oauth = OAuth()
    oauth.register(
        name='google', client_id=client_id, client_secret=client_secret,
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_kwargs={'scope': 'openid email profile'},
    )
    callback = str(request.url_for('google_callback'))
    configured = os.getenv('PUBLIC_BASE_URL', '').rstrip('/')
    if configured:
        callback = configured + '/auth/google/callback'
    return await oauth.google.authorize_redirect(request, callback)


@app.get('/auth/google/callback', name='google_callback')
async def google_callback(request: Request, db: DBSession = Depends(db_dep)):
    from authlib.integrations.starlette_client import OAuth
    oauth = OAuth()
    oauth.register(
        name='google', client_id=os.getenv('GOOGLE_CLIENT_ID'),
        client_secret=os.getenv('GOOGLE_CLIENT_SECRET'),
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_kwargs={'scope': 'openid email profile'},
    )
    try:
        token = await oauth.google.authorize_access_token(request)
        info = token.get('userinfo')
        if not info or not info.get('sub') or not info.get('email_verified'):
            raise ValueError('Verified Google identity not returned')
    except Exception:
        raise HTTPException(401, 'Google authentication failed')
    key = 'google:' + str(info['sub'])
    name = str(info.get('given_name') or info.get('name') or 'Investor')[:150]
    rec = db.get(User, key)
    if rec is None:
        db.add(User(key=key, name=name))
        db.commit()
    request.session.clear()
    request.session['user'] = key
    return RedirectResponse('/', status_code=303)


def _all_currencies(trades_df: pd.DataFrame, cash_df: pd.DataFrame) -> list[str]:
    ccys: list[str] = []
    if not trades_df.empty:
        ccys.extend(trades_df.trade_ccy.unique().tolist())
    if not cash_df.empty:
        ccys.extend(cash_df['foreign_ccy'].astype(str).str.upper().unique().tolist())
    return list(dict.fromkeys(c for c in ccys if c))


def _open_currencies(inv_state: engine.SummaryState, cash_state: cash_engine.CashSummaryState) -> list[str]:
    return list(dict.fromkeys(c for c in (*inv_state.open_currencies, *cash_state.open_currencies) if c))


def dashboard_payload(
    p: Portfolio | PortfolioMeta,
    ledger: Ledger,
    c: CashLedger | CashMeta,
    cash_df: pd.DataFrame | CashData,
    preloaded_fx: tuple[dict[str, float], list[str]] | None = None,
) -> dict:
    d = ledger.trades
    cash_obj = cash_df if isinstance(cash_df, CashData) else CashData(cash_df)
    cdf = cash_obj.df

    inv_state = ledger.get_summary_state()
    cash_state = cash_obj.get_summary_state()
    ccys = _open_currencies(inv_state, cash_state)
    if preloaded_fx is not None:
        fx, fxwarns = dict(preloaded_fx[0]), list(preloaded_fx[1])
    else:
        fx, fxwarns = market.spot_rates(ccys, p.base_currency) if ccys else ({}, [])

    for ccy in ccys:
        if ccy not in fx:
            if not d.empty:
                prev = d.loc[(d.trade_ccy == ccy) & (d.fx_to_base > 0)].sort_values('trade_date')
                if not prev.empty:
                    fx[ccy] = float(prev.iloc[-1].fx_to_base)
                    fxwarns.append(f'Current FX unavailable for {ccy} → {p.base_currency}; using last recorded trade FX as an estimate.')
                    continue
            if not cdf.empty:
                prev_c = cdf.loc[(cdf['foreign_ccy'] == ccy) & (cdf['fx_rate'] > 0)].sort_values('date')
                if not prev_c.empty:
                    fx[ccy] = float(prev_c.iloc[-1]['fx_rate'])
                    fxwarns.append(f'Current FX unavailable for {ccy} → {p.base_currency}; using last recorded cash FX as an estimate.')

    out = engine.summarize_from_state(inv_state, p.base_currency, fx)
    out['realized_total'] = inv_state.realized_total

    cash_summary = cash_engine.summarize_cash_from_state(cash_state, p.base_currency, fx)

    inv_val = float(out['totals']['value'] or 0.0)
    inv_cost = float(out['totals']['invested'] or 0.0)
    inv_pnl = float(out['totals']['pnl'] or 0.0)
    inv_realized = float(out['realized_total'] or 0.0)

    cash_val = float(cash_summary['totals']['value'] or 0.0)
    cash_cost = float(cash_summary['totals']['invested'] or 0.0)
    cash_pnl = float(cash_summary['totals']['pnl'] or 0.0)
    cash_realized = float(cash_summary['totals']['realized_total'] or 0.0)

    nav_val = inv_val + cash_val
    nav_cost = inv_cost + cash_cost
    nav_pnl = inv_pnl + cash_pnl
    nav_pnl_pct = (100.0 * nav_pnl / nav_cost) if abs(nav_cost) > 1e-9 else 0.0
    pos_sum = max(0.0, inv_val) + max(0.0, cash_val)
    inv_share = (100.0 * max(0.0, inv_val) / pos_sum) if pos_sum > 1e-9 else 0.0
    cash_share = (100.0 * max(0.0, cash_val) / pos_sum) if pos_sum > 1e-9 else 0.0

    inv_hash, has_nse = ledger.get_inv_hash()
    cash_hash = cash_obj.get_cash_hash()
    nse_tok = market.nse_revision_token() if has_nse else b''
    inv_rev = market.history_revision(
        d, p.base_currency, mode='investments',
        inv_hash_bytes=inv_hash, has_nse=has_nse, nse_token=nse_tok,
    )
    cash_rev = market.history_revision(
        d, p.base_currency, cash_df=cdf, mode='cash',
        cash_hash_bytes=cash_hash,
    )
    nav_rev = market.history_revision(
        d, p.base_currency, cash_df=cdf, mode='nav',
        inv_hash_bytes=inv_hash, cash_hash_bytes=cash_hash, has_nse=has_nse, nse_token=nse_tok,
    )

    out['warnings'] = list(dict.fromkeys(fxwarns + out['warnings'] + cash_summary['warnings']))
    out['base_currency'] = p.base_currency
    out['trades_count'] = len(d)
    out['cash_count'] = len(cdf)
    out['version'] = p.version
    out['cash_version'] = c.version
    out['cash'] = cash_summary
    out['nav'] = {
        'value': nav_val,
        'invested': nav_cost,
        'pnl': nav_pnl,
        'pnl_pct': nav_pnl_pct,
        'realized_total': round(inv_realized + cash_realized, 2),
        'investments_value': inv_val,
        'cash_value': cash_val,
        'investments_share': inv_share,
        'cash_share': cash_share,
    }
    out['history_revision'] = inv_rev
    out['history_revisions'] = {
        'investments': inv_rev,
        'cash': cash_rev,
        'nav': nav_rev,
    }
    ledger.tickers = inv_state.open_tickers
    return out


@app.get('/api/dashboard')
def dashboard(
    request: Request,
    background_tasks: BackgroundTasks,
    user: str = Depends(session_user),
    db: DBSession = Depends(db_dep),
):
    p_meta, ledger = load_portfolio_state(user, db)
    c_meta, cash_data = load_cash_state(user, db)
    payload = dashboard_payload(p_meta, ledger, c_meta, cash_data)
    _, has_nse = ledger.get_inv_hash()
    background_tasks.add_task(market.warm_quote_providers, has_nse)
    return payload


@app.get('/api/history')
def history(
    background_tasks: BackgroundTasks,
    span: str = Query('1M'),
    mode: str = Query('investments'),
    user: str = Depends(session_user),
    db: DBSession = Depends(db_dep),
):
    if mode not in ('investments', 'cash', 'nav'):
        raise HTTPException(400, 'Unsupported history mode')
    p_meta, ledger = load_portfolio_state(user, db)
    c_meta, cash_data = load_cash_state(user, db)
    d = ledger.trades
    cdf = cash_data.df
    inv_hash, has_nse = ledger.get_inv_hash()
    cash_hash = cash_data.get_cash_hash()
    nse_tok = market.nse_revision_token() if has_nse else b''
    cash_comp = cash_data.get_computed() if not cdf.empty else None
    try:
        revision = market.history_revision(
            d, p_meta.base_currency, cash_df=cdf, mode=mode,
            inv_hash_bytes=inv_hash, cash_hash_bytes=cash_hash, has_nse=has_nse, nse_token=nse_tok,
        )
        result = market.portfolio_history(
            d, p_meta.base_currency, span,
            revision=revision, owner=user, cash_df=cdf, mode=mode,
            inv_hash_bytes=inv_hash, cash_hash_bytes=cash_hash,
            has_nse=has_nse, nse_token=nse_tok, cash_computed=cash_comp,
        )
        if span == '1M':
            background_tasks.add_task(
                market.warm_history_cache,
                d, cdf, p_meta.base_currency,
                inv_hash_bytes=inv_hash, cash_hash_bytes=cash_hash, cash_computed=cash_comp,
            )
        return {**result, 'history_revision': revision, 'mode': mode}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get('/api/trades')
def trades(user: str = Depends(session_user), db: DBSession = Depends(db_dep)):
    _, ledger = load_portfolio_state(user, db)
    d = ledger.trades
    if d.empty:
        return {'rows': []}
    d_sorted = d.sort_values('trade_date', ascending=False)
    cols = engine.INPUT_COLS + ['trade_id']
    formatted = d_sorted[cols].copy()
    formatted['trade_date'] = formatted['trade_date'].dt.strftime('%Y-%m-%d %H:%M')
    return {'rows': formatted.to_dict('records')}


class TradeInput(BaseModel):
    trade_date: str
    asset: str
    asset_type: str = 'Stock'
    side: str = 'BUY'
    quantity: float
    price_trade_ccy: float
    trade_ccy: str = 'USD'
    charges_trade_ccy: float = 0
    stt_trade_ccy: float = 0
    fx_to_base: float = 1
    ticker: str = ''
    last_price_trade_ccy: float = 0
    last_price_base: float = 0
    auto_fx: bool = False


class CashTxnInput(BaseModel):
    date: str
    foreign_ccy: str
    txn_type: str = 'Credit'
    foreign_amount: float
    fx_rate: float = 1.0
    fee_foreign: float = 0.0
    memo: str = ''
    auto_fx: bool = False


def _fx_on_date(ccy: str, base: str, date_val: date) -> float:
    ccy_u, base_u = ccy.upper(), base.upper()
    if ccy_u == base_u:
        return 1.0
    s, e = date_val - timedelta(days=8), date_val + timedelta(days=1)
    data, _ = market.get_fx_series([ccy_u], base_u, s, e)
    rates = data.get(ccy_u, {})
    valid = [(day, r) for day, r in rates.items() if day <= date_val.isoformat()]
    if not valid:
        raise HTTPException(503, f'FX unavailable for {ccy_u} → {base_u} on {date_val}; supply manual FX')
    return sorted(valid)[-1][1]


def _validate_oversell(d: pd.DataFrame, asset: str, qty: float):
    owned = d.loc[d.asset == asset].assign(sgn=lambda x: np.where(x.side == 'BUY', 1, -1))
    available = float((owned.quantity * owned.sgn).sum())
    if qty > available + 1e-5:
        raise HTTPException(422, f'Insufficient {asset} units; available {available:.6g}')


@app.post('/api/trades')
def add_trade(item: TradeInput, request: Request, user: str = Depends(session_user), db: DBSession = Depends(db_dep)):
    _check_origin(request)
    p = get_owned(user, db)
    d = data_for(p)
    item_dict = item.model_dump(exclude={'auto_fx'})
    val = engine.ensure_valid_trade(item_dict)
    trade_day = pd.Timestamp(val['trade_date']).date()
    if trade_day > date.today():
        raise HTTPException(422, 'Trade date cannot be in the future')
    if item.auto_fx:
        val['fx_to_base'] = _fx_on_date(val['trade_ccy'], p.base_currency, trade_day)
    if val['side'] == 'SELL':
        _validate_oversell(d, val['asset'], val['quantity'])
    if not val['ticker'] and not d.empty:
        prev = d.loc[(d.asset == val['asset']) & (d.ticker.astype(str).str.len() > 0)].sort_values('trade_date')
        if not prev.empty:
            val['ticker'] = str(prev.iloc[-1].ticker)
    val['trade_id'] = uuid.uuid4().hex
    combined = pd.concat([d, pd.DataFrame([val])], ignore_index=True)
    save_data(db, p, engine.read_trades(engine.to_csv(combined)))

    gross = float(val['quantity']) * float(val['price_trade_ccy'])
    fees = float(val['charges_trade_ccy']) + float(val['stt_trade_ccy'])
    is_buy = val['side'] == 'BUY'
    cash_amount = round(gross + fees, 8) if is_buy else round(max(0.0, gross - fees), 8)
    suggested_cash = {
        'date': trade_day.isoformat(),
        'foreign_ccy': val['trade_ccy'],
        'txn_type': 'Debit' if is_buy else 'Credit',
        'foreign_amount': cash_amount,
        'fx_rate': float(val['fx_to_base']),
        'fee_foreign': 0.0,
        'memo': f"{'Buy' if is_buy else 'Sell'} {val['asset']}"[:200],
    }
    return {'ok': True, 'suggested_cash': suggested_cash}


@app.put('/api/trades/{trade_id}')
def edit_trade(trade_id: str, item: TradeInput, request: Request, user: str = Depends(session_user), db: DBSession = Depends(db_dep)):
    _check_origin(request)
    p = get_owned(user, db)
    d = data_for(p)
    found = d.index[d.trade_id == trade_id]
    if len(found) != 1:
        raise HTTPException(404, 'Trade not found')
    val = engine.ensure_valid_trade(item.model_dump(exclude={'auto_fx'}))
    if item.auto_fx:
        val['fx_to_base'] = _fx_on_date(val['trade_ccy'], p.base_currency, pd.Timestamp(val['trade_date']).date())
    for key, value in val.items():
        d.at[found[0], key] = value
    save_data(db, p, engine.read_trades(engine.to_csv(d)))
    return {'ok': True}


@app.delete('/api/trades/{trade_id}')
def remove_trade(trade_id: str, request: Request, user: str = Depends(session_user), db: DBSession = Depends(db_dep)):
    _check_origin(request)
    p = get_owned(user, db)
    d = data_for(p)
    if not (d.trade_id == trade_id).any():
        raise HTTPException(404, 'Trade not found')
    save_data(db, p, d[d.trade_id != trade_id])
    return {'ok': True}


@app.get('/api/cash/transactions')
def list_cash_txns(user: str = Depends(session_user), db: DBSession = Depends(db_dep)):
    _, cash_data = load_cash_state(user, db)
    if cash_data.df.empty:
        return {'rows': []}
    computed = cash_data.get_computed().iloc[::-1]
    ids = computed['txn_id'].astype(str).tolist()
    dates = pd.to_datetime(computed['date']).dt.strftime('%Y-%m-%d').tolist()
    ccys = computed['foreign_ccy'].astype(str).tolist()
    types = computed['txn_type'].astype(str).tolist()
    f_amts = computed['foreign_amount'].to_numpy(dtype=float).tolist()
    fxs = computed['fx_rate'].to_numpy(dtype=float).tolist()
    fees = computed['fee_foreign'].to_numpy(dtype=float).tolist()
    memos = [str(m or '') for m in computed['memo'].tolist()]
    b_amts = np.abs(computed['base_amount'].to_numpy(dtype=float)).tolist()
    r_pls = computed['realized_pl_base'].to_numpy(dtype=float).tolist()
    out = [
        {
            'txn_id': tid,
            'date': dt,
            'foreign_ccy': ccy,
            'txn_type': tt,
            'foreign_amount': fa,
            'amount': abs(fa),
            'fx_rate': fx,
            'fee_foreign': fee,
            'memo': memo,
            'base_amount': ba,
            'realized_pl_base': rpl,
        }
        for tid, dt, ccy, tt, fa, fx, fee, memo, ba, rpl in zip(
            ids, dates, ccys, types, f_amts, fxs, fees, memos, b_amts, r_pls
        )
    ]
    return {'rows': out}


@app.post('/api/cash/transactions')
def add_cash_txn(item: CashTxnInput, request: Request, user: str = Depends(session_user), db: DBSession = Depends(db_dep)):
    _check_origin(request)
    p = get_owned(user, db)
    c = get_owned_cash(user, db)
    d = cash_data_for(c)
    raw = item.model_dump(exclude={'auto_fx'})
    try:
        val = cash_engine.ensure_valid_cash_txn(raw)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    txn_day = pd.Timestamp(val['date']).date()
    if txn_day > date.today():
        raise HTTPException(422, 'Transaction date cannot be in the future')
    if item.auto_fx:
        val['fx_rate'] = _fx_on_date(val['foreign_ccy'], p.base_currency, txn_day)
    val['txn_id'] = uuid.uuid4().hex
    combined = pd.concat([d, pd.DataFrame([val])], ignore_index=True)
    save_cash_data(db, c, cash_engine.read_cash_txns(cash_engine.to_cash_csv(combined)))
    return {'ok': True}


@app.put('/api/cash/transactions/{txn_id}')
def edit_cash_txn(txn_id: str, item: CashTxnInput, request: Request, user: str = Depends(session_user), db: DBSession = Depends(db_dep)):
    _check_origin(request)
    p = get_owned(user, db)
    c = get_owned_cash(user, db)
    d = cash_data_for(c)
    found = d.index[d.txn_id == txn_id]
    if len(found) != 1:
        raise HTTPException(404, 'Cash transaction not found')
    try:
        val = cash_engine.ensure_valid_cash_txn(item.model_dump(exclude={'auto_fx'}))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    txn_day = pd.Timestamp(val['date']).date()
    if txn_day > date.today():
        raise HTTPException(422, 'Transaction date cannot be in the future')
    if item.auto_fx:
        val['fx_rate'] = _fx_on_date(val['foreign_ccy'], p.base_currency, txn_day)
    for key, value in val.items():
        d.at[found[0], key] = value
    save_cash_data(db, c, cash_engine.read_cash_txns(cash_engine.to_cash_csv(d)))
    return {'ok': True}


@app.delete('/api/cash/transactions/{txn_id}')
def remove_cash_txn(txn_id: str, request: Request, user: str = Depends(session_user), db: DBSession = Depends(db_dep)):
    _check_origin(request)
    c = get_owned_cash(user, db)
    d = cash_data_for(c)
    if not (d.txn_id == txn_id).any():
        raise HTTPException(404, 'Cash transaction not found')
    save_cash_data(db, c, d[d.txn_id != txn_id])
    return {'ok': True}


@app.get('/api/export/cash')
def export_cash(user: str = Depends(session_user), db: DBSession = Depends(db_dep)):
    c = get_owned_cash(user, db)
    b = cash_engine.to_cash_csv(cash_data_for(c), include_ids=False).encode('utf-8-sig')
    return StreamingResponse(
        iter([b]),
        media_type='text/csv',
        headers={'Content-Disposition': 'attachment; filename="cash_txns.csv"'},
    )


@app.post('/api/cash/import')
async def import_cash(request: Request, file: UploadFile = File(...), user: str = Depends(session_user), db: DBSession = Depends(db_dep)):
    _check_origin(request)
    raw = await file.read(6_000_001)
    if len(raw) > 6_000_000:
        raise HTTPException(413, 'CSV limit is 6 MB')
    try:
        d = cash_engine.read_cash_txns(raw.decode('utf-8-sig'))
    except (ValueError, UnicodeDecodeError) as e:
        raise HTTPException(422, str(e))
    c = get_owned_cash(user, db)
    save_cash_data(db, c, d)
    return {'ok': True, 'count': len(d)}


def _combined_tax_report(
    p: Portfolio | PortfolioMeta,
    c: CashLedger | CashMeta,
    mode: str,
    start: date,
    end: date,
    *,
    ledger: Ledger | None = None,
    cash_data: CashData | None = None,
) -> dict:
    d = ledger.trades if ledger is not None else ledger_for(p).trades
    c_obj = cash_data if cash_data is not None else cash_obj_for(c)
    cdf = c_obj.df
    report = engine.realized_report(d, p.base_currency, mode, start, end)
    if report['rows'] and not d.empty:
        chronological = d.sort_values('trade_date', kind='stable')
        latest_type = chronological.drop_duplicates('asset', keep='last').set_index('asset')['asset_type'].to_dict()
        named = chronological[chronological.ticker.astype(str).str.len() > 0].drop_duplicates('asset', keep='last')
        latest_ticker = named.set_index('asset')['ticker'].to_dict()
        for row in report['rows']:
            asset = row['asset']
            row['ticker'] = str(latest_ticker.get(asset, ''))
            row['type'] = str(latest_type.get(asset, 'Stock') or 'Stock')
    cash_rows = cash_engine.realized_cash_report(cdf, p.base_currency, start, end, computed=c_obj.get_computed())
    if cash_rows:
        report['rows'] = sorted(report['rows'] + cash_rows, key=lambda r: r['realized'], reverse=True)
        if mode == 'base':
            report['total'] = sum(x['realized'] for x in report['rows'])
    return report


@app.get('/api/tax')
def tax(
    start: date = Query(default_factory=lambda: date(date.today().year, 1, 1)),
    end: date = Query(default_factory=date.today),
    mode: str = 'base',
    user: str = Depends(session_user),
    db: DBSession = Depends(db_dep),
):
    if start > end:
        raise HTTPException(400, 'Start must precede end')
    p_meta, ledger = load_portfolio_state(user, db)
    c_meta, cash_data = load_cash_state(user, db)
    try:
        return _combined_tax_report(p_meta, c_meta, mode, start, end, ledger=ledger, cash_data=cash_data)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get('/api/export/tax')
def tax_export(start: date, end: date, mode: str = 'base', user: str = Depends(session_user), db: DBSession = Depends(db_dep)):
    p_meta, ledger = load_portfolio_state(user, db)
    c_meta, cash_data = load_cash_state(user, db)
    report = _combined_tax_report(p_meta, c_meta, mode, start, end, ledger=ledger, cash_data=cash_data)
    output = StringIO()
    fieldnames = ['asset', 'quantity_sold', 'buy_value', 'sell_value', 'realized', 'currency']
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for row in report['rows']:
        out_row = {k: row.get(k) for k in fieldnames}
        if mode == 'base':
            out_row['currency'] = p_meta.base_currency
        writer.writerow(out_row)
    return StreamingResponse(
        iter([output.getvalue().encode('utf-8-sig')]),
        media_type='text/csv',
        headers={'Content-Disposition': f'attachment; filename="realized_pl_{start}_{end}_{mode}.csv"'},
    )


@app.get('/api/export/trades')
def export(user: str = Depends(session_user), db: DBSession = Depends(db_dep)):
    p = get_owned(user, db)
    b = engine.to_csv(data_for(p), include_ids=False).encode('utf-8-sig')
    return StreamingResponse(
        iter([b]),
        media_type='text/csv',
        headers={'Content-Disposition': 'attachment; filename="portfolio_trades.csv"'},
    )


@app.post('/api/import')
async def do_import(request: Request, file: UploadFile = File(...), user: str = Depends(session_user), db: DBSession = Depends(db_dep)):
    _check_origin(request)
    raw = await file.read(6_000_001)
    if len(raw) > 6_000_000:
        raise HTTPException(413, 'CSV limit is 6 MB')
    try:
        d = engine.read_trades(raw.decode('utf-8-sig'))
    except (ValueError, UnicodeDecodeError) as e:
        raise HTTPException(422, str(e))
    p = get_owned(user, db)
    save_data(db, p, d)
    return {'ok': True, 'count': len(d)}


class SettingsInput(BaseModel):
    base_currency: str


@app.put('/api/settings')
def settings(item: SettingsInput, request: Request, user: str = Depends(session_user), db: DBSession = Depends(db_dep)):
    _check_origin(request)
    new_base = item.base_currency.upper().strip()
    if not re.fullmatch('[A-Z]{3}', new_base):
        raise HTTPException(400, 'Enter a three-letter currency code')
    p = get_owned(user, db)
    c = get_owned_cash(user, db)
    if p.base_currency == new_base:
        return {'ok': True, 'base_currency': new_base}
    d = data_for(p)
    cdf = cash_data_for(c)
    if not d.empty:
        rates, warnings = market.trade_fx_rates(d, new_base)
        if warnings or not np.isfinite(rates).all():
            raise HTTPException(503, 'Cannot switch base currency: ' + ('; '.join(warnings) or 'FX missing'))
        d['fx_to_base'] = rates
        p.csv_text = engine.to_csv(d)
    if not cdf.empty:
        c_rates, c_warnings = market.cash_fx_rates(cdf, new_base)
        if c_warnings or not np.isfinite(c_rates).all():
            raise HTTPException(503, 'Cannot switch base currency for cash: ' + ('; '.join(c_warnings) or 'FX missing'))
        cdf['fx_rate'] = c_rates
        c.csv_text = cash_engine.to_cash_csv(cdf)
        touch(c)
    p.base_currency = new_base
    touch(p)
    db.commit()
    if not d.empty:
        _store_ledger(p.user_key, p.csv_text, Ledger(d), version=p.version, updated_at=p.updated_at)
    if not cdf.empty:
        _store_cash(c.user_key, c.csv_text, cdf, version=c.version, updated_at=c.updated_at)
    return {'ok': True, 'base_currency': new_base}


@app.post('/api/refresh')
def refresh(
    request: Request,
    user: str = Depends(session_user),
    db: DBSession = Depends(db_dep),
    include_dashboard: bool = False,
    include_history: bool = False,
    span: str = Query('1M'),
    mode: str = Query('investments'),
    known_history_revision: str = Query(''),
):
    _check_origin(request)
    if mode not in ('investments', 'cash', 'nav'):
        mode = 'investments'
    p_meta, ledger = load_portfolio_state(user, db)
    c_meta, cash_data = load_cash_state(user, db)
    inv_state = ledger.get_summary_state()
    tickers = inv_state.open_tickers
    ccys = _open_currencies(inv_state, cash_data.get_summary_state()) if include_dashboard else []
    fx_future = (
        market._coord_pool.submit(market.spot_rates, ccys, p_meta.base_currency, True)
        if (include_dashboard and ccys) else None
    )
    quotes, errors = market.get_latest_prices(list(tickers)) if tickers else ({}, {})
    reply = {
        'updated': len(quotes),
        'requested': len(tickers),
        'errors': errors,
        'quotes': {t: {'source': q['source'], 'as_of': q['as_of']} for t, q in quotes.items()},
    }
    active_ledger = ledger
    active_meta = p_meta
    if quotes:
        trades = ledger.trades
        fresh = trades.ticker.map({t: q['price'] for t, q in quotes.items()})
        changed = fresh.notna() & fresh.ne(trades.last_price_trade_ccy)
        if changed.any():
            trades = trades.copy()
            trades['last_price_trade_ccy'] = trades.last_price_trade_ccy.astype(float).where(~changed, fresh)
            new_csv = engine.to_csv(trades)
            now_utc = datetime.now(timezone.utc)
            saved = db.execute(
                update(Portfolio)
                .where(Portfolio.user_key == user, Portfolio.version == p_meta.version)
                .values(csv_text=new_csv, version=Portfolio.version + 1, updated_at=now_utc)
                .execution_options(synchronize_session=False)
            )
            db.commit()
            if saved.rowcount != 1:
                raise HTTPException(409, 'Portfolio changed during refresh. Refresh again for current prices.')
            new_version = p_meta.version + 1
            active_meta = PortfolioMeta(user, p_meta.base_currency, new_version, now_utc)
            inv_hash, has_nse = ledger.get_inv_hash()
            updated_state = engine.refresh_summary_state_prices(inv_state, trades)
            active_ledger = Ledger(
                trades=trades,
                tickers=tickers,
                summary_state=updated_state,
                inv_hash_bytes=inv_hash,
                has_nse=has_nse,
            )
            _store_ledger(user, new_csv, active_ledger, version=new_version, updated_at=now_utc)
    preloaded_fx = fx_future.result() if fx_future is not None else None
    if include_dashboard:
        dash = dashboard_payload(active_meta, active_ledger, c_meta, cash_data, preloaded_fx=preloaded_fx)
        reply['dashboard'] = dash
        if include_history:
            rev = dash['history_revisions'].get(mode, dash['history_revision'])
            if known_history_revision and known_history_revision == rev:
                reply['history_unchanged'] = True
            else:
                cached_hist = market.peek_cached_history(span, revision=rev, owner=user, mode=mode)
                if cached_hist is not None:
                    reply['history'] = {**cached_hist, 'history_revision': rev, 'mode': mode}
    return reply
