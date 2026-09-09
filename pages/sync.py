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

    # Show any warnings that were stored before the last st.rerun()
    for _w in st.session_state.pop("bunq_sync_warnings", []):
        st.warning(_w)

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
    bunq_ready = bool(config.bunq_api_key())
    col_b1, col_b2 = st.columns(2)
    with col_b1:
        if st.button("🔄 Sync bunq accounts", type="primary", disabled=not bunq_ready):
            _sync_bunq()
    with col_b2:
        if st.button("📥 Backfill bunq history", disabled=not bunq_ready,
                     help="Reconstruct balance history from payment transactions (up to 24 months)"):
            _backfill_bunq()
    if not bunq_ready:
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
    _SAVINGS_SUBTYPES = {"MonetaryAccountSavings"}
    _ARCHIVED_SUBTYPE = "MonetaryAccountArchived"
    with get_session() as session:
        bunq_accounts: list[Account] = [
            a for a in repo.all_accounts(session) if a.connector == "bunq"
        ]

        for bal in result.balances:
            # 1) Match by persisted external_id (subsequent syncs)
            matched: Optional[Account] = None
            for acc in bunq_accounts:
                if acc.external_id and acc.external_id == bal.external_id:
                    matched = acc
                    break

            if matched is None:
                # 2) Type-based assignment — intentionally ignores existing external_id
                #    so stale assignments from failed syncs are corrected.
                for acc in bunq_accounts:
                    if bal.subtype in _SAVINGS_SUBTYPES and "savings" in acc.key.lower():
                        matched = acc
                        break
                    elif bal.subtype == _ARCHIVED_SUBTYPE and "archived" in acc.key.lower():
                        matched = acc
                        break
                    elif bal.subtype not in _SAVINGS_SUBTYPES | {_ARCHIVED_SUBTYPE} and (
                        "checking" in acc.key.lower() or "joint" in acc.key.lower()
                    ):
                        matched = acc
                        break
                if matched is None and bunq_accounts:
                    matched = bunq_accounts[0]

                if matched:
                    repo.update_account_external_id(session, matched.id, bal.external_id)

            if matched is None:
                warnings.append(
                    f"Could not map bunq account '{bal.name}' (id={bal.external_id}, "
                    f"type={bal.subtype}) to a Cashz account."
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

    # Persist any warnings through the rerun so the user can read them
    if warnings:
        st.session_state["bunq_sync_warnings"] = warnings

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
        base_ccy = result.base_currency or "USD"

        if backfill and result.nav_history:
            # Write one snapshot per day in the historical series
            saved_hist = 0
            skipped_hist = 0
            for hist_date, hist_nav in result.nav_history:
                try:
                    hist_eur, rate, rate_date = fx.to_eur(hist_nav, base_ccy, hist_date)
                except Exception:
                    skipped_hist += 1
                    continue
                repo.upsert_snapshot(
                    session,
                    account_id=acc.id,
                    as_of=hist_date,
                    balance_original=hist_nav,
                    currency=base_ccy,
                    fx_rate=rate,
                    fx_rate_date=rate_date,
                    balance_eur=hist_eur,
                    source="api",
                    note="IBKR Flex history backfill",
                )
                saved_hist += 1
            if skipped_hist:
                st.warning(
                    f"{skipped_hist} historical NAV rows skipped (FX rate unavailable). "
                    "Run 'Backfill FX history' first then retry."
                )
            msg = f"Backfilled {saved_hist} IBKR NAV snapshots"

        else:
            # Regular sync: latest NAV snapshot + positions
            if result.nav is not None:
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

            for pos in result.positions:
                value_eur = pos.value_eur
                if value_eur is not None and base_ccy != "EUR":
                    try:
                        value_eur, _, _ = fx.to_eur(value_eur, base_ccy, report_date)
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


def _backfill_bunq():
    from cashz.connectors.bunq import fetch_history

    with get_session() as session:
        log_entry = repo.log_sync_start(session, "bunq")

    with st.spinner("Fetching bunq payment history (up to 24 months)…"):
        result = fetch_history(max_months=24)

    if not result.success:
        with get_session() as session:
            repo.log_sync_finish(session, log_entry, "error", result.error)
        st.error(f"bunq backfill failed: {result.error}")
        return

    # ── Diagnostic breakdown ─────────────────────────────────────────────────
    n_active_pts = sum(1 for _, eid, _, _ in result.balance_history if eid != "__archived__")
    n_archived_pts = sum(1 for _, eid, _, _ in result.balance_history if eid == "__archived__")
    st.info(
        f"Payment history fetched: **{n_active_pts}** active account data points, "
        f"**{n_archived_pts}** archived account data points."
    )

    if not result.balance_history:
        with get_session() as session:
            repo.log_sync_finish(session, log_entry, "ok", "No payment history found")
        st.info("No payment history found — the accounts may have no transactions yet.")
        return

    today = datetime.date.today()
    saved = 0
    skipped = 0
    skipped_no_map = 0
    warnings: list[str] = []

    with get_session() as session:
        bunq_accounts = [a for a in repo.all_accounts(session) if a.connector == "bunq"]
        # Build external_id → Account mapping (requires regular sync to have run first)
        ext_id_map: dict[str, Account] = {
            a.external_id: a for a in bunq_accounts if a.external_id
        }
        if not ext_id_map:
            with get_session() as session2:
                repo.log_sync_finish(session2, log_entry, "error", "No external_id mappings")
            st.error(
                "No bunq account mappings found. Run **Sync bunq accounts** first so "
                "Cashz knows which bunq monetary account belongs to which Cashz account."
            )
            return

        unmapped_ids: set[str] = set()
        for (pay_date, ext_id, balance, currency) in result.balance_history:
            acc = ext_id_map.get(ext_id)
            if acc is None:
                unmapped_ids.add(ext_id)
                skipped_no_map += 1
                continue

            try:
                bal_eur, rate, rate_date = fx.to_eur(balance, currency, pay_date)
            except Exception as e:
                skipped += 1
                continue

            repo.upsert_snapshot(
                session,
                account_id=acc.id,
                as_of=pay_date,
                balance_original=balance,
                currency=currency,
                fx_rate=rate,
                fx_rate_date=rate_date,
                balance_eur=bal_eur,
                source="api",
                note="bunq payment history backfill",
            )
            saved += 1

    if skipped:
        warnings.append(
            f"{skipped} data points skipped (FX rate unavailable). "
            "Run 'Backfill FX history' first then retry for full coverage."
        )
    if skipped_no_map and unmapped_ids:
        warnings.append(
            f"{skipped_no_map} data points skipped — no Cashz account mapped to "
            f"bunq id(s): {', '.join(sorted(unmapped_ids))}. "
            "Run 'Sync bunq accounts' first to create the mapping."
        )
    if n_archived_pts == 0:
        warnings.append(
            "No payment history found for archived bunq accounts. "
            "Possible causes: (1) the account was closed more than ~6 years ago; "
            "(2) bunq does not expose payment history for cancelled accounts via the API."
        )
    if warnings:
        st.session_state["bunq_sync_warnings"] = warnings

    msg = f"Backfilled {saved} bunq balance snapshots"
    with get_session() as session:
        repo.log_sync_finish(session, log_entry, "ok", msg)
    st.success(f"✅ {msg}")
    st.rerun()


# Streamlit executes this file top-to-bottom; call the page function.
show()
