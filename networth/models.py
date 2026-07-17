"""SQLAlchemy Core table definitions.

Extends the brief's schema with one field the rule engine needs but the
brief didn't specify a home for: `accounts.interest_rate` (annualized,
e.g. 0.02 = 2%/yr). Without it there is no way to compute "idle cash
yielding below hurdle" or "debt rate exceeds asset return" -- both rules
are explicitly required in the brief. It is nullable/optional everywhere
else in the app.
"""
import enum

import sqlalchemy as sa

metadata = sa.MetaData()


class AccountClass(str, enum.Enum):
    asset = "asset"
    liability = "liability"


class AccountType(str, enum.Enum):
    cash = "cash"
    mmf = "mmf"
    bank = "bank"
    receivable = "receivable"
    equity_holding = "equity_holding"
    crypto = "crypto"
    property = "property"
    loan = "loan"
    credit_card = "credit_card"
    payable = "payable"
    other = "other"


class TxnType(str, enum.Enum):
    income = "income"
    expense = "expense"
    transfer = "transfer"
    buy = "buy"
    sell = "sell"
    repayment = "repayment"
    drawdown = "drawdown"
    valuation_adjustment = "valuation_adjustment"
    dividend = "dividend"


class PriceSource(str, enum.Enum):
    scrape = "scrape"
    manual = "manual"


class FxSource(str, enum.Enum):
    frankfurter = "frankfurter"
    manual = "manual"


accounts = sa.Table(
    "accounts",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("name", sa.String, nullable=False, unique=True),
    sa.Column("class_", sa.Enum(AccountClass, name="account_class"), nullable=False),
    sa.Column("type", sa.Enum(AccountType, name="account_type"), nullable=False),
    sa.Column("is_holding", sa.Boolean, nullable=False, default=False),
    sa.Column("native_currency", sa.String(3), nullable=False),
    sa.Column("price_ticker", sa.String, nullable=True),
    sa.Column("interest_rate", sa.Float, nullable=True),
    sa.Column("opening_date", sa.Date, nullable=False),
    sa.Column("notes", sa.String, nullable=True),
    sa.Column("is_active", sa.Boolean, nullable=False, default=True),
)

transactions = sa.Table(
    "transactions",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("date", sa.Date, nullable=False),
    sa.Column("type", sa.Enum(TxnType, name="txn_type"), nullable=False),
    sa.Column("from_account_id", sa.Integer, sa.ForeignKey("accounts.id"), nullable=True),
    sa.Column("to_account_id", sa.Integer, sa.ForeignKey("accounts.id"), nullable=True),
    sa.Column("amount_native", sa.Float, nullable=False, default=0.0),
    sa.Column("currency", sa.String(3), nullable=False),
    sa.Column("fx_rate_to_usd", sa.Float, nullable=False),
    sa.Column("units", sa.Float, nullable=True),
    sa.Column("unit_price", sa.Float, nullable=True),
    sa.Column("category", sa.String, nullable=True),
    sa.Column("counterparty", sa.String, nullable=True),
    sa.Column("note", sa.String, nullable=True),
)

prices = sa.Table(
    "prices",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("ticker", sa.String, nullable=False),
    sa.Column("date", sa.Date, nullable=False),
    sa.Column("price", sa.Float, nullable=False),
    sa.Column("currency", sa.String(3), nullable=False, default="NGN"),
    sa.Column("source", sa.Enum(PriceSource, name="price_source"), nullable=False),
    sa.Column("is_stale", sa.Boolean, nullable=False, default=False),
    sa.UniqueConstraint("ticker", "date", name="uq_price_ticker_date"),
)

fx_rates = sa.Table(
    "fx_rates",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("date", sa.Date, nullable=False),
    sa.Column("currency", sa.String(3), nullable=False),
    sa.Column("rate_to_usd", sa.Float, nullable=False),
    sa.Column("source", sa.Enum(FxSource, name="fx_source"), nullable=False),
    sa.UniqueConstraint("date", "currency", name="uq_fx_date_currency"),
)

valuation_snapshots = sa.Table(
    "valuation_snapshots",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("date", sa.Date, nullable=False),
    sa.Column("account_id", sa.Integer, sa.ForeignKey("accounts.id"), nullable=False),
    sa.Column("native_balance", sa.Float, nullable=True),
    sa.Column("units", sa.Float, nullable=True),
    sa.Column("price_used", sa.Float, nullable=True),
    sa.Column("fx_rate_used", sa.Float, nullable=False),
    sa.Column("usd_value", sa.Float, nullable=False),
    sa.UniqueConstraint("date", "account_id", name="uq_snapshot_date_account"),
)

net_worth_daily = sa.Table(
    "net_worth_daily",
    metadata,
    sa.Column("date", sa.Date, primary_key=True),
    sa.Column("total_assets_usd", sa.Float, nullable=False),
    sa.Column("total_liabilities_usd", sa.Float, nullable=False),
    sa.Column("net_worth_usd", sa.Float, nullable=False),
)

# Editable overrides for config.RULE_DEFAULTS, keyed by the same dict keys.
# value_json holds a JSON-encoded scalar or dict (for fx_target_mix).
settings = sa.Table(
    "settings",
    metadata,
    sa.Column("key", sa.String, primary_key=True),
    sa.Column("value_json", sa.String, nullable=False),
)
