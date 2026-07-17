"""Standalone prices + FX refresh, cron-able: `python refresh.py`.

Does the same work as the "Refresh prices & FX" button in the UI, without
starting Streamlit. Never raises on a scrape/FX failure -- failures are
printed and the run continues so a cron job doesn't wedge on one bad day.
"""
import datetime as dt
import sys

import pandas as pd
import sqlalchemy as sa

import db
from engine.valuation import rebuild_all
from ingest.fx import refresh_fx_rates
from ingest.ngx import refresh_ngx_prices
from models import accounts


def main():
    engine = db.init_db()

    with engine.connect() as conn:
        adf = pd.read_sql(sa.select(accounts), conn)

    if adf.empty:
        print("No accounts configured yet -- nothing to refresh.")
        return

    currencies = set(adf["native_currency"].tolist())
    start_date = pd.to_datetime(adf["opening_date"]).min().date()

    fx_report = refresh_fx_rates(engine, currencies, start_date)
    print(f"FX updated: {fx_report['updated']}")
    if fx_report["skipped_unsupported"]:
        print(f"FX not covered by Frankfurter (needs manual entry): {fx_report['skipped_unsupported']}")
    if fx_report["errors"]:
        print(f"FX errors: {fx_report['errors']}", file=sys.stderr)

    tickers = [t for t in adf.loc[adf["is_holding"], "price_ticker"].tolist() if t]
    ngx_report = refresh_ngx_prices(engine, tickers)
    print(f"Prices updated: {ngx_report['updated']}")
    if ngx_report["missing"]:
        print(f"Prices missing (marked stale): {ngx_report['missing']}")
    if ngx_report["errors"]:
        print(f"Price scrape errors: {ngx_report['errors']}", file=sys.stderr)

    rebuild_all(engine)
    print(f"Valuation snapshots rebuilt as of {dt.date.today()}.")


if __name__ == "__main__":
    main()
