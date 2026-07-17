import datetime as dt
import sys
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.attribution import compute_bridge
from ui_common import get_engine, fmt_usd

st.set_page_config(page_title="Attribution", layout="wide")
st.title("Net-Worth Attribution")
st.caption("Decomposes the change in net worth between two dates into cash flow, market P&L, and FX P&L.")

engine = get_engine()

col1, col2 = st.columns(2)
with col1:
    start_date = st.date_input("Start date", value=dt.date.today() - dt.timedelta(days=30))
with col2:
    end_date = st.date_input("End date", value=dt.date.today())

if start_date >= end_date:
    st.error("Start date must be before end date.")
    st.stop()

bridge, table_df = compute_bridge(engine, start_date, end_date)

if bridge["opening_nw"] == 0 and bridge["closing_nw"] == 0 and table_df.empty:
    st.info("No valuation history in this range yet. Add transactions and run a refresh on the Prices & FX page.")
    st.stop()

# "Opening balance adj" only appears when an account was onboarded (given an
# opening_balance transaction) inside the selected window -- in the common
# case it's exactly 0 and the waterfall stays the standard 3-driver shape.
has_ob_adj = abs(bridge["opening_balance_adj"]) > 0.005

labels = ["Opening"]
values = [bridge["opening_nw"]]
measures = ["absolute"]
if has_ob_adj:
    labels.append("Opening balance adj")
    values.append(bridge["opening_balance_adj"])
    measures.append("relative")
labels += ["Cash flow", "Market P&L", "FX P&L", "Closing"]
values += [bridge["cash_flow"], bridge["market_pnl"], bridge["fx_pnl"], bridge["closing_nw"]]
measures += ["relative", "relative", "relative", "total"]

fig = go.Figure(
    go.Waterfall(
        orientation="v",
        measure=measures,
        x=labels,
        y=values,
        connector={"line": {"color": "rgba(120,120,120,0.4)"}},
        decreasing={"marker": {"color": "#d9534f"}},
        increasing={"marker": {"color": "#5cb85c"}},
        totals={"marker": {"color": "#5b8def"}},
        text=[fmt_usd(v) for v in values],
        textposition="outside",
    )
)
fig.update_layout(title=f"Net worth bridge: {start_date} to {end_date}", showlegend=False)
st.plotly_chart(fig, width='stretch')

m1, m2, m3, m4 = st.columns(4)
m1.metric("Opening net worth", fmt_usd(bridge["opening_nw"]))
m2.metric("Closing net worth", fmt_usd(bridge["closing_nw"]))
m3.metric("Total change", fmt_usd(bridge["total_change"]))
m4.metric(
    "Reconciliation residual", fmt_usd(bridge["residual"]),
    help="Should be ~$0 -- confirms the drivers fully explain the change.",
)

st.subheader("Driver breakdown")
driver_names = ["Cash flow", "Market P&L", "FX P&L"]
driver_values = [bridge["cash_flow"], bridge["market_pnl"], bridge["fx_pnl"]]
if has_ob_adj:
    driver_names.append("Opening balance adj")
    driver_values.append(bridge["opening_balance_adj"])
driver_names.append("Total change")
driver_values.append(bridge["total_change"])
st.table({"Driver": driver_names, "USD": [fmt_usd(v) for v in driver_values]})

st.subheader("Per-account contribution")
if table_df.empty:
    st.caption("No account-level activity in this range.")
else:
    display = table_df.rename(
        columns={
            "account_name": "Account", "cash_flow": "Cash flow ($)",
            "market_pnl": "Market P&L ($)", "fx_pnl": "FX P&L ($)",
            "opening_balance_adj": "Opening balance adj ($)", "total": "Total ($)",
        }
    )[["Account", "Cash flow ($)", "Market P&L ($)", "FX P&L ($)", "Opening balance adj ($)", "Total ($)"]]
    st.dataframe(display.style.format({c: "{:,.2f}" for c in display.columns if c != "Account"}), width='stretch', hide_index=True)
