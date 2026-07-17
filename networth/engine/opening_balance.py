"""Opening-balance bootstrapping.

An account's opening balance is never written directly onto the account --
it is posted as an `opening_balance` ledger transaction (from the singleton
"Opening Balance Equity" contra account, to the new account), exactly like
every other balance-affecting event. The ledger stays the sole source of
truth; the valuation engine derives the account's starting position from
this transaction the same way it derives everything else.

The equity account is excluded from net worth by construction: it carries
`class_ = AccountClass.equity`, and net_worth_daily only sums accounts whose
class is "asset" or "liability" (see engine/valuation.rebuild_all).
"""
import datetime as dt

import sqlalchemy as sa

import config
from engine.lookup import fx_rate_asof
from models import accounts, transactions, AccountClass, AccountType, TxnType


def get_or_create_equity_account(engine, as_of_date: dt.date | None = None) -> int:
    """Return the id of the singleton Opening Balance Equity account, creating it on first use."""
    with engine.begin() as conn:
        row = conn.execute(
            sa.select(accounts.c.id).where(accounts.c.name == config.OPENING_BALANCE_EQUITY_NAME)
        ).fetchone()
        if row is not None:
            return row[0]
        result = conn.execute(
            accounts.insert().values(
                name=config.OPENING_BALANCE_EQUITY_NAME,
                class_=AccountClass.equity.value,
                type=AccountType.equity.value,
                is_holding=False,
                native_currency=config.BASE_CURRENCY,
                price_ticker=None,
                interest_rate=None,
                opening_date=as_of_date or dt.date.today(),
                notes="Auto-created contra account for account opening balances. Excluded from net worth.",
                is_active=True,
            )
        )
        return result.inserted_primary_key[0]


def record_opening_balance(
    engine,
    account_id: int,
    opening_date: dt.date,
    currency: str,
    is_holding: bool,
    amount_native: float | None = None,
    units: float | None = None,
    unit_price: float | None = None,
) -> None:
    """Post the opening_balance transaction for a newly created account.

    Monetary accounts: pass `amount_native` (in the account's own currency).
    Holding accounts: pass `units` and, optionally, `unit_price` (opening
    unit cost) -- the contra leg to equity is only monetary-valued when a
    unit cost is given; without one the position is still recorded but the
    equity contra leg carries no dollar amount (nothing to value it at).
    """
    equity_id = get_or_create_equity_account(engine, opening_date)

    with engine.begin() as conn:
        fx_rate = fx_rate_asof(conn, currency, opening_date) or 1.0

        if is_holding:
            contra_amount = (units or 0.0) * unit_price if unit_price else 0.0
            conn.execute(
                transactions.insert().values(
                    date=opening_date,
                    type=TxnType.opening_balance.value,
                    from_account_id=equity_id,
                    to_account_id=account_id,
                    amount_native=contra_amount,
                    currency=currency,
                    fx_rate_to_usd=fx_rate,
                    units=units,
                    unit_price=unit_price,
                    category="opening_balance",
                    counterparty=None,
                    note="Opening balance",
                )
            )
        else:
            conn.execute(
                transactions.insert().values(
                    date=opening_date,
                    type=TxnType.opening_balance.value,
                    from_account_id=equity_id,
                    to_account_id=account_id,
                    amount_native=amount_native or 0.0,
                    currency=currency,
                    fx_rate_to_usd=fx_rate,
                    units=None,
                    unit_price=None,
                    category="opening_balance",
                    counterparty=None,
                    note="Opening balance",
                )
            )
