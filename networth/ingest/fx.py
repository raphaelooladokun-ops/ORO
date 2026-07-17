"""Frankfurter (ECB) FX ingestion.

Only fetches for currencies Frankfurter actually covers
(config.FRANKFURTER_SUPPORTED_CURRENCIES). Currencies outside that set
(notably NGN) are reported back as skipped so the caller/UI can prompt for
manual fx_rates entry -- this must never raise, per the brief's "nothing
crashes on missing FX date" requirement.

Frankfurter's time-series endpoint only returns rates for days it has data
for (weekdays ECB publishes on); we intentionally do NOT synthesize
carry-forward rows here. Forward-filling for weekends/holidays happens at
read time (engine/lookup.py, engine/valuation.py), so the table only ever
holds rates Frankfurter/an operator actually asserted.
"""
import datetime as dt

import requests
import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

import config
from models import fx_rates, FxSource


def fetch_frankfurter_series(currency: str, start: dt.date, end: dt.date) -> dict[dt.date, float]:
    url = f"{config.FRANKFURTER_BASE_URL}/{start.isoformat()}..{end.isoformat()}"
    resp = requests.get(url, params={"from": currency, "to": "USD"}, timeout=config.NGX_REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    data = resp.json()
    return {
        dt.date.fromisoformat(date_str): rate_map["USD"]
        for date_str, rate_map in data.get("rates", {}).items()
        if "USD" in rate_map
    }


def upsert_fx_rates(conn, currency: str, series: dict[dt.date, float], source: str = FxSource.frankfurter.value):
    if not series:
        return
    rows = [
        {"date": date, "currency": currency, "rate_to_usd": rate, "source": source}
        for date, rate in series.items()
    ]
    stmt = sqlite_insert(fx_rates).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["date", "currency"],
        set_={"rate_to_usd": stmt.excluded.rate_to_usd, "source": stmt.excluded.source},
    )
    conn.execute(stmt)


def refresh_fx_rates(engine, currencies: set[str], start_date: dt.date, end_date: dt.date | None = None) -> dict:
    """Pull and upsert FX history for every currency Frankfurter covers.

    Returns a report: {"updated": [...], "skipped_unsupported": [...], "errors": [...]}.
    Never raises -- a failure for one currency is recorded and the rest continue.
    """
    end_date = end_date or dt.date.today()
    report = {"updated": [], "skipped_unsupported": [], "errors": []}
    for currency in sorted(set(currencies) - {"USD"}):
        if currency not in config.FRANKFURTER_SUPPORTED_CURRENCIES:
            report["skipped_unsupported"].append(currency)
            continue
        try:
            series = fetch_frankfurter_series(currency, start_date, end_date)
            with engine.begin() as conn:
                upsert_fx_rates(conn, currency, series)
            report["updated"].append(currency)
        except Exception as exc:  # network/API failures must not crash the app
            report["errors"].append(f"{currency}: {exc}")
    return report


def record_manual_fx_rate(engine, currency: str, date: dt.date, rate_to_usd: float):
    with engine.begin() as conn:
        upsert_fx_rates(conn, currency, {date: rate_to_usd}, source=FxSource.manual.value)
