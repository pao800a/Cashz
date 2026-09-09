"""Tests for the bunq connector."""

from __future__ import annotations

import base64
import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import responses as resp_mock

from cashz.connectors.bunq import _sign, _unique_id
from cashz.connectors.base import FetchResult


# ── Fixture data ──────────────────────────────────────────────────────────────
MONETARY_ACCOUNT_RESPONSE = {
    "Response": [
        {
            "MonetaryAccountBank": {
                "id": 1001,
                "description": "Main Account",
                "status": "ACTIVE",
                "sub_status": "NONE",
                "currency": "EUR",
                "balance": {"currency": "EUR", "value": "4321.99"},
            }
        },
        {
            "MonetaryAccountSavings": {
                "id": 1002,
                "description": "Savings",
                "status": "ACTIVE",
                "sub_status": "NONE",
                "currency": "EUR",
                "balance": {"currency": "EUR", "value": "15000.00"},
            }
        },
        {
            "MonetaryAccountJoint": {
                "id": 1003,
                "description": "Joint",
                "status": "ACTIVE",
                "sub_status": "NONE",
                "currency": "EUR",
                "balance": {"currency": "EUR", "value": "500.00"},
            }
        },
        {
            # Unknown subtype — should be silently skipped, not raise
            "MonetaryAccountCard": {
                "id": 1004,
                "balance": {"currency": "EUR", "value": "99.00"},
            }
        },
    ]
}


class TestBunqBalanceParsing:
    """Test balance extraction without hitting the network."""

    def _simulate_fetch(self, response_json: dict):
        """Simulate the _get() call returning a parsed JSON dict."""
        from cashz.connectors import bunq as bunq_mod

        # Patch context so we skip the handshake
        ctx = {
            "env": "SANDBOX",
            "api_key": "fake_key",
            "installation_token": "inst_tok",
            "session_token": "sess_tok",
            "user_id": 42,
            "server_public_key": "pk",
        }

        with (
            patch.object(bunq_mod, "_load_context", return_value=ctx),
            patch.object(bunq_mod, "_load_private_key", return_value=MagicMock()),
            patch.object(bunq_mod, "config") as mock_cfg,
            patch("cashz.connectors.bunq.requests.get") as mock_get,
        ):
            mock_cfg.bunq_api_key.return_value = "fake_key"
            mock_cfg.bunq_env.return_value = "SANDBOX"
            mock_cfg.bunq_allow_all_ips.return_value = False
            mock_cfg.HTTP_HEADERS = {}
            mock_cfg.VERIFY_TLS = False

            mock_resp = MagicMock()
            mock_resp.json.return_value = response_json
            mock_resp.raise_for_status.return_value = None
            mock_get.return_value = mock_resp

            result = bunq_mod.fetch_balances()
        return result

    def test_extracts_bank_balance(self):
        result = self._simulate_fetch(MONETARY_ACCOUNT_RESPONSE)
        assert result.success
        bank = next(b for b in result.balances if b.subtype == "MonetaryAccountBank")
        assert bank.balance == Decimal("4321.99")
        assert bank.currency == "EUR"
        assert bank.external_id == "1001"

    def test_extracts_savings_balance(self):
        result = self._simulate_fetch(MONETARY_ACCOUNT_RESPONSE)
        savings = next(b for b in result.balances if b.subtype == "MonetaryAccountSavings")
        assert savings.balance == Decimal("15000.00")

    def test_skips_unknown_subtype(self):
        result = self._simulate_fetch(MONETARY_ACCOUNT_RESPONSE)
        subtypes = {b.subtype for b in result.balances}
        assert "MonetaryAccountCard" not in subtypes

    def test_balance_is_decimal_not_float(self):
        result = self._simulate_fetch(MONETARY_ACCOUNT_RESPONSE)
        for b in result.balances:
            assert isinstance(b.balance, Decimal)

    def test_cancelled_accounts_aggregated(self):
        """Cancelled accounts are no longer excluded — they appear as the
        synthetic __archived__ entry so historical data is preserved."""
        response = {
            "Response": [
                {
                    "MonetaryAccountBank": {
                        "id": 9999,
                        "description": "Closed",
                        "status": "CANCELLED",
                        "currency": "EUR",
                        "balance": {"currency": "EUR", "value": "0.00"},
                    }
                }
            ]
        }
        result = self._simulate_fetch(response)
        assert len(result.balances) == 1
        archived = result.balances[0]
        assert archived.external_id == "__archived__"
        assert archived.subtype == "MonetaryAccountArchived"

    def test_missing_api_key_fails(self):
        from cashz.connectors import bunq as bunq_mod
        with patch.object(bunq_mod, "config") as mock_cfg:
            mock_cfg.bunq_api_key.return_value = ""
            result = bunq_mod.fetch_balances()
        assert not result.success
        assert "BUNQ_API_KEY" in result.error


class TestBunqSigning:
    def test_signature_over_exact_bytes(self):
        """The signature must be computed over the exact bytes that will be sent."""
        from cryptography.hazmat.primitives.asymmetric import rsa, padding
        from cryptography.hazmat.primitives import hashes

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        body = {"secret": "test_api_key_123"}
        body_bytes = json.dumps(body, separators=(",", ":"), ensure_ascii=True).encode("utf-8")

        sig_b64 = _sign(private_key, body_bytes)
        sig = base64.b64decode(sig_b64)

        # Verify with the public key
        private_key.public_key().verify(sig, body_bytes, padding.PKCS1v15(), hashes.SHA256())
        # If verify() doesn't raise, the signature is valid over body_bytes

    def test_signature_different_body_fails(self):
        from cryptography.hazmat.primitives.asymmetric import rsa, padding
        from cryptography.hazmat.primitives import hashes
        from cryptography.exceptions import InvalidSignature

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        original_bytes = b'{"secret":"key"}'
        sig_b64 = _sign(private_key, original_bytes)
        sig = base64.b64decode(sig_b64)

        with pytest.raises(InvalidSignature):
            private_key.public_key().verify(
                sig, b'{"secret":"different_key"}', padding.PKCS1v15(), hashes.SHA256()
            )
