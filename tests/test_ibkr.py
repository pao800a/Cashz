"""Tests for the IBKR connector."""

from __future__ import annotations

import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from cashz.connectors.ibkr import (
    _parse_send_response,
    parse_statement,
    _handle_error,
)
from cashz.connectors.base import FetchResult


# ── Flex XML fixtures ─────────────────────────────────────────────────────────
_SEND_SUCCESS = b"""
<FlexStatementResponse timestamp="09 September, 2026 08:00 AM EDT">
  <Status>Success</Status>
  <ReferenceCode>123456789</ReferenceCode>
  <url>https://gdcdyn.interactivebrokers.com/legacy/path</url>
</FlexStatementResponse>
"""

_SEND_FAIL_1019 = b"""
<FlexStatementResponse timestamp="09 September, 2026 08:01 AM EDT">
  <Status>Fail</Status>
  <ErrorCode>1019</ErrorCode>
  <ErrorMessage>Statement generation in progress. Please try again shortly.</ErrorMessage>
</FlexStatementResponse>
"""

_SEND_FAIL_1012 = b"""
<FlexStatementResponse timestamp="09 September, 2026 08:01 AM EDT">
  <Status>Fail</Status>
  <ErrorCode>1012</ErrorCode>
  <ErrorMessage>Token has expired.</ErrorMessage>
</FlexStatementResponse>
"""

_SEND_FAIL_1018 = b"""
<FlexStatementResponse timestamp="09 September, 2026 08:01 AM EDT">
  <Status>Fail</Status>
  <ErrorCode>1018</ErrorCode>
  <ErrorMessage>Too many requests have been made from this token.</ErrorMessage>
</FlexStatementResponse>
"""

# Minimal realistic Flex statement with the three sections we care about,
# including the BASE_SUMMARY trap and a multi-row EquitySummaryInBase.
_FLEX_STATEMENT = b"""<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse queryName="My Portfolio" type="AF">
  <FlexStatements count="1">
    <FlexStatement accountId="U123456" fromDate="2026-09-01" toDate="2026-09-08"
                   period="LastBusinessDay" whenGenerated="2026-09-09;070000">
      <AccountInformation accountId="U123456" accountType="Individual"
                          currency="USD" name="Test Account"/>
      <ChangeInNAV startingValue="95000" endingValue="100000" twr="0.0526"/>
      <EquitySummaryInBase>
        <EquitySummaryByReportDateInBase reportDate="2026-09-05" currency="USD" total="98000"/>
        <EquitySummaryByReportDateInBase reportDate="2026-09-08" currency="USD" total="100000"/>
      </EquitySummaryInBase>
      <OpenPositions>
        <OpenPosition accountId="U123456" symbol="AAPL" description="Apple Inc"
                      isin="US0378331005" conid="265598" assetCategory="STK"
                      position="50" markPrice="220.00" positionValue="11000.00"
                      currency="USD" fxRateToBase="1.0" reportDate="2026-09-08"
                      costBasisPrice="200.00" fifoPnlUnrealized="1000.00"/>
        <OpenPosition accountId="U123456" symbol="VWCE" description="Vanguard FTSE All-World ETF"
                      isin="IE00B3RBWM25" conid="399506549" assetCategory="ETF"
                      position="100" markPrice="125.00" positionValue="12500.00"
                      currency="EUR" fxRateToBase="0.861" reportDate="2026-09-08"
                      costBasisPrice="110.00" fifoPnlUnrealized="1500.00"/>
      </OpenPositions>
      <CashReport>
        <CashReportCurrency currency="BASE_SUMMARY" fromDate="2026-09-01" toDate="2026-09-08"
                            startingCash="1000" endingCash="2000" endingSettledCash="1900"
                            levelOfDetail="Currency"/>
        <CashReportCurrency currency="USD" fromDate="2026-09-01" toDate="2026-09-08"
                            startingCash="800" endingCash="1500" endingSettledCash="1400"
                            levelOfDetail="Currency"/>
        <CashReportCurrency currency="EUR" fromDate="2026-09-01" toDate="2026-09-08"
                            startingCash="200" endingCash="500" endingSettledCash="500"
                            levelOfDetail="Currency"/>
      </CashReport>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>
"""


