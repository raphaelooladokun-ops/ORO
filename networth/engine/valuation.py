"""The valuation engine -- the trust centre of the app.

Reconstructs, for every account and every day since the account's opening
(or its first transaction, whichever is earlier), the native balance or
unit count implied by the ledger, then values it in USD using the FX rate
and (for holdings) price that were actually in force *on that day* --
never today's rate. Writes `valuation_snapshots` (one row per account per
day) and the denormalised `net_worth_daily` rollup.

`rebuild_all` is a full delete + recompute of both tables from the ledger
each time it runs. At personal-finance data volumes (a handful of
accounts, a few thousand days) this is cheap and it keeps the engine
deterministic: the tables are always exactly what the ledger + price/FX
history implies, with no incremental-update drift to debug.

Transaction effects (see models.TxnType). `amount_native`/`currency` on a
transaction describe one leg's movement; the other leg is derived by
FX-bridging through USD at that day's rate when the two accounts' native
currencies differ (a no-op when they match):

    type                   | from_account            | to_account
    -----------------------|--------------------------|---------------------------
    income                 | (external)               | native += amount
    expense                | native -= amount         | (external)
    transfer               | native -= amount         | native += amount (bridged)
    buy                    | native -= amount         | units += units
    sell                   | units -= units           | native += amount
    dividend               | (informational only)     | native += amount
    repayment              | native -= amount         | native -= amount (debt down)
    drawdown               | native += amount (debt up)| native += amount (cash in)
    valuation_adjustment   | (unused)                 | native += amount, or
                            |                          | units += units if the
                            |                          | target account is a holding
    opening_balance         | native -= amount (bridged)| native += amount, or
                            | (equity contra account)  | units += units if the
                            |                          | target account is a holding

`opening_balance` always books its contra to the singleton "Opening Balance
Equity" account (engine/opening_balance.py), which carries
`class_ = AccountClass.equity` and is therefore excluded from
net_worth_daily's asset/liability sums below.

Historical FX/price lookups always go through the stored `fx_rates` /
`prices` history (forward/back-filled across gaps), not the
`fx_rate_to_usd` value captured on the transaction at entry time -- so a
later backfilled/corrected rate is reflected retroactively, which is the
behaviour the brief's "historically reconstructable" requirement implies.
"""
import datetime as dt

import pandas as pd
import sqlalchemy as sa

from models import accounts, transactions, fx_rates, prices, valuation_snapshots, net_worth_daily


def _enum_val(v):
    return v.value if hasattr(v, "value") else v


def _build_fx_wide(fx_df: pd.DataFrame, date_range: list[dt.date]) -> pd.DataFrame:
    idx = pd.Index(date_range, name="date")
    wide = pd.DataFrame(index=idx)
    if not fx_df.empty:
        fx_df = fx_df.copy()
        fx_df["date"] = pd.to_datetime(fx_df["date"]).dt.date
        for ccy, grp in fx_df.groupby("currency"):
            s = grp.sort_values("date").drop_duplicates("date", keep="last").set_index("date")["rate_to_usd"]
            wide[ccy] = s.reindex(idx).ffill().bfill()
    wide["USD"] = 1.0
    return wide


def _build_price_wide(price_df: pd.DataFrame, date_range: list[dt.date]) -> tuple[pd.DataFrame, dict]:
    idx = pd.Index(date_range, name="date")
    wide = pd.DataFrame(index=idx)
    ccy_map: dict[str, str] = {}
    if not price_df.empty:
        price_df = price_df.copy()
        price_df["date"] = pd.to_datetime(price_df["date"]).dt.date
        for ticker, grp in price_df.groupby("ticker"):
            grp = grp.sort_values("date").drop_duplicates("date", keep="last")
            wide[ticker] = grp.set_index("date")["price"].reindex(idx).ffill().bfill()
            ccy_map[ticker] = grp["currency"].iloc[-1]
    return wide, ccy_map


