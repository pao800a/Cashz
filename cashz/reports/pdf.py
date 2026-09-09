"""PDF report generator using fpdf2 (pure Python, no external binaries)."""

from __future__ import annotations

import datetime
import io
from decimal import Decimal
from typing import Optional

from fpdf import FPDF, FPDFException

from cashz.models import Account, Snapshot
from cashz.networth import CATEGORY_LABELS


class CashzPDF(FPDF):
    def __init__(self, title: str, as_of: datetime.date):
        super().__init__(orientation="P", unit="mm", format="A4")
        self._title = title
        self._as_of = as_of
        self.set_auto_page_break(auto=True, margin=18)
        self.set_margins(18, 18, 18)

    def header(self):
        self.set_font("Helvetica", "B", 11)
        self.cell(0, 7, self._title, new_x="LMARGIN", new_y="NEXT", align="L")
        self.set_font("Helvetica", "", 8)
        self.set_text_color(120, 120, 120)
        self.cell(
            0, 5,
            f"As of {self._as_of.isoformat()}  |  Generated {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}",
            new_x="LMARGIN", new_y="NEXT", align="L",
        )
        self.set_text_color(0, 0, 0)
        self.ln(2)
        self.set_draw_color(200, 200, 200)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(3)

    def footer(self):
        self.set_y(-13)
        self.set_font("Helvetica", "", 7)
        self.set_text_color(160, 160, 160)
        self.cell(
            0, 5,
            f"Cashz personal finance tracker  |  Page {self.page_no()}/{{nb}}",
            align="C",
        )
        self.set_text_color(0, 0, 0)


def _fmt(val: Decimal, decimals: int = 2) -> str:
    return f"€{val:,.{decimals}f}"


def generate(
    as_of: datetime.date,
    total: Decimal,
    by_category: dict[str, Decimal],
    account_rows: list[dict],
    trend_chart: Optional[io.BytesIO] = None,
    allocation_chart: Optional[io.BytesIO] = None,
    prev_total: Optional[Decimal] = None,
    prev_date: Optional[datetime.date] = None,
    title: str = "Cashz Wealth Report",
) -> bytes:
    """Build and return a PDF as bytes."""
    pdf = CashzPDF(title=title, as_of=as_of)
    pdf.alias_nb_pages()
    pdf.add_page()

    # ── Summary box ───────────────────────────────────────────────────────────
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "Net Worth Summary", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)

    pdf.set_fill_color(240, 244, 255)
    pdf.rect(pdf.l_margin, pdf.get_y(), pdf.w - pdf.l_margin - pdf.r_margin, 14, "F")
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 14, _fmt(total), new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(2)

    if prev_total is not None and prev_date is not None:
        delta = total - prev_total
        sign = "+" if delta >= 0 else ""
        pct = (delta / prev_total * 100) if prev_total != 0 else Decimal("0")
        colour = (0, 128, 0) if delta >= 0 else (192, 0, 0)
        pdf.set_text_color(*colour)
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(
            0, 5,
            f"Change vs {prev_date.isoformat()}: {sign}{_fmt(delta)} ({sign}{float(pct):.1f}%)",
            new_x="LMARGIN", new_y="NEXT", align="C",
        )
        pdf.set_text_color(0, 0, 0)
    pdf.ln(4)

    # ── Charts ────────────────────────────────────────────────────────────────
    usable_w = pdf.w - pdf.l_margin - pdf.r_margin
    if trend_chart is not None:
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 6, "Net Worth Trend", new_x="LMARGIN", new_y="NEXT")
        trend_chart.seek(0)
        pdf.image(trend_chart, x=pdf.l_margin, w=usable_w, h=55)
        pdf.ln(3)

    if allocation_chart is not None:
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 6, "Allocation", new_x="LMARGIN", new_y="NEXT")
        allocation_chart.seek(0)
        pdf.image(allocation_chart, x=pdf.l_margin, w=usable_w * 0.5, h=50)
        pdf.ln(3)

    # ── By category ───────────────────────────────────────────────────────────
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 6, "By Category", new_x="LMARGIN", new_y="NEXT")

    col_w = [usable_w * 0.40, usable_w * 0.35, usable_w * 0.25]
    pdf.set_fill_color(230, 235, 250)
    pdf.set_font("Helvetica", "B", 8)
    for header, w in zip(("Category", "Balance", "% Total"), col_w):
        align = "R" if header != "Category" else "L"
        pdf.cell(w, 6, header, border=0, align=align, fill=True)
    pdf.ln()

    pdf.set_font("Helvetica", "", 8)
    for cat in ["liquidity", "investments", "pension"]:
        val = by_category.get(cat, Decimal("0"))
        label = CATEGORY_LABELS.get(cat, cat)
        pct = float(val / total * 100) if total != 0 else 0.0
        pdf.cell(col_w[0], 5.5, label, border=0, align="L")
        pdf.cell(col_w[1], 5.5, _fmt(val), border=0, align="R")
        pdf.cell(col_w[2], 5.5, f"{pct:.1f}%", border=0, align="R")
        pdf.ln()
    pdf.ln(3)

    # ── By account ────────────────────────────────────────────────────────────
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 6, "By Account", new_x="LMARGIN", new_y="NEXT")

    headers = ["Account", "Institution", "Balance", "As Of", "Source"]
    widths = [usable_w * 0.28, usable_w * 0.20, usable_w * 0.18, usable_w * 0.18, usable_w * 0.16]
    aligns = ["L", "L", "R", "L", "L"]

    pdf.set_fill_color(230, 235, 250)
    pdf.set_font("Helvetica", "B", 7)
    for h, w in zip(headers, widths):
        pdf.cell(w, 6, h, border=0, align="L", fill=True)
    pdf.ln()

    pdf.set_font("Helvetica", "", 7)
    fill = False
    for row in account_rows:
        acc: Account = row["account"]
        as_of_str = row["as_of"].isoformat() if row["as_of"] else "—"
        cells = [
            acc.name,
            acc.institution,
            _fmt(row["balance_eur"]),
            as_of_str,
            row["source"] or "—",
        ]
        pdf.set_fill_color(248, 249, 254)
        for cell_text, w, a in zip(cells, widths, aligns):
            pdf.cell(w, 5.5, cell_text, border=0, align=a, fill=fill)
        pdf.ln()
        fill = not fill
    pdf.ln(3)

    # ── Disclaimer ────────────────────────────────────────────────────────────
    pdf.set_font("Helvetica", "I", 7)
    pdf.set_text_color(140, 140, 140)
    pdf.multi_cell(
        0, 4,
        "TFR figures are estimates based on salary history and the ISTAT FOI index. "
        "Verify against your payslips and INPS account statements. "
        "This report is for personal use only and is not financial advice.",
    )

    return bytes(pdf.output())
