import datetime as dt

import pytest
import sqlalchemy as sa

import db
from engine.opening_balance import record_opening_balance
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


def test_income_increases_usd_balance(engine):
    a = add_account(engine, name="Bank USD", class_="asset", type="bank", native_currency="USD", opening_date=dt.date(2024, 1, 1))
    add_txn(engine, date=dt.date(2024, 1, 5), type="income", to_account_id=a, amount_native=1000.0, currency="USD", fx_rate_to_usd=1.0)

    snap_df, nw_df = rebuild_all(engine, as_of=dt.date(2024, 1, 10))

    row = snap_df[(snap_df.account_id == a) & (snap_df.date == dt.date(2024, 1, 10))].iloc[0]
    assert row["native_balance"] == pytest.approx(1000.0)
    assert row["usd_value"] == pytest.approx(1000.0)

    before = snap_df[(snap_df.account_id == a) & (snap_df.date == dt.date(2024, 1, 4))].iloc[0]
    assert before["native_balance"] == pytest.approx(0.0)


def test_cross_currency_transfer_bridges_through_fx(engine):
    usd = add_account(engine, name="Bank USD", class_="asset", type="bank", native_currency="USD", opening_date=dt.date(2024, 1, 1))
    ngn = add_account(engine, name="Bank NGN", class_="asset", type="bank", native_currency="NGN", opening_date=dt.date(2024, 1, 1))

    add_fx(engine, "NGN", dt.date(2024, 1, 1), 0.001)  # 1000 NGN = 1 USD
    add_txn(engine, date=dt.date(2024, 1, 1), type="income", to_account_id=usd, amount_native=500.0, currency="USD", fx_rate_to_usd=1.0)
    add_txn(engine, date=dt.date(2024, 1, 2), type="transfer", from_account_id=usd, to_account_id=ngn, amount_native=100.0, currency="USD", fx_rate_to_usd=1.0)

    snap_df, nw_df = rebuild_all(engine, as_of=dt.date(2024, 1, 5))

    usd_row = snap_df[(snap_df.account_id == usd) & (snap_df.date == dt.date(2024, 1, 5))].iloc[0]
    ngn_row = snap_df[(snap_df.account_id == ngn) & (snap_df.date == dt.date(2024, 1, 5))].iloc[0]

    assert usd_row["native_balance"] == pytest.approx(400.0)
    # 100 USD bridged into NGN at 0.001 USD/NGN -> 100 / 0.001 = 100000 NGN
    assert ngn_row["native_balance"] == pytest.approx(100000.0)
    assert ngn_row["usd_value"] == pytest.approx(100.0)


def test_fx_move_changes_usd_value_with_no_new_transactions(engine):
    ngn = add_account(engine, name="Bank NGN", class_="asset", type="bank", native_currency="NGN", opening_date=dt.date(2024, 1, 1))
    add_fx(engine, "NGN", dt.date(2024, 1, 1), 0.001)
    add_txn(engine, date=dt.date(2024, 1, 1), type="income", to_account_id=ngn, amount_native=100000.0, currency="NGN", fx_rate_to_usd=0.001)
    add_fx(engine, "NGN", dt.date(2024, 1, 10), 0.0009)  # NGN weakens

    snap_df, _ = rebuild_all(engine, as_of=dt.date(2024, 1, 15))

    early = snap_df[(snap_df.account_id == ngn) & (snap_df.date == dt.date(2024, 1, 5))].iloc[0]
    late = snap_df[(snap_df.account_id == ngn) & (snap_df.date == dt.date(2024, 1, 15))].iloc[0]

    assert early["native_balance"] == pytest.approx(late["native_balance"])  # no txns in between
    assert early["usd_value"] == pytest.approx(100.0)
    assert late["usd_value"] == pytest.approx(90.0)


