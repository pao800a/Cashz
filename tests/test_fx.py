"""Tests for the FX module."""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
import responses as resp_mock

from cashz import fx


# ── Helpers ───────────────────────────────────────────────────────────────────
def _seed_rates(session, rates: list[tuple[str, datetime.date, Decimal]]) -> None:
    from cashz.storage.repo import upsert_fx_rate
    for ccy, date, rate in rates:
        upsert_fx_rate(session, ccy, date, rate)
    session.commit()


class TestToEur:
    def test_eur_identity(self, tmp_db):
        eur, rate, rate_date = fx.to_eur(Decimal("1234.56"), "EUR", datetime.date(2024, 6, 1))
        assert eur == Decimal("1234.56")
        assert rate == Decimal("1")

    def test_division_direction(self, tmp_db):
        """EUR-base means 1 EUR = rate × foreign → foreign / rate = EUR."""
        from cashz.storage.db import get_session
        from cashz.storage.repo import upsert_fx_rate
        date = datetime.date(2024, 6, 3)
        rate = Decimal("1.2000")
        with get_session() as session:
            upsert_fx_rate(session, "USD", date, rate)
            session.commit()
        usd_amount = Decimal("120.00")
        eur, used_rate, _ = fx.to_eur(usd_amount, "USD", date)
        assert eur == usd_amount / rate  # 120 / 1.2 = 100
        assert eur == Decimal("100")
        assert used_rate == rate

    def test_carry_back_weekend(self, tmp_db):
        """Saturday lookup should return Friday's rate."""
        from cashz.storage.db import get_session
        from cashz.storage.repo import upsert_fx_rate
        friday = datetime.date(2024, 6, 7)  # Friday
        saturday = datetime.date(2024, 6, 8)  # Saturday — no ECB rate
        rate = Decimal("1.1500")
        with get_session() as session:
            upsert_fx_rate(session, "GBP", friday, rate)
            session.commit()
        eur, used_rate, rate_date = fx.to_eur(Decimal("100"), "GBP", saturday)
        assert rate_date == friday
        assert used_rate == rate

    def test_missing_raises(self, tmp_db):
        with pytest.raises(fx.FxRateMissing):
            fx.to_eur(Decimal("100"), "XYZ", datetime.date(2024, 1, 1))

    def test_stale_raises(self, tmp_db):
        from cashz.storage.db import get_session
        from cashz.storage.repo import upsert_fx_rate
        old_date = datetime.date(2020, 1, 1)
        ask_date = datetime.date(2024, 6, 1)
        with get_session() as session:
            upsert_fx_rate(session, "CHF", old_date, Decimal("1.05"))
            session.commit()
        with pytest.raises(fx.FxRateStale):
            fx.to_eur(Decimal("100"), "CHF", ask_date)

    def test_no_forward_fill(self, tmp_db):
        """A future rate must NOT be used for a past date."""
        from cashz.storage.db import get_session
        from cashz.storage.repo import upsert_fx_rate
        future = datetime.date(2030, 1, 1)
        past = datetime.date(2024, 1, 1)
        with get_session() as session:
            upsert_fx_rate(session, "USD", future, Decimal("1.50"))
            session.commit()
        # Asking for a past date with only a future rate: no result → FxRateMissing
        with pytest.raises(fx.FxRateMissing):
            fx.to_eur(Decimal("100"), "USD", past)


class TestEcbParsing:
    _DAILY_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01"
  xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
  <Cube>
    <Cube time="2024-06-03">
      <Cube currency="USD" rate="1.1614"/>
      <Cube currency="GBP" rate="0.8574"/>
      <Cube currency="CHF" rate="0.9425"/>
    </Cube>
  </Cube>
</gesmes:Envelope>"""

    def test_parse_daily_xml(self):
        rows = fx._parse_ecb_xml(self._DAILY_XML)
        assert len(rows) == 3
        dates = {d for d, _, _ in rows}
        assert dates == {datetime.date(2024, 6, 3)}
        rates = {ccy: r for _, ccy, r in rows}
        assert rates["USD"] == Decimal("1.1614")
        assert rates["GBP"] == Decimal("0.8574")

    def test_parse_ignores_bad_rates(self):
        xml = self._DAILY_XML.replace('rate="1.1614"', 'rate="N/A"')
        rows = fx._parse_ecb_xml(xml)
        ccys = {ccy for _, ccy, _ in rows}
        assert "USD" not in ccys
        assert "GBP" in ccys

    _HIST_CSV = """\
Date ,USD ,GBP ,
2024-06-01,1.1600,0.8560,
2024-06-02,N/A,0.8565,
"""

    def test_parse_hist_csv(self):
        rows = fx._parse_ecb_hist_csv(self._HIST_CSV)
        assert any(ccy == "USD" and d == datetime.date(2024, 6, 1) for d, ccy, _ in rows)
        # N/A should be skipped
        assert not any(ccy == "USD" and d == datetime.date(2024, 6, 2) for d, ccy, _ in rows)

    @resp_mock.activate
    def test_refresh_recent_calls_ecb(self, tmp_db):
        """refresh_recent() should call the two ECB XML endpoints."""
        resp_mock.add(resp_mock.GET, fx._ECB_DAILY, body=self._DAILY_XML.encode(), status=200)
        resp_mock.add(resp_mock.GET, fx._ECB_90D, body=self._DAILY_XML.encode(), status=200)
        n = fx.refresh_recent()
        assert n > 0
        assert len(resp_mock.calls) == 2
