"""bunq connector — direct REST API, no SDK.

Handshake:
  1. POST /v1/installation  (unsigned, registers RSA public key)
  2. POST /v1/device-server (authenticated with installation token)
  3. POST /v1/session-server (signed with private key, returns session token + userID)

After that, all balance reads are a single unsigned GET with the session token.

Signing is only required for POST /v1/session-server.
Signature: RSA-2048, SHA-256, PKCS#1 v1.5, base64-encoded, over the exact
transmitted request body bytes (serialised with separators=(',', ':')).

Context is persisted to data/bunq/context.json and reused across runs.
"""

from __future__ import annotations

import base64
import datetime
import json
import logging
import os
import socket
import time
from decimal import Decimal
from pathlib import Path
from typing import Optional

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from cashz import config
from cashz.connectors.base import AccountBalance, FetchResult

log = logging.getLogger(__name__)

_PROD_BASE = "https://api.bunq.com/v1"
_SAND_BASE = "https://public-api.sandbox.bunq.com/v1"
_GET_DELAY = 1.1  # seconds between GET requests (limit: 3/3s per endpoint)

# Account subtypes that carry a balance we want (active accounts)
_BALANCE_SUBTYPES = {
    "MonetaryAccountBank",
    "MonetaryAccountSavings",
    "MonetaryAccountJoint",
}

# All subtypes that can be monetary accounts, including hard-closed ones
_ALL_MONETARY_SUBTYPES = _BALANCE_SUBTYPES | {
    "MonetaryAccountClosed",
    "MonetaryAccountExternal",
    "MonetaryAccountSavingGoal",
}


def _base_url() -> str:
    return _SAND_BASE if config.bunq_env() == "SANDBOX" else _PROD_BASE


def _context_path() -> Path:
    return config.bunq_dir() / "context.json"


def _key_path() -> Path:
    return config.bunq_dir() / "private_key.pem"


# ── RSA key management ────────────────────────────────────────────────────────
def _generate_key_pair() -> tuple[rsa.RSAPrivateKey, str]:
    """Generate a 2048-bit RSA key and return (private_key, public_key_pem_str)."""
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    pub_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return private_key, pub_pem


