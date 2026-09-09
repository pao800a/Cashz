"""SQLAlchemy 2.0 ORM models for Cashz."""

from __future__ import annotations

import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    Date,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ── Accounts ──────────────────────────────────────────────────────────────────
class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    institution: Mapped[str] = mapped_column(String(128), nullable=False)
    # liquidity | investments | pension
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="EUR")
    # manual | bunq | ibkr | tfr_estimate
    connector: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    # bunq monetary-account id, IBKR accountId, etc.
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    snapshots: Mapped[list[Snapshot]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    positions: Mapped[list[Position]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


# ── Snapshots ─────────────────────────────────────────────────────────────────
class Snapshot(Base):
    __tablename__ = "snapshots"
    __table_args__ = (UniqueConstraint("account_id", "as_of", name="uq_snapshot_account_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    as_of: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    balance_original: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    fx_rate: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    fx_rate_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    balance_eur: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    # api | manual | csv | estimate
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    account: Mapped[Account] = relationship(back_populates="snapshots")


# ── IBKR Positions ────────────────────────────────────────────────────────────
class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "as_of", "conid", "currency", name="uq_position_account_date_conid"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    as_of: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str | None] = mapped_column(String(256), nullable=True)
    isin: Mapped[str | None] = mapped_column(String(16), nullable=True)
    conid: Mapped[str | None] = mapped_column(String(32), nullable=True)
    asset_category: Mapped[str | None] = mapped_column(String(16), nullable=True)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    mark_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    position_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    fx_rate_to_base: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    value_eur: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)

    account: Mapped[Account] = relationship(back_populates="positions")


# ── FX Rates ──────────────────────────────────────────────────────────────────
class FxRate(Base):
    __tablename__ = "fx_rates"

    currency: Mapped[str] = mapped_column(String(8), primary_key=True)
    rate_date: Mapped[datetime.date] = mapped_column(Date, primary_key=True)
    # EUR-base: 1 EUR = rate × currency
    rate: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)


# ── TFR salary inputs ─────────────────────────────────────────────────────────
class SalaryMonth(Base):
    __tablename__ = "salary_months"

    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    month: Mapped[int] = mapped_column(Integer, primary_key=True)
    # retribuzione utile (art. 2120 c.c.) for this calendar month in EUR
    retribuzione_utile: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    # imponibile previdenziale (defaults to retribuzione_utile when NULL)
    imponibile_previdenziale: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2), nullable=True
    )


# ── Sync log ──────────────────────────────────────────────────────────────────
class SyncLog(Base):
    __tablename__ = "sync_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    connector: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    # ok | error | partial
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ok")
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
