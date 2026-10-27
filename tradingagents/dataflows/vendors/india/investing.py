"""Investing.com daily history, for instruments with an Investing.com id.

Keyless. Talks to the same TradingView-style ``tvc6.investing.com`` history
endpoint the ``investiny`` package uses, directly via ``requests`` — investiny
pins pydantic<2 and httpx<0.24, which would break the LangChain stack.
Investing.com sits behind Cloudflare and blocks some networks, so any
transport failure is an outage and the chain moves on.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from uuid import uuid4

import pandas as pd
import requests

from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorUnavailableError,
)
from tradingagents.dataflows.vendors.india.instruments import IndiaInstrument

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Referer": "https://tvc-invdn-com.investing.com/",
    "Content-Type": "application/json",
}
CHUNK_DAYS = 365
TIMEOUT = 20


def _history(investing_id: int, start: date, end: date) -> dict:
    url = f"https://tvc6.investing.com/{uuid4().hex}/0/0/0/0/history"
    params = {
        "symbol": investing_id, "resolution": "D",
        "from": int(datetime.combine(start, time.min, UTC).timestamp()),
        "to": int(datetime.combine(end + timedelta(days=1), time.min, UTC).timestamp()),
    }
    try:
        r = requests.get(url, params=params, headers=_HEADERS, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise VendorUnavailableError(f"Investing.com unreachable: {type(exc).__name__}") from exc
    if r.status_code != 200:
        raise VendorUnavailableError(f"Investing.com HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError as exc:  # Cloudflare challenge page
        raise VendorUnavailableError("Investing.com returned a non-JSON (Cloudflare) page") from exc


def fetch_daily(inst: IndiaInstrument, start: date, end: date) -> tuple[pd.DataFrame, str]:
    if inst.investing_id is None:
        raise VendorNotConfiguredError(f"no Investing.com id for {inst.key} (set INVESTING_IDS)")
    frames = []
    cur = start
    while cur <= end:
        chunk_end = min(end, cur + timedelta(days=CHUNK_DAYS))
        d = _history(inst.investing_id, cur, chunk_end)
        if d.get("s") == "ok" and d.get("t"):
            frames.append(pd.DataFrame({
                "Date": pd.to_datetime(d["t"], unit="s", utc=True).tz_convert("Asia/Kolkata")
                          .tz_localize(None).normalize(),
                "Open": d["o"], "High": d["h"], "Low": d["l"], "Close": d["c"],
                "Volume": d.get("v") or [0] * len(d["t"]),
            }))
        elif d.get("s") not in ("ok", "no_data"):
            raise VendorUnavailableError(f"Investing.com answered status {d.get('s')!r}")
        cur = chunk_end + timedelta(days=1)
    if not frames:
        raise NoMarketDataError(inst.key, f"investing:{inst.investing_id}", "Investing.com returned no rows")
    df = pd.concat(frames).drop_duplicates("Date").sort_values("Date").reset_index(drop=True)
    return df, f"Investing.com id {inst.investing_id}"
