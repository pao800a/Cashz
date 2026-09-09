"""Reports page — Markdown and PDF report generation."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Optional

import streamlit as st

from cashz.storage.db import get_session
from cashz.storage import repo
from cashz import networth as nw, config


def show():
    st.title("📄 Reports")
    st.write(
        "Generate a snapshot or period-over-period report as Markdown and PDF. "
        "Reports are archived in `data/reports/`."
    )

    report_type = st.radio(
        "Report type",
        ["Point-in-time snapshot", "Period comparison (from → to)"],
        horizontal=True,
    )

    col1, col2 = st.columns(2)
    today = datetime.date.today()

    if report_type == "Point-in-time snapshot":
        with col1:
            as_of = st.date_input("As of", value=today)
        prev_date = None
    else:
        with col1:
            prev_date = st.date_input("From (prior date)", value=today.replace(day=1))
        with col2:
            as_of = st.date_input("To (report date)", value=today)

    title = st.text_input(
        "Report title",
        value=f"Cashz Wealth Report — {as_of.isoformat()}",
    )

    if st.button("📊 Generate Report", type="primary"):
        _generate(as_of, prev_date, title)


def _generate(
    as_of: datetime.date,
    prev_date: Optional[datetime.date],
    title: str,
):
    with st.spinner("Building report…"):
        with get_session() as session:
            total = nw.total_net_worth(session, as_of)
            by_category = nw.by_category(session, as_of)
            account_rows = nw.by_account(session, as_of)

            prev_total: Optional[Decimal] = None
            if prev_date:
                prev_total = nw.total_net_worth(session, prev_date)

            trend_data = repo.net_worth_trend(session, since=_since_12m(as_of))

    # Charts
    from cashz.reports import charts
    trend_chart = charts.net_worth_trend(trend_data)
    alloc_chart = charts.allocation_donut(by_category)

    # Markdown
    from cashz.reports import markdown as md_gen
    md_text = md_gen.generate(
        as_of=as_of,
        total=total,
        by_category=by_category,
        account_rows=account_rows,
        prev_total=prev_total,
        prev_date=prev_date,
        title=title,
    )

    # PDF
    from cashz.reports import pdf as pdf_gen
    pdf_bytes = pdf_gen.generate(
        as_of=as_of,
        total=total,
        by_category=by_category,
        account_rows=account_rows,
        trend_chart=trend_chart,
        allocation_chart=alloc_chart,
        prev_total=prev_total,
        prev_date=prev_date,
        title=title,
    )

    # Archive
    ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    slug = as_of.isoformat()
    reports_dir = config.reports_dir()
    md_path = reports_dir / f"cashz_{slug}_{ts}.md"
    pdf_path = reports_dir / f"cashz_{slug}_{ts}.pdf"
    md_path.write_text(md_text, encoding="utf-8")
    pdf_path.write_bytes(pdf_bytes)

    # Display
    st.success(f"✅ Report generated and saved to `data/reports/`")

    st.subheader("Markdown Preview")
    st.markdown(md_text)

    col_md, col_pdf = st.columns(2)
    with col_md:
        st.download_button(
            "⬇️ Download Markdown",
            data=md_text.encode("utf-8"),
            file_name=md_path.name,
            mime="text/markdown",
        )
    with col_pdf:
        st.download_button(
            "⬇️ Download PDF",
            data=pdf_bytes,
            file_name=pdf_path.name,
            mime="application/pdf",
        )


def _since_12m(as_of: datetime.date) -> datetime.date:
    return as_of.replace(year=as_of.year - 1)


# Streamlit executes this file top-to-bottom; call the page function.
show()
