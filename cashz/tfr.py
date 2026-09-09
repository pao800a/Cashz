"""TFR (Trattamento di Fine Rapporto) estimator.

Based on Art. 2120 c.c.
- Annual accrual: retribuzione_utile / 13.5 - 0.50% × imponibile_previdenziale
- Revaluation: stock_31dec(Y-1) × coeff(Y, m)
  where coeff(Y, m) = 1.5% × m/12 + 75% × max(0, FOI_m / FOI_dec(Y-1) − 1)

ISTAT rebased FOI from 2015=100 to 2025=100 starting January 2026.
Splice formula: for Y=2026, dec_base = FOI[2025-12] / RACCORDO

The 17% imposta sostitutiva is charged against the employee's fund.
Default: balance is NET (K=0.83). Toggle to gross (K=1.0) via the UI.

FOI table sources:
  - Monthly ISTAT releases: https://www.istat.it/notizia/indice-dei-prezzi-per-le-rivalutazioni-monetarie/
  - Published coefficients: https://www.codiceazienda.it/servizi/coefficiente-di-rivalutazione-del-tfr/
  - TO UPDATE: append new monthly FOI entries as ISTAT publishes them (~16th of following month).
    Format: (year, month): value  — base 2015=100 through 2025-12, base 2025=100 from 2026-01.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Optional

# ── ISTAT FOI index ────────────────────────────────────────────────────────────
# Base 2015 = 100 through Dec 2025; base 2025 = 100 from Jan 2026.
# Raccordo coefficient for the series break: 1.214
# (multiply 2025-base values by 1.214 to get the 2015-base-equivalent)

RACCORDO: Decimal = Decimal("1.214")

# To add a new month: append (year, month): Decimal("...")
# Values for 2025-base (2026+) are the raw ISTAT published value.
FOI: dict[tuple[int, int], Decimal] = {
    # 2015=100 base
    (2022, 12): Decimal("118.2"),
    (2023, 1): Decimal("118.3"),
    (2023, 2): Decimal("118.5"),
    (2023, 3): Decimal("118.0"),
    (2023, 4): Decimal("118.4"),
    (2023, 5): Decimal("118.6"),
    (2023, 6): Decimal("118.6"),
    (2023, 7): Decimal("118.7"),
    (2023, 8): Decimal("119.1"),
    (2023, 9): Decimal("119.3"),
    (2023, 10): Decimal("119.2"),
    (2023, 11): Decimal("118.7"),
    (2023, 12): Decimal("118.9"),
    (2024, 1): Decimal("119.3"),
    (2024, 2): Decimal("119.3"),
    (2024, 3): Decimal("119.4"),
    (2024, 4): Decimal("119.3"),
    (2024, 5): Decimal("119.5"),
    (2024, 6): Decimal("119.5"),
    (2024, 7): Decimal("120.0"),
    (2024, 8): Decimal("120.1"),
    (2024, 9): Decimal("120.0"),
    (2024, 10): Decimal("120.1"),
    (2024, 11): Decimal("120.1"),
    (2024, 12): Decimal("120.2"),
    (2025, 1): Decimal("120.9"),
    (2025, 2): Decimal("121.1"),
    (2025, 3): Decimal("121.4"),
    (2025, 4): Decimal("121.3"),
    (2025, 5): Decimal("121.2"),
    (2025, 6): Decimal("121.3"),
    (2025, 7): Decimal("121.8"),
    (2025, 8): Decimal("121.8"),
    (2025, 9): Decimal("121.7"),
    (2025, 10): Decimal("121.4"),
    (2025, 11): Decimal("121.3"),
    (2025, 12): Decimal("121.5"),
    # 2025=100 base (from January 2026)
    # To convert to compare with 2015-base Dec 2025: multiply by 1.214
    (2026, 1): Decimal("100.4"),
    (2026, 2): Decimal("100.9"),
    (2026, 3): Decimal("101.5"),
    (2026, 4): Decimal("102.5"),
    (2026, 5): Decimal("102.8"),
    (2026, 6): Decimal("102.8"),
    (2026, 7): Decimal("103.1"),
    # ADD NEW MONTHS HERE as ISTAT publishes (source URL above)
}

# First year of the 2025-base series (Jan 2026 onwards)
_FIRST_NEW_BASE_YEAR = 2026


def _dec_base(year: int) -> Decimal:
    """FOI for December of the previous year, expressed in the same base as FOI[(year, m)]."""
    dec_val = FOI[(year - 1, 12)]
    if year >= _FIRST_NEW_BASE_YEAR:
        # 2025-base is active from Jan 2026; we need to compare with Dec 2025 in 2025-base.
        # FOI[2025,12] = 121.5 is in 2015-base; divide by raccordo to get 2025-base equivalent.
        return dec_val / RACCORDO
    return dec_val


def revaluation_coeff(year: int, month: int) -> Decimal:
    """Monthly TFR revaluation coefficient (as a fraction, e.g. 0.02786543 for Jun 2026).

    coeff = 1.5% × m/12 + 75% × max(0, FOI_m / FOI_dec(Y-1) − 1)
    The ISTAT component is floored at zero (may be negative if FOI dips).
    """
    if (year, month) not in FOI:
        raise KeyError(
            f"FOI index for ({year}, {month}) not in table — update cashz/tfr.py"
        )
    foi_m = FOI[(year, month)]
    dec_b = _dec_base(year)

    inflation = max(Decimal("0"), foi_m / dec_b - Decimal("1"))
    time_component = Decimal("0.015") * Decimal(month) / Decimal("12")
    return time_component + Decimal("0.75") * inflation


def monthly_quota(
    retribuzione_utile: Decimal,
    imponibile_previdenziale: Optional[Decimal] = None,
) -> Decimal:
    """Net monthly TFR quota.

    quota = RU / 13.5  −  0.50% × RIP
    If imponibile_previdenziale is None, it defaults to retribuzione_utile.
    """
    rip = imponibile_previdenziale if imponibile_previdenziale is not None else retribuzione_utile
    return retribuzione_utile / Decimal("13.5") - Decimal("0.005") * rip


def estimate_balance_history(
    salary_months: list[tuple[int, int, Decimal, Optional[Decimal]]],
    net_of_tax: bool = True,
) -> list[tuple[datetime.date, Decimal]]:
    """Compute month-end TFR balances from a salary history.

    Args:
        salary_months: list of (year, month, retribuzione_utile, imponibile_previdenziale | None)
                       sorted by date.
        net_of_tax: if True, multiply revaluation by 0.83 (net of 17% imposta sostitutiva).

    Returns:
        list of (month_end_date, balance_eur) in chronological order.

    Assumptions:
    - Accrual starts from the first (year, month) in salary_months.
    - stock_31dec(prior_year) for the first year is 0.
    - Only the prior year's closing stock is revalued, never the current-year accrual.
    - Fractions of a month are not modelled (each entry is a full calendar month).
    """
    K = Decimal("0.83") if net_of_tax else Decimal("1")

    # Build a dict keyed by (year, month) for quick lookup
    salary: dict[tuple[int, int], tuple[Decimal, Optional[Decimal]]] = {}
    for yr, mo, ru, rip in salary_months:
        salary[(yr, mo)] = (ru, rip)

    if not salary:
        return []

    # Track the 31-December closing stock for the year-end revaluation
    # We walk month by month from the earliest to the latest.
    all_periods = sorted(salary)
    start_year = all_periods[0][0]
    end_year = all_periods[-1][0]
    end_month = all_periods[-1][1]

    stock_31dec: dict[int, Decimal] = {}  # keyed by year
    # Before the accrual start, stock is 0
    stock_31dec[start_year - 1] = Decimal("0")

    result: list[tuple[datetime.date, Decimal]] = []

    for year in range(start_year, end_year + 1):
        # Opening stock for this year
        opening = stock_31dec.get(year - 1, Decimal("0"))

        running = Decimal("0")  # sum of quotas accrued in this year so far
        max_month = 12 if year < end_year else end_month

        for month in range(1, max_month + 1):
            try:
                coeff = revaluation_coeff(year, month)
            except KeyError:
                # FOI not yet published for this month — stop here
                break

            # Revalued prior-year stock at this month
            revalued_opening = opening * (1 + K * coeff)

            # Add this month's quota if salary data exists
            if (year, month) in salary:
                ru, rip = salary[(year, month)]
                running += monthly_quota(ru, rip)

            balance = revalued_opening + running

            # Emit a result row only for months where salary data was provided —
            # intervening months (no salary entry) are needed for the revaluation
            # accumulation but need not appear in the output.
            if (year, month) in salary:
                # Last day of the month
                if month == 12:
                    last_day = datetime.date(year, 12, 31)
                else:
                    last_day = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)
                result.append((last_day, balance))

        # Year-end: revalue the full stock at coeff(year, 12)
        try:
            coeff_dec = revaluation_coeff(year, 12)
            # Sum all quotas for the full year (only those with salary data)
            year_quotas = sum(
                monthly_quota(ru, rip)
                for (yr, mo), (ru, rip) in salary.items()
                if yr == year
            )
            stock_31dec[year] = opening * (1 + K * coeff_dec) + year_quotas
        except KeyError:
            # FOI for Dec not published yet; carry forward
            stock_31dec[year] = opening + sum(
                monthly_quota(ru, rip)
                for (yr, mo), (ru, rip) in salary.items()
                if yr == year
            )

    return result


def latest_foi_month() -> tuple[int, int]:
    """Return the most recent (year, month) in the FOI table."""
    return max(FOI)
