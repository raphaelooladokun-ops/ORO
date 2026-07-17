import datetime as dt

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import sqlalchemy as sa
import streamlit as st

import config
from models import accounts, net_worth_daily, valuation_snapshots, transactions, prices
from rules.flags import run_all_rules
from ui_common import get_engine, accounts_df, fmt_usd, fmt_pct

st.set_page_config(page_title="Net Worth Tracker", layout="wide", page_icon="\U0001F4B0")
st.title("Net Worth Dashboard")

engine = get_engine()

with engine.connect() as conn:
    nw_df = pd.read_sql(sa.select(net_worth_daily).order_by(net_worth_daily.c.date), conn)
    price_df = pd.read_sql(sa.select(prices), conn)

if nw_df.empty:
    st.info(
        "No valuation history yet. Add accounts and transactions, then run a refresh "
        "on the Prices & FX page to build the net-worth history."
    )
    st.stop()

nw_df["date"] = pd.to_datetime(nw_df["date"])

# --- staleness banner ---
if not price_df.empty:
    price_df["date"] = pd.to_datetime(price_df["date"])
    latest_prices = price_df.sort_values("date").groupby("ticker").last().reset_index()
    stale_cutoff = pd.Timestamp(dt.date.today() - dt.timedelta(days=config.STALE_AFTER_DAYS))
    stale = latest_prices[(latest_prices["date"] < stale_cutoff) | (latest_prices["is_stale"] == True)]  # noqa: E712
    if not stale.empty:
        tickers = ", ".join(stale["ticker"].tolist())
        st.warning(f"Prices stale since: {tickers}. Refresh or mark manually on the Prices & FX page.")

# --- headline net worth ---
latest = nw_df.iloc[-1]
latest_date = latest["date"].date()


def nw_on_or_before(target_date):
    sub = nw_df[nw_df["date"] <= pd.Timestamp(target_date)]
    return float(sub.iloc[-1]["net_worth_usd"]) if not sub.empty else None


week_ago = nw_on_or_before(latest_date - dt.timedelta(days=7))
month_ago = nw_on_or_before(latest_date - dt.timedelta(days=30))

st.caption(f"As of {latest_date}")
m1, m2, m3 = st.columns(3)
m1.metric("Net worth (USD)", fmt_usd(latest["net_worth_usd"]))
m2.metric(
    "vs last week",
    fmt_usd(latest["net_worth_usd"] - week_ago) if week_ago is not None else "-",
)
m3.metric(
    "vs last month",
    fmt_usd(latest["net_worth_usd"] - month_ago) if month_ago is not None else "-",
)

# --- net worth over time ---
st.subheader("Net worth over time")
fig_nw = px.line(nw_df, x="date", y="net_worth_usd", labels={"net_worth_usd": "Net worth (USD)"})
fig_nw.update_xaxes(
    rangeselector=dict(
        buttons=[
            dict(count=1, label="1m", step="month", stepmode="backward"),
            dict(count=3, label="3m", step="month", stepmode="backward"),
            dict(count=6, label="6m", step="month", stepmode="backward"),
            dict(count=1, label="YTD", step="year", stepmode="todate"),
            dict(count=1, label="1y", step="year", stepmode="backward"),
            dict(step="all", label="All"),
        ]
    ),
    rangeslider=dict(visible=True),
)
st.plotly_chart(fig_nw, width='stretch')

# --- allocation donuts ---
with engine.connect() as conn:
    snap_df = pd.read_sql(sa.select(valuation_snapshots).where(valuation_snapshots.c.date == latest_date.isoformat()), conn)
adf = accounts_df(engine)

