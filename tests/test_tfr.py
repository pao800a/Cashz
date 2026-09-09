"""Tests for the TFR estimator — validates against officially published coefficients."""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from cashz.tfr import revaluation_coeff, monthly_quota, estimate_balance_history, FOI


class TestRevCoeff:
    """Verify against the nine published TFR coefficients."""

    PUBLISHED = [
        # (year, month, expected_percent)  — coefficient as percentage, 6 decimal places
        (2023, 3, "0.375000"),   # floored: FOI Mar < FOI Dec 2022
        (2023, 10, "1.884518"),
        (2023, 12, "1.944162"),  # formula: 0.015 + 0.75*(118.9/118.2-1) = 1.944162436...%
        (2024, 7, "1.568860"),
        (2024, 12, "2.320017"),
        (2026, 1, "0.363025"),
        (2026, 3, "1.437346"),
        (2026, 6, "2.786543"),
        (2026, 7, "3.136358"),
    ]

    @pytest.mark.parametrize("year,month,expected_pct", PUBLISHED)
    def test_matches_published(self, year, month, expected_pct):
        coeff = revaluation_coeff(year, month)
        computed_pct = coeff * Decimal("100")
        assert round(computed_pct, 6) == Decimal(expected_pct), (
            f"({year},{month}): got {computed_pct:.6f}%, expected {expected_pct}%"
        )

    def test_floor_at_zero_when_foi_dips(self):
        """March 2023: FOI 118.0 < Dec 2022 FOI 118.2 → ISTAT term = 0, result = 1.5%×3/12."""
        coeff = revaluation_coeff(2023, 3)
        # 1.5% × 3/12 = 0.375%
        assert coeff == Decimal("0.375") / Decimal("100")

    def test_result_is_decimal(self):
        c = revaluation_coeff(2024, 6)
        assert isinstance(c, Decimal)

    def test_missing_foi_raises_key_error(self):
        with pytest.raises(KeyError):
            revaluation_coeff(2099, 1)


class TestMonthlyQuota:
    def test_basic_quota(self):
        """RU = 3000 → quota = 3000/13.5 - 0.5%×3000 = 222.222... - 15 = 207.222..."""
        q = monthly_quota(Decimal("3000"))
        expected = Decimal("3000") / Decimal("13.5") - Decimal("0.005") * Decimal("3000")
        assert q == expected

    def test_separate_imponibile(self):
        """When RIP < RU, deduction is smaller."""
        q = monthly_quota(Decimal("3000"), imponibile_previdenziale=Decimal("2500"))
        expected = Decimal("3000") / Decimal("13.5") - Decimal("0.005") * Decimal("2500")
        assert q == expected

    def test_result_is_decimal(self):
        assert isinstance(monthly_quota(Decimal("2000")), Decimal)


class TestEstimateBalance:
    def _salary(self, months=3, ru=Decimal("3000")):
        """Simple monthly salary for testing."""
        return [
            (2023, 10 + i, ru, None)
            for i in range(months)
        ]

    def test_empty_salary(self):
        assert estimate_balance_history([]) == []

    def test_single_month_no_opening_stock(self):
        """One month: only quota, no revaluation (opening stock is 0)."""
        rows = estimate_balance_history([(2023, 10, Decimal("3000"), None)])
        assert len(rows) == 1
        date, bal = rows[0]
        assert date == datetime.date(2023, 10, 31)
        assert bal > Decimal("0")
        # Balance = quota = RU/13.5 - 0.5%×RU
        expected_quota = Decimal("3000") / Decimal("13.5") - Decimal("0.005") * Decimal("3000")
        assert bal == expected_quota

    def test_balance_increases_over_time(self):
        """Subsequent months add more quotas so the balance grows."""
        rows = estimate_balance_history(
            [(2024, m, Decimal("3000"), None) for m in range(1, 7)]
        )
        balances = [b for _, b in rows]
        for a, b in zip(balances, balances[1:]):
            assert b > a, "Balance should grow month to month"

    def test_gross_greater_than_net(self):
        """Gross (K=1.0) should be higher than net (K=0.83) after any year boundary."""
        salary = [(2023, m, Decimal("3000"), None) for m in range(10, 13)] + [
            (2024, m, Decimal("3000"), None) for m in range(1, 4)
        ]
        rows_net = estimate_balance_history(salary, net_of_tax=True)
        rows_gross = estimate_balance_history(salary, net_of_tax=False)
        # At March 2024 (after the Dec 2023 year-end revaluation), gross > net
        march_2024_net = next(b for d, b in rows_net if d == datetime.date(2024, 3, 31))
        march_2024_gross = next(b for d, b in rows_gross if d == datetime.date(2024, 3, 31))
        assert march_2024_gross > march_2024_net

    def test_2026_raccordo_splice(self):
        """Balance computation should not crash or produce negative values in 2026."""
        salary = [(2026, m, Decimal("3500"), None) for m in range(1, 8)]
        rows = estimate_balance_history(salary)
        for d, b in rows:
            assert b >= Decimal("0"), f"Negative balance at {d}"

    def test_decimal_not_float(self):
        rows = estimate_balance_history([(2024, 1, Decimal("2500"), None)])
        for _, b in rows:
            assert isinstance(b, Decimal)


class TestFOITable:
    def test_dec_2022_present(self):
        assert (2022, 12) in FOI

    def test_jan_2026_base_change(self):
        """FOI for 2026 should be in the new 2025=100 base (~100, not ~120)."""
        assert FOI[(2026, 1)] < Decimal("110")  # clearly not 2015=100 base
        assert FOI[(2026, 1)] > Decimal("90")

    def test_continuity(self):
        """No gaps from Dec 2022 through Jul 2026."""
        d = datetime.date(2022, 12, 1)
        end = datetime.date(2026, 7, 1)
        while d <= end:
            assert (d.year, d.month) in FOI, f"Missing FOI entry for ({d.year}, {d.month})"
            if d.month == 12:
                d = datetime.date(d.year + 1, 1, 1)
            else:
                d = datetime.date(d.year, d.month + 1, 1)