def _convert(amount: float, from_ccy: str, to_ccy: str, date: dt.date, fx_wide: pd.DataFrame) -> float:
    if from_ccy == to_ccy or amount == 0:
        return amount
    rate_from = fx_wide[from_ccy].get(date) if from_ccy in fx_wide.columns else None
    rate_to = fx_wide[to_ccy].get(date) if to_ccy in fx_wide.columns else None
    if not rate_from or not rate_to or pd.isna(rate_from) or pd.isna(rate_to):
        # No FX data to bridge with -- degrade to a same-magnitude passthrough
        # rather than crashing or silently dropping the transaction.
        return amount
    return amount * rate_from / rate_to


def _build_events(txns_df: pd.DataFrame, accounts_df: pd.DataFrame, fx_wide: pd.DataFrame) -> list[dict]:
    acct_ccy = dict(zip(accounts_df["id"], accounts_df["native_currency"]))
    is_holding_map = dict(zip(accounts_df["id"], accounts_df["is_holding"]))
    events: list[dict] = []

    def add(account_id, date, native_delta=0.0, units_delta=0.0):
        if account_id is not None:
            events.append({"account_id": account_id, "date": date, "native_delta": native_delta, "units_delta": units_delta})

    for t in txns_df.itertuples(index=False):
        d, ttype = t.date, t.type
        amt, ccy = t.amount_native or 0.0, t.currency
        units = t.units
        from_id, to_id = t.from_account_id, t.to_account_id

        def conv(target_id):
            return _convert(amt, ccy, acct_ccy.get(target_id), d, fx_wide)

        if ttype == "income":
            add(to_id, d, native_delta=conv(to_id))
        elif ttype == "expense":
            add(from_id, d, native_delta=-conv(from_id))
        elif ttype == "transfer":
            add(from_id, d, native_delta=-conv(from_id))
            add(to_id, d, native_delta=conv(to_id))
        elif ttype == "buy":
            add(from_id, d, native_delta=-conv(from_id))
            add(to_id, d, units_delta=units or 0.0)
        elif ttype == "sell":
            add(from_id, d, units_delta=-(units or 0.0))
            add(to_id, d, native_delta=conv(to_id))
        elif ttype == "dividend":
            add(to_id, d, native_delta=conv(to_id))
        elif ttype == "repayment":
            add(from_id, d, native_delta=-conv(from_id))
            add(to_id, d, native_delta=-conv(to_id))
        elif ttype == "drawdown":
            add(from_id, d, native_delta=conv(from_id))
            add(to_id, d, native_delta=conv(to_id))
        elif ttype == "valuation_adjustment":
            target_id = to_id if to_id is not None else from_id
            if target_id is not None and is_holding_map.get(target_id) and units is not None:
                add(target_id, d, units_delta=units)
            else:
                add(target_id, d, native_delta=amt)
        elif ttype == "opening_balance":
            add(from_id, d, native_delta=-conv(from_id))
            if to_id is not None and is_holding_map.get(to_id):
                add(to_id, d, units_delta=units or 0.0)
            else:
                add(to_id, d, native_delta=conv(to_id))

    return events


