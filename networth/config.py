"""Central configuration: paths, base currency, thresholds, external service settings.

All values here are defaults. Where the brief calls for "configurable thresholds",
they are also mirrored into the `settings` table on first run (see db.py) so they
can be edited from the UI without touching this file.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "networth.db"
DB_URL = f"sqlite:///{DB_PATH}"

BASE_CURRENCY = "USD"

# Singleton contra account name for opening balances (see engine/opening_balance.py).
OPENING_BALANCE_EQUITY_NAME = "Opening Balance Equity"

# --- Anthropic (weekly narrative) ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

# --- FX ingestion (Frankfurter / ECB reference rates) ---
FRANKFURTER_BASE_URL = "https://api.frankfurter.app"

# Frankfurter serves ECB reference rates, which only cover this fixed currency
# set. NGN (and most other African currencies) are NOT included, even though
# NGX holdings make NGN central to this tracker. Currencies outside this set
# fall back to manual fx_rates entry in the UI (source='manual'), using the
# same stale/manual-override pattern as prices.
FRANKFURTER_SUPPORTED_CURRENCIES = {
    "USD", "EUR", "JPY", "BGN", "CZK", "DKK", "GBP", "HUF", "PLN", "RON",
    "SEK", "CHF", "ISK", "NOK", "TRY", "AUD", "BRL", "CAD", "CNY", "HKD",
    "IDR", "ILS", "INR", "KRW", "MXN", "MYR", "NZD", "PHP", "SGD", "THB",
    "ZAR",
}

# --- NGX price scraper ---
# Fetch method left as 'requests' (static-page assumption). The target page
# could not be reached from this build environment (outbound network policy
# blocked afrimetrics.com), so the parser in ingest/ngx.py could not be
# verified against real markup. It is written defensively (try/except,
# multiple selector strategies, never raises) and always falls back to the
# manual price-mark path, but the selectors should be checked against the
# live page and adjusted if the scrape keeps failing.
NGX_SOURCE_URL = "https://afrimetrics.com/"
NGX_FETCH_METHOD = "requests"  # 'requests' | 'playwright'
NGX_REQUEST_TIMEOUT_SECONDS = 15

# A price is considered stale once it hasn't refreshed in this many days.
STALE_AFTER_DAYS = 3

# --- Rule engine defaults (see rules/flags.py). Overridable via `settings` table. ---
RULE_DEFAULTS = {
    "idle_cash_hurdle_ngn": 0.158,
    "idle_cash_hurdle_other": 0.04,
    "concentration_pct": 0.25,
    "savings_rate_target": 0.20,
    "fx_target_mix": {"USD": 0.6, "NGN": 0.4},
    "fx_drift_tolerance": 0.10,
    "trailing_window_days": 30,
}
