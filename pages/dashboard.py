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
        delta_ytd = nw.ytd_delta(session, today)

        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric(
                "Total Net Worth",
                f"€{total:,.0f}",
                delta=f"€{delta_30:,.0f} (30 d)" if delta_30 is not None else None,
                delta_color="normal",
            )
        with col2:
            st.metric(
                "YTD Change",
                f"€{delta_ytd:,.0f}" if delta_ytd is not None else "—",
                delta=None,
            )
        with col3:
            since_data = _earliest_snapshot_date(session)
            st.metric(
                "Tracking since",
                since_data.isoformat() if since_data else "—",
            )

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
            chart_toggle = st.checkbox("Stacked by category", value=False)

            if chart_toggle:
                _stacked_area_chart(session, since, today)
            else:
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


def _stacked_area_chart(session, since, today):
    """Render a stacked area chart with per-category series."""
    accounts = repo.all_accounts(session)
    cat_series: dict[str, list[tuple[datetime.date, float]]] = {}

    for cat in ["liquidity", "investments", "pension"]:
        cat_accounts = [a for a in accounts if a.category == cat]
        if not cat_accounts:
            continue
        # Get all dates for accounts in this category
        all_dates = sorted(
            {
                s.as_of
                for a in cat_accounts
                for s in repo.snapshots_for_account(session, a.id, since=since)
            }
        )
        if not all_dates:
            continue
        points = []
        for d in all_dates:
            total_cat = sum(
                (snap.balance_eur if (snap := _latest_before(session, a.id, d)) else Decimal("0"))
                for a in cat_accounts
            )
            points.append((d, float(total_cat)))
        cat_series[nw.CATEGORY_LABELS.get(cat, cat)] = points

    if not cat_series:
        st.info("No category data yet.")
        return

    rows = []
    for label, pts in cat_series.items():
        for d, v in pts:
            rows.append({"Date": pd.Timestamp(d), "Balance": v, "Category": label})
    df = pd.DataFrame(rows)
    fig = px.area(
        df, x="Date", y="Balance", color="Category",
        color_discrete_map={
            nw.CATEGORY_LABELS["liquidity"]: "#4C72B0",
            nw.CATEGORY_LABELS["investments"]: "#55A868",
            nw.CATEGORY_LABELS["pension"]: "#C44E52",
        },
    )
    fig.update_layout(
        yaxis_tickformat="€,.0f", hovermode="x unified",
        margin=dict(t=20, b=20), plot_bgcolor="white",
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
