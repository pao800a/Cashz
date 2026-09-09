"""Sync page — pull fresh data from bunq and IBKR, refresh FX rates."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Optional

import streamlit as st

from cashz import config, fx
from cashz.storage.db import get_session
from cashz.storage import repo
from cashz.models import Account


def show():
    st.title("🔄 Sync")

    # ── Config status ─────────────────────────────────────────────────────────
    with st.expander("⚙️ Configuration status", expanded=False):
        status = config.config_status()
        for var, is_set in status.items():
            icon = "✅" if is_set else "❌"
            st.write(f"{icon} `{var}`")
        st.caption("Values are never displayed here — only whether they are set.")
        if not status.get("BUNQ_API_KEY"):
            st.info("To connect bunq: set `BUNQ_API_KEY` in your `.env` file.")
        if not (status.get("IBKR_FLEX_TOKEN") and status.get("IBKR_FLEX_QUERY_ID")):
            st.info(
                "To connect IBKR: set `IBKR_FLEX_TOKEN` and `IBKR_FLEX_QUERY_ID` in `.env`. "
                "See README for how to generate these in Client Portal."
            )

    st.divider()

    # ── FX buttons ────────────────────────────────────────────────────────────
    st.subheader("💱 FX Rates")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("🔄 Refresh FX (recent)", help="Fetch today's and last 90 days"):
            with st.spinner("Fetching ECB reference rates..."):
                try:
                    n = fx.refresh_recent()
                    st.success(f"✅ {n} FX rate(s) updated")
                except Exception as e:
                    st.error(f"FX refresh failed: {e}")
    with col2:
        if st.button("📥 Backfill FX history", help="Download full ECB history (since 1999) — one-time"):
            with st.spinner("Downloading ECB history (~3 MB zip)…"):
                try:
                    n = fx.backfill_history()
                    st.success(f"✅ {n} historical FX rates imported")
                except Exception as e:
                    st.error(f"FX backfill failed: {e}")

    with get_session() as session:
        n_rates = repo.fx_rate_count(session)
    st.caption(f"Stored FX rates: {n_rates:,}")

    st.divider()

    # ── bunq ─────────────────────────────────────────────────────────────────
    st.subheader("🏦 bunq")
    if st.button("🔄 Sync bunq accounts", type="primary", disabled=not config.bunq_api_key()):
        _sync_bunq()
    if not config.bunq_api_key():
        st.warning("Set `BUNQ_API_KEY` in `.env` to enable bunq sync.")

    st.divider()

    # ── IBKR ─────────────────────────────────────────────────────────────────
    st.subheader("📈 Interactive Brokers")
    ibkr_ready = bool(config.ibkr_flex_token() and config.ibkr_flex_query_id())
    col3, col4 = st.columns(2)
    with col3:
        if st.button("🔄 Sync IBKR (latest)", type="primary", disabled=not ibkr_ready):
            _sync_ibkr()
    with col4:
        if st.button("📥 Backfill IBKR NAV history", disabled=not ibkr_ready,
                     help="Fetches a historical NAV series from the Flex Query"):
            _sync_ibkr(backfill=True)
    if not ibkr_ready:
        st.warning("Set `IBKR_FLEX_TOKEN` and `IBKR_FLEX_QUERY_ID` in `.env` to enable IBKR sync.")

    st.divider()

    # ── Sync log ─────────────────────────────────────────────────────────────
    st.subheader("📋 Recent Sync Log")
    with get_session() as session:
        logs = repo.recent_sync_log(session, limit=20)
        if logs:
            import pandas as pd
            log_rows = []
            for entry in logs:
                duration = ""
                if entry.finished_at and entry.started_at:
                    secs = (entry.finished_at - entry.started_at).total_seconds()
                    duration = f"{secs:.1f}s"
                log_rows.append(
                    {
                        "Connector": entry.connector,
                        "Status": entry.status,
                        "Started": entry.started_at.strftime("%Y-%m-%d %H:%M:%S"),
                        "Duration": duration,
                        "Message": (entry.message or "")[:100],
                    }
                )
            st.dataframe(pd.DataFrame(log_rows), use_container_width=True, hide_index=True)
        else:
            st.info("No sync history yet.")


# ── bunq sync logic ───────────────────────────────────────────────────────────
def _sync_bunq():
    from cashz.connectors.bunq import fetch_balances

    with get_session() as session:
        log_entry = repo.log_sync_start(session, "bunq")

    with st.spinner("Connecting to bunq…"):
        result = fetch_balances()

    if not result.success:
        with get_session() as session:
            repo.log_sync_finish(session, log_entry, "error", result.error)
        st.error(f"bunq sync failed: {result.error}")
        return

    # Match bunq accounts to our accounts table
    today = datetime.date.today()
    saved = 0
    warnings = []
    with get_session() as session:
        # Look up our two bunq account records
        bunq_accounts: list[Account] = [
            a for a in repo.all_accounts(session) if a.connector == "bunq"
        ]

        for bal in result.balances:
            # Match by external_id first, then by subtype if external_id not yet set
            matched: Optional[Account] = None
            for acc in bunq_accounts:
                if acc.external_id and acc.external_id == bal.external_id:
                    matched = acc
                    break

            if matched is None:
                # Auto-assign by subtype: Bank→checking, Savings→savings
                for acc in bunq_accounts:
                    if acc.external_id:
                        continue
                    if "savings" in acc.key.lower() and "Savings" in bal.subtype:
                        matched = acc
                    elif "checking" in acc.key.lower() and "Bank" in bal.subtype:
                        matched = acc
                    elif "joint" in acc.key.lower() and "Joint" in bal.subtype:
                        matched = acc
                    if matched:
                        # Persist the external_id for next time
                        repo.update_account_external_id(session, matched.id, bal.external_id)
                        break

            if matched is None:
                warnings.append(
                    f"Could not map bunq account '{bal.name}' (id={bal.external_id}, "
                    f"type={bal.subtype}) to a Cashz account — add it via manual entry if needed."
                )
                continue

            try:
                bal_eur, rate, rate_date = fx.to_eur(bal.balance, bal.currency, today)
            except Exception as e:
                warnings.append(f"{matched.name}: FX conversion failed — {e}")
                continue

            repo.upsert_snapshot(
                session,
                account_id=matched.id,
                as_of=today,
                balance_original=bal.balance,
                currency=bal.currency,
                fx_rate=rate,
                fx_rate_date=rate_date,
                balance_eur=bal_eur,
                source="api",
                note=f"bunq sync {today}",
            )
            saved += 1

    for w in warnings:
        st.warning(w)

    msg = f"Synced {saved} bunq account(s)"
    with get_session() as session:
        repo.log_sync_finish(session, log_entry, "ok", msg)
    st.success(f"✅ {msg}")
    st.rerun()


# ── IBKR sync logic ───────────────────────────────────────────────────────────
def _sync_ibkr(backfill: bool = False):
    from cashz.connectors.ibkr import fetch, parse_statement, fetch_statement
    from cashz.models import Position

    with get_session() as session:
        log_entry = repo.log_sync_start(session, "ibkr")

    with st.spinner("Fetching IBKR Flex statement…"):
        result = fetch()

    if not result.success:
        with get_session() as session:
            repo.log_sync_finish(session, log_entry, "error", result.error)
        st.error(f"IBKR sync failed: {result.error}")
        if "token expired" in (result.error or "").lower() or "1012" in (result.error or ""):
            st.info(
                "To regenerate: Client Portal → Performance & Reports → Flex Queries → "
                "Flex Web Service Configuration → Generate A New Token"
            )
        return

    with get_session() as session:
        acc = repo.account_by_key(session, "ibkr_portfolio")
        if acc is None:
            st.error("IBKR account not found in database")
            return

        report_date = result.report_date or datetime.date.today()

        # NAV snapshot
        if result.nav is not None:
            base_ccy = result.base_currency or "USD"
            try:
                nav_eur, rate, rate_date = fx.to_eur(result.nav, base_ccy, report_date)
            except Exception as e:
                st.warning(f"Could not convert IBKR NAV to EUR: {e}. Storing as-is.")
                nav_eur, rate, rate_date = result.nav, Decimal("1"), report_date

            repo.upsert_snapshot(
                session,
                account_id=acc.id,
                as_of=report_date,
                balance_original=result.nav,
                currency=base_ccy,
                fx_rate=rate,
                fx_rate_date=rate_date,
                balance_eur=nav_eur,
                source="api",
                note=f"IBKR Flex {result.message}",
            )

        # Positions
        for pos in result.positions:
            value_eur = pos.value_eur
            if value_eur is not None and (result.base_currency or "USD") != "EUR":
                # pos.value_eur is in base currency; convert to EUR
                try:
                    value_eur, _, _ = fx.to_eur(value_eur, result.base_currency or "USD", report_date)
                except Exception:
                    pass

            p = Position(
                account_id=acc.id,
                as_of=report_date,
                symbol=pos.symbol,
                description=pos.description,
                isin=pos.isin,
                conid=pos.conid,
                asset_category=pos.asset_category,
                quantity=pos.quantity,
                mark_price=pos.mark_price,
                position_value=pos.position_value,
                currency=pos.currency,
                fx_rate_to_base=pos.fx_rate_to_base,
                value_eur=value_eur,
            )
            repo.upsert_position(session, p)

        session.commit()

    msg = result.message or "ok"
    with get_session() as session:
        repo.log_sync_finish(session, log_entry, "ok", msg)
    st.success(f"✅ IBKR sync complete: {msg}")
    st.rerun()


# Streamlit executes this file top-to-bottom; call the page function.
show()
