"""Currency conversion using ECB reference rates.

ECB quotes are EUR-base: 1 EUR = rate × foreign.
To convert foreign → EUR:  eur = foreign_amount / rate
To convert EUR → foreign:  fgn = eur_amount * rate

Rates are published on TARGET business days only (~16:00 CET).
Weekends, TARGET holidays, and dates before the earliest stored rate return
the most recent prior available rate ("last observation carried back").
"""

from __future__ import annotations

import csv
import datetime
import io
import logging
import zipfile
from decimal import Decimal, InvalidOperation
from typing import Optional
from xml.etree import ElementTree

import requests

from cashz import config
from cashz.storage.db import get_session
from cashz.storage.repo import upsert_fx_rate, get_fx_rate, fx_rate_count

log = logging.getLogger(__name__)

_ECB_DAILY = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
_ECB_90D = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
_ECB_HIST_ZIP = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"


class FxRateStale(Exception):
    """Raised when the nearest available rate is older than FX_MAX_STALENESS_DAYS."""


class FxRateMissing(Exception):
    """Raised when no rate at all is found for a currency."""


# ── Parsing ECB XML ────────────────────────────────────────────────────────────
_ECB_NS = "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"


def _parse_ecb_xml(text: str) -> list[tuple[datetime.date, str, Decimal]]:
    """Parse ECB eurofxref XML → list of (date, currency, rate)."""
    root = ElementTree.fromstring(text)
    rows = []
    for cube_date in root.iter(f"{{{_ECB_NS}}}Cube"):
        time_attr = cube_date.get("time")
        if not time_attr:
            continue
        try:
            d = datetime.date.fromisoformat(time_attr)
        except ValueError:
            continue
        for cube_ccy in cube_date:
            ccy = cube_ccy.get("currency")
            rate_str = cube_ccy.get("rate")
            if ccy and rate_str:
                try:
                    rows.append((d, ccy, Decimal(rate_str)))
                except InvalidOperation:
                    pass
    return rows


def _parse_ecb_hist_csv(text: str) -> list[tuple[datetime.date, str, Decimal]]:
    """Parse ECB eurofxref-hist.csv (one row per date, one column per currency)."""
    reader = csv.DictReader(io.StringIO(text))
    # ECB CSV has trailing spaces in column names ("Date ", "USD ", …); normalise them.
    if reader.fieldnames:
        reader.fieldnames = [f.strip() for f in reader.fieldnames]
    rows = []
    for row in reader:
        date_str = row.get("Date", "").strip()
        if not date_str:
            continue
        try:
            d = datetime.date.fromisoformat(date_str)
        except ValueError:
            continue
        for ccy, val in row.items():
            ccy = ccy.strip()
            if not ccy or ccy == "Date":
                continue
            val = val.strip()
            if val in ("", "N/A"):
                continue
            try:
                rows.append((d, ccy, Decimal(val)))
            except InvalidOperation:
                pass
    return rows


def _store_rows(rows: list[tuple[datetime.date, str, Decimal]]) -> int:
    with get_session() as session:
        for d, ccy, rate in rows:
            upsert_fx_rate(session, ccy, d, rate)
        session.commit()
    return len(rows)


# ── Public API ────────────────────────────────────────────────────────────────
def refresh_recent() -> int:
    """Fetch daily + 90-day XML and upsert. Returns number of rows written."""
    total = 0
    for url, label in [(_ECB_DAILY, "daily"), (_ECB_90D, "90d")]:
        try:
            r = requests.get(
                url,
                headers=config.HTTP_HEADERS,
                verify=config.VERIFY_TLS,
                timeout=30,
            )
            r.raise_for_status()
            rows = _parse_ecb_xml(r.text)
            total += _store_rows(rows)
            log.info("FX refresh_recent %s: %d rows", label, len(rows))
        except Exception as exc:
            log.warning("FX refresh_recent %s failed: %s", label, exc)
    return total


def backfill_history() -> int:
    """Download the full ECB history zip and bulk-upsert. Idempotent."""
    try:
        r = requests.get(
            _ECB_HIST_ZIP,
            headers=config.HTTP_HEADERS,
            verify=config.VERIFY_TLS,
            timeout=120,
            stream=True,
        )
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            csv_names = [n for n in zf.namelist() if n.endswith(".csv")]
            if not csv_names:
                raise ValueError("No CSV in ECB history zip")
            text = zf.read(csv_names[0]).decode("utf-8")
        rows = _parse_ecb_hist_csv(text)
        n = _store_rows(rows)
        log.info("FX backfill_history: %d rows", n)
        return n
    except Exception as exc:
        log.error("FX backfill_history failed: %s", exc)
        raise


def to_eur(
    amount: Decimal,
    currency: str,
    on_date: datetime.date,
    session=None,
) -> tuple[Decimal, Decimal, Optional[datetime.date]]:
    """Convert *amount* in *currency* to EUR as of *on_date*.

    Returns (eur_amount, rate_used, rate_date).
    EUR input returns (amount, Decimal('1'), on_date).
    Raises FxRateMissing if no rate exists for the currency.
    Raises FxRateStale if the nearest rate is > FX_MAX_STALENESS_DAYS old.
    """
    if currency.upper() == "EUR":
        return amount, Decimal("1"), on_date

    own_session = session is None
    if own_session:
        session = get_session()

    try:
        result = get_fx_rate(session, currency.upper(), on_date)
    finally:
        if own_session:
            session.close()

    if result is None:
        raise FxRateMissing(f"No ECB rate found for {currency}")

    rate, rate_date = result
    staleness = (on_date - rate_date).days
    if staleness > config.FX_MAX_STALENESS_DAYS:
        raise FxRateStale(
            f"Nearest {currency} rate ({rate_date}) is {staleness} days older than {on_date}"
        )

    eur = amount / rate  # EUR-base convention: divide to get EUR
    return eur, rate, rate_date


def has_rates(session=None) -> bool:
    own = session is None
    if own:
        session = get_session()
    try:
        return fx_rate_count(session) > 0
    finally:
        if own:
            session.close()
