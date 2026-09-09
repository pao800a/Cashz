"""Central configuration loaded from .env."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env if present; existing env vars are never overwritten.
_repo_root = Path(__file__).parent.parent
load_dotenv(_repo_root / ".env")

# ── Paths ─────────────────────────────────────────────────────────────────────
def db_path() -> Path:
    raw = os.getenv("CASHZ_DB_PATH", "data/cashz.db")
    p = Path(raw)
    if not p.is_absolute():
        p = _repo_root / p
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def data_dir() -> Path:
    d = _repo_root / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def bunq_dir() -> Path:
    d = data_dir() / "bunq"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ibkr_raw_dir() -> Path:
    d = data_dir() / "ibkr_raw"
    d.mkdir(parents=True, exist_ok=True)
    return d


def reports_dir() -> Path:
    d = data_dir() / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── bunq ─────────────────────────────────────────────────────────────────────
def bunq_api_key() -> str:
    return os.getenv("BUNQ_API_KEY", "")


def bunq_env() -> str:
    """'PRODUCTION' or 'SANDBOX'."""
    return os.getenv("BUNQ_ENV", "PRODUCTION").upper()


def bunq_allow_all_ips() -> bool:
    return os.getenv("BUNQ_ALLOW_ALL_IPS", "false").lower() in ("1", "true", "yes")


# ── IBKR ─────────────────────────────────────────────────────────────────────
def ibkr_flex_token() -> str:
    return os.getenv("IBKR_FLEX_TOKEN", "")


def ibkr_flex_query_id() -> str:
    return os.getenv("IBKR_FLEX_QUERY_ID", "")


# ── HTTP ─────────────────────────────────────────────────────────────────────
HTTP_HEADERS = {
    "User-Agent": "Cashz/0.1 (personal finance tracker; https://github.com/local/cashz)",
}

# Requests will use the certifi CA bundle rather than the Windows store,
# which avoids TLS failures on managed Windows machines.
VERIFY_TLS = True

# Maximum age (days) of a cached FX rate before we refuse to use it
FX_MAX_STALENESS_DAYS = 7


# ── Config summary (never prints values) ─────────────────────────────────────
def config_status() -> dict[str, bool]:
    """Return which env vars are set (True/False), never their values."""
    return {
        "BUNQ_API_KEY": bool(bunq_api_key()),
        "BUNQ_ENV": bool(os.getenv("BUNQ_ENV")),
        "IBKR_FLEX_TOKEN": bool(ibkr_flex_token()),
        "IBKR_FLEX_QUERY_ID": bool(ibkr_flex_query_id()),
        "CASHZ_DB_PATH": bool(os.getenv("CASHZ_DB_PATH")),
    }
