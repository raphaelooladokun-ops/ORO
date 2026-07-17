import datetime as dt

import pytest
import sqlalchemy as sa

import db
from engine.attribution import compute_bridge
from engine.valuation import rebuild_all
from models import accounts, transactions, fx_rates, prices


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


def add_fx(engine, currency, date, rate, source="manual"):
    with engine.begin() as conn:
        conn.execute(fx_rates.insert().values(date=date, currency=currency, rate_to_usd=rate, source=source))


def add_price(engine, ticker, date, price, currency="NGN", source="manual"):
    with engine.begin() as conn:
        conn.execute(prices.insert().values(ticker=ticker, date=date, price=price, currency=currency, source=source, is_stale=False))


def test_bridge_components_sum_to_total_change_mixed_scenario(engine):
    cash_usd = add_account(engine, name="Bank USD", class_="asset", type="bank", native_currency="USD", opening_date=dt.date(2024, 1, 1))
    cash_ngn = add_account(engine, name="Bank NGN", class_="asset", type="bank", native_currency="NGN", opening_date=dt.date(2024, 1, 1))
    stock = add_account(
        engine, name="MTN", class_="asset", type="equity_holding", native_currency="NGN",
        is_holding=True, price_ticker="MTNN", opening_date=dt.date(2024, 1, 1),
    )
    loan = add_account(engine, name="Loan", class_="liability", type="loan", native_currency="USD", opening_date=dt.date(2024, 1, 1))

    add_fx(engine, "NGN", dt.date(2024, 1, 1), 0.001)
    add_fx(engine, "NGN", dt.date(2024, 1, 15), 0.0009)
    add_price(engine, "MTNN", dt.date(2024, 1, 1), 200.0)
    add_price(engine, "MTNN", dt.date(2024, 1, 15), 230.0)

    # income (external cash flow)
    add_txn(engine, date=dt.date(2024, 1, 2), type="income", to_account_id=cash_usd, amount_native=1000.0, currency="USD", fx_rate_to_usd=1.0)
    # internal buy (should roughly net to zero cash-flow contribution)
    add_txn(engine, date=dt.date(2024, 1, 3), type="transfer", from_account_id=cash_usd, to_account_id=cash_ngn, amount_native=300.0, currency="USD", fx_rate_to_usd=1.0)
    add_txn(engine, date=dt.date(2024, 1, 4), type="buy", from_account_id=cash_ngn, to_account_id=stock, amount_native=60_000.0, currency="NGN", fx_rate_to_usd=0.001, units=300.0, unit_price=200.0)
    # drawdown (wash: asset up, liability up)
    add_txn(engine, date=dt.date(2024, 1, 5), type="drawdown", from_account_id=loan, to_account_id=cash_usd, amount_native=200.0, currency="USD", fx_rate_to_usd=1.0)
    # expense (external outflow)
    add_txn(engine, date=dt.date(2024, 1, 6), type="expense", from_account_id=cash_usd, amount_native=50.0, currency="USD", fx_rate_to_usd=1.0)

    rebuild_all(engine, as_of=dt.date(2024, 1, 20))

    bridge, table_df = compute_bridge(engine, dt.date(2024, 1, 1), dt.date(2024, 1, 15))

    assert bridge["residual"] == pytest.approx(0.0, abs=1e-6)
    assert bridge["cash_flow"] + bridge["market_pnl"] + bridge["fx_pnl"] == pytest.approx(bridge["total_change"], abs=1e-6)
    assert not table_df.empty
    # per-account totals should also sum to the grand total change
    assert table_df["total"].sum() == pytest.approx(bridge["total_change"], abs=1e-6)


def test_bridge_pure_fx_move_isolated(engine):
    # Bridge starts *before* the deposit so the deposit's cash-flow effect
    # falls inside the window (a transaction dated exactly on start_date is
    # already baked into the opening balance, and out of scope for the delta).
    ngn = add_account(engine, name="Bank NGN", class_="asset", type="bank", native_currency="NGN", opening_date=dt.date(2024, 1, 1))
    add_fx(engine, "NGN", dt.date(2024, 1, 1), 0.001)
    add_txn(engine, date=dt.date(2024, 1, 3), type="income", to_account_id=ngn, amount_native=100000.0, currency="NGN", fx_rate_to_usd=0.001)
    add_fx(engine, "NGN", dt.date(2024, 1, 10), 0.0009)

    rebuild_all(engine, as_of=dt.date(2024, 1, 10))
    bridge, _ = compute_bridge(engine, dt.date(2024, 1, 1), dt.date(2024, 1, 10))

    assert bridge["market_pnl"] == pytest.approx(0.0, abs=1e-6)
    assert bridge["fx_pnl"] == pytest.approx(-10.0, abs=1e-6)  # 100 USD -> 90 USD, on the deposited balance
    assert bridge["cash_flow"] == pytest.approx(100.0, abs=1e-6)  # the deposit itself
