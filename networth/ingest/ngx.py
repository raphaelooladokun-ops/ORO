"""NGX price scraper (afrimetrics.com) with manual-mark fallback.

NOTE: the target page could not be reached from the build sandbox (its
outbound network policy blocked afrimetrics.com -- see the CONNECT 403 in
the proxy status), so the table-parsing logic below could not be verified
against real markup. It is written defensively: multiple rows/cells are
scanned generically rather than relying on a specific CSS class, every
ticker is handled independently so one bad row can't take down the rest,
and the function never raises -- a total failure just means nothing gets
updated and the existing prices are marked stale. If scraping keeps
missing tickers once run against the live page, inspect its HTML and
adjust `_parse_table_rows`. Manual price marks (`record_manual_price`)
work regardless and are the documented fallback per the brief.
"""
import datetime as dt
import re

import requests
import sqlalchemy as sa
from bs4 import BeautifulSoup
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

import config
from models import prices, PriceSource

_PRICE_RE = re.compile(r"[-+]?\d[\d,]*\.?\d*")
_TICKER_RE = re.compile(r"[A-Z][A-Z0-9]{1,9}")


def _parse_table_rows(html: str) -> dict[str, float]:
    """Best-effort: scan every <table> row for a ticker-like first cell and
    a numeric price in a later cell. Returns {TICKER: price}, possibly empty."""
    found: dict[str, float] = {}
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return found

    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            ticker_candidate = cells[0].upper().strip()
            if not _TICKER_RE.fullmatch(ticker_candidate):
                continue
            for cell in cells[1:]:
                m = _PRICE_RE.search(cell.replace(",", ""))
                if m:
                    try:
                        found[ticker_candidate] = float(m.group(0))
                    except ValueError:
                        pass
                    break
    return found


def fetch_ngx_prices(tickers: list[str]) -> dict[str, float]:
    """Fetch config.NGX_SOURCE_URL and return {ticker: price} for whichever
    of `tickers` were found. Raises on network/parse failure -- caller catches."""
    resp = requests.get(
        config.NGX_SOURCE_URL,
        timeout=config.NGX_REQUEST_TIMEOUT_SECONDS,
        headers={"User-Agent": "Mozilla/5.0 (compatible; networth-tracker/1.0)"},
    )
    resp.raise_for_status()
    parsed = _parse_table_rows(resp.text)
    return {t: parsed[t] for t in tickers if t in parsed}


def _upsert_price(conn, ticker, date, price, currency, source, is_stale):
    stmt = sqlite_insert(prices).values(
        ticker=ticker, date=date, price=price, currency=currency, source=source, is_stale=is_stale,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["ticker", "date"],
        set_={"price": stmt.excluded.price, "source": stmt.excluded.source, "is_stale": stmt.excluded.is_stale},
    )
    conn.execute(stmt)


def refresh_ngx_prices(engine, tickers: list[str], date: dt.date | None = None) -> dict:
    """Scrape prices for `tickers`. On any failure (whole-page or per-ticker
    miss), mark that ticker's most recent stored price stale instead of
    writing a new one. Never raises."""
    date = date or dt.date.today()
    report = {"updated": [], "errors": [], "missing": []}
    if not tickers:
        return report

    try:
        found = fetch_ngx_prices(tickers)
    except Exception as exc:
        report["errors"].append(f"page fetch failed: {exc}")
        found = {}

    with engine.begin() as conn:
        for ticker in tickers:
            if ticker in found:
                _upsert_price(conn, ticker, date, found[ticker], "NGN", PriceSource.scrape.value, False)
                report["updated"].append(ticker)
            else:
                report["missing"].append(ticker)
                latest = conn.execute(
                    sa.select(prices.c.id)
                    .where(prices.c.ticker == ticker)
                    .order_by(prices.c.date.desc())
                    .limit(1)
                ).fetchone()
                if latest is not None:
                    conn.execute(prices.update().where(prices.c.id == latest.id).values(is_stale=True))

    return report


def record_manual_price(engine, ticker: str, date: dt.date, price: float, currency: str = "NGN"):
    with engine.begin() as conn:
        _upsert_price(conn, ticker, date, price, currency, PriceSource.manual.value, False)