if not snap_df.empty and not adf.empty:
    merged = snap_df.merge(adf, left_on="account_id", right_on="id")
    assets_merged = merged[merged["class_"] == "asset"]

    col1, col2, col3 = st.columns(3)
    with col1:
        st.caption("Allocation by account type")
        by_type = assets_merged.groupby("type")["usd_value"].sum().reset_index()
        by_type = by_type[by_type["usd_value"] > 0]
        if not by_type.empty:
            st.plotly_chart(px.pie(by_type, names="type", values="usd_value", hole=0.5), width='stretch')
    with col2:
        st.caption("Allocation by currency")
        by_ccy = assets_merged.groupby("native_currency")["usd_value"].sum().reset_index()
        by_ccy = by_ccy[by_ccy["usd_value"] > 0]
        if not by_ccy.empty:
            st.plotly_chart(px.pie(by_ccy, names="native_currency", values="usd_value", hole=0.5), width='stretch')
    with col3:
        st.caption("Allocation by holding")
        holdings = assets_merged[assets_merged["is_holding"]]
        holdings = holdings[holdings["usd_value"] > 0]
        if not holdings.empty:
            st.plotly_chart(px.pie(holdings, names="name", values="usd_value", hole=0.5), width='stretch')
        else:
            st.caption("No holding accounts with a positive balance.")

# --- debt paydown trajectory ---
st.subheader("Debt paydown trajectory")
liab_ids = adf.loc[adf["class_"] == "liability", "id"].tolist() if not adf.empty else []
if liab_ids:
    with engine.connect() as conn:
        liab_snap = pd.read_sql(
            sa.select(valuation_snapshots).where(valuation_snapshots.c.account_id.in_(liab_ids)), conn
        )
    if not liab_snap.empty:
        liab_snap["date"] = pd.to_datetime(liab_snap["date"])
        liab_trend = liab_snap.groupby("date")["usd_value"].sum().reset_index()
        st.plotly_chart(px.line(liab_trend, x="date", y="usd_value", labels={"usd_value": "Liabilities (USD)"}), width='stretch')
else:
    st.caption("No liability accounts.")

# --- income vs expense + savings rate ---
st.subheader("Income vs expense, and savings rate")
with engine.connect() as conn:
    txn_df = pd.read_sql(sa.select(transactions).where(transactions.c.type.in_(["income", "expense"])), conn)

if not txn_df.empty:
    txn_df["type"] = txn_df["type"].map(lambda v: v.value if hasattr(v, "value") else v)
    txn_df["usd"] = txn_df["amount_native"] * txn_df["fx_rate_to_usd"]
    txn_df["date"] = pd.to_datetime(txn_df["date"])
    txn_df["month"] = txn_df["date"].dt.to_period("M").dt.to_timestamp()

    monthly = txn_df.pivot_table(index="month", columns="type", values="usd", aggfunc="sum", fill_value=0.0).reset_index()
    for col in ("income", "expense"):
        if col not in monthly.columns:
            monthly[col] = 0.0
    monthly["net"] = monthly["income"] - monthly["expense"]
    monthly["savings_rate"] = monthly.apply(lambda r: (r["net"] / r["income"]) if r["income"] > 0 else None, axis=1)

    fig_ie = go.Figure()
    fig_ie.add_bar(x=monthly["month"], y=monthly["income"], name="Income")
    fig_ie.add_bar(x=monthly["month"], y=monthly["expense"], name="Expense")
    fig_ie.add_trace(go.Scatter(x=monthly["month"], y=monthly["savings_rate"], name="Savings rate", yaxis="y2", mode="lines+markers"))
    fig_ie.update_layout(
        barmode="group",
        yaxis=dict(title="USD"),
        yaxis2=dict(title="Savings rate", overlaying="y", side="right", tickformat=".0%"),
    )
    st.plotly_chart(fig_ie, width='stretch')
else:
    st.caption("No income/expense transactions yet.")

# --- weekly suggestion panel ---
st.subheader("Weekly suggestions")
flags = run_all_rules(engine)
if not flags:
    st.success("No flags fired -- nothing high-leverage detected this week.")
else:
    severity_order = {"high": 0, "medium": 1, "low": 2}
    top_flags = sorted(flags, key=lambda f: severity_order.get(f["severity"], 3))[:3]
    for flag in top_flags:
        icon = {"high": "\U0001F534", "medium": "\U0001F7E1", "low": "\U0001F7E2"}.get(flag["severity"], "⚪")
        st.write(f"{icon} **{flag['rule']}** -- {flag['one_line_explanation']}")
    st.page_link("pages/7_Suggestions.py", label="See full suggestions + narrative", icon="\U0001F449")
