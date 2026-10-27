"""Zerodha Kite Connect: daily OHLCV for NSE/BSE indices & equities, MCX/NFO futures.

Credentials, first match wins:
    KITE_API_KEY            required
    KITE_ACCESS_TOKEN       a token for today, or
    <home>/kite_token.json  written by ``tradingagents kite-login`` (valid for the
                            IST trading day it was issued; Kite expires tokens ~06:00 IST)

Futures are priced as Kite's continuous series (``continuous=True``) on the
near-month contract, so a 5-year MCX gold history spans rolled contracts.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorUnavailableError,
)
from tradingagents.dataflows.files import replace_file
from tradingagents.dataflows.vendors.india.instruments import IndiaInstrument

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
MAX_DAYS_PER_REQUEST = 1900          # Kite allows 2000 days per daily request
TOKEN_FILENAME = "kite_token.json"


def tradingagents_home() -> str:
    return os.getenv("TRADINGAGENTS_HOME") or os.path.join(os.path.expanduser("~"), ".tradingagents")


def token_path() -> str:
    return os.path.join(tradingagents_home(), TOKEN_FILENAME)


def ist_today() -> date:
    return datetime.now(IST).date()


def save_access_token(access_token: str, user_id: str = "") -> str:
    path = token_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {"access_token": access_token, "user_id": user_id, "date": ist_today().isoformat()}
    def _write(tmp: str) -> None:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)

    replace_file(path, _write)
    return path


def _access_token() -> str | None:
    if tok := os.getenv("KITE_ACCESS_TOKEN", "").strip():
        return tok
    try:
        with open(token_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if data.get("date") != ist_today().isoformat():
        return None  # Kite tokens die at the next morning's reset
    return data.get("access_token")


def client():
    api_key = os.getenv("KITE_API_KEY", "").strip()
    if not api_key:
        raise VendorNotConfiguredError("KITE_API_KEY is not set")
    token = _access_token()
    if not token:
        raise VendorNotConfiguredError(
            "no Kite access token for today; run `tradingagents kite-login` or set KITE_ACCESS_TOKEN"
        )
    try:
        from kiteconnect import KiteConnect
    except ImportError as exc:
        raise VendorNotConfiguredError("kiteconnect is not installed (pip install kiteconnect)") from exc
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(token)
    return kite


def _translate(exc: Exception, what: str) -> Exception:
    """Kite exception -> the router's error taxonomy."""
    name = type(exc).__name__
    if name in ("TokenException", "PermissionException"):
        return VendorNotConfiguredError(f"Kite rejected the credentials for {what}: {exc}")
    if name == "InputException":
        return NoMarketDataError(what, what, f"Kite refused the request: {exc}")
    return VendorUnavailableError(f"Kite request for {what} failed: {name}: {exc}")


# ----------------------------------------------------------------- instruments
def _instruments(kite, exchange: str) -> pd.DataFrame:
    """The exchange's instrument dump, cached for the IST day."""
    cache_dir = get_config()["data_cache_dir"]
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"kite-instruments-{exchange}-{ist_today().isoformat()}.csv")
    if os.path.exists(path):
        try:
            return pd.read_csv(path, parse_dates=["expiry"])
        except Exception:  # noqa: BLE001 — a corrupt cache is a miss
            pass
    try:
        rows = kite.instruments(exchange)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc, f"{exchange} instruments") from exc
    df = pd.DataFrame(rows)
    if df.empty:
        raise VendorUnavailableError(f"Kite returned no {exchange} instruments")
    df["expiry"] = pd.to_datetime(df.get("expiry"), errors="coerce")
    replace_file(path, lambda tmp: df.to_csv(tmp, index=False))
    return df


def instrument_token(kite, inst: IndiaInstrument) -> tuple[int, bool, str]:
    """(token, continuous?, tradingsymbol) for ``inst``."""
    df = _instruments(kite, inst.kite_exchange)
    if inst.kind == "future":
        futs = df[(df["name"].astype(str).str.upper() == inst.kite_symbol)
                  & (df["instrument_type"].astype(str).str.upper() == "FUT")]
        futs = futs[futs["expiry"].dt.date >= ist_today()].sort_values("expiry")
        if futs.empty:
            raise NoMarketDataError(inst.key, f"{inst.kite_exchange}:{inst.kite_symbol}",
                                    "no live futures contract on Kite")
        row = futs.iloc[0]
        return int(row["instrument_token"]), True, str(row["tradingsymbol"])
    hit = df[df["tradingsymbol"].astype(str).str.upper() == inst.kite_symbol.upper()]
    if hit.empty:
        raise NoMarketDataError(inst.key, f"{inst.kite_exchange}:{inst.kite_symbol}", "not listed on Kite")
    row = hit.iloc[0]
    return int(row["instrument_token"]), False, str(row["tradingsymbol"])


# ------------------------------------------------------------------ history
def fetch_daily(inst: IndiaInstrument, start: date, end: date) -> tuple[pd.DataFrame, str]:
    """Daily bars [start, end] as Date/Open/High/Low/Close/Volume, plus a source label."""
    kite = client()
    token, continuous, tsym = instrument_token(kite, inst)
    rows: list[dict] = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(end, chunk_start + timedelta(days=MAX_DAYS_PER_REQUEST))
        try:
            rows += kite.historical_data(token, chunk_start, chunk_end, "day",
                                         continuous=continuous, oi=continuous)
        except Exception as exc:  # noqa: BLE001
            raise _translate(exc, inst.key) from exc
        chunk_start = chunk_end + timedelta(days=1)
    if not rows:
        raise NoMarketDataError(inst.key, f"{inst.kite_exchange}:{tsym}", "Kite returned no daily bars")
    df = pd.DataFrame(rows).rename(columns={
        "date": "Date", "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume", "oi": "OI",
    })
    df["Date"] = pd.to_datetime(df["Date"]).map(
        lambda t: (t.tz_convert(IST).tz_localize(None) if t.tzinfo else t).normalize())
    df = df.drop_duplicates("Date").sort_values("Date").reset_index(drop=True)
    label = f"Kite Connect {inst.kite_exchange}:{tsym}" + (" (continuous)" if continuous else "")
    return df, label
