"""Dashboard page — total net worth, trends, and account breakdown."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Optional

import pandas as pd
import plotly.express as px
import streamlit as st

from cashz.storage.db import get_session
from cashz.storage import repo
from cashz import networth as nw


def show():
    st.title("📊 Dashboard")

    with get_session() as session:
        today = datetime.date.today()

        # ── KPI row ───────────────────────────────────────────────────────────
        total = nw.total_net_worth(session, today)
        delta_30 = nw.delta(session, today, days_back=30)
        delta_qtd = nw.qtd_delta(session, today)
        delta_ytd = nw.ytd_delta(session, today)
        delta_1yr = nw.delta_1yr(session, today)

        col1, col2 = st.columns([2, 1])
        with col1:
            st.metric("Total Net Worth", f"€{total:,.0f}")
        with col2:
            since_data = _earliest_snapshot_date(session)
            st.metric("Tracking since", since_data.isoformat() if since_data else "—")

        # ── 4 change banners ─────────────────────────────────────────────────
        dc1, dc2, dc3, dc4 = st.columns(4)
        with dc1:
            st.metric("30 days", "", delta=_fmt_delta(delta_30), delta_color="normal")
        with dc2:
            st.metric("QTD", "", delta=_fmt_delta(delta_qtd), delta_color="normal")
        with dc3:
            st.metric("YTD", "", delta=_fmt_delta(delta_ytd), delta_color="normal")
        with dc4:
            st.metric("1 year", "", delta=_fmt_delta(delta_1yr), delta_color="normal")

        st.divider()

        # ── Trend chart ───────────────────────────────────────────────────────
        st.subheader("Net Worth Over Time")
        trend_range = st.selectbox(
            "Show",
            ["All time", "This year", "Last 12 months", "Last 3 months"],
            label_visibility="collapsed",
        )
        since = _since_date(trend_range, today)
        trend_data = repo.net_worth_trend(session, since=since)

        if trend_data:
            chart_mode = st.radio(
                "View",
                ["Total", "By category", "By account"],
                horizontal=True,
                label_visibility="collapsed",
            )

            if chart_mode == "Total":
                df = pd.DataFrame(trend_data, columns=["date", "net_worth"])
                df["date"] = pd.to_datetime(df["date"])
                df["net_worth"] = df["net_worth"].astype(float)
                fig = px.line(
                    df, x="date", y="net_worth",
                    labels={"date": "", "net_worth": "Net Worth (EUR)"},
                    color_discrete_sequence=["#4C72B0"],
                )
                fig.update_layout(
                    yaxis_tickformat="€,.0f",
                    hovermode="x unified",
                    margin=dict(t=20, b=20),
                    plot_bgcolor="white",
                )
                fig.update_traces(fill="tozeroy", fillcolor="rgba(76,114,176,0.10)")
                st.plotly_chart(fig, use_container_width=True)
            else:
                group_by = "category" if chart_mode == "By category" else "account"
                _stacked_area_chart(session, since, today, group_by=group_by)
        else:
            st.info("No snapshot data yet. Add balances on the **Accounts** page or run a sync.")

        st.divider()

        # ── Breakdown ─────────────────────────────────────────────────────────
        col_cat, col_acc = st.columns([1, 2])

        with col_cat:
            st.subheader("By Category")
            by_cat = nw.by_category(session, today)
            if total > 0:
                cat_df = pd.DataFrame(
                    [
                        {"Category": nw.CATEGORY_LABELS.get(k, k), "Balance": float(v)}
                        for k, v in by_cat.items() if v > 0
                    ]
                )
                fig_pie = px.pie(
                    cat_df, names="Category", values="Balance",
                    hole=0.45,
                    color_discrete_map={
                        nw.CATEGORY_LABELS["liquidity"]: "#4C72B0",
                        nw.CATEGORY_LABELS["investments"]: "#55A868",
                        nw.CATEGORY_LABELS["pension"]: "#C44E52",
                    },
                )
                fig_pie.update_traces(textinfo="percent+label")
                fig_pie.update_layout(showlegend=False, margin=dict(t=10, b=10))
                st.plotly_chart(fig_pie, use_container_width=True)

                for cat in ["liquidity", "investments", "pension"]:
                    val = by_cat.get(cat, Decimal("0"))
                    label = nw.CATEGORY_LABELS.get(cat, cat)
                    pct = float(val / total * 100) if total else 0
                    st.write(f"**{label}**: €{float(val):,.0f} ({pct:.1f}%)")
            else:
                st.info("No data yet.")

        with col_acc:
            st.subheader("By Account")
            accounts_data = nw.by_account(session, today)
            rows = []
            for item in accounts_data:
                acc = item["account"]
                staleness = item["staleness_days"]
                badge = ""
                if staleness is not None:
                    if staleness == 0:
                        badge = "🟢"
                    elif staleness <= 7:
                        badge = "🟡"
                    elif staleness <= 30:
                        badge = "🟠"
                    else:
                        badge = "🔴"
                rows.append(
                    {
                        "": badge,
                        "Account": acc.name,
                        "Category": nw.CATEGORY_LABELS.get(acc.category, acc.category),
                        "Balance": f"€{float(item['balance_eur']):,.2f}",
                        "As Of": item["as_of"].isoformat() if item["as_of"] else "—",
                        "Source": item["source"] or "—",
                    }
                )
            if rows:
                acc_df = pd.DataFrame(rows)
                st.dataframe(acc_df, use_container_width=True, hide_index=True)
            else:
                st.info("No account data yet.")

        # ── IBKR holdings breakdown ───────────────────────────────────────────
        _ibkr_holdings_section(session)


def _stacked_area_chart(session, since, today, group_by: str = "category"):
    """Stacked area chart with carry-forward so there are no gaps between snapshots.

    group_by="category" → one area per category (liquidity / investments / pension)
    group_by="account"  → one area per account
    """
    accounts = repo.all_accounts(session)

    # 1. Collect all snapshot dates that fall in range
    account_snaps: dict[int, dict[datetime.date, float]] = {}
    all_dates_set: set[datetime.date] = set()

    for acc in accounts:
        snaps = repo.snapshots_for_account(session, acc.id, since=since)
        if snaps:
            account_snaps[acc.id] = {s.as_of: float(s.balance_eur) for s in snaps}
            all_dates_set.update(account_snaps[acc.id].keys())

    if not all_dates_set:
        st.info("No snapshot data yet.")
        return

    all_dates = sorted(all_dates_set)

    # 2. Seed carry-forward with the last known value before the window starts
    #    so accounts not updated recently don't appear as zero at the left edge.
    carried: dict[int, float] = {}
    if since:
        for acc in accounts:
            snap = _latest_before(session, acc.id, since)
            if snap:
                carried[acc.id] = float(snap.balance_eur)

    # 3. Walk every date; carry forward each account's balance and emit one row per series
    rows = []
    for d in all_dates:
        # Update carry-forward for any account that has a snapshot on this date
        for acc in accounts:
            if acc.id in account_snaps and d in account_snaps[acc.id]:
                carried[acc.id] = account_snaps[acc.id][d]

        if group_by == "account":
            for acc in accounts:
                val = carried.get(acc.id, 0.0)
                if val > 0:
                    rows.append({"Date": pd.Timestamp(d), "Balance": val, "Series": acc.name})
        else:
            cat_totals: dict[str, float] = {}
            for acc in accounts:
                val = carried.get(acc.id, 0.0)
                label = nw.CATEGORY_LABELS.get(acc.category, acc.category)
                cat_totals[label] = cat_totals.get(label, 0.0) + val
            for label, val in cat_totals.items():
                if val > 0:
                    rows.append({"Date": pd.Timestamp(d), "Balance": val, "Series": label})

    if not rows:
        st.info("No data for the selected period.")
        return

    df = pd.DataFrame(rows)

    color_map = (
        {
            nw.CATEGORY_LABELS["liquidity"]: "#4C72B0",
            nw.CATEGORY_LABELS["investments"]: "#55A868",
            nw.CATEGORY_LABELS["pension"]: "#C44E52",
        }
        if group_by == "category"
        else None
    )

    fig = px.area(
        df, x="Date", y="Balance", color="Series",
        color_discrete_map=color_map,
        labels={"Balance": "Balance (EUR)", "Series": ""},
    )
    fig.update_layout(
        yaxis_tickformat="€,.0f",
        hovermode="x unified",
        margin=dict(t=20, b=20),
        plot_bgcolor="white",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
    )
    st.plotly_chart(fig, use_container_width=True)


def _ibkr_holdings_section(session):
    from cashz.storage.repo import latest_positions, account_by_key
    acc = account_by_key(session, "ibkr_portfolio")
    if acc is None:
        return
    positions = latest_positions(session, acc.id)
    if not positions:
        return

    st.divider()
    st.subheader("🏦 IBKR Holdings Breakdown")

    # By asset class
    by_class: dict[str, float] = {}
    for p in positions:
        cat = (p.asset_category or "Other").upper()
        by_class[cat] = by_class.get(cat, 0.0) + (float(p.value_eur) if p.value_eur else 0.0)

    col1, col2 = st.columns([1, 2])
    with col1:
        if by_class:
            df_class = pd.DataFrame(list(by_class.items()), columns=["Asset Class", "Value (EUR)"])
            fig = px.pie(df_class, names="Asset Class", values="Value (EUR)", hole=0.4)
            fig.update_layout(margin=dict(t=10, b=10))
            st.plotly_chart(fig, use_container_width=True)

    with col2:
        st.markdown("**Top positions**")
        rows = [
            {
                "Symbol": p.symbol,
                "Description": (p.description or "")[:40],
                "Asset": p.asset_category or "",
                "Value (EUR)": f"€{float(p.value_eur or 0):,.0f}",
                "Currency": p.currency,
            }
            for p in positions[:20]
        ]
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def _fmt_delta(val: Optional[Decimal]) -> Optional[str]:
    """Format a delta value for st.metric.

    Streamlit colours the delta red when the string starts with '-' and green
    otherwise.  We must put the minus sign BEFORE the currency symbol, not
    after it (the default f-string would produce '€-1,234').
    """
    if val is None:
        return None
    abs_eur = f"€{abs(float(val)):,.0f}"
    return f"-{abs_eur}" if val < 0 else f"+{abs_eur}"


def _earliest_snapshot_date(session) -> Optional[datetime.date]:
    from sqlalchemy import select, func
    from cashz.models import Snapshot
    return session.scalar(select(func.min(Snapshot.as_of)))


def _latest_before(session, account_id, as_of):
    from sqlalchemy import select
    from cashz.models import Snapshot
    return session.scalars(
        select(Snapshot)
        .where(Snapshot.account_id == account_id, Snapshot.as_of <= as_of)
        .order_by(Snapshot.as_of.desc())
        .limit(1)
    ).first()


def _since_date(label: str, today: datetime.date) -> Optional[datetime.date]:
    if label == "This year":
        return datetime.date(today.year, 1, 1)
    elif label == "Last 12 months":
        return today.replace(year=today.year - 1)
    elif label == "Last 3 months":
        # approximate
        m = today.month - 3
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        return datetime.date(y, m, today.day)
    return None


# Streamlit executes this file top-to-bottom; call the page function.
show()
