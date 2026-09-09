"""Base types for Cashz connectors."""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, Protocol


@dataclass
class AccountBalance:
    """A single account's balance as returned by a connector."""
    external_id: str          # bunq monetary-account id, IBKR accountId, etc.
    name: str                 # human-readable account name from the institution
    balance: Decimal
    currency: str
    subtype: str              # e.g. 'MonetaryAccountBank', 'MonetaryAccountSavings'


@dataclass
class PositionDetail:
    """A single IBKR open position."""
    symbol: str
    description: str
    isin: Optional[str]
    conid: Optional[str]
    asset_category: Optional[str]
    quantity: Optional[Decimal]
    mark_price: Optional[Decimal]
    position_value: Optional[Decimal]   # in position's own currency
    currency: str
    fx_rate_to_base: Optional[Decimal]  # base_currency per 1 foreign unit
    value_eur: Optional[Decimal]        # derived: position_value / fx_rate (or × if inverted)


@dataclass
class FetchResult:
    """The outcome of a sync operation."""
    connector: str
    success: bool
    balances: list[AccountBalance] = field(default_factory=list)
    positions: list[PositionDetail] = field(default_factory=list)
    nav: Optional[Decimal] = None
    base_currency: Optional[str] = None
    report_date: Optional[datetime.date] = None
    error: Optional[str] = None
    message: Optional[str] = None
