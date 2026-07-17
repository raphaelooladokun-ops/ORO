"""Shared Streamlit helpers: cached engine/connection, formatting, account queries."""
import datetime as dt

import pandas as pd
import sqlalchemy as sa
import streamlit as st

import db
from models import accounts


@st.cache_resource
def get_engine():
    return db.init_db()


def fmt_usd(value: float) -> str:
    if value is None or pd.isna(value):
        return "-"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def fmt_pct(value: float) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{value * 100:,.1f}%"


def accounts_df(engine=None, active_only: bool = False) -> pd.DataFrame:
    engine = engine or get_engine()
    query = sa.select(accounts)
    if active_only:
        query = query.where(accounts.c.is_active.is_(True))
    with engine.connect() as conn:
        df = pd.read_sql(query, conn)
    if not df.empty:
        df["class_"] = df["class_"].apply(lambda v: v.value if hasattr(v, "value") else v)
        df["type"] = df["type"].apply(lambda v: v.value if hasattr(v, "value") else v)
    return df


def account_options(engine=None, active_only: bool = True) -> dict[str, int]:
    """Returns {display_label: account_id} for selectboxes."""
    df = accounts_df(engine, active_only=active_only)
    return {
        f"{row['name']} ({row['native_currency']})": int(row["id"])
        for _, row in df.iterrows()
    }


def today() -> dt.date:
    return dt.date.today()
