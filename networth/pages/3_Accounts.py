import datetime as dt

import pandas as pd
import plotly.express as px
import sqlalchemy as sa
import streamlit as st

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import accounts, AccountClass, AccountType, valuation_snapshots
from ui_common import get_engine, accounts_df, fmt_usd

st.set_page_config(page_title="Accounts", layout="wide")
st.title("Accounts")

engine = get_engine()

with st.expander("Add account", expanded=False):
    with st.form("add_account_form", clear_on_submit=True):
        name = st.text_input("Name")
        col1, col2, col3 = st.columns(3)
        with col1:
            class_ = st.selectbox("Class", [c.value for c in AccountClass])
        with col2:
            type_ = st.selectbox("Type", [t.value for t in AccountType])
        with col3:
            currency = st.text_input("Native currency (ISO 4217)", value="USD", max_chars=3)
        col4, col5, col6 = st.columns(3)
        with col4:
            is_holding = st.checkbox("Holding account (valued by units x price)")
        with col5:
            price_ticker = st.text_input("Price ticker (holdings only)", value="")
        with col6:
            interest_rate = st.number_input(
                "Annual interest/yield rate (e.g. 0.02 = 2%)",
                value=0.0, step=0.005, format="%.4f",
            )
        opening_date = st.date_input("Opening date", value=dt.date.today())
        notes = st.text_area("Notes", value="")
        submitted = st.form_submit_button("Create account")

        if submitted:
            if not name.strip():
                st.error("Name is required.")
            else:
                with engine.begin() as conn:
                    conn.execute(
                        accounts.insert().values(
                            name=name.strip(),
                            class_=class_,
                            type=type_,
                            is_holding=is_holding,
                            native_currency=currency.strip().upper(),
                            price_ticker=price_ticker.strip() or None,
                            interest_rate=interest_rate or None,
                            opening_date=opening_date,
                            notes=notes.strip() or None,
                            is_active=True,
                        )
                    )
                st.success(f"Created account '{name}'.")
                st.rerun()

st.subheader("All accounts")
df = accounts_df(engine)

if df.empty:
    st.info("No accounts yet. Add one above.")
else:
    with engine.connect() as conn:
        snap_df = pd.read_sql(sa.select(valuation_snapshots), conn)

    latest_by_account = {}
    if not snap_df.empty:
        snap_df["date"] = pd.to_datetime(snap_df["date"])
        latest_idx = snap_df.groupby("account_id")["date"].idxmax()
        latest_by_account = snap_df.loc[latest_idx].set_index("account_id")["usd_value"].to_dict()

    df["usd_value"] = df["id"].map(latest_by_account).fillna(0.0)
    display_cols = [
        "id", "name", "class_", "type", "native_currency", "is_holding",
        "price_ticker", "interest_rate", "usd_value", "is_active",
    ]
    show_df = df[display_cols].rename(columns={"class_": "class", "usd_value": "usd_value ($)"})
    st.dataframe(show_df, width='stretch', hide_index=True)

    st.subheader("Per-account trend")
    acct_names = dict(zip(df["id"], df["name"]))
    selected = st.selectbox("Account", options=list(acct_names.keys()), format_func=lambda i: acct_names[i])
    if not snap_df.empty:
        acct_snap = snap_df[snap_df["account_id"] == selected].sort_values("date")
        if not acct_snap.empty:
            fig = px.line(acct_snap, x="date", y="usd_value", title=f"{acct_names[selected]} — USD value over time")
            st.plotly_chart(fig, width='stretch')
        else:
            st.caption("No valuation history yet for this account. Run a refresh from the Prices & FX page.")
    else:
        st.caption("No valuation snapshots yet. Run a refresh from the Prices & FX page.")

    st.subheader("Edit / deactivate")
    edit_id = st.selectbox(
        "Select account to edit", options=list(acct_names.keys()),
        format_func=lambda i: acct_names[i], key="edit_select",
    )
    row = df[df["id"] == edit_id].iloc[0]
    with st.form("edit_account_form"):
        new_name = st.text_input("Name", value=row["name"])
        new_rate = st.number_input(
            "Annual interest/yield rate", value=float(row["interest_rate"] or 0.0),
            step=0.005, format="%.4f",
        )
        new_notes = st.text_area("Notes", value=row["notes"] or "")
        new_active = st.checkbox("Active", value=bool(row["is_active"]))
        save = st.form_submit_button("Save changes")
        if save:
            with engine.begin() as conn:
                conn.execute(
                    accounts.update().where(accounts.c.id == edit_id).values(
                        name=new_name.strip(),
                        interest_rate=new_rate or None,
                        notes=new_notes.strip() or None,
                        is_active=new_active,
                    )
                )
            st.success("Saved.")
            st.rerun()
