"""IBKR Flex Web Service connector.

Transport: own requests-based implementation against the verified ndcdyn endpoints.
Parsing: ibflex.parser (zero-dependency, Decimal-typed, lenient on unknown attributes).

Two-step flow:
  1. GET .../SendRequest?t={token}&q={queryId}&v=3
     → FlexStatementResponse: Status=Success, ReferenceCode=...
  2. GET .../GetStatement?t={token}&q={ReferenceCode}&v=3
     → FlexQueryResponse (the actual XML statement)

Readiness check: substring test on raw bytes, not full parse.
Retry table: 1009/1019 → 5s; 1018 → 10s; anything else → raise immediately.

Critical data traps:
  - CashReport includes BASE_SUMMARY rows — must exclude them.
  - positionValueInBase is undocumented and None in real data — compute as
    positionValue × fxRateToBase instead.
  - Use the EquitySummaryByReportDateInBase row with max(reportDate) for NAV.
"""

from __future__ import annotations

import datetime
import logging
import random
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree

import requests

from cashz import config
from cashz.connectors.base import AccountBalance, FetchResult, PositionDetail

log = logging.getLogger(__name__)

# Confirmed live endpoints (2026-09-09); do NOT use gdcdyn legacy URLs
_SEND_REQUEST_URL = (
    "https://ndcdyn.interactivebrokers.com"
    "/AccountManagement/FlexWebService/SendRequest"
)
_GET_STATEMENT_URL = (
    "https://ndcdyn.interactivebrokers.com"
    "/AccountManagement/FlexWebService/GetStatement"
)

# Error codes that are "retry" vs "hard fail"
_RETRY_5S = {1009, 1019}   # statement generation in progress / server busy
_RETRY_10S = {1018}        # rate limit exceeded
_MAX_POLLS = 6


def _get(url: str, params: dict) -> requests.Response:
    r = requests.get(
        url,
        params=params,
        headers=config.HTTP_HEADERS,
        verify=config.VERIFY_TLS,
        timeout=45,
    )
    r.raise_for_status()
    return r


def _parse_send_response(content: bytes) -> tuple[bool, Optional[str], Optional[int], Optional[str]]:
    """Parse FlexStatementResponse XML.
    Returns (success, reference_code, error_code, error_message).
    """
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise ValueError(f"IBKR SendRequest returned unparseable XML: {exc}") from exc

    status = root.findtext("Status") or ""
    if status.upper() == "SUCCESS":
        ref = root.findtext("ReferenceCode")
        return True, ref, None, None

    err_code_str = root.findtext("ErrorCode") or ""
    err_msg = root.findtext("ErrorMessage") or "Unknown error"
    try:
        err_code = int(err_code_str)
    except (ValueError, TypeError):
        err_code = 0
    return False, None, err_code, err_msg


def _save_raw_xml(content: bytes, label: str) -> Path:
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = config.ibkr_raw_dir() / f"flex_{label}_{ts}.xml"
    path.write_bytes(content)
    log.debug("IBKR raw XML saved to %s", path)
    return path


def fetch_statement(
    token: Optional[str] = None,
    query_id: Optional[str] = None,
) -> tuple[bytes, datetime.date, str]:
    """Download a Flex statement; return (raw_xml_bytes, report_date, base_currency).

    Raises on hard errors (bad token, expired token, query invalid, etc.).
    """
    token = token or config.ibkr_flex_token()
    query_id = query_id or config.ibkr_flex_query_id()
    if not token or not query_id:
        raise ValueError("IBKR_FLEX_TOKEN and IBKR_FLEX_QUERY_ID must be set")

    # Step 1 — SendRequest
    log.info("IBKR: sending Flex request (query_id=%s)", query_id)
    time.sleep(1.1)  # respect 1 req/s limit
    r = _get(_SEND_REQUEST_URL, {"t": token, "q": query_id, "v": "3"})
    ok, ref_code, err_code, err_msg = _parse_send_response(r.content)
    if not ok:
        _handle_error(err_code, err_msg)

    # Step 2 — poll GetStatement
    raw_xml: Optional[bytes] = None
    for attempt in range(1, _MAX_POLLS + 1):
        log.info("IBKR: polling GetStatement (attempt %d/%d)", attempt, _MAX_POLLS)
        time.sleep(1.1)
        r2 = _get(_GET_STATEMENT_URL, {"t": token, "q": ref_code, "v": "3"})
        content = r2.content

        # Readiness check: substring on raw bytes (statements are large — don't parse)
        if b"FlexQueryResponse" in content:
            raw_xml = content
            break

        # Parse the FlexStatementResponse to get the error code
        try:
            _, _, ec, em = _parse_send_response(content)
        except ValueError:
            raise RuntimeError(f"IBKR GetStatement returned unexpected content: {content[:200]}")

        if ec in _RETRY_5S:
            delay = 5 + random.uniform(0, 1)
            log.info("IBKR: statement not ready (%d), waiting %.1f s", ec, delay)
            time.sleep(delay)
        elif ec in _RETRY_10S:
            delay = 10 + random.uniform(0, 2)
            log.info("IBKR: rate limited (%d), waiting %.1f s", ec, delay)
            time.sleep(delay)
        else:
            _handle_error(ec, em)
    else:
        raise TimeoutError(f"IBKR statement not ready after {_MAX_POLLS} polls")

    _save_raw_xml(raw_xml, "stmt")  # type: ignore[arg-type]
    return raw_xml


