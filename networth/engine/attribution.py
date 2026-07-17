"""Cash-flow / market / FX attribution bridge between two dates.

Reads `valuation_snapshots` (already-computed daily native_balance/units,
price_used, fx_rate_used per account) rather than re-deriving from the
ledger, so it stays consistent with whatever `engine.valuation.rebuild_all`
last produced.

For each account and each pair of consecutive snapshot days (d-1, d), the
change in that account's USD value is split three ways using a standard
sequential (Brinson-style) decomposition, applied to the *closing* quantity
of the day so a same-day trade's price move lands in market P&L rather than
being invented out of nothing:

    cash_flow_d = (qty_d - qty_{d-1}) * price_{d-1} * fx_{d-1}
    market_d    =  qty_d             * (price_d - price_{d-1}) * fx_{d-1}
    fx_d        =  qty_d *  price_d  * (fx_d - fx_{d-1})

These three telescope exactly to (usd_value_d - usd_value_{d-1}) with no
residual term, for every day, so summed over the whole period they always
reconstruct the account's total change exactly. Monetary accounts are
treated as qty=native_balance, price=1 (their "market" component is
therefore always zero -- price doesn't apply to cash). Holding accounts use
qty=units, price=price_used.

An account's own three components are added to net worth for asset
accounts and subtracted for liability accounts, so a wash transaction (e.g.
drawdown: cash up, debt up by the same USD amount) nets to ~0 across the
pair of accounts it touches -- consistent with the brief's "value added by
the user" framing for the cash-flow bucket. Accounts of any other class
(currently just the "equity" contra account used for opening balances) are
skipped entirely -- they're excluded from net worth in net_worth_daily, so
they must not contribute to any bucket here either, not even via a sign flip.

`opening_balance` transactions are excluded from all three buckets -- they
represent an account's starting state, not activity that happened during
the bridge period. In the common case (an account's opening balance predates
the bridge's start_date) this is automatic: the balance is already baked
into `opening_nw` and produces no delta inside [start, end] at all. The one
case that needs explicit handling is an opening_balance dated *inside* the
window (an account onboarded mid-period) -- naively that day's quantity jump
would otherwise be counted as cash flow. Its full delta is instead routed
into a separate `opening_balance_adj` component so the reconciliation
identity (opening + cash_flow + market_pnl + fx_pnl + opening_balance_adj ==
closing) still holds exactly; it is 0.0 in the common case.
"""
import datetime as dt

import numpy as np
import pandas as pd
import sqlalchemy as sa

from models import accounts, transactions, valuation_snapshots, net_worth_daily


def _enum_val(v):
    return v.value if hasattr(v, "value") else v


