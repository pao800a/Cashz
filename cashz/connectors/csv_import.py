"""Generic CSV importer for manual balance snapshots.

Expected CSV columns (auto-detected, case-insensitive):
  - date / as_of / Date
  - balance / amount / value / Balance
  - currency (optional, defaults to account currency)
  - note / notes (optional)

Rows with unparseable dates or amounts are skipped with a warning.
"""

from __future__ import annotations

import csv
import datetime
import io
import logging
from decimal import Decimal, InvalidOperation
from typing import Optional

log = logging.getLogger(__name__)


class CsvRow:
    def __init__(
        self,
        as_of: datetime.date,
        balance: Decimal,
        currency: Optional[str],
        note: Optional[str],
        raw_line: int,
    ):
        self.as_of = as_of
        self.balance = balance
        self.currency = currency
        self.note = note
        self.raw_line = raw_line


class CsvImportError(Exception):
    pass


def _find_col(headers: list[str], candidates: list[str]) -> Optional[str]:
    lower_map = {h.lower().strip(): h for h in headers}
    for c in candidates:
        if c.lower() in lower_map:
            return lower_map[c.lower()]
    return None


def _parse_date(val: str) -> Optional[datetime.date]:
    """Try ISO, DD/MM/YYYY, MM/DD/YYYY, DD-MM-YYYY."""
    val = val.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.datetime.strptime(val, fmt).date()
        except ValueError:
            pass
    return None


def _parse_decimal(val: str) -> Optional[Decimal]:
    val = val.strip().replace(",", "").replace(" ", "")
    try:
        return Decimal(val)
    except InvalidOperation:
        return None


def parse_csv(
    content: str | bytes,
    default_currency: str = "EUR",
) -> tuple[list[CsvRow], list[str]]:
    """Parse CSV content; return (rows, warnings).

    Returns all parseable rows and a list of human-readable warning strings
    for rows that could not be parsed.
    """
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig")  # strip UTF-8 BOM (Notepad/Excel)

    reader = csv.DictReader(io.StringIO(content))
    if reader.fieldnames is None:
        raise CsvImportError("CSV file is empty or has no header row")

    headers = list(reader.fieldnames)
    date_col = _find_col(headers, ["date", "as_of", "Date", "As Of", "AsOf", "data"])
    balance_col = _find_col(headers, ["balance", "amount", "value", "Balance", "Amount", "importo"])
    currency_col = _find_col(headers, ["currency", "Currency", "ccy", "CCY", "valuta"])
    note_col = _find_col(headers, ["note", "notes", "Note", "Notes", "comment", "memo"])

    if date_col is None:
        raise CsvImportError(
            f"Cannot find a date column. Expected one of: date, as_of. "
            f"Found: {headers}"
        )
    if balance_col is None:
        raise CsvImportError(
            f"Cannot find a balance column. Expected one of: balance, amount, value. "
            f"Found: {headers}"
        )

    rows: list[CsvRow] = []
    warnings: list[str] = []

    for line_num, row in enumerate(reader, start=2):
        date_str = row.get(date_col, "").strip()
        bal_str = row.get(balance_col, "").strip()

        if not date_str and not bal_str:
            continue  # blank line

        d = _parse_date(date_str)
        if d is None:
            warnings.append(f"Line {line_num}: skipped — unrecognised date '{date_str}'")
            continue

        bal = _parse_decimal(bal_str)
        if bal is None:
            warnings.append(f"Line {line_num}: skipped — unrecognised balance '{bal_str}'")
            continue

        ccy: Optional[str] = None
        if currency_col:
            ccy_str = row.get(currency_col, "").strip()
            ccy = ccy_str if ccy_str else None

        note: Optional[str] = None
        if note_col:
            note = row.get(note_col, "").strip() or None

        rows.append(CsvRow(as_of=d, balance=bal, currency=ccy, note=note, raw_line=line_num))

    return rows, warnings
