"""The EODHD code the market display sends for a symbol (lane UI4b, step 1).

A symbol that already ends in an EODHD exchange code (INDX, CC, FOREX, SHG, US) is passed through
unchanged; a bare ticker still gets the guessed suffix, as before.
"""
import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.backend.market_display import _eodhd_symbol_code
from dashboard.backend.routes import display


@pytest.mark.parametrize(
    "symbol, expected",
    [
        ("GSPC.INDX", "GSPC.INDX"),
        ("BTC-USD.CC", "BTC-USD.CC"),
        ("000001.SHG", "000001.SHG"),
        ("AIR.PA", "AIR.PA"),
        ("AAPL.US", "AAPL.US"),
        ("EURUSD.FOREX", "EURUSD.FOREX"),
        ("BRK.B", "BRK.B.US"),
        ("AAPL", "AAPL.US"),
        ("BTC-USD", "BTC-USD.CC"),
        ("EURUSD", "EURUSD.FOREX"),
    ],
)
def test_symbol_code(symbol, expected):
    assert _eodhd_symbol_code(symbol) == expected


def test_search_rows_carry_the_us_symbol(monkeypatch, tmp_path):
    """Lane LK1: a search hit is answered from the SEC name list, with the symbol "<TICKER>.US"."""
    from dashboard.backend import eventsdb

    db = tmp_path / "events.db"
    eventsdb.init_events_db(db)
    with eventsdb.events_transaction(db) as conn:
        conn.execute(
            "INSERT INTO research_cik_ticker_map (cik, ticker, effective_from, effective_to, source, name, loaded_at) "
            "VALUES (884394, 'SPY', NULL, NULL, 'sec_company_tickers', 'SPDR S&P 500 ETF Trust', 1)"
        )
    monkeypatch.setattr(eventsdb, "DB_PATH", db)
    app = FastAPI()
    app.include_router(display.router)
    body = TestClient(app).get("/api/display/search", params={"q": "spy"}).json()
    assert body["results"] == [
        {"symbol": "SPY.US", "ticker": "SPY", "name": "SPDR S&P 500 ETF Trust", "cik": 884394}
    ]
