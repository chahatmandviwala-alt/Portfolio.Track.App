"""Database-backed user data; PostgreSQL on hosting, SQLite for local development.

Drop-in replacement. Uses a bounded, recycled SQLAlchemy connection pool to
reduce repeated Supabase TLS connection setup without exhausting free-tier slots.
There are NO schema changes or data migrations.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from sqlalchemy import create_engine, Column, String, Integer, Text, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker

DB_URL = os.environ.get('DATABASE_URL') or 'sqlite:///./portfolio_local.db'
if DB_URL.startswith('postgres://'):
    DB_URL = 'postgresql+psycopg://' + DB_URL[len('postgres://'):]
elif DB_URL.startswith('postgresql://'):
    DB_URL = 'postgresql+psycopg://' + DB_URL[len('postgresql://'):]

if DB_URL.startswith('sqlite:'):
    engine = create_engine(DB_URL, pool_pre_ping=True,
                           connect_args={'check_same_thread': False})
else:
    engine = create_engine(DB_URL, pool_pre_ping=True,
                           pool_size=3, max_overflow=2, pool_timeout=12,
                           pool_recycle=1500)
Session = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    key = Column(String(250), primary_key=True)
    password_salt = Column(String(120))
    password_hash = Column(String(120))
    name = Column(String(150), nullable=False, default='Investor')

class Portfolio(Base):
    __tablename__ = 'portfolios'
    user_key = Column(String(250), primary_key=True)
    csv_text = Column(Text, nullable=False, default='')
    base_currency = Column(String(8), nullable=False, default='SEK')
    version = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))

class MarketCache(Base):
    __tablename__ = 'market_cache'
    cache_key = Column(String(160), primary_key=True)
    csv_text = Column(Text, nullable=False, default='')
    start_date = Column(String(12), nullable=False, default='')
    end_date = Column(String(12), nullable=False, default='')
    fetched_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))


def init_db():
    Base.metadata.create_all(bind=engine)


def get_portfolio(session, user_key: str) -> Portfolio:
    p = session.get(Portfolio, user_key)
    if p is None:
        # Two first reads (dashboard and history) can arrive together. Persist the
        # same empty ledger once and release the write transaction immediately.
        if engine.dialect.name == 'postgresql':
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        session.execute(insert(Portfolio).values(user_key=user_key, csv_text='', base_currency='SEK', version=0)
                        .on_conflict_do_nothing(index_elements=[Portfolio.user_key]))
        session.commit()
        p = session.get(Portfolio, user_key)
    return p


def touch(p: Portfolio):
    p.version += 1
    p.updated_at = datetime.now(timezone.utc)
