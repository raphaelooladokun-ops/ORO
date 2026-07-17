import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import sqlalchemy as sa
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from engine.valuation import rebuild_all
from ingest.fx import refresh_fx_rates, record_manual_fx_rate
from ingest.ngx import refresh_ngx_prices, record_manual_price
from models import prices, fx_rates
from ui_common import get_engine, accounts_df

st.set_page_config(page_title="Prices & FX", layout="wide")
st.title("Prices & FX")

engine = get_engine()
adf = accounts_df(engine)

st.subheader("Refresh")
col1, col2 = st.columns(2)

with col1:
    if st.button("Refresh prices & FX", type="primary"):
        currencies = set(adf["native_currency"].tolist()) if not adf.empty else set()
        start_date = pd.to_datetime(adf["opening_date"]).min().date() if not adf.empty else dt.date.today()

        fx_report = refresh_fx_rates(engine, currencies, start_date)
        tickers = [t for t in adf.loc[adf["is_holding"], "price_ticker"].tolist() if t] if not adf.empty else []
        ngx_report = refresh_ngx_prices(engine, tickers)

        with st.spinner("Rebuilding valuation snapshots..."):
            rebuild_all(engine)

        st.success("Refresh complete.")
        if fx_report["updated"]:
            st.write(f"FX updated: {', '.join(fx_report['updated'])}")
        if fx_report["skipped_unsupported"]:
            st.warning(
                f"FX not covered by Frankfurter (enter manually below): {', '.join(fx_report['skipped_unsupported'])}"
            )
        if fx_report["errors"]:
            st.error(f"FX errors: {'; '.join(fx_report['errors'])}")
        if ngx_report["updated"]:
            st.write(f"Prices updated: {', '.join(ngx_report['updated'])}")
        if ngx_report["errors"]:
            st.error(f"Price scrape errors: {'; '.join(ngx_report['errors'])}")

with col2:
    if st.button("Rebuild valuations only (no network)"):
        with st.spinner("Rebuilding..."):
            rebuild_all(engine)
        st.success("Valuation snapshots rebuilt from ledger + stored prices/FX.")

st.divider()

# --- Staleness banner ---
with engine.connect() as conn:
    price_df = pd.read_sql(sa.select(prices), conn)
if not price_df.empty:
    price_df["date"] = pd.to_datetime(price_df["date"])
    latest = price_df.sort_values("date").groupby("ticker").last().reset_index()
    stale_cutoff = pd.Timestamp(dt.date.today() - dt.timedelta(days=config.STALE_AFTER_DAYS))
    stale = latest[(latest["date"] < stale_cutoff) | (latest["is_stale"] == True)]  # noqa: E712
    if not stale.empty:
        lines = [f"{r['ticker']} (last {r['date'].date()})" for _, r in stale.iterrows()]
        st.warning("Prices stale since: " + ", ".join(lines))

st.subheader("Manual price mark")
holdings = adf[adf["is_holding"]] if not adf.empty else adf
if holdings.empty:
    st.caption("No holding accounts configured yet.")
else:
    with st.form("manual_price_form"):
        ticker = st.selectbox("Ticker", options=sorted(holdings["price_ticker"].dropna().unique()))
        price_date = st.date_input("Date", value=dt.date.today())
        price_val = st.number_input("Price", min_value=0.0, step=0.01, format="%.4f")
        price_ccy = st.text_input("Currency", value="NGN", max_chars=3).upper()
        submitted = st.form_submit_button("Save manual price")
        if submitted:
            record_manual_price(engine, ticker, price_date, price_val, price_ccy)
            st.success(f"Recorded manual price for {ticker}.")
            st.rerun()

st.subheader("Manual FX rate")
with st.form("manual_fx_form"):
    ccy = st.text_input("Currency (ISO 4217)", value="NGN", max_chars=3).upper()
    fx_date = st.date_input("Date", value=dt.date.today(), key="fx_date")
    rate = st.number_input("Rate to USD (1 unit of currency = ? USD)", min_value=0.0, step=0.000001, format="%.6f")
    submitted_fx = st.form_submit_button("Save manual FX rate")
    if submitted_fx:
        record_manual_fx_rate(engine, ccy, fx_date, rate)
        st.success(f"Recorded manual FX rate for {ccy} on {fx_date}.")
        st.rerun()

st.subheader("Recent prices")
if not price_df.empty:
    st.dataframe(price_df.sort_values("date", ascending=False).head(50), width='stretch', hide_index=True)
else:
    st.caption("No prices recorded yet.")

st.subheader("Recent FX rates")
with engine.connect() as conn:
    fx_df = pd.read_sql(sa.select(fx_rates), conn)
if not fx_df.empty:
    st.dataframe(fx_df.sort_values("date", ascending=False).head(50), width='stretch', hide_index=True)
else:
    st.caption("No FX rates recorded yet.")
