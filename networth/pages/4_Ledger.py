import sys
from pathlib import Path

import pandas as pd
import sqlalchemy as sa
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import transactions, TxnType
from ui_common import get_engine, accounts_df

st.set_page_config(page_title="Ledger", layout="wide")
st.title("Ledger")

engine = get_engine()
adf = accounts_df(engine)
acct_names = dict(zip(adf["id"], adf["name"])) if not adf.empty else {}

with engine.connect() as conn:
    df = pd.read_sql(sa.select(transactions).order_by(transactions.c.date.desc()), conn)

if df.empty:
    st.info("No transactions yet.")
    st.stop()

df["type"] = df["type"].apply(lambda v: v.value if hasattr(v, "value") else v)
df["from_account"] = df["from_account_id"].map(acct_names)
df["to_account"] = df["to_account_id"].map(acct_names)
df["usd_value"] = df["amount_native"] * df["fx_rate_to_usd"]

col1, col2, col3 = st.columns(3)
with col1:
    type_filter = st.multiselect("Type", options=sorted(df["type"].unique()))
with col2:
    acct_filter = st.multiselect("Account (from or to)", options=sorted(set(acct_names.values())))
with col3:
    search = st.text_input("Search note/category/counterparty")

filtered = df.copy()
if type_filter:
    filtered = filtered[filtered["type"].isin(type_filter)]
if acct_filter:
    filtered = filtered[filtered["from_account"].isin(acct_filter) | filtered["to_account"].isin(acct_filter)]
if search:
    mask = (
        filtered["note"].fillna("").str.contains(search, case=False)
        | filtered["category"].fillna("").str.contains(search, case=False)
        | filtered["counterparty"].fillna("").str.contains(search, case=False)
    )
    filtered = filtered[mask]

display_cols = [
    "id", "date", "type", "from_account", "to_account", "amount_native", "currency",
    "usd_value", "units", "unit_price", "category", "counterparty", "note",
]
st.dataframe(filtered[display_cols], width='stretch', hide_index=True)
st.caption(f"{len(filtered)} of {len(df)} transactions shown.")

st.download_button(
    "Export filtered as CSV",
    data=filtered[display_cols].to_csv(index=False).encode("utf-8"),
    file_name="ledger_export.csv",
    mime="text/csv",
)

st.subheader("Edit / delete a transaction")
txn_id = st.selectbox("Transaction id", options=filtered["id"].tolist())
row = df[df["id"] == txn_id].iloc[0]
with st.form("edit_txn"):
    new_note = st.text_area("Note", value=row["note"] or "")
    new_category = st.text_input("Category", value=row["category"] or "")
    new_counterparty = st.text_input("Counterparty", value=row["counterparty"] or "")
    col_a, col_b = st.columns(2)
    save = col_a.form_submit_button("Save")
    delete = col_b.form_submit_button("Delete transaction", type="secondary")

    if save:
        with engine.begin() as conn:
            conn.execute(
                transactions.update().where(transactions.c.id == txn_id).values(
                    note=new_note.strip() or None,
                    category=new_category.strip() or None,
                    counterparty=new_counterparty.strip() or None,
                )
            )
        st.success("Saved.")
        st.rerun()
    if delete:
        with engine.begin() as conn:
            conn.execute(transactions.delete().where(transactions.c.id == txn_id))
        st.success("Deleted. Re-run a valuation refresh to update net worth.")
        st.rerun()
