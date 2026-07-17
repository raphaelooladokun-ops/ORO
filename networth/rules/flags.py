"""Rule-based quant flags -- the deterministic, trusted layer of the
suggestion engine (see rules/narrative.py for the LLM layer on top).

Each rule reads the latest valuation snapshot (+ ledger, for the
trailing-window savings rule) and returns zero or more flags shaped as
{rule, severity, figure, one_line_explanation}. Thresholds come from
db.get_rule_config -- config.RULE_DEFAULTS overlaid with any `settings`
table rows -- so they're editable from the UI without code changes.
"""
import datetime as dt

import pandas as pd
import sqlalchemy as sa

import db
from models import accounts, transactions, valuation_snapshots

LIQUID_TYPES = {"cash", "mmf", "bank"}


def _enum_val(v):
    return v.value if hasattr(v, "value") else v


def latest_snapshot_with_accounts(engine, as_of: dt.date | None = None) -> pd.DataFrame:
    """One row per account: its most recent valuation_snapshots row joined
    with the account's own fields (class, type, currency, interest_rate)."""
    as_of = as_of or dt.date.today()
    with engine.connect() as conn:
        snap_df = pd.read_sql(
            sa.select(valuation_snapshots).where(valuation_snapshots.c.date <= as_of), conn
        )
        accounts_df = pd.read_sql(sa.select(accounts), conn)
    accounts_df["class_"] = accounts_df["class_"].map(_enum_val)
    accounts_df["type"] = accounts_df["type"].map(_enum_val)
    if snap_df.empty:
        return pd.DataFrame()
    snap_df["date"] = pd.to_datetime(snap_df["date"])
    latest_idx = snap_df.groupby("account_id")["date"].idxmax()
    latest = snap_df.loc[latest_idx]
    return latest.merge(accounts_df, left_on="account_id", right_on="id")


def rule_idle_cash_drag(cfg: dict, snap: pd.DataFrame) -> list[dict]:
    flags = []
    if snap.empty:
        return flags
    liquid = snap[(snap["class_"] == "asset") & (snap["type"].isin(LIQUID_TYPES))]
    for _, row in liquid.iterrows():
        rate = row["interest_rate"] or 0.0
        hurdle = cfg["idle_cash_hurdle_ngn"] if row["native_currency"] == "NGN" else cfg["idle_cash_hurdle_other"]
        if rate < hurdle and row["usd_value"] > 0:
            opp_cost = row["usd_value"] * (hurdle - rate)
            flags.append({
                "rule": "idle_cash_drag",
                "severity": "high" if opp_cost > 500 else "medium",
                "figure": {
                    "account": row["name"], "usd_value": row["usd_value"],
                    "current_rate": rate, "hurdle": hurdle, "annual_opportunity_cost_usd": opp_cost,
                },
                "one_line_explanation": (
                    f"{row['name']} holds ${row['usd_value']:,.0f} yielding {rate*100:.1f}% vs a "
                    f"{hurdle*100:.1f}% hurdle -- ~${opp_cost:,.0f}/yr opportunity cost."
                ),
            })
    return flags


def rule_debt_vs_return_arbitrage(cfg: dict, snap: pd.DataFrame) -> list[dict]:
    flags = []
    if snap.empty:
        return flags
    liabilities = snap[(snap["class_"] == "liability") & (snap["interest_rate"].fillna(0) > 0)]
    liquid_assets = snap[(snap["class_"] == "asset") & (snap["type"].isin(LIQUID_TYPES))]
    for _, debt in liabilities.iterrows():
        debt_rate = debt["interest_rate"] or 0.0
        for _, asset in liquid_assets.iterrows():
            asset_rate = asset["interest_rate"] or 0.0
            if debt_rate > asset_rate and debt["usd_value"] > 0 and asset["usd_value"] > 0:
                spread = debt_rate - asset_rate
                amount = min(debt["usd_value"], asset["usd_value"])
                annual_cost = amount * spread
                flags.append({
                    "rule": "debt_vs_return_arbitrage",
                    "severity": "high" if annual_cost > 500 else "medium",
                    "figure": {
                        "liability": debt["name"], "liability_rate": debt_rate,
                        "asset": asset["name"], "asset_rate": asset_rate,
                        "spread": spread, "amount_usd": amount, "annual_cost_usd": annual_cost,
                    },
                    "one_line_explanation": (
                        f"Paying {debt_rate*100:.1f}% on {debt['name']} while {asset['name']} earns "
                        f"{asset_rate*100:.1f}% -- a {spread*100:.1f}pt spread costs ~${annual_cost:,.0f}/yr "
                        f"on ${amount:,.0f}."
                    ),
                })
    return flags


