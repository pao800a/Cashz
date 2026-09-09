"""Repository layer: all database reads and writes for Cashz."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select, delete
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from cashz.models import Account, FxRate, Position, Snapshot, SalaryMonth, SyncLog


# ── Accounts ──────────────────────────────────────────────────────────────────
def all_accounts(session: Session) -> list[Account]:
    return session.scalars(
        select(Account).where(Account.active == True).order_by(Account.sort_order)
    ).all()


def account_by_key(session: Session, key: str) -> Optional[Account]:
    return session.scalars(select(Account).where(Account.key == key)).first()


def account_by_id(session: Session, account_id: int) -> Optional[Account]:
    return session.get(Account, account_id)


def update_account_external_id(session: Session, account_id: int, external_id: str) -> None:
    acc = session.get(Account, account_id)
    if acc:
        acc.external_id = external_id
        session.commit()


# ── Snapshots ─────────────────────────────────────────────────────────────────
def upsert_snapshot(
    session: Session,
    account_id: int,
    as_of: datetime.date,
    balance_original: Decimal,
    currency: str,
    fx_rate: Decimal,
    fx_rate_date: Optional[datetime.date],
    balance_eur: Decimal,
    source: str,
    note: Optional[str] = None,
) -> Snapshot:
    """Insert or update the snapshot for (account_id, as_of).

    A 'manual' source snapshot will never be overwritten by 'api' or 'estimate'
    — the source priority order is: manual > api > csv > estimate.
    """
    existing = session.scalars(
        select(Snapshot).where(
            Snapshot.account_id == account_id, Snapshot.as_of == as_of
        )
    ).first()

    SOURCE_PRIORITY = {"manual": 4, "api": 3, "csv": 2, "estimate": 1}
    incoming_priority = SOURCE_PRIORITY.get(source, 0)

    if existing is not None:
        existing_priority = SOURCE_PRIORITY.get(existing.source, 0)
        if incoming_priority <= existing_priority:
            # Don't overwrite a higher-priority snapshot
            return existing
        existing.balance_original = balance_original
        existing.currency = currency
        existing.fx_rate = fx_rate
        existing.fx_rate_date = fx_rate_date
        existing.balance_eur = balance_eur
        existing.source = source
        existing.note = note
        session.commit()
        return existing
    else:
        snap = Snapshot(
            account_id=account_id,
            as_of=as_of,
            balance_original=balance_original,
            currency=currency,
            fx_rate=fx_rate,
            fx_rate_date=fx_rate_date,
            balance_eur=balance_eur,
            source=source,
            note=note,
        )
        session.add(snap)
        session.commit()
        return snap


def latest_snapshot(session: Session, account_id: int) -> Optional[Snapshot]:
    return session.scalars(
        select(Snapshot)
        .where(Snapshot.account_id == account_id)
        .order_by(Snapshot.as_of.desc())
        .limit(1)
    ).first()


def latest_per_account(session: Session) -> list[tuple[Account, Optional[Snapshot]]]:
    """Return (account, latest_snapshot_or_None) for all active accounts."""
    accounts = all_accounts(session)
    result = []
    for acc in accounts:
        snap = latest_snapshot(session, acc.id)
        result.append((acc, snap))
    return result


def snapshots_for_account(
    session: Session,
    account_id: int,
    since: Optional[datetime.date] = None,
) -> list[Snapshot]:
    q = select(Snapshot).where(Snapshot.account_id == account_id)
    if since:
        q = q.where(Snapshot.as_of >= since)
    q = q.order_by(Snapshot.as_of)
    return session.scalars(q).all()


def delete_snapshot(session: Session, snapshot_id: int) -> None:
    session.execute(delete(Snapshot).where(Snapshot.id == snapshot_id))
    session.commit()


def net_worth_trend(
    session: Session,
    since: Optional[datetime.date] = None,
) -> list[tuple[datetime.date, Decimal]]:
    """Daily total net worth in EUR, using the latest snapshot ≤ each date."""
    # Get all distinct snapshot dates across all accounts
    q = select(func.distinct(Snapshot.as_of)).order_by(Snapshot.as_of)
    if since:
        q = q.where(Snapshot.as_of >= since)
    dates = session.scalars(q).all()

    accounts = all_accounts(session)
    result = []
    for d in dates:
        total = Decimal("0")
        for acc in accounts:
            # Latest snapshot on or before this date
            snap = session.scalars(
                select(Snapshot)
                .where(Snapshot.account_id == acc.id, Snapshot.as_of <= d)
                .order_by(Snapshot.as_of.desc())
                .limit(1)
            ).first()
            if snap:
                total += snap.balance_eur
        result.append((d, total))
    return result


def net_worth_by_category(
    session: Session, as_of: Optional[datetime.date] = None
) -> dict[str, Decimal]:
    """Latest balance per account, grouped by category."""
    if as_of is None:
        as_of = datetime.date.today()
    accounts = all_accounts(session)
    totals: dict[str, Decimal] = {}
    for acc in accounts:
        snap = session.scalars(
            select(Snapshot)
            .where(Snapshot.account_id == acc.id, Snapshot.as_of <= as_of)
            .order_by(Snapshot.as_of.desc())
            .limit(1)
        ).first()
        if snap:
            totals[acc.category] = totals.get(acc.category, Decimal("0")) + snap.balance_eur
    return totals


# ── FX Rates ──────────────────────────────────────────────────────────────────
def upsert_fx_rate(session: Session, currency: str, rate_date: datetime.date, rate: Decimal) -> None:
    stmt = sqlite_insert(FxRate).values(currency=currency, rate_date=rate_date, rate=rate)
    stmt = stmt.on_conflict_do_update(
        index_elements=["currency", "rate_date"], set_={"rate": stmt.excluded.rate}
    )
    session.execute(stmt)


def get_fx_rate(
    session: Session, currency: str, as_of: datetime.date
) -> Optional[tuple[Decimal, datetime.date]]:
    """Return (rate, rate_date) — the latest rate on or before as_of. EUR returns 1."""
    if currency.upper() == "EUR":
        return Decimal("1"), as_of
    row = session.scalars(
        select(FxRate)
        .where(FxRate.currency == currency, FxRate.rate_date <= as_of)
        .order_by(FxRate.rate_date.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    return row.rate, row.rate_date


def fx_rate_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(FxRate)) or 0


# ── IBKR Positions ────────────────────────────────────────────────────────────
def upsert_position(session: Session, position: Position) -> None:
    stmt = sqlite_insert(Position).values(
        account_id=position.account_id,
        as_of=position.as_of,
        symbol=position.symbol,
        description=position.description,
        isin=position.isin,
        conid=position.conid,
        asset_category=position.asset_category,
        quantity=position.quantity,
        mark_price=position.mark_price,
        position_value=position.position_value,
        currency=position.currency,
        fx_rate_to_base=position.fx_rate_to_base,
        value_eur=position.value_eur,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["account_id", "as_of", "conid", "currency"],
        set_={
            "symbol": stmt.excluded.symbol,
            "description": stmt.excluded.description,
            "isin": stmt.excluded.isin,
            "asset_category": stmt.excluded.asset_category,
            "quantity": stmt.excluded.quantity,
            "mark_price": stmt.excluded.mark_price,
            "position_value": stmt.excluded.position_value,
            "fx_rate_to_base": stmt.excluded.fx_rate_to_base,
            "value_eur": stmt.excluded.value_eur,
        },
    )
    session.execute(stmt)


def positions_for_date(session: Session, account_id: int, as_of: datetime.date) -> list[Position]:
    return session.scalars(
        select(Position)
        .where(Position.account_id == account_id, Position.as_of == as_of)
        .order_by(Position.value_eur.desc())
    ).all()


def latest_positions(session: Session, account_id: int) -> list[Position]:
    sub = (
        select(func.max(Position.as_of))
        .where(Position.account_id == account_id)
        .scalar_subquery()
    )
    return session.scalars(
        select(Position)
        .where(Position.account_id == account_id, Position.as_of == sub)
        .order_by(Position.value_eur.desc())
    ).all()


# ── Salary months ─────────────────────────────────────────────────────────────
def get_salary_months(session: Session) -> list[SalaryMonth]:
    return session.scalars(
        select(SalaryMonth).order_by(SalaryMonth.year, SalaryMonth.month)
    ).all()


def upsert_salary_month(
    session: Session,
    year: int,
    month: int,
    retribuzione_utile: Decimal,
    imponibile_previdenziale: Optional[Decimal] = None,
) -> None:
    existing = session.get(SalaryMonth, (year, month))
    if existing:
        existing.retribuzione_utile = retribuzione_utile
        existing.imponibile_previdenziale = imponibile_previdenziale
    else:
        session.add(
            SalaryMonth(
                year=year,
                month=month,
                retribuzione_utile=retribuzione_utile,
                imponibile_previdenziale=imponibile_previdenziale,
            )
        )
    session.commit()


def delete_salary_months(session: Session) -> None:
    session.execute(delete(SalaryMonth))
    session.commit()


# ── Sync log ──────────────────────────────────────────────────────────────────
def log_sync_start(session: Session, connector: str) -> int:
    """Insert a running sync_log entry and return its integer PK."""
    entry = SyncLog(
        connector=connector,
        started_at=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None),
        status="running",
    )
    session.add(entry)
    session.flush()   # assigns entry.id without full commit
    entry_id = entry.id
    session.commit()
    return entry_id


def log_sync_finish(
    session: Session, entry_id: int, status: str, message: Optional[str] = None
) -> None:
    log = session.get(SyncLog, entry_id)
    if log:
        log.finished_at = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        log.status = status
        log.message = message
    session.commit()


def recent_sync_log(session: Session, limit: int = 20) -> list[SyncLog]:
    return session.scalars(
        select(SyncLog).order_by(SyncLog.started_at.desc()).limit(limit)
    ).all()