def _save_private_key(private_key: rsa.RSAPrivateKey) -> None:
    path = _key_path()
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.write_bytes(pem)
    # Best-effort 600 on Windows (ACL is more complex; README documents this)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _load_private_key() -> Optional[rsa.RSAPrivateKey]:
    path = _key_path()
    if not path.exists():
        return None
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def _sign(private_key: rsa.RSAPrivateKey, body_bytes: bytes) -> str:
    """Sign body_bytes with SHA-256 / PKCS#1 v1.5; return base64 string."""
    sig = private_key.sign(body_bytes, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode("utf-8")


# ── Context persistence ───────────────────────────────────────────────────────
def _load_context() -> Optional[dict]:
    p = _context_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return None


def _save_context(ctx: dict) -> None:
    p = _context_path()
    p.write_text(json.dumps(ctx, indent=2), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


# ── HTTP helpers ──────────────────────────────────────────────────────────────
def _headers(
    auth_token: Optional[str] = None,
    request_id: Optional[str] = None,
) -> dict:
    h = {
        **config.HTTP_HEADERS,
        "Cache-Control": "no-cache",
        "Content-Type": "application/json",
        "X-Bunq-Geolocation": "0 0 0 0 NL",
        "X-Bunq-Language": "en_US",
        "X-Bunq-Region": "en_US",
    }
    if request_id:
        h["X-Bunq-Client-Request-Id"] = request_id
    if auth_token:
        h["X-Bunq-Client-Authentication"] = auth_token
    return h


def _unique_id() -> str:
    import uuid
    return str(uuid.uuid4())


def _post_unsigned(url: str, body: dict) -> dict:
    body_bytes = json.dumps(body, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    r = requests.post(
        url,
        data=body_bytes,
        headers=_headers(request_id=_unique_id()),
        verify=config.VERIFY_TLS,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _post_signed(url: str, body: dict, auth_token: str, private_key: rsa.RSAPrivateKey) -> dict:
    """POST with X-Bunq-Client-Signature — only used for /session-server."""
    body_bytes = json.dumps(body, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    sig = _sign(private_key, body_bytes)
    h = _headers(auth_token=auth_token, request_id=_unique_id())
    h["X-Bunq-Client-Signature"] = sig
    r = requests.post(
        url,
        data=body_bytes,
        headers=h,
        verify=config.VERIFY_TLS,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _post_authenticated(url: str, body: dict, auth_token: str) -> dict:
    body_bytes = json.dumps(body, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    r = requests.post(
        url,
        data=body_bytes,
        headers=_headers(auth_token=auth_token, request_id=_unique_id()),
        verify=config.VERIFY_TLS,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _get(url: str, session_token: str) -> dict:
    r = requests.get(
        url,
        headers=_headers(auth_token=session_token, request_id=_unique_id()),
        verify=config.VERIFY_TLS,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


# ── Handshake ─────────────────────────────────────────────────────────────────
def _install(api_key: str) -> dict:
    """Run the full three-step handshake; return a context dict."""
    base = _base_url()
    log.info("bunq: running installation handshake (%s)", config.bunq_env())

    # Step 1 — generate RSA key pair
    private_key, pub_pem = _generate_key_pair()
    _save_private_key(private_key)

    # POST /v1/installation
    data = _post_unsigned(f"{base}/installation", {"client_public_key": pub_pem})
    install_token = None
    server_pub_key = None
    for item in data.get("Response", []):
        if "Token" in item:
            install_token = item["Token"]["token"]
        if "ServerPublicKey" in item:
            server_pub_key = item["ServerPublicKey"]["server_public_key"]

    # Step 2 — device-server
    permitted_ips = ["*"] if config.bunq_allow_all_ips() else [_my_public_ip()]
    _post_authenticated(
        f"{base}/device-server",
        {"description": f"Cashz/{socket.gethostname()}", "secret": api_key, "permitted_ips": permitted_ips},
        install_token,
    )

    # Step 3 — session-server (signed)
    data = _post_signed(
        f"{base}/session-server",
        {"secret": api_key},
        install_token,
        private_key,
    )
    session_token = None
    user_id = None
    for item in data.get("Response", []):
        if "Token" in item:
            session_token = item["Token"]["token"]
        for key in ("UserPerson", "UserCompany", "UserApiKey"):
            if key in item:
                user_id = item[key]["id"]

    ctx = {
        "env": config.bunq_env(),
        "api_key": api_key,
        "installation_token": install_token,
        "session_token": session_token,
        "user_id": user_id,
        "server_public_key": server_pub_key,
        "installed_at": datetime.datetime.utcnow().isoformat(),
    }
    _save_context(ctx)
    log.info("bunq: installation complete, user_id=%s", user_id)
    return ctx


def _refresh_session(ctx: dict, private_key: rsa.RSAPrivateKey) -> dict:
    """Refresh session token only (steps 1+2 are one-time)."""
    base = _base_url()
    data = _post_signed(
        f"{base}/session-server",
        {"secret": ctx["api_key"]},
        ctx["installation_token"],
        private_key,
    )
    for item in data.get("Response", []):
        if "Token" in item:
            ctx["session_token"] = item["Token"]["token"]
    _save_context(ctx)
    log.info("bunq: session refreshed")
    return ctx


def _my_public_ip() -> str:
    """Return the public IP visible to external servers, or '0.0.0.0' on failure.

    The UDP-socket trick (connect to 8.8.8.8) returns the local interface IP,
    which is wrong behind NAT/CGNAT.  bunq validates permitted_ips against the
    source IP it sees on the request, so we must use the public one.
    """
    for url in (
        "https://api.ipify.org",
        "https://checkip.amazonaws.com",
        "https://icanhazip.com",
    ):
        try:
            r = requests.get(url, timeout=5, headers=config.HTTP_HEADERS)
            if r.ok:
                ip = r.text.strip()
                if ip:
                    return ip
        except Exception:
            continue
    # Last resort: local interface IP (works only when no NAT)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "0.0.0.0"


# ── Public API ─────────────────────────────────────────────────────────────────
def fetch_balances() -> FetchResult:
    """Fetch all bunq account balances. Returns a FetchResult."""
    api_key = config.bunq_api_key()
    if not api_key:
        return FetchResult(
            connector="bunq", success=False, error="BUNQ_API_KEY not set"
        )

    ctx = _load_context()
    private_key = _load_private_key()

    # Fresh install if no context or the env changed
    needs_install = (
        ctx is None
        or private_key is None
        or ctx.get("env") != config.bunq_env()
        or ctx.get("api_key") != api_key
    )
    if needs_install:
        try:
            ctx = _install(api_key)
            private_key = _load_private_key()
        except Exception as exc:
            return FetchResult(connector="bunq", success=False, error=str(exc))

    # Fetch accounts
    base = _base_url()
    user_id = ctx["user_id"]

    def _do_fetch() -> list[AccountBalance]:
        url = f"{base}/user/{user_id}/monetary-account"
        time.sleep(_GET_DELAY)
        data = _get(url, ctx["session_token"])
        balances = []
        archived_total = Decimal("0")
        archived_ccy = "EUR"
        has_archived = False

        for item in data.get("Response", []):
            for subtype, account_data in item.items():
                if subtype not in _BALANCE_SUBTYPES:
                    if subtype not in ("Id", "Token", "UserPerson", "UserCompany"):
                        log.debug("bunq: skipping unknown subtype %s", subtype)
                    continue
                status = account_data.get("status", "")
                balance_obj = account_data.get("balance", {})
                try:
                    bal = Decimal(str(balance_obj.get("value", "0")))
                    ccy = balance_obj.get("currency", "EUR")
                except Exception:
                    continue

                if status in ("ACTIVE", ""):
                    ext_id = str(account_data.get("id", ""))
                    name = account_data.get("description", subtype)
                    balances.append(
                        AccountBalance(
                            external_id=ext_id,
                            name=name,
                            balance=bal,
                            currency=ccy,
                            subtype=subtype,
                        )
                    )
                else:
                    # Aggregate all non-active (archived/cancelled) accounts
                    if ccy == "EUR":
                        archived_total += bal
                        archived_ccy = ccy
                        has_archived = True

        if has_archived:
            balances.append(
                AccountBalance(
                    external_id="__archived__",
                    name="Archived accounts",
                    balance=archived_total,
                    currency=archived_ccy,
                    subtype="MonetaryAccountArchived",
                )
            )
        return balances

    try:
        balances = _do_fetch()
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 401:
            # Session expired — refresh and retry once
            log.info("bunq: session expired, refreshing")
            try:
                ctx = _refresh_session(ctx, private_key)
                balances = _do_fetch()
            except Exception as exc2:
                return FetchResult(connector="bunq", success=False, error=str(exc2))
        else:
            return FetchResult(connector="bunq", success=False, error=str(exc))
    except Exception as exc:
        return FetchResult(connector="bunq", success=False, error=str(exc))

    return FetchResult(
        connector="bunq",
        success=True,
        balances=balances,
        message=f"Fetched {len(balances)} account(s)",
    )


def _fetch_eod_balances(
    base: str,
    user_id: str,
    acct_id: str,
    session_token_ref: list[str],  # mutable ref so refresh can update it
    ctx_ref: list[dict],
    cutoff: datetime.date,
) -> list[tuple[datetime.date, Decimal, str]]:
    """Walk payment history for one monetary account; return EOD (date, balance, currency) list.

    Payments come newest-first from bunq.  The first occurrence of each calendar
    date is the last transaction of that day → end-of-day balance.
    """
    results: list[tuple[datetime.date, Decimal, str]] = []
    seen_dates: set[datetime.date] = set()
    next_url: Optional[str] = (
        f"{base}/user/{user_id}/monetary-account/{acct_id}/payment?count=200"
    )

    while next_url:
        try:
            time.sleep(_GET_DELAY)
            data = _get(next_url, session_token_ref[0])
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 401:
                try:
                    ctx_ref[0] = _refresh_session(ctx_ref[0], _load_private_key())
                    session_token_ref[0] = ctx_ref[0]["session_token"]
                    data = _get(next_url, session_token_ref[0])
                except Exception as exc2:
                    log.warning("bunq session refresh failed: %s", exc2)
                    break
            elif exc.response is not None and exc.response.status_code == 404:
                break  # no payments for this account
            else:
                log.warning("bunq payment fetch error for account %s: %s", acct_id, exc)
                break
        except Exception as exc:
            log.warning("bunq payment fetch error for account %s: %s", acct_id, exc)
            break

        payments = data.get("Response", [])
        if not payments:
            break

        reached_cutoff = False
        oldest_id: Optional[int] = None

        for payment_item in payments:
            payment = payment_item.get("Payment", {})
            if not payment:
                continue

            pay_date_str = (payment.get("created") or "")[:10]
            try:
                pay_date = datetime.date.fromisoformat(pay_date_str)
            except ValueError:
                continue

            if pay_date < cutoff:
                reached_cutoff = True
                break

            bal_obj = payment.get("balance_after_mutation") or {}
            bal_val = bal_obj.get("value")
            bal_ccy = bal_obj.get("currency", "EUR")
            oldest_id = payment.get("id")

            if bal_val is None:
                continue
            try:
                balance = Decimal(str(bal_val))
            except Exception:
                continue

            if pay_date not in seen_dates:
                seen_dates.add(pay_date)
                results.append((pay_date, balance, bal_ccy))

        if reached_cutoff or oldest_id is None:
            break

        next_url = (
            f"{base}/user/{user_id}/monetary-account/{acct_id}"
            f"/payment?count=200&older_id={oldest_id}"
        )

    return results


def fetch_history(max_months: int = 24) -> FetchResult:
    """Reconstruct end-of-day balance history for all bunq accounts.

    Walks the payment history endpoint for every monetary account (active and
    archived) and records balance_after_mutation for each day that had at least
    one transaction.  Archived accounts are aggregated into a single synthetic
    external_id="__archived__" timeline.

    Returns FetchResult with balance_history populated.
    """
    api_key = config.bunq_api_key()
    if not api_key:
        return FetchResult(connector="bunq", success=False, error="BUNQ_API_KEY not set")

    ctx = _load_context()
    private_key = _load_private_key()
    needs_install = (
        ctx is None
        or private_key is None
        or ctx.get("env") != config.bunq_env()
        or ctx.get("api_key") != api_key
    )
    if needs_install:
        try:
            ctx = _install(api_key)
        except Exception as exc:
            return FetchResult(connector="bunq", success=False, error=str(exc))

    base = _base_url()
    user_id = ctx["user_id"]
    # Mutable refs so _fetch_eod_balances can update them on 401
    session_token_ref = [ctx["session_token"]]
    ctx_ref = [ctx]
    cutoff_active = datetime.date.today() - datetime.timedelta(days=max_months * 31)
    # Archived accounts may have been closed years ago — look back much further
    cutoff_archived = datetime.date.today() - datetime.timedelta(days=max_months * 31 * 3)

    try:
        time.sleep(_GET_DELAY)
        accounts_resp = _get(f"{base}/user/{user_id}/monetary-account", session_token_ref[0])
    except Exception as exc:
        return FetchResult(connector="bunq", success=False, error=str(exc))

    balance_history: list[tuple[datetime.date, str, Decimal, str]] = []
    # Archived: collect each account's timeline separately, then aggregate per date
    archived_timelines: dict[str, list[tuple[datetime.date, Decimal, str]]] = {}

    for item in accounts_resp.get("Response", []):
        for subtype, account_data in item.items():
            # Use the expanded set so MonetaryAccountClosed etc. are included
            if subtype not in _ALL_MONETARY_SUBTYPES:
                continue
            acct_id = str(account_data.get("id", ""))
            if not acct_id:
                continue
            status = account_data.get("status", "")
            is_active = status in ("ACTIVE", "")
            cutoff = cutoff_active if is_active else cutoff_archived

            log.debug(
                "bunq history: account %s subtype=%s status=%s active=%s",
                acct_id, subtype, status, is_active,
            )
            timeline = _fetch_eod_balances(
                base, user_id, acct_id, session_token_ref, ctx_ref, cutoff
            )
            log.debug("bunq history: account %s → %d EOD entries", acct_id, len(timeline))

            if is_active:
                for d, bal, ccy in timeline:
                    balance_history.append((d, acct_id, bal, ccy))
            else:
                if timeline:
                    archived_timelines[acct_id] = timeline

    # Aggregate archived timelines: for each date, sum the last-known balance
    # of every archived account (carry-forward between transactions).
    if archived_timelines:
        all_archived_dates = sorted(
            {d for pairs in archived_timelines.values() for d, _, _ in pairs}
        )
        last_known: dict[str, Decimal] = {aid: Decimal("0") for aid in archived_timelines}

        for d in all_archived_dates:
            for acct_id, pairs in archived_timelines.items():
                for pd, pb, _ in pairs:
                    if pd == d:
                        last_known[acct_id] = pb
            total = sum(last_known.values())
            balance_history.append((d, "__archived__", total, "EUR"))

    balance_history.sort()
    return FetchResult(
        connector="bunq",
        success=True,
        balance_history=balance_history,
        message=f"Fetched {len(balance_history)} historical balance point(s)",
    )
