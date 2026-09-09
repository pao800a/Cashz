"""Shared pytest fixtures."""

from __future__ import annotations

import os
import socket
import pytest


# ── Block real network in all tests ──────────────────────────────────────────
@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Fail any test that opens a real TCP socket (catches real HTTP calls)."""
    original_connect = socket.socket.connect

    def blocked_connect(self, address):
        raise ConnectionRefusedError(
            f"Tests must not open real network connections. "
            f"Tried to connect to {address}. Use `responses` to mock HTTP."
        )

    monkeypatch.setattr(socket.socket, "connect", blocked_connect)
    yield


# ── Temporary SQLite DB ───────────────────────────────────────────────────────
@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """Provide a fresh temporary SQLite database for each test."""
    db_file = tmp_path / "test_cashz.db"
    monkeypatch.setenv("CASHZ_DB_PATH", str(db_file))

    # Reset the engine singleton so it picks up the new path
    import cashz.storage.db as db_module
    db_module._engine = None
    db_module._SessionLocal = None
    db_module._initialized = False

    from cashz.storage.db import init_db
    init_db()

    yield db_file

    # Teardown: reset again
    db_module._engine = None
    db_module._SessionLocal = None
    db_module._initialized = False
