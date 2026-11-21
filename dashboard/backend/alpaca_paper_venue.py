"""Alpaca paper account reads (lane P6a, DECISIONS #456).

Four reads only: the account (equity and status), one asset (is it tradable), the latest trade
price of some symbols (lane UI19-0, IEX feed, display data) and the account's position quantities. There is no submit,
cancel or close method here: orders belong to lane P6b. Nothing in this module can move money.

alpaca-py is imported only inside functions, so importing this module needs no network and no
alpaca-py install (the tests use a fake client).

Errors, each with a plain cause:
  PaperVenueNotConfigured  the paper keys are missing or are not paper keys.
  PaperVenueUnreachable    Alpaca did not answer, or answered something we cannot use.
Anything else (a bug in our code) is never caught here.
"""
from __future__ import annotations

import math
import os

import httpx
import requests


class PaperVenueNotConfigured(RuntimeError):
    """The Alpaca paper keys are absent from the environment, or the key is not a paper key."""


class PaperVenueUnreachable(RuntimeError):
    """Alpaca could not be read, or its answer cannot be used. The message carries the cause."""


def _enum_text(value) -> str:
    """An alpaca-py enum or a plain string, as upper-case text."""
    return str(getattr(value, "value", value)).upper()


class AlpacaPaperVenue:
    """Reads the Alpaca paper account. Holds one TradingClient (or a test double with the same
    get_account and get_asset methods)."""

    def __init__(self, client):
        self._client = client

    def get_account(self) -> dict:
        from alpaca.common.exceptions import APIError

        try:
            account = self._client.get_account()
        except (APIError, requests.exceptions.RequestException) as exc:
            raise PaperVenueUnreachable(f"Alpaca paper account read failed: {exc}") from exc
        try:
            equity = float(account.equity)
        except (TypeError, ValueError) as exc:
            raise PaperVenueUnreachable(
                f"Alpaca paper account equity is not a number: {account.equity!r}"
            ) from exc
        if not math.isfinite(equity) or equity < 0:
            raise PaperVenueUnreachable(
                f"Alpaca paper account equity is not usable: {account.equity!r}"
            )
        return {"equity": equity, "status": _enum_text(account.status)}

    def resolve_symbol(self, symbol: str) -> bool:
        from alpaca.common.exceptions import APIError

        try:
            asset = self._client.get_asset(symbol)
        except APIError as exc:
            if exc.status_code == 404:
                return False
            raise PaperVenueUnreachable(
                f"Alpaca asset read for {symbol!r} failed: {exc}"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise PaperVenueUnreachable(
                f"Alpaca asset read for {symbol!r} failed: {exc}"
            ) from exc
        return asset.tradable is True and _enum_text(asset.status) == "ACTIVE"

    def latest_trades(self, symbols) -> dict:
        """The latest IEX trade per symbol: {symbol: {"price", "time"}}. A symbol Alpaca does not
        answer for is left out. A display mark only; nothing here reaches an order.

        Uses httpx directly (alpaca-py's data client imports pytz, which is not installed). The
        keys are read here and only ever sent as request headers; no error text includes them.
        """
        symbols = list(symbols)
        if not symbols:
            return {}
        key = os.environ.get("ALPACA_API_KEY", "")
        secret = os.environ.get("ALPACA_SECRET_KEY", "")
        if not key or not secret:
            raise PaperVenueNotConfigured("ALPACA_API_KEY and ALPACA_SECRET_KEY must both be set")
        url = "https://data.alpaca.markets/v2/stocks/trades/latest"
        params = {"symbols": ",".join(symbols), "feed": "iex"}
        headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        try:
            with httpx.Client(timeout=10) as http:
                response = http.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise PaperVenueUnreachable(
                f"Alpaca latest trades read failed: {type(exc).__name__}: {exc}"
            ) from exc
        if response.status_code != 200:
            raise PaperVenueUnreachable(
                f"Alpaca latest trades answered status {response.status_code}"
            )
        try:
            trades = response.json()["trades"]
        except (ValueError, KeyError, TypeError) as exc:
            raise PaperVenueUnreachable(
                f"Alpaca latest trades answer has no trades field ({type(exc).__name__})"
            ) from exc
        if not isinstance(trades, dict):
            raise PaperVenueUnreachable("Alpaca latest trades answer: trades is not a map")
        answer = {}
        for symbol in symbols:
            trade = trades.get(symbol)
            if trade is None:
                continue
            try:
                answer[symbol] = {"price": float(trade["p"]), "time": trade["t"]}
            except (KeyError, TypeError, ValueError) as exc:
                raise PaperVenueUnreachable(
                    f"Alpaca latest trade for {symbol!r} has an unexpected shape "
                    f"({type(exc).__name__})"
                ) from exc
        return answer

    def position_quantities(self) -> dict:
        """{symbol: signed quantity} for every position in the account (a short is negative)."""
        from alpaca.common.exceptions import APIError

        try:
            positions = self._client.get_all_positions()
        except (APIError, requests.exceptions.RequestException) as exc:
            raise PaperVenueUnreachable(f"Alpaca paper positions read failed: {exc}") from exc
        try:
            return {position.symbol: float(position.qty) for position in positions}
        except (TypeError, ValueError, AttributeError) as exc:
            raise PaperVenueUnreachable(
                f"Alpaca paper positions answer is not usable ({type(exc).__name__})"
            ) from exc


def get_paper_venue() -> AlpacaPaperVenue:
    """The venue for the paper account, built from ALPACA_API_KEY and ALPACA_SECRET_KEY in the
    process environment. A key that does not start with PK is not a paper key (the rule of
    routes/connections.py _alpaca_fetch) and is refused."""
    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key:
        raise PaperVenueNotConfigured("ALPACA_API_KEY is not set")
    if not secret:
        raise PaperVenueNotConfigured("ALPACA_SECRET_KEY is not set")
    if not key.startswith("PK"):
        raise PaperVenueNotConfigured("ALPACA_API_KEY is not a paper key (paper keys start with PK)")
    from alpaca.trading.client import TradingClient

    return AlpacaPaperVenue(TradingClient(key, secret, paper=True))
