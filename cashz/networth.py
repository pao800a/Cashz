"""Net worth aggregation helpers."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from cashz.models import Account, Snapshot
from cashz.storage import repo


CATEGORIES = ["liquidity", "investments", "pension"]

CATEGORY_LABELS = {
    "liquidity": "💰 Liquidity",
    "investments": "📈 Investments",
    "pension": "🏦 Pension",
}


def total_net_worth(session: Session, as_of: Optional[datetime.date] = None) -> Decimal:
    """Sum of the latest snapshot for each active account on or before as_of."""
    if as_of is None:
        as_of = datetime.date.today()
    accounts = repo.all_accounts(session)
    total = Decimal("0")
    for acc in accounts:
        snap = _latest_snap_for(session, acc.id, as_of)
        if snap:
            total += snap.balance_eur
    return total


def _latest_snap_for(
    session: Session, account_id: int, as_of: datetime.date
) -> Optional[Snapshot]:
    from sqlalchemy import select
    return session.scalars(
        select(Snapshot)
        .where(Snapshot.account_id == account_id, Snapshot.as_of <= as_of)
        .order_by(Snapshot.as_of.desc())
        .limit(1)
    ).first()


def by_category(
    session: Session, as_of: Optional[datetime.date] = None
) -> dict[str, Decimal]:
    if as_of is None:
        as_of = datetime.date.today()
    accounts = repo.all_accounts(session)
    result: dict[str, Decimal] = {c: Decimal("0") for c in CATEGORIES}
    for acc in accounts:
        snap = _latest_snap_for(session, acc.id, as_of)
        if snap:
            result[acc.category] = result.get(acc.category, Decimal("0")) + snap.balance_eur
    return result


def by_account(
    session: Session, as_of: Optional[datetime.date] = None
) -> list[dict]:
    """Return list of dicts with account info and latest balance."""
    if as_of is None:
        as_of = datetime.date.today()
    accounts = repo.all_accounts(session)
    result = []
    for acc in accounts:
        snap = _latest_snap_for(session, acc.id, as_of)
        result.append(
            {
                "account": acc,
                "snapshot": snap,
                "balance_eur": snap.balance_eur if snap else Decimal("0"),
                "as_of": snap.as_of if snap else None,
                "source": snap.source if snap else None,
                "staleness_days": (
                    (as_of - snap.as_of).days if snap else None
                ),
            }
        )
    return result


def delta(
    session: Session,
    as_of: Optional[datetime.date] = None,
    days_back: int = 30,
) -> Optional[Decimal]:
    """Net worth change over the last *days_back* days."""
    if as_of is None:
        as_of = datetime.date.today()
    prior = as_of - datetime.timedelta(days=days_back)
    now_val = total_net_worth(session, as_of)
    prior_val = total_net_worth(session, prior)
    if prior_val == Decimal("0"):
        return None
    return now_val - prior_val


def ytd_delta(session: Session, as_of: Optional[datetime.date] = None) -> Optional[Decimal]:
    if as_of is None:
        as_of = datetime.date.today()
    jan1 = datetime.date(as_of.year, 1, 1)
    now_val = total_net_worth(session, as_of)
    start_val = total_net_worth(session, jan1)
    return now_val - start_val