def _handle_error(code: int, message: str) -> None:
    """Map IBKR error codes to readable exceptions."""
    if code in (1012, 1015):
        raise PermissionError(
            f"IBKR token expired or invalid (error {code}). "
            "Regenerate it in Client Portal: Performance & Reports → Flex Queries."
        )
    if code == 1013:
        raise PermissionError(f"IBKR IP restriction (error {code}): {message}")
    if code == 1014:
        raise ValueError(f"IBKR query invalid (error {code}): {message}")
    if code == 1010:
        raise ValueError(
            f"IBKR legacy Flex Query (error 1010) — convert to Activity Flex in Client Portal."
        )
    raise RuntimeError(f"IBKR Flex error {code}: {message}")


def _parse_date(s: Optional[str]) -> Optional[datetime.date]:
    """Parse an ISO date string, returning None on failure."""
    if not s:
        return None
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        return None


def parse_statement(raw_xml: bytes) -> FetchResult:
    """Parse a Flex XML statement using ElementTree.

    Uses ElementTree directly (not ibflex) so that unknown assetCategory values
    such as 'ETF' do not cause a hard parse failure.
    """
    try:
        root = ElementTree.fromstring(raw_xml)
    except ElementTree.ParseError as exc:
        return FetchResult(connector="ibkr", success=False, error=f"XML parse error: {exc}")

    stmt = root.find(".//FlexStatement")
    if stmt is None:
        return FetchResult(connector="ibkr", success=False, error="No FlexStatement in response")

    account_id = stmt.get("accountId", "ibkr")
    period = stmt.get("period", "")
    from_date = _parse_date(stmt.get("fromDate"))
    to_date = _parse_date(stmt.get("toDate"))
    when_gen = stmt.get("whenGenerated")

    log.info(
        "IBKR: parsing statement accountId=%s period=%s from=%s to=%s whenGenerated=%s",
        account_id, period, from_date, to_date, when_gen,
    )

    # ── Base currency ─────────────────────────────────────────────────────────
    base_currency = "USD"
    acc_info = stmt.find("AccountInformation")
    if acc_info is not None:
        base_currency = acc_info.get("currency", "USD") or "USD"

    # ── NAV from EquitySummaryInBase ──────────────────────────────────────────
    # Collect the full daily series; nav / report_date = latest row.
    nav: Optional[Decimal] = None
    report_date: Optional[datetime.date] = None
    nav_history: list[tuple[datetime.date, Decimal]] = []

    eq_summary = stmt.find("EquitySummaryInBase")
    if eq_summary is not None:
        for elem in eq_summary:
            rd = _parse_date(elem.get("reportDate"))
            total = _decimal_or_none(elem.get("total"))
            if rd is not None and total is not None:
                nav_history.append((rd, total))
        nav_history.sort()
        if nav_history:
            report_date, nav = nav_history[-1]

    # Cross-check with ChangeInNAV when EquitySummaryInBase is absent
    if nav is None:
        change_nav = stmt.find("ChangeInNAV")
        if change_nav is not None:
            nav = _decimal_or_none(change_nav.get("endingValue"))

    # ── Open positions ────────────────────────────────────────────────────────
    positions: list[PositionDetail] = []
    open_pos = stmt.find("OpenPositions")
    if open_pos is not None:
        for pos in open_pos:
            symbol = pos.get("symbol", "") or ""
            ccy = pos.get("currency", base_currency) or base_currency
            pos_val = _decimal_or_none(pos.get("positionValue"))
            fx = _decimal_or_none(pos.get("fxRateToBase"))

            value_eur: Optional[Decimal] = None
            if pos_val is not None and fx is not None and fx != 0:
                value_eur = pos_val * fx  # pos_val in local ccy → base ccy (EUR by caller)

            positions.append(
                PositionDetail(
                    symbol=symbol,
                    description=pos.get("description", "") or "",
                    isin=pos.get("isin"),
                    conid=str(pos.get("conid", "") or ""),
                    asset_category=pos.get("assetCategory"),
                    quantity=_decimal_or_none(pos.get("position")),
                    mark_price=_decimal_or_none(pos.get("markPrice")),
                    position_value=pos_val,
                    currency=ccy,
                    fx_rate_to_base=fx,
                    value_eur=value_eur,
                )
            )

    # ── Cash report (exclude BASE_SUMMARY) ───────────────────────────────────
    cash_rows: list[AccountBalance] = []
    cash_report = stmt.find("CashReport")
    if cash_report is not None:
        for row in cash_report:
            ccy = row.get("currency", "") or ""
            if ccy.upper() == "BASE_SUMMARY":
                continue  # synthetic aggregation row — would double-count
            ending = _decimal_or_none(row.get("endingCash"))
            if ending is not None:
                cash_rows.append(
                    AccountBalance(
                        external_id=account_id,
                        name=f"Cash {ccy}",
                        balance=ending,
                        currency=ccy,
                        subtype="Cash",
                    )
                )

    if report_date is None and to_date is not None:
        report_date = to_date

    return FetchResult(
        connector="ibkr",
        success=True,
        balances=cash_rows,
        positions=positions,
        nav=nav,
        base_currency=base_currency,
        report_date=report_date,
        nav_history=nav_history,
        message=(
            f"NAV={nav} {base_currency} | {len(positions)} positions | {len(cash_rows)} cash rows"
            f" | {len(nav_history)} NAV history rows | period={period} from={from_date} to={to_date}"
        ),
    )


def fetch() -> FetchResult:
    """Full sync: download + parse. Entry point for the Sync page."""
    try:
        raw_xml = fetch_statement()
    except Exception as exc:
        return FetchResult(connector="ibkr", success=False, error=str(exc))
    return parse_statement(raw_xml)


def _decimal_or_none(val) -> Optional[Decimal]:
    if val is None:
        return None
    try:
        return Decimal(str(val))
    except InvalidOperation:
        return None