def rule_concentration_risk(cfg: dict, snap: pd.DataFrame) -> list[dict]:
    flags = []
    if snap.empty:
        return flags
    assets = snap[snap["class_"] == "asset"]
    net_worth = assets["usd_value"].sum() - snap[snap["class_"] == "liability"]["usd_value"].sum()
    if net_worth <= 0:
        return flags
    threshold = cfg["concentration_pct"]

    for _, row in assets.iterrows():
        share = row["usd_value"] / net_worth
        if share > threshold:
            flags.append({
                "rule": "concentration_risk",
                "severity": "high" if share > threshold * 1.5 else "medium",
                "figure": {"account": row["name"], "usd_value": row["usd_value"], "share_of_net_worth": share, "threshold": threshold},
                "one_line_explanation": (
                    f"{row['name']} is {share*100:.0f}% of net worth (${row['usd_value']:,.0f}), "
                    f"above the {threshold*100:.0f}% concentration threshold."
                ),
            })

    by_ccy = assets.groupby("native_currency")["usd_value"].sum()
    for ccy, val in by_ccy.items():
        share = val / net_worth
        if share > threshold:
            flags.append({
                "rule": "concentration_risk",
                "severity": "high" if share > threshold * 1.5 else "medium",
                "figure": {"currency": ccy, "usd_value": val, "share_of_net_worth": share, "threshold": threshold},
                "one_line_explanation": (
                    f"{ccy} exposure is {share*100:.0f}% of net worth (${val:,.0f}), "
                    f"above the {threshold*100:.0f}% concentration threshold."
                ),
            })
    return flags


def rule_savings_rate_burn(engine, cfg: dict, snap: pd.DataFrame) -> list[dict]:
    flags = []
    window = cfg["trailing_window_days"]
    cutoff = dt.date.today() - dt.timedelta(days=window)
    with engine.connect() as conn:
        txns = pd.read_sql(
            sa.select(transactions).where(
                transactions.c.date >= cutoff, transactions.c.type.in_(["income", "expense"])
            ),
            conn,
        )
    if txns.empty:
        return flags
    txns["type"] = txns["type"].map(_enum_val)
    txns["usd"] = txns["amount_native"] * txns["fx_rate_to_usd"]
    income = txns[txns["type"] == "income"]["usd"].sum()
    expense = txns[txns["type"] == "expense"]["usd"].sum()
    net = income - expense
    savings_rate = (net / income) if income > 0 else None

    liquid_assets_usd = (
        snap[(snap["class_"] == "asset") & (snap["type"].isin(LIQUID_TYPES))]["usd_value"].sum()
        if not snap.empty else 0.0
    )
    target = cfg["savings_rate_target"]

    if net < 0 or (savings_rate is not None and savings_rate < target):
        monthly_burn = -net * (30 / window) if net < 0 else None
        runway_months = (liquid_assets_usd / monthly_burn) if monthly_burn else None
        if net < 0 and runway_months is not None:
            explanation = (
                f"Trailing {window}d net cash flow is ${net:,.0f} (burning ~${monthly_burn:,.0f}/mo, "
                f"~{runway_months:.1f} months of liquid runway)."
            )
        elif savings_rate is not None:
            explanation = (
                f"Trailing {window}d net cash flow is ${net:,.0f} -- savings rate {savings_rate*100:.0f}% "
                f"is below the {target*100:.0f}% target."
            )
        else:
            explanation = f"Trailing {window}d net cash flow is ${net:,.0f}."
        flags.append({
            "rule": "savings_rate_burn",
            "severity": "high" if net < 0 else "medium",
            "figure": {
                "window_days": window, "income_usd": income, "expense_usd": expense, "net_usd": net,
                "savings_rate": savings_rate, "target": target,
                "monthly_burn_usd": monthly_burn, "runway_months": runway_months,
            },
            "one_line_explanation": explanation,
        })
    return flags


def rule_fx_exposure_drift(cfg: dict, snap: pd.DataFrame) -> list[dict]:
    flags = []
    if snap.empty:
        return flags
    assets = snap[snap["class_"] == "asset"]
    net_worth = assets["usd_value"].sum() - snap[snap["class_"] == "liability"]["usd_value"].sum()
    if net_worth <= 0:
        return flags
    by_ccy = (assets.groupby("native_currency")["usd_value"].sum() / net_worth).to_dict()
    target_mix = cfg["fx_target_mix"]
    tolerance = cfg["fx_drift_tolerance"]
    for ccy, target_share in target_mix.items():
        actual_share = by_ccy.get(ccy, 0.0)
        drift = actual_share - target_share
        if abs(drift) > tolerance:
            flags.append({
                "rule": "fx_exposure_drift",
                "severity": "medium",
                "figure": {"currency": ccy, "actual_share": actual_share, "target_share": target_share, "drift": drift},
                "one_line_explanation": (
                    f"{ccy} is {actual_share*100:.0f}% of net worth vs a {target_share*100:.0f}% target "
                    f"({'over' if drift > 0 else 'under'} by {abs(drift)*100:.0f}pt)."
                ),
            })
    return flags


def run_all_rules(engine, as_of: dt.date | None = None) -> list[dict]:
    with engine.connect() as conn:
        cfg = db.get_rule_config(conn)
    snap = latest_snapshot_with_accounts(engine, as_of)
    flags: list[dict] = []
    flags += rule_idle_cash_drag(cfg, snap)
    flags += rule_debt_vs_return_arbitrage(cfg, snap)
    flags += rule_concentration_risk(cfg, snap)
    flags += rule_savings_rate_burn(engine, cfg, snap)
    flags += rule_fx_exposure_drift(cfg, snap)
    return flags
