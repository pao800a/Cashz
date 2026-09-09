"""Accounts page — manual entries, CSV import, TFR estimator."""

from __future__ import annotations

import datetime
import io
from decimal import Decimal, InvalidOperation
from typing import Optional

import pandas as pd
import streamlit as st

from cashz.storage.db import get_session
from cashz.storage import repo
from cashz import fx, tfr
from cashz.connectors.csv_import import parse_csv, CsvImportError
from cashz.models import Account


def show():
    st.title("📒 Accounts")

    tab_list, tab_manual, tab_csv, tab_tfr = st.tabs(
        ["All Accounts", "Add / Edit Balance", "CSV Import", "TFR Estimator"]
    )

    with tab_list:
        _accounts_list()
    with tab_manual:
        _manual_entry()
    with tab_csv:
        _csv_import()
    with tab_tfr:
        _tfr_panel()


# ── All Accounts ──────────────────────────────────────────────────────────────
def _accounts_list():
    st.subheader("All Accounts")
    with get_session() as session:
        pairs = repo.latest_per_account(session)
        rows = []
        today = datetime.date.today()
        for acc, snap in pairs:
            staleness = (today - snap.as_of).days if snap else None
            rows.append(
                {
                    "Account": acc.name,
                    "Institution": acc.institution,
                    "Category": acc.category,
                    "Balance (EUR)": f"€{float(snap.balance_eur):,.2f}" if snap else "—",
                    "As Of": snap.as_of.isoformat() if snap else "—",
                    "Source": snap.source if snap else "—",
                    "Staleness (d)": staleness if staleness is not None else "—",
                }
            )
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # Per-account history expander
    with get_session() as session:
        accounts = repo.all_accounts(session)
        for acc in accounts:
            snaps = repo.snapshots_for_account(session, acc.id)
            if not snaps:
                continue
            with st.expander(f"{acc.name} — {len(snaps)} snapshot(s)"):
                rows_h = [
                    {
                        "Date": s.as_of.isoformat(),
                        "Balance (orig)": f"{float(s.balance_original):,.2f} {s.currency}",
                        "Balance (EUR)": f"€{float(s.balance_eur):,.2f}",
                        "FX Rate": str(s.fx_rate),
                        "Source": s.source,
                        "Note": s.note or "",
                    }
                    for s in sorted(snaps, key=lambda x: x.as_of, reverse=True)
                ]
                st.dataframe(pd.DataFrame(rows_h), use_container_width=True, hide_index=True)


# ── Manual entry ──────────────────────────────────────────────────────────────
def _manual_entry():
    st.subheader("Add / Edit a Balance Snapshot")
    st.write(
        "Enter the balance as reported by your institution. "
        "EUR balances are stored as-is; other currencies are converted automatically."
    )

    with get_session() as session:
        accounts = repo.all_accounts(session)
        acc_names = [f"{a.name} ({a.institution})" for a in accounts]
        acc_map = {f"{a.name} ({a.institution})": a for a in accounts}

    selected_label = st.selectbox("Account", acc_names)
    selected_acc: Account = acc_map[selected_label]

    with st.form("manual_entry"):
        col_date, col_bal, col_ccy = st.columns([2, 2, 1])
        with col_date:
            as_of = st.date_input("Date", value=datetime.date.today())
        with col_bal:
            balance_str = st.text_input("Balance", placeholder="e.g. 12345.67")
        with col_ccy:
            ccy = st.text_input("Currency", value=selected_acc.currency, max_chars=3).upper()
        note = st.text_input("Note (optional)", placeholder="e.g. INPS statement August 2026")
        submitted = st.form_submit_button("Save Snapshot")

    if submitted:
        try:
            balance = Decimal(balance_str.strip().replace(",", ""))
        except (InvalidOperation, AttributeError):
            st.error("Invalid balance — enter a number like 12345.67")
            return

        with get_session() as session:
            _save_snapshot(session, selected_acc, as_of, balance, ccy, note, source="manual")
            st.success(f"✅ Saved {ccy} {balance:,.2f} for **{selected_acc.name}** on {as_of}")