def compute_bridge(engine, start_date: dt.date, end_date: dt.date) -> tuple[dict, pd.DataFrame]:
    with engine.connect() as conn:
        snap_df = pd.read_sql(
            sa.select(valuation_snapshots).where(
                valuation_snapshots.c.date >= start_date, valuation_snapshots.c.date <= end_date
            ),
            conn,
        )
        accounts_df = pd.read_sql(sa.select(accounts), conn)
        nw_df = pd.read_sql(sa.select(net_worth_daily), conn)
        ob_df = pd.read_sql(
            sa.select(transactions.c.to_account_id, transactions.c.date).where(
                transactions.c.type == "opening_balance"
            ),
            conn,
        )

    accounts_df["class_"] = accounts_df["class_"].map(_enum_val)
    class_map = dict(zip(accounts_df["id"], accounts_df["class_"]))
    name_map = dict(zip(accounts_df["id"], accounts_df["name"]))
    is_holding_map = dict(zip(accounts_df["id"], accounts_df["is_holding"]))

    opening_balance_dates: set[tuple[int, dt.date]] = set()
    if not ob_df.empty:
        ob_df["date"] = pd.to_datetime(ob_df["date"]).dt.date
        opening_balance_dates = set(zip(ob_df["to_account_id"], ob_df["date"]))

    per_account: dict[int, dict] = {}

    if not snap_df.empty:
        snap_df["date"] = pd.to_datetime(snap_df["date"]).dt.date
        for acct_id, grp in snap_df.groupby("account_id"):
            grp = grp.sort_values("date")
            if len(grp) < 2:
                continue
            acct_class = class_map.get(acct_id)
            if acct_class not in ("asset", "liability"):
                # Contra-equity accounts (e.g. Opening Balance Equity) are
                # excluded from net worth entirely -- they must not
                # contribute to any bucket, not even via a sign flip.
                continue
            sign = 1.0 if acct_class == "asset" else -1.0
            is_holding = bool(is_holding_map.get(acct_id))

            qty = grp["units"].to_numpy(dtype=float) if is_holding else grp["native_balance"].to_numpy(dtype=float)
            qty = np.nan_to_num(qty)
            price = grp["price_used"].fillna(1.0).to_numpy(dtype=float) if is_holding else np.ones(len(grp))
            fx = np.nan_to_num(grp["fx_rate_used"].to_numpy(dtype=float))

            q0, q1 = qty[:-1], qty[1:]
            p0, p1 = price[:-1], price[1:]
            fx0, fx1 = fx[:-1], fx[1:]

            cash_flow_daily = (q1 - q0) * p0 * fx0
            market_daily = q1 * (p1 - p0) * fx0
            fx_daily = q1 * p1 * (fx1 - fx0)

            # Transitions landing on an opening_balance date are re-routed
            # wholesale into opening_balance_adj rather than split three ways.
            transition_dates = grp["date"].to_numpy()[1:]
            excluded = np.array([(acct_id, d) in opening_balance_dates for d in transition_dates])

            opening_adj_daily = np.where(excluded, cash_flow_daily + market_daily + fx_daily, 0.0)
            cash_flow_daily = np.where(excluded, 0.0, cash_flow_daily)
            market_daily = np.where(excluded, 0.0, market_daily)
            fx_daily = np.where(excluded, 0.0, fx_daily)

            per_account[acct_id] = {
                "account_name": name_map.get(acct_id, str(acct_id)),
                "cash_flow": sign * cash_flow_daily.sum(),
                "market_pnl": sign * market_daily.sum(),
                "fx_pnl": sign * fx_daily.sum(),
                "opening_balance_adj": sign * opening_adj_daily.sum(),
            }
            per_account[acct_id]["total"] = (
                per_account[acct_id]["cash_flow"]
                + per_account[acct_id]["market_pnl"]
                + per_account[acct_id]["fx_pnl"]
                + per_account[acct_id]["opening_balance_adj"]
            )

    totals = {"cash_flow": 0.0, "market_pnl": 0.0, "fx_pnl": 0.0, "opening_balance_adj": 0.0}
    for v in per_account.values():
        totals["cash_flow"] += v["cash_flow"]
        totals["market_pnl"] += v["market_pnl"]
        totals["fx_pnl"] += v["fx_pnl"]
        totals["opening_balance_adj"] += v["opening_balance_adj"]

    def nw_on_or_before(date):
        if nw_df.empty:
            return 0.0
        d = pd.to_datetime(nw_df["date"]).dt.date
        sub = nw_df.assign(_d=d)
        sub = sub[sub["_d"] <= date]
        if sub.empty:
            return 0.0
        return float(sub.sort_values("_d").iloc[-1]["net_worth_usd"])

    opening_nw = nw_on_or_before(start_date)
    closing_nw = nw_on_or_before(end_date)
    total_change = closing_nw - opening_nw

    bridge = {
        "start_date": start_date,
        "end_date": end_date,
        "opening_nw": opening_nw,
        "cash_flow": totals["cash_flow"],
        "market_pnl": totals["market_pnl"],
        "fx_pnl": totals["fx_pnl"],
        "opening_balance_adj": totals["opening_balance_adj"],
        "closing_nw": closing_nw,
        "total_change": total_change,
        "residual": total_change - (
            totals["cash_flow"] + totals["market_pnl"] + totals["fx_pnl"] + totals["opening_balance_adj"]
        ),
    }

    table_df = (
        pd.DataFrame.from_dict(per_account, orient="index")
        .reset_index(names="account_id")
        .sort_values("total", ascending=False)
        if per_account
        else pd.DataFrame(
            columns=["account_id", "account_name", "cash_flow", "market_pnl", "fx_pnl", "opening_balance_adj", "total"]
        )
    )

    return bridge, table_df