def test_buy_and_sell_holding_units_and_price(engine):
    cash = add_account(engine, name="Bank NGN", class_="asset", type="bank", native_currency="NGN", opening_date=dt.date(2024, 1, 1))
    stock = add_account(
        engine, name="MTN", class_="asset", type="equity_holding", native_currency="NGN",
        is_holding=True, price_ticker="MTNN", opening_date=dt.date(2024, 1, 1),
    )
    add_fx(engine, "NGN", dt.date(2024, 1, 1), 0.001)
    add_price(engine, "MTNN", dt.date(2024, 1, 1), 200.0)
    add_price(engine, "MTNN", dt.date(2024, 1, 10), 250.0)

    add_txn(engine, date=dt.date(2024, 1, 1), type="income", to_account_id=cash, amount_native=1_000_000.0, currency="NGN", fx_rate_to_usd=0.001)
    add_txn(engine, date=dt.date(2024, 1, 2), type="buy", from_account_id=cash, to_account_id=stock, amount_native=20_000.0, currency="NGN", fx_rate_to_usd=0.001, units=100.0, unit_price=200.0)

    snap_df, _ = rebuild_all(engine, as_of=dt.date(2024, 1, 10))

    stock_row = snap_df[(snap_df.account_id == stock) & (snap_df.date == dt.date(2024, 1, 10))].iloc[0]
    assert stock_row["units"] == pytest.approx(100.0)
    assert stock_row["price_used"] == pytest.approx(250.0)
    assert stock_row["usd_value"] == pytest.approx(100 * 250.0 * 0.001)

    add_txn(engine, date=dt.date(2024, 1, 11), type="sell", from_account_id=stock, to_account_id=cash, amount_native=25_000.0, currency="NGN", fx_rate_to_usd=0.001, units=100.0, unit_price=250.0)
    snap_df2, _ = rebuild_all(engine, as_of=dt.date(2024, 1, 12))
    stock_row2 = snap_df2[(snap_df2.account_id == stock) & (snap_df2.date == dt.date(2024, 1, 12))].iloc[0]
    assert stock_row2["units"] == pytest.approx(0.0)


def test_net_worth_daily_subtracts_liabilities(engine):
    cash = add_account(engine, name="Bank USD", class_="asset", type="bank", native_currency="USD", opening_date=dt.date(2024, 1, 1))
    loan = add_account(engine, name="Loan", class_="liability", type="loan", native_currency="USD", opening_date=dt.date(2024, 1, 1))

    add_txn(engine, date=dt.date(2024, 1, 1), type="drawdown", from_account_id=loan, to_account_id=cash, amount_native=500.0, currency="USD", fx_rate_to_usd=1.0)

    snap_df, nw_df = rebuild_all(engine, as_of=dt.date(2024, 1, 5))
    row = nw_df[nw_df.date == dt.date(2024, 1, 5)].iloc[0]

    assert row["total_assets_usd"] == pytest.approx(500.0)
    assert row["total_liabilities_usd"] == pytest.approx(500.0)
    assert row["net_worth_usd"] == pytest.approx(0.0)  # drawdown nets to zero: cash up, debt up equally


def test_opening_balance_monetary_seeds_balance_via_equity_contra(engine):
    cash = add_account(engine, name="Bank USD", class_="asset", type="bank", native_currency="USD", opening_date=dt.date(2024, 1, 1))
    record_opening_balance(engine, cash, dt.date(2024, 1, 1), "USD", is_holding=False, amount_native=2500.0)

    snap_df, nw_df = rebuild_all(engine, as_of=dt.date(2024, 1, 5))

    cash_row = snap_df[(snap_df.account_id == cash) & (snap_df.date == dt.date(2024, 1, 5))].iloc[0]
    assert cash_row["native_balance"] == pytest.approx(2500.0)
    assert cash_row["usd_value"] == pytest.approx(2500.0)

    # The equity contra account absorbed the opposite leg...
    with engine.connect() as conn:
        equity_row = conn.execute(sa.text("SELECT id, class_ FROM accounts WHERE name = 'Opening Balance Equity'")).fetchone()
    assert equity_row is not None
    equity_id = equity_row[0]
    equity_snap = snap_df[(snap_df.account_id == equity_id) & (snap_df.date == dt.date(2024, 1, 5))].iloc[0]
    assert equity_snap["native_balance"] == pytest.approx(-2500.0)

    # ...but is excluded from net worth entirely (not asset, not liability).
    nw_row = nw_df[nw_df.date == dt.date(2024, 1, 5)].iloc[0]
    assert nw_row["total_assets_usd"] == pytest.approx(2500.0)
    assert nw_row["net_worth_usd"] == pytest.approx(2500.0)


def test_opening_balance_holding_seeds_units_and_optional_cost(engine):
    stock = add_account(
        engine, name="MTN", class_="asset", type="equity_holding", native_currency="NGN",
        is_holding=True, price_ticker="MTNN", opening_date=dt.date(2024, 1, 1),
    )
    add_fx(engine, "NGN", dt.date(2024, 1, 1), 0.001)
    add_price(engine, "MTNN", dt.date(2024, 1, 1), 220.0)

    record_opening_balance(
        engine, stock, dt.date(2024, 1, 1), "NGN", is_holding=True, units=150.0, unit_price=200.0,
    )

    snap_df, _ = rebuild_all(engine, as_of=dt.date(2024, 1, 5))
    stock_row = snap_df[(snap_df.account_id == stock) & (snap_df.date == dt.date(2024, 1, 5))].iloc[0]
    assert stock_row["units"] == pytest.approx(150.0)
    assert stock_row["usd_value"] == pytest.approx(150.0 * 220.0 * 0.001)  # valued at the *stored price*, not opening cost
