import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import sqlalchemy as sa
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import net_worth_daily
from rules.flags import run_all_rules
from rules.narrative import generate_narrative
from ui_common import get_engine, fmt_usd, fmt_pct

st.set_page_config(page_title="Suggestions", layout="wide")
st.title("Weekly Suggestions")

engine = get_engine()

with engine.connect() as conn:
    nw_df = pd.read_sql(sa.select(net_worth_daily).order_by(net_worth_daily.c.date.desc()).limit(1), conn)

if nw_df.empty:
    st.info("No valuation history yet. Add transactions and run a refresh on the Prices & FX page.")
    st.stop()

latest = nw_df.iloc[0]
position_summary = {
    "as_of": str(latest["date"]),
    "net_worth_usd": round(float(latest["net_worth_usd"]), 2),
    "total_assets_usd": round(float(latest["total_assets_usd"]), 2),
    "total_liabilities_usd": round(float(latest["total_liabilities_usd"]), 2),
}

m1, m2, m3 = st.columns(3)
m1.metric("Net worth", fmt_usd(position_summary["net_worth_usd"]))
m2.metric("Total assets", fmt_usd(position_summary["total_assets_usd"]))
m3.metric("Total liabilities", fmt_usd(position_summary["total_liabilities_usd"]))

if "flags" not in st.session_state or st.button("Regenerate"):
    st.session_state["flags"] = run_all_rules(engine)
    st.session_state["narrative"] = None

flags = st.session_state["flags"]

st.subheader("Rule flags")
if not flags:
    st.success("No flags fired -- nothing high-leverage detected this week.")
else:
    severity_order = {"high": 0, "medium": 1, "low": 2}
    for flag in sorted(flags, key=lambda f: severity_order.get(f["severity"], 3)):
        icon = {"high": "\U0001F534", "medium": "\U0001F7E1", "low": "\U0001F7E2"}.get(flag["severity"], "⚪")
        with st.expander(f"{icon} [{flag['severity'].upper()}] {flag['rule']} -- {flag['one_line_explanation']}"):
            st.json(flag["figure"])

st.subheader("Narrative")
if st.session_state.get("narrative") is None:
    with st.spinner("Generating narrative..."):
        st.session_state["narrative"] = generate_narrative(position_summary, flags)
st.markdown(st.session_state["narrative"])
