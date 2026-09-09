"""Tests for networth aggregation."""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from cashz.storage.db import get_session
from cashz.storage.repo import upsert_snapshot, account_by_key
from cashz import networth as nw


class TestTotalNetWorth:
    def test_empty_returns_zero(self, tmp_db):
        with get_session() as session:
            assert nw.total_net_worth(session) == Decimal("0")

    def test_sums_all_accounts(self, tmp_db):
        with get_session() as session:
            bunq_chk = account_by_key(session, "bunq_checking")
            fineco = account_by_key(session, "fineco_checking")
            today = datetime.date.today()
            upsert_snapshot(session, bunq_chk.id, today, Decimal("1000"), "EUR",
                            Decimal("1"), today, Decimal("1000"), "manual")
            upsert_snapshot(session, fineco.id, today, Decimal("500"), "EUR",
                            Decimal("1"), today, Decimal("500"), "manual")
            total = nw.total_net_worth(session, today)
        assert total == Decimal("1500")

    def test_uses_latest_snapshot_for_date(self, tmp_db):
        with get_session() as session:
            acc = account_by_key(session, "bunq_checking")
            jan1 = datetime.date(2024, 1, 1)
            feb1 = datetime.date(2024, 2, 1)
            upsert_snapshot(session, acc.id, jan1, Decimal("100"), "EUR",
                            Decimal("1"), jan1, Decimal("100"), "manual")
            upsert_snapshot(session, acc.id, feb1, Decimal("200"), "EUR",
                            Decimal("1"), feb1, Decimal("200"), "manual")

            # As-of Jan 15: should use Jan 1 snapshot
            total_mid_jan = nw.total_net_worth(session, datetime.date(2024, 1, 15))
            assert total_mid_jan == Decimal("100")

            # As-of Feb: should use Feb 1 snapshot
            total_feb = nw.total_net_worth(session, datetime.date(2024, 2, 1))
            assert total_feb == Decimal("200")


class TestByCategory:
    def test_category_rollup(self, tmp_db):
        with get_session() as session:
            bunq_chk = account_by_key(session, "bunq_checking")    # liquidity
            ibkr = account_by_key(session, "ibkr_portfolio")        # investments
            amundi = account_by_key(session, "amundi_pension")      # investments (moved from pension)
            today = datetime.date.today()

            for acc, val in [(bunq_chk, "1000"), (ibkr, "5000"), (amundi, "2000")]:
                d = Decimal(val)
                upsert_snapshot(session, acc.id, today, d, "EUR", Decimal("1"), today, d, "manual")

            result = nw.by_category(session, today)

        assert result["liquidity"] == Decimal("1000")
        assert result["investments"] == Decimal("7000")  # ibkr 5000 + amundi 2000
        assert result["pension"] == Decimal("0")

    def test_multiple_accounts_same_category(self, tmp_db):
        """bunq_checking and bunq_savings are both liquidity."""
        with get_session() as session:
            checking = account_by_key(session, "bunq_checking")
            savings = account_by_key(session, "bunq_savings")
            today = datetime.date.today()
            upsert_snapshot(session, checking.id, today, Decimal("1000"), "EUR",
                            Decimal("1"), today, Decimal("1000"), "manual")
            upsert_snapshot(session, savings.id, today, Decimal("3000"), "EUR",
                            Decimal("1"), today, Decimal("3000"), "manual")

            result = nw.by_category(session, today)
        assert result["liquidity"] == Decimal("4000")


class TestUpsertPriority:
    def test_manual_not_overwritten_by_api(self, tmp_db):
        with get_session() as session:
            acc = account_by_key(session, "bunq_checking")
            today = datetime.date.today()

            snap_manual = upsert_snapshot(
                session, acc.id, today, Decimal("1234"), "EUR",
                Decimal("1"), today, Decimal("1234"), "manual"
            )
            # Now try to overwrite with 'api' source
            snap_api = upsert_snapshot(
                session, acc.id, today, Decimal("9999"), "EUR",
                Decimal("1"), today, Decimal("9999"), "api"
            )
            # Should still be 1234
            from sqlalchemy import select
            from cashz.models import Snapshot
            stored = session.scalar(
                select(Snapshot).where(
                    Snapshot.account_id == acc.id, Snapshot.as_of == today
                )
            )
        assert stored.balance_eur == Decimal("1234")
        assert stored.source == "manual"

    def test_api_overwrites_estimate(self, tmp_db):
        with get_session() as session:
            acc = account_by_key(session, "inps_tfr")
            today = datetime.date.today()

            upsert_snapshot(session, acc.id, today, Decimal("500"), "EUR",
                            Decimal("1"), today, Decimal("500"), "estimate")
            upsert_snapshot(session, acc.id, today, Decimal("600"), "EUR",
                            Decimal("1"), today, Decimal("600"), "api")

            from sqlalchemy import select
            from cashz.models import Snapshot
            stored = session.scalar(
                select(Snapshot).where(
                    Snapshot.account_id == acc.id, Snapshot.as_of == today
                )
            )
        assert stored.balance_eur == Decimal("600")
        assert stored.source == "api"
