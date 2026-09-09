"""Database engine, session factory, schema creation and seed data."""

from __future__ import annotations

from decimal import Decimal
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session

from cashz import config
from cashz.models import Base, Account

_engine = None
_SessionLocal = None


def _get_engine():
    global _engine
    if _engine is None:
        db_url = f"sqlite:///{config.db_path()}"
        _engine = create_engine(db_url, echo=False, future=True)

        # Enable WAL mode for better concurrency
        @event.listens_for(_engine, "connect")
        def set_sqlite_pragma(dbapi_conn, _connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return _engine


def get_session() -> Session:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=_get_engine(), autoflush=False, autocommit=False)
    return _SessionLocal()


def init_db() -> None:
    """Create all tables (if not exist) and seed the 7 accounts."""
    engine = _get_engine()
    Base.metadata.create_all(engine)
    with get_session() as session:
        _seed_accounts(session)
        session.commit()


# ── Seed data ─────────────────────────────────────────────────────────────────
_ACCOUNTS = [
    dict(
        key="bunq_checking",
        name="bunq Checking",
        institution="bunq",
        category="liquidity",
        currency="EUR",
        connector="bunq",
        sort_order=0,
    ),
    dict(
        key="bunq_savings",
        name="bunq Savings",
        institution="bunq",
        category="liquidity",
        currency="EUR",
        connector="bunq",
        sort_order=1,
    ),
    dict(
        key="fineco_checking",
        name="Fineco Checking",
        institution="Fineco",
        category="liquidity",
        currency="EUR",
        connector="manual",
        sort_order=2,
    ),
    dict(
        key="ibkr_portfolio",
        name="IBKR Portfolio",
        institution="Interactive Brokers",
        category="investments",
        currency="EUR",  # updated to base currency at first sync
        connector="ibkr",
        sort_order=3,
    ),
    dict(
        key="amundi_pension",
        name="Amundi Pension Fund",
        institution="Amundi",
        category="pension",
        currency="EUR",
        connector="manual",
        sort_order=4,
    ),
    dict(
        key="cometa_pension",
        name="Cometa Pension Fund",
        institution="Cometa",
        category="pension",
        currency="EUR",
        connector="manual",
        sort_order=5,
    ),
    dict(
        key="inps_tfr",
        name="INPS / TFR",
        institution="INPS",
        category="pension",
        currency="EUR",
        connector="tfr_estimate",
        sort_order=6,
    ),
]


def _seed_accounts(session: Session) -> None:
    existing_keys = {row.key for row in session.query(Account.key).all()}
    for spec in _ACCOUNTS:
        if spec["key"] not in existing_keys:
            session.add(Account(**spec))
