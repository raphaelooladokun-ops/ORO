import datetime as dt
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.lookup import fx_rate_asof
from models import transactions, TxnType
from ui_common import get_engine, accounts_df, account_options

st.set_page_config(page_title="Add Transaction", layout="wide")
st.title("Add Transaction")

engine = get_engine()
adf = accounts_df(engine, active_only=True)

if adf.empty:
    st.warning("Create at least one account first (Accounts page).")
    st.stop()

opts = account_options(engine, active_only=True)
opts_with_none = {"(external / none)": None, **opts}

txn_type = st.selectbox("Type", [t.value for t in TxnType])
date = st.date_input("Date", value=dt.date.today())

col1, col2 = st.columns(2)
with col1:
    from_label = st.selectbox("From account", options=list(opts_with_none.keys()), key="from_acct")
with col2:
    to_label = st.selectbox("To account", options=list(opts_with_none.keys()), key="to_acct")

from_id = opts_with_none[from_label]
to_id = opts_with_none[to_label]

# Default currency: prefer the from-account's currency, else to-account's.
default_ccy = "USD"
if from_id is not None:
    default_ccy = adf.loc[adf["id"] == from_id, "native_currency"].iloc[0]
elif to_id is not None:
    default_ccy = adf.loc[adf["id"] == to_id, "native_currency"].iloc[0]

col3, col4 = st.columns(2)
with col3:
    amount_native = st.number_input("Amount (native currency)", min_value=0.0, step=1.0, format="%.2f")
with col4:
    currency = st.text_input("Currency (ISO 4217)", value=default_ccy, max_chars=3).upper()

with engine.connect() as conn:
    auto_rate = fx_rate_asof(conn, currency, date)

fx_rate_to_usd = st.number_input(
    "FX rate to USD (auto-filled, editable — multiply native amount by this to get USD)",
    value=float(auto_rate) if auto_rate is not None else 1.0,
    step=0.0001, format="%.6f",
)
if auto_rate is None:
    st.caption(
        f"No stored FX rate for {currency} yet — enter it manually, or run a refresh "
        "on the Prices & FX page first."
    )

units = unit_price = None
if txn_type in (TxnType.buy.value, TxnType.sell.value):
    col5, col6 = st.columns(2)
    with col5:
        units = st.number_input("Units", min_value=0.0, step=1.0, format="%.4f")
    with col6:
        unit_price = st.number_input("Unit price (in currency above)", min_value=0.0, step=0.01, format="%.4f")
elif txn_type == TxnType.valuation_adjustment.value:
    to_is_holding = to_id is not None and bool(adf.loc[adf["id"] == to_id, "is_holding"].iloc[0])
    if to_is_holding:
        units = st.number_input("Units delta (+/-)", step=1.0, format="%.4f")
        st.caption("For a holding valuation_adjustment, the units delta is applied; amount_native is ignored.")

category = st.text_input("Category", value="")
counterparty = st.text_input("Counterparty (optional)", value="")
note = st.text_area("Note", value="")

if st.button("Save transaction", type="primary"):
    if from_id is None and to_id is None:
        st.error("At least one of from/to account must be set.")
    else:
        with engine.begin() as conn:
            conn.execute(
                transactions.insert().values(
                    date=date,
                    type=txn_type,
                    from_account_id=from_id,
                    to_account_id=to_id,
                    amount_native=amount_native,
                    currency=currency,
                    fx_rate_to_usd=fx_rate_to_usd,
                    units=units,
                    unit_price=unit_price,
                    category=category.strip() or None,
                    counterparty=counterparty.strip() or None,
                    note=note.strip() or None,
                )
            )
        st.success("Transaction saved. Re-run a valuation refresh (Prices & FX page) to update net worth.")
