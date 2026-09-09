"""Markdown report generator."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Optional

from cashz.models import Account, Snapshot
from cashz.networth import CATEGORY_LABELS


def _fmt(val: Decimal, decimals: int = 2) -> str:
    return f"€{val:,.{decimals}f}"


def generate(
    as_of: datetime.date,
    total: Decimal,
    by_category: dict[str, Decimal],
    account_rows: list[dict],
    prev_total: Optional[Decimal] = None,
    prev_date: Optional[datetime.date] = None,
    title: str = "Cashz Wealth Report",
) -> str:
    lines: list[str] = []

    lines.append(f"# {title}")
    lines.append(f"\n_Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}_  ")
    lines.append(f"_As of: {as_of.isoformat()}_\n")
    lines.append("---\n")

    # ── Summary ───────────────────────────────────────────────────────────────
    lines.append("## Summary\n")
    lines.append(f"**Total Net Worth:** {_fmt(total)}")
    if prev_total is not None and prev_date is not None:
        delta = total - prev_total
        sign = "+" if delta >= 0 else ""
        pct = (delta / prev_total * 100) if prev_total != 0 else Decimal("0")
        lines.append(
            f"  \n**Change vs {prev_date.isoformat()}:** {sign}{_fmt(delta)} ({sign}{pct:.1f}%)"
        )
    lines.append("\n")

    # ── By category ───────────────────────────────────────────────────────────
    lines.append("## By Category\n")
    lines.append("| Category | Balance | % of Total |")
    lines.append("|---|---:|---:|")
    for cat in ["liquidity", "investments", "pension"]:
        val = by_category.get(cat, Decimal("0"))
        label = CATEGORY_LABELS.get(cat, cat)
        pct = (val / total * 100) if total != 0 else Decimal("0")
        lines.append(f"| {label} | {_fmt(val)} | {pct:.1f}% |")
    lines.append("\n")

    # ── By account ────────────────────────────────────────────────────────────
    lines.append("## By Account\n")
    lines.append("| Account | Institution | Category | Balance (EUR) | As Of | Source |")
    lines.append("|---|---|---|---:|---|---|")
    for row in account_rows:
        acc: Account = row["account"]
        snap: Optional[Snapshot] = row["snapshot"]
        bal = _fmt(row["balance_eur"])
        as_of_str = row["as_of"].isoformat() if row["as_of"] else "—"
        source = row["source"] or "—"
        cat_label = CATEGORY_LABELS.get(acc.category, acc.category)
        lines.append(
            f"| {acc.name} | {acc.institution} | {cat_label} | {bal} | {as_of_str} | {source} |"
        )
    lines.append("\n")

    lines.append("---")
    lines.append(
        "_Cashz — local-first personal wealth tracker. "
        "TFR figures are estimates; verify against payslips/INPS statements._"
    )

    return "\n".join(lines)