def _save_snapshot(session, acc: Account, as_of, balance: Decimal, ccy: str, note, source: str):
    if not fx.has_rates(session) and ccy != "EUR":
        st.warning("⚠️ FX rates not loaded. Go to **Sync** → Refresh FX first.")
    try:
        bal_eur, rate, rate_date = fx.to_eur(balance, ccy, as_of, session)
    except fx.FxRateMissing:
        st.error(f"No FX rate found for {ccy}. Is it a supported ECB currency?")
        return
    except fx.FxRateStale as e:
        st.warning(str(e))
        return
    except Exception as e:
        st.error(str(e))
        return

    repo.upsert_snapshot(
        session,
        account_id=acc.id,
        as_of=as_of,
        balance_original=balance,
        currency=ccy,
        fx_rate=rate,
        fx_rate_date=rate_date,
        balance_eur=bal_eur,
        source=source,
        note=note or None,
    )
    session.commit()


# ── CSV Import ────────────────────────────────────────────────────────────────
def _csv_import():
    st.subheader("Bulk Import from CSV")
    st.write(
        "Upload a CSV file with `date` and `balance` columns (and optionally `currency`, `note`). "
        "Useful for loading Fineco export files or backfilling any account's history."
    )

    with get_session() as session:
        accounts = repo.all_accounts(session)
        acc_names = [f"{a.name} ({a.institution})" for a in accounts]
        acc_map = {f"{a.name} ({a.institution})": a for a in accounts}

    selected_label = st.selectbox("Target Account", acc_names, key="csv_account")
    selected_acc: Account = acc_map[selected_label]

    uploaded = st.file_uploader("Upload CSV", type=["csv", "txt"])
    if uploaded is None:
        st.caption("Expected columns: `date`, `balance` (required) · `currency`, `note` (optional)")
        return

    content = uploaded.read()
    try:
        rows, warnings = parse_csv(content, default_currency=selected_acc.currency)
    except CsvImportError as e:
        st.error(f"CSV error: {e}")
        return

    for w in warnings:
        st.warning(w)

    if not rows:
        st.error("No valid rows found in the CSV.")
        return

    # Preview
    preview_df = pd.DataFrame(
        [
            {
                "Date": r.as_of.isoformat(),
                "Balance": float(r.balance),
                "Currency": r.currency or selected_acc.currency,
                "Note": r.note or "",
            }
            for r in rows
        ]
    )
    st.write(f"**{len(rows)} row(s) found. Preview:**")
    st.dataframe(preview_df, use_container_width=True, hide_index=True)

    if st.button("Import into Database", type="primary"):
        saved = 0
        errors: list[str] = []
        with get_session() as session:
            for r in rows:
                ccy = r.currency or selected_acc.currency
                try:
                    _save_snapshot(session, selected_acc, r.as_of, r.balance, ccy, r.note, "csv")
                    saved += 1
                except Exception as e:
                    errors.append(f"{r.as_of}: {e}")
        st.success(f"✅ Imported {saved} snapshot(s) for **{selected_acc.name}**")
        for err in errors:
            st.error(err)
        st.rerun()


