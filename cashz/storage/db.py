"""Database engine, session factory, schema creation and seed data."""

from __future__ import annotations

from decimal import Decimal
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session

from cashz import config
from cashz.models import Base, Account

_engine = None
_SessionLocal = None
_initialized = False


def _get_engine():
    global _engine
    if _engine is None:
        db_url = f"sqlite:///{config.db_path()}"
        # timeout=30: give SQLite 30 s to wait for write-lock acquisition
        _engine = create_engine(
            db_url, echo=False, future=True, connect_args={"timeout": 30}
        )

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
    """Create all tables (if not exist) and seed the 7 accounts.

    Guarded by _initialized so it only runs once per process — Streamlit
    re-runs app.py on every interaction, and calling create_all() repeatedly
    acquires brief DDL write-locks that can race with sync operations.
    """
    global _initialized
    if _initialized:
        return
    engine = _get_engine()
    Base.metadata.create_all(engine)
    with get_session() as session:
        _seed_accounts(session)
        session.commit()
    _initialized = True


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
        key="bunq_archived",
        name="bunq Archived",
        institution="bunq",
        category="liquidity",
        currency="EUR",
        connector="bunq",
        sort_order=2,
    ),
    dict(
        key="fineco_checking",
        name="Fineco Checking",
        institution="Fineco",
        category="liquidity",
        currency="EUR",
        connector="manual",
        sort_order=3,
    ),
    dict(
        key="ibkr_portfolio",
        name="IBKR Portfolio",
        institution="Interactive Brokers",
        category="investments",
        currency="EUR",  # updated to base currency at first sync
        connector="ibkr",
        sort_order=4,
    ),
    dict(
        key="amundi_pension",
        name="Amundi Pension Fund",
        institution="Amundi",
        category="investments",
        currency="EUR",
        connector="manual",
        sort_order=5,
    ),
    dict(
        key="cometa_pension",
        name="Cometa Pension Fund",
        institution="Cometa",
        category="pension",
        currency="EUR",
        connector="manual",
        sort_order=6,
    ),
    dict(
        key="inps_tfr",
        name="INPS / TFR",
        institution="INPS",
        category="pension",
        currency="EUR",
        connector="tfr_estimate",
        sort_order=7,
    ),
]


def _seed_accounts(session: Session) -> None:
    from sqlalchemy import select as _select
    existing_keys = {row.key for row in session.query(Account.key).all()}

    # One-time migration: shift sort_order ≥ 2 up by 1 to make room for bunq_archived
    if "bunq_archived" not in existing_keys:
        for acc in session.scalars(_select(Account).where(Account.sort_order >= 2)).all():
            acc.sort_order += 1

    # One-time migration: move Amundi from pension to investments
    amundi = session.scalars(_select(Account).where(Account.key == "amundi_pension")).first()
    if amundi and amundi.category == "pension":
        amundi.category = "investments"

    for spec in _ACCOUNTS:
        if spec["key"] not in existing_keys:
            session.add(Account(**spec))