class TestSendResponseParsing:
    def test_success(self):
        ok, ref, ec, em = _parse_send_response(_SEND_SUCCESS)
        assert ok is True
        assert ref == "123456789"
        assert ec is None

    def test_fail_1019(self):
        ok, ref, ec, em = _parse_send_response(_SEND_FAIL_1019)
        assert ok is False
        assert ec == 1019
        assert "progress" in em.lower()

    def test_fail_1012(self):
        ok, ref, ec, em = _parse_send_response(_SEND_FAIL_1012)
        assert ok is False
        assert ec == 1012

    def test_fail_1018(self):
        ok, ref, ec, em = _parse_send_response(_SEND_FAIL_1018)
        assert ec == 1018


class TestHandleError:
    def test_1012_raises_permission_error(self):
        with pytest.raises(PermissionError, match="token expired"):
            _handle_error(1012, "Token has expired.")

    def test_1015_raises_permission_error(self):
        with pytest.raises(PermissionError):
            _handle_error(1015, "Token is invalid.")

    def test_1013_raises_permission_error(self):
        with pytest.raises(PermissionError, match="IP restriction"):
            _handle_error(1013, "IP restriction.")

    def test_1014_raises_value_error(self):
        with pytest.raises(ValueError, match="query invalid"):
            _handle_error(1014, "Query is invalid.")

    def test_1010_raises_value_error(self):
        with pytest.raises(ValueError, match="legacy"):
            _handle_error(1010, "Legacy Flex Queries.")

    def test_generic_raises_runtime_error(self):
        with pytest.raises(RuntimeError, match="1001"):
            _handle_error(1001, "Could not generate.")


class TestParseStatement:
    def test_nav_uses_max_report_date(self):
        result = parse_statement(_FLEX_STATEMENT)
        assert result.success
        # There are two rows: 2026-09-05 (98000) and 2026-09-08 (100000)
        # Must pick the latest = 100000
        assert result.nav == Decimal("100000")

    def test_base_currency_from_account_info(self):
        result = parse_statement(_FLEX_STATEMENT)
        assert result.base_currency == "USD"

    def test_base_summary_excluded_from_cash(self):
        result = parse_statement(_FLEX_STATEMENT)
        currencies = {b.currency for b in result.balances}
        assert "BASE_SUMMARY" not in currencies
        # Should have USD and EUR only
        assert "USD" in currencies
        assert "EUR" in currencies

    def test_cash_not_double_counted(self):
        result = parse_statement(_FLEX_STATEMENT)
        # Total cash should be USD 1500 + EUR 500, NOT BASE_SUMMARY 2000 added on top
        total = sum(b.balance for b in result.balances)
        # BASE_SUMMARY is 2000, USD is 1500, EUR is 500 → 4000 if double-counted
        assert total == Decimal("2000"), (
            f"Cash double-counted: got {total} instead of 2000 (1500+500)"
        )

    def test_position_value_computed_from_position_value_times_fx(self):
        """positionValueInBase is unreliable — connector derives value_eur = positionValue × fxRateToBase."""
        result = parse_statement(_FLEX_STATEMENT)
        aapl = next(p for p in result.positions if p.symbol == "AAPL")
        # positionValue=11000 USD, fxRateToBase=1.0 → value_eur should be 11000.00
        assert aapl.value_eur == Decimal("11000.00") * Decimal("1.0")

    def test_positions_populated(self):
        result = parse_statement(_FLEX_STATEMENT)
        assert len(result.positions) == 2

    def test_report_date(self):
        result = parse_statement(_FLEX_STATEMENT)
        assert result.report_date == datetime.date(2026, 9, 8)

    def test_decimal_types(self):
        result = parse_statement(_FLEX_STATEMENT)
        assert isinstance(result.nav, Decimal)
        for p in result.positions:
            if p.position_value is not None:
                assert isinstance(p.position_value, Decimal)
