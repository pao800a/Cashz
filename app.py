"""Cashz — local-first personal wealth tracker.

Launch with:
    streamlit run app.py

Uses st.navigation + st.Page (the current recommended multipage pattern in Streamlit 1.x+).
"""

import streamlit as st

from cashz.storage.db import init_db

# Initialise DB on first run (idempotent)
init_db()

# ── Page definitions ──────────────────────────────────────────────────────────
dashboard_page = st.Page(
    "pages/dashboard.py",
    title="Dashboard",
    icon="📊",
    default=True,
)
accounts_page = st.Page(
    "pages/accounts.py",
    title="Accounts",
    icon="📒",
)
sync_page = st.Page(
    "pages/sync.py",
    title="Sync",
    icon="🔄",
)
reports_page = st.Page(
    "pages/reports.py",
    title="Reports",
    icon="📄",
)

# ── Navigation ────────────────────────────────────────────────────────────────
pg = st.navigation([dashboard_page, accounts_page, sync_page, reports_page])

st.set_page_config(
    page_title="Cashz",
    page_icon="💰",
    layout="wide",
    initial_sidebar_state="expanded",
)

pg.run()
