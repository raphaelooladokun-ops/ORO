"""Scalar as-of lookups for FX rates and prices.

Used by the transaction-entry form (auto-fill fx_rate_to_usd for a chosen
date/currency) and anywhere else a single point value is needed. The batch
valuation engine (engine/valuation.py) does the equivalent lookup
vectorized over a whole date range instead of calling these row-by-row.
"""
import datetime as dt

import sqlalchemy as sa

from models import fx_rates, prices


def fx_rate_asof(conn, currency: str, date: dt.date) -> float | None:
    if currency == "USD":
        return 1.0
    row = conn.execute(
        sa.select(fx_rates.c.rate_to_usd)
        .where(fx_rates.c.currency == currency, fx_rates.c.date <= date)
        .order_by(fx_rates.c.date.desc())
        .limit(1)
    ).fetchone()
    if row is not None:
        return row[0]
    # No rate on/before this date yet -- fall back to the earliest known
    # rate rather than returning None, so entry forms never hard-fail.
    row = conn.execute(
        sa.select(fx_rates.c.rate_to_usd)
        .where(fx_rates.c.currency == currency)
        .order_by(fx_rates.c.date.asc())
        .limit(1)
    ).fetchone()
    return row[0] if row is not None else None


def price_asof(conn, ticker: str, date: dt.date) -> tuple[float, str] | None:
    row = conn.execute(
        sa.select(prices.c.price, prices.c.currency)
        .where(prices.c.ticker == ticker, prices.c.date <= date)
        .order_by(prices.c.date.desc())
        .limit(1)
    ).fetchone()
    if row is not None:
        return row[0], row[1]
    row = conn.execute(
        sa.select(prices.c.price, prices.c.currency)
        .where(prices.c.ticker == ticker)
        .order_by(prices.c.date.asc())
        .limit(1)
    ).fetchone()
    return (row[0], row[1]) if row is not None else None