def rebuild_all(engine, as_of: dt.date | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    as_of = as_of or dt.date.today()
    with engine.connect() as conn:
        accounts_df = pd.read_sql(sa.select(accounts), conn)
        txns_df = pd.read_sql(sa.select(transactions), conn)
        fx_df = pd.read_sql(sa.select(fx_rates), conn)
        price_df = pd.read_sql(sa.select(prices), conn)

    if accounts_df.empty:
        with engine.begin() as conn:
            conn.execute(valuation_snapshots.delete())
            conn.execute(net_worth_daily.delete())
        return pd.DataFrame(), pd.DataFrame()

    accounts_df["class_"] = accounts_df["class_"].map(_enum_val)
    accounts_df["type"] = accounts_df["type"].map(_enum_val)
    accounts_df["opening_date"] = pd.to_datetime(accounts_df["opening_date"]).dt.date
    if not txns_df.empty:
        txns_df["type"] = txns_df["type"].map(_enum_val)
        txns_df["date"] = pd.to_datetime(txns_df["date"]).dt.date

    earliest = accounts_df["opening_date"].min()
    if not txns_df.empty:
        earliest = min(earliest, txns_df["date"].min())
    date_range = list(pd.date_range(earliest, as_of, freq="D").date)

    fx_wide = _build_fx_wide(fx_df, date_range)
    price_wide, price_ccy = _build_price_wide(price_df, date_range)

    events = _build_events(txns_df, accounts_df, fx_wide) if not txns_df.empty else []
    events_df = pd.DataFrame(events, columns=["account_id", "date", "native_delta", "units_delta"])

    snap_rows: list[dict] = []
    for acct in accounts_df.itertuples(index=False):
        acct_id = int(acct.id)
        currency = acct.native_currency

        if events_df.empty:
            native_series = pd.Series(0.0, index=date_range)
            units_series = pd.Series(0.0, index=date_range)
        else:
            acct_events = events_df[events_df.account_id == acct_id]
            native_series = acct_events.groupby("date")["native_delta"].sum().reindex(date_range, fill_value=0.0).cumsum()
            units_series = acct_events.groupby("date")["units_delta"].sum().reindex(date_range, fill_value=0.0).cumsum()

        fx_series = fx_wide[currency] if currency in fx_wide.columns else pd.Series(pd.NA, index=date_range)

        if acct.is_holding:
            ticker = acct.price_ticker
            price_series = price_wide[ticker] if ticker in price_wide.columns else pd.Series(pd.NA, index=date_range)
            price_currency = price_ccy.get(ticker, currency)
            price_fx_series = fx_wide[price_currency] if price_currency in fx_wide.columns else pd.Series(pd.NA, index=date_range)
            usd_series = units_series * price_series.fillna(0.0) * price_fx_series.fillna(0.0)
            for d, u, p, fxr, usd in zip(date_range, units_series, price_series, price_fx_series, usd_series):
                snap_rows.append({
                    "date": d, "account_id": acct_id, "native_balance": None,
                    "units": float(u),
                    "price_used": None if pd.isna(p) else float(p),
                    "fx_rate_used": None if pd.isna(fxr) else float(fxr),
                    "usd_value": float(usd) if not pd.isna(usd) else 0.0,
                })
        else:
            usd_series = native_series * fx_series.fillna(0.0)
            for d, n, fxr, usd in zip(date_range, native_series, fx_series, usd_series):
                snap_rows.append({
                    "date": d, "account_id": acct_id, "native_balance": float(n),
                    "units": None, "price_used": None,
                    "fx_rate_used": None if pd.isna(fxr) else float(fxr),
                    "usd_value": float(usd) if not pd.isna(usd) else 0.0,
                })

    snap_df = pd.DataFrame(snap_rows)
    merged = snap_df.merge(accounts_df[["id", "class_"]], left_on="account_id", right_on="id")
    assets = merged[merged["class_"] == "asset"].groupby("date")["usd_value"].sum()
    liabs = merged[merged["class_"] == "liability"].groupby("date")["usd_value"].sum()
    nw_df = pd.DataFrame({"total_assets_usd": assets, "total_liabilities_usd": liabs}).fillna(0.0)
    nw_df["net_worth_usd"] = nw_df["total_assets_usd"] - nw_df["total_liabilities_usd"]
    nw_df = nw_df.reset_index().rename(columns={"index": "date"})

    with engine.begin() as conn:
        conn.execute(valuation_snapshots.delete())
        conn.execute(net_worth_daily.delete())
        if not snap_df.empty:
            conn.execute(valuation_snapshots.insert(), snap_df.to_dict("records"))
        if not nw_df.empty:
            conn.execute(net_worth_daily.insert(), nw_df.to_dict("records"))

    return snap_df, nw_df