# ── TFR Estimator ─────────────────────────────────────────────────────────────
def _tfr_panel():
    st.subheader("TFR Estimator (INPS / TFR account)")
    st.write(
        "Enter your monthly **retribuzione utile** (art. 2120 c.c.) — all regular pay "
        "including 13ᵃ/14ᵃ in the months paid. The app computes the accrued TFR balance "
        "using the official ISTAT FOI coefficients."
    )

    with st.expander("ℹ️ What is retribuzione utile?"):
        st.write(
            "Include: base pay, fixed allowances, 13ᵃ/14ᵃ salary (in the month paid), "
            "continuative shift allowances, fringe benefits. "
            "Exclude: occasional overtime, expense reimbursements, one-off bonuses."
        )

    net_of_tax = st.checkbox(
        "Show net of 17% imposta sostitutiva (matches payslip 'TFR netto')",
        value=True,
    )

    latest_foi = tfr.latest_foi_month()
    st.caption(
        f"FOI table covers up to {latest_foi[0]}-{latest_foi[1]:02d}. "
        "To extend, add new entries to `cashz/tfr.py` (see source URL in that file)."
    )

    # Load existing salary months
    with get_session() as session:
        existing = repo.get_salary_months(session)
        existing_map = {(s.year, s.month): s for s in existing}

    # Build an editable grid from Oct 2023 to Jun 2026
    start = datetime.date(2023, 10, 1)
    end = datetime.date(2026, 6, 30)
    months = []
    d = start
    while d <= end:
        months.append((d.year, d.month))
        if d.month == 12:
            d = datetime.date(d.year + 1, 1, 1)
        else:
            d = datetime.date(d.year, d.month + 1, 1)

    grid_data = []
    for y, m in months:
        existing_row = existing_map.get((y, m))
        grid_data.append(
            {
                "Year": y,
                "Month": m,
                "Retribuzione Utile (€)": float(existing_row.retribuzione_utile) if existing_row else 0.0,
                "Imponibile Prev. (€, blank=same)": float(existing_row.imponibile_previdenziale)
                if (existing_row and existing_row.imponibile_previdenziale is not None)
                else "",
            }
        )

    st.write("**Monthly salary grid** (edit cells, then click Save):")
    edited = st.data_editor(
        pd.DataFrame(grid_data),
        use_container_width=True,
        num_rows="fixed",
        column_config={
            "Year": st.column_config.NumberColumn(disabled=True),
            "Month": st.column_config.NumberColumn(disabled=True),
            "Retribuzione Utile (€)": st.column_config.NumberColumn(
                min_value=0.0, format="€%.2f"
            ),
            "Imponibile Prev. (€, blank=same)": st.column_config.TextColumn(),
        },
        key="salary_grid",
    )

    if st.button("💾 Save Salary Grid and Recompute TFR", type="primary"):
        salary_rows = []
        with get_session() as session:
            repo.delete_salary_months(session)
            for _, row in edited.iterrows():
                y, m = int(row["Year"]), int(row["Month"])
                ru_val = row["Retribuzione Utile (€)"]
                if ru_val is None or (isinstance(ru_val, float) and ru_val == 0.0):
                    continue
                ru = Decimal(str(ru_val))
                rip_raw = row["Imponibile Prev. (€, blank=same)"]
                rip: Optional[Decimal] = None
                if rip_raw and str(rip_raw).strip():
                    try:
                        rip = Decimal(str(rip_raw).strip())
                    except InvalidOperation:
                        pass
                repo.upsert_salary_month(session, y, m, ru, rip)
                salary_rows.append((y, m, ru, rip))

        # Recompute and write estimate snapshots
        salary_tuples = [(y, m, ru, rip) for (y, m, ru, rip) in salary_rows]
        if salary_tuples:
            estimates = tfr.estimate_balance_history(salary_tuples, net_of_tax=net_of_tax)

            with get_session() as session:
                inps_acc = repo.account_by_key(session, "inps_tfr")
                if inps_acc is None:
                    st.error("INPS/TFR account not found in the database.")
                    return

                saved = 0
                for as_of_date, balance_eur in estimates:
                    snap = repo.upsert_snapshot(
                        session,
                        account_id=inps_acc.id,
                        as_of=as_of_date,
                        balance_original=balance_eur,
                        currency="EUR",
                        fx_rate=Decimal("1"),
                        fx_rate_date=as_of_date,
                        balance_eur=balance_eur,
                        source="estimate",
                    )
                    saved += 1

            st.success(
                f"✅ Saved {len(salary_rows)} salary months, "
                f"computed {saved} TFR snapshots."
            )

            # Quick chart
            if estimates:
                df_tfr = pd.DataFrame(estimates, columns=["Date", "TFR Balance"])
                df_tfr["Date"] = pd.to_datetime(df_tfr["Date"])
                df_tfr["TFR Balance"] = df_tfr["TFR Balance"].astype(float)
                import plotly.express as px
                fig = px.line(df_tfr, x="Date", y="TFR Balance",
                              labels={"TFR Balance": "Estimated TFR (€)"},
                              color_discrete_sequence=["#C44E52"])
                fig.update_layout(yaxis_tickformat="€,.0f", plot_bgcolor="white")
                st.plotly_chart(fig, use_container_width=True)
        else:
            st.warning("No salary data entered. Fill in at least one month.")

    # Note about manual override
    st.info(
        "💡 To anchor the TFR estimate to a real INPS/payslip figure: go to "
        "**Add / Edit Balance** tab, select **INPS / TFR**, and enter the actual balance. "
        "Manual snapshots are never overwritten by the estimator."
    )


# Streamlit executes this file top-to-bottom; call the page function.
show()
