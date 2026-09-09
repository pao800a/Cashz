"""Matplotlib chart renderers for the PDF report.

All functions return a BytesIO object containing a PNG image.
These functions are *not* used by the Streamlit dashboard (which uses Plotly);
they exist solely to generate static images for the fpdf2 PDF report.
"""

from __future__ import annotations

import datetime
import io
from decimal import Decimal
from typing import Optional

import matplotlib
matplotlib.use("Agg")  # non-interactive backend
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

# Colour palette — neutral, accessible, consistent
_CATEGORY_COLOURS = {
    "liquidity": "#4C72B0",
    "investments": "#55A868",
    "pension": "#C44E52",
}
_TREND_COLOUR = "#4C72B0"
_GRID_COLOUR = "#E5E5E5"
_FONT_SIZE = 9


def _setup_ax(ax, title: str) -> None:
    ax.set_title(title, fontsize=_FONT_SIZE + 1, pad=6)
    ax.tick_params(labelsize=_FONT_SIZE - 1)
    ax.yaxis.grid(True, color=_GRID_COLOUR, linewidth=0.5)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)


def _eur_formatter():
    return mticker.FuncFormatter(lambda x, _: f"€{x:,.0f}")


def net_worth_trend(
    trend_data: list[tuple[datetime.date, Decimal]],
) -> io.BytesIO:
    """Line chart of total net worth over time."""
    if not trend_data:
        return _empty_chart("Net Worth Trend (no data)")

    dates = [d for d, _ in trend_data]
    values = [float(v) for _, v in trend_data]

    fig, ax = plt.subplots(figsize=(7, 3), dpi=150)
    ax.plot(dates, values, color=_TREND_COLOUR, linewidth=1.5)
    ax.fill_between(dates, values, alpha=0.12, color=_TREND_COLOUR)
    _setup_ax(ax, "Net Worth Over Time")
    ax.yaxis.set_major_formatter(_eur_formatter())
    fig.autofmt_xdate()
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf


def category_stacked_area(
    trend_by_category: dict[str, list[tuple[datetime.date, Decimal]]],
) -> io.BytesIO:
    """Stacked area chart of net worth by category over time."""
    if not trend_by_category:
        return _empty_chart("Category Breakdown (no data)")

    categories = list(trend_by_category)
    all_dates = sorted({d for cat_data in trend_by_category.values() for d, _ in cat_data})
    if not all_dates:
        return _empty_chart("Category Breakdown (no data)")

    # For each category, build a dict from date to value for easy lookup
    by_cat: dict[str, dict[datetime.date, float]] = {}
    for cat, rows in trend_by_category.items():
        by_cat[cat] = {d: float(v) for d, v in rows}

    stacks = []
    for cat in categories:
        vals = []
        for d in all_dates:
            vals.append(by_cat[cat].get(d, 0.0))
        stacks.append(vals)

    fig, ax = plt.subplots(figsize=(7, 3), dpi=150)
    colours = [_CATEGORY_COLOURS.get(cat, "#888888") for cat in categories]
    ax.stackplot(all_dates, stacks, labels=categories, colors=colours, alpha=0.85)
    ax.legend(fontsize=_FONT_SIZE - 1, loc="upper left")
    _setup_ax(ax, "Net Worth by Category")
    ax.yaxis.set_major_formatter(_eur_formatter())
    fig.autofmt_xdate()
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf


def allocation_donut(
    by_category: dict[str, Decimal],
) -> io.BytesIO:
    """Donut chart of current allocation by category."""
    filtered = {k: float(v) for k, v in by_category.items() if v > 0}
    if not filtered:
        return _empty_chart("Allocation (no data)")

    labels = list(filtered)
    values = list(filtered.values())
    colours = [_CATEGORY_COLOURS.get(k, "#888888") for k in labels]

    fig, ax = plt.subplots(figsize=(4, 3.5), dpi=150)
    wedges, texts, autotexts = ax.pie(
        values,
        labels=None,
        colors=colours,
        autopct="%1.1f%%",
        pctdistance=0.75,
        startangle=90,
        wedgeprops={"width": 0.5},  # donut
    )
    for t in autotexts:
        t.set_fontsize(_FONT_SIZE - 1)
    ax.legend(labels, fontsize=_FONT_SIZE - 1, loc="lower center",
              bbox_to_anchor=(0.5, -0.08), ncol=len(labels))
    ax.set_title("Current Allocation", fontsize=_FONT_SIZE + 1, pad=6)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf


def _empty_chart(title: str) -> io.BytesIO:
    fig, ax = plt.subplots(figsize=(7, 2), dpi=120)
    ax.text(0.5, 0.5, "No data", ha="center", va="center", transform=ax.transAxes,
            fontsize=_FONT_SIZE, color="#888888")
    ax.set_title(title, fontsize=_FONT_SIZE + 1)
    ax.axis("off")
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf
