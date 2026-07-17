import datetime as dt

import pytest
import sqlalchemy as sa

import db
from engine.valuation import rebuild_all
from models import accounts, transactions, fx_rates
from rules.flags import (
    latest_snapshot_with_accounts,
    rule_idle_cash_drag,
    rule_debt_vs_return_arbitrage,
    rule_concentration_risk,
    rule_savings_rate_burn,
    rule_fx_exposure_drift,
)
import config


@pytest.fixture
def engine():
    eng = sa.create_engine("sqlite:///:memory:", future=True)
    db.init_db(eng)
    return eng


def add_account(engine, **kwargs):
    defaults = dict(is_holding=False, price_ticker=None, interest_rate=None, notes=None, is_active=True)
    defaults.update(kwargs)
    with engine.begin() as conn:
        result = conn.execute(accounts.insert().values(**defaults))
        return result.inserted_primary_key[0]


def add_txn(engine, **kwargs):
    defaults = dict(
        from_account_id=None, to_account_id=None, units=None, unit_price=None,
        category=None, counterparty=None, note=None,
    )
    defaults.update(kwargs)
    with engine.begin() as conn:
        conn.execute(transactions.insert().values(**defaults))


def test_idle_cash_drag_fires_below_hurdle(engine):
    acct = add_account(
        engine, name="NGN Bank", class_="asset", type="bank", native_currency="NGN",
        interest_rate=0.02, opening_date=dt.date(2024, 1, 1),
    )
    with engine.begin() as conn:
        conn.execute(fx_rates.insert().values(date=dt.date(2024, 1, 1), currency="NGN", rate_to_usd=0.001, source="manual"))
    add_txn(engine, date=dt.date(2024, 1, 1), type="income", to_account_id=acct, amount_native=1_000_000.0, currency="NGN", fx_rate_to_usd=0.001)

    rebuild_all(engine, as_of=dt.date(2024, 1, 5))
    snap = latest_snapshot_with_accounts(engine, dt.date(2024, 1, 5))
    flags = rule_idle_cash_drag(config.RULE_DEFAULTS, snap)

    assert len(flags) == 1
    assert flags[0]["rule"] == "idle_cash_drag"
    assert flags[0]["figure"]["current_rate"] == pytest.approx(0.02)


def test_debt_vs_return_arbitrage_fires_when_debt_rate_exceeds_asset_rate(engine):
    loan = add_account(
        engine, name="Loan", class_="liability", type="loan", native_currency="USD",
        interest_rate=0.15, opening_date=dt.date(2024, 1, 1),
    )
    cash = add_account(
        engine, name="Bank", class_="asset", type="bank", native_currency="USD",
        interest_rate=0.03, opening_date=dt.date(2024, 1, 1),
    )
    add_txn(engine, date=dt.date(2024, 1, 1), type="drawdown", from_account_id=loan, to_account_id=cash, amount_native=1000.0, currency="USD", fx_rate_to_usd=1.0)

    rebuild_all(engine, as_of=dt.date(2024, 1, 5))
    snap = latest_snapshot_with_accounts(engine, dt.date(2024, 1, 5))
    flags = rule_debt_vs_return_arbitrage(config.RULE_DEFAULTS, snap)

    assert len(flags) == 1
    assert flags[0]["figure"]["spread"] == pytest.approx(0.12)


def test_concentration_risk_fires_over_threshold(engine):
    big = add_account(engine, name="Big Holding", class_="asset", type="cash", native_currency="USD", opening_date=dt.date(2024, 1, 1))
    small = add_account(engine, name="Small Holding", class_="asset", type="cash", native_currency="USD", opening_date=dt.date(2024, 1, 1))
    add_txn(engine, date=dt.date(2024, 1, 1), type="income", to_account_id=big, amount_native=9000.0, currency="USD", fx_rate_to_usd=1.0)
    add_txn(engine, date=dt.date(2024, 1, 1), type="income", to_account_id=small, amount_native=1000.0, currency="USD", fx_rate_to_usd=1.0)

    rebuild_all(engine, as_of=dt.date(2024, 1, 5))
    snap = latest_snapshot_with_accounts(engine, dt.date(2024, 1, 5))
    flags = rule_concentration_risk(config.RULE_DEFAULTS, snap)

    account_flags = [f for f in flags if "account" in f["figure"]]
    assert any(f["figure"]["account"] == "Big Holding" for f in account_flags)


def test_savings_rate_burn_fires_on_negative_cash_flow(engine):
    cash = add_account(engine, name="Bank", class_="asset", type="bank", native_currency="USD", opening_date=dt.date(2024, 1, 1))
    today = dt.date.today()
    add_txn(engine, date=today - dt.timedelta(days=5), type="income", to_account_id=cash, amount_native=1000.0, currency="USD", fx_rate_to_usd=1.0)
    add_txn(engine, date=today - dt.timedelta(days=3), type="expense", from_account_id=cash, amount_native=2000.0, currency="USD", fx_rate_to_usd=1.0)

    rebuild_all(engine, as_of=today)
    snap = latest_snapshot_with_accounts(engine, today)
    flags = rule_savings_rate_burn(engine, config.RULE_DEFAULTS, snap)

    assert len(flags) == 1
    assert flags[0]["figure"]["net_usd"] == pytest.approx(-1000.0)


def test_fx_exposure_drift_fires_on_drift(engine):
    usd = add_account(engine, name="Bank USD", class_="asset", type="bank", native_currency="USD", opening_date=dt.date(2024, 1, 1))
    add_txn(engine, date=dt.date(2024, 1, 1), type="income", to_account_id=usd, amount_native=1000.0, currency="USD", fx_rate_to_usd=1.0)

    rebuild_all(engine, as_of=dt.date(2024, 1, 5))
    snap = latest_snapshot_with_accounts(engine, dt.date(2024, 1, 5))
    cfg = dict(config.RULE_DEFAULTS)
    flags = rule_fx_exposure_drift(cfg, snap)

    ngn_flags = [f for f in flags if f["figure"].get("currency") == "NGN"]
    assert len(ngn_flags) == 1
    assert ngn_flags[0]["figure"]["actual_share"] == pytest.approx(0.0)
