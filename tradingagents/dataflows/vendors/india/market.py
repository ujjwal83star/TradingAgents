"""The ``india`` vendor: daily OHLCV and indicators for Indian instruments.

Sources are tried in ``config["india_data_sources"]`` order (default
``kite,investing,yahoo``); the first that returns bars wins and the report
header names it. A symbol that is not Indian raises ``NoMarketDataError`` so
the router moves straight to the next configured vendor (yfinance).

One 5-year-to-today download per instrument is cached per day, then sliced
to the requested window — rows after the analysis date are never returned.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta

import pandas as pd
import yfinance as yf
from stockstats import wrap

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorError,
    VendorNotConfiguredError,
    VendorUnavailableError,
)
from tradingagents.dataflows.files import replace_file
from tradingagents.dataflows.vendors.india import investing, kite
from tradingagents.dataflows.vendors.india.instruments import IndiaInstrument, resolve
from tradingagents.dataflows.vendors.yahoo.common import raise_for_empty, yf_retry
from tradingagents.dataflows.vendors.yahoo.market import (
    INDICATOR_DESCRIPTIONS,
    get_closes as yahoo_get_closes,
)
from tradingagents.dataflows.vendors.yahoo.ohlcv import (
    _assert_ohlcv_not_stale,
    _cache_is_fresh,
    _clean_dataframe,
    _fill_price_gaps,
)

logger = logging.getLogger(__name__)

DEFAULT_SOURCES = "kite,investing,yahoo"
HISTORY_YEARS = 5
_SOURCE_COL = "_source"


# --------------------------------------------------------------- sources
def _yahoo_daily(inst: IndiaInstrument, start: date, end: date) -> tuple[pd.DataFrame, str]:
    if not inst.yahoo:
        raise VendorNotConfiguredError(f"{inst.display} is not listed on Yahoo Finance")
    data = yf_retry(lambda: yf.Ticker(inst.yahoo).history(
        start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
        auto_adjust=True, actions=False))
    if data is None or data.empty:
        raise_for_empty(inst.key, inst.yahoo, "price rows")
    data = data.reset_index().rename(columns={"index": "Date", "Datetime": "Date"})
    data["Date"] = pd.to_datetime(data["Date"]).map(
        lambda t: (t.tz_localize(None) if t.tzinfo else t).normalize())
    return data[["Date", "Open", "High", "Low", "Close", "Volume"]], f"Yahoo Finance {inst.yahoo}"


_SOURCES = {
    "kite": kite.fetch_daily,
    "investing": investing.fetch_daily,
    "yahoo": _yahoo_daily,
}


def _source_chain() -> list[str]:
    raw = get_config().get("india_data_sources") or os.getenv("INDIA_DATA_SOURCES") or DEFAULT_SOURCES
    chain = [s.strip().lower() for s in raw.split(",") if s.strip()]
    unknown = [s for s in chain if s not in _SOURCES]
    if unknown:
        raise ValueError(f"unknown india_data_sources {unknown}; choose from {list(_SOURCES)}")
    return chain


def _cache_file(inst: IndiaInstrument) -> str:
    safe = re.sub(r"[^A-Za-z0-9._\-^=]", "_", inst.key)
    cache_dir = get_config()["data_cache_dir"]
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, f"{safe}-IN-data.csv")


def _download(inst: IndiaInstrument) -> pd.DataFrame:
    """5y-to-today bars from the first source in the chain that has them."""
    end = kite.ist_today()
    start = end - timedelta(days=365 * HISTORY_YEARS + 2)
    unconfigured: list[str] = []
    unavailable: list[str] = []
    no_data: list[str] = []
    for name in _source_chain():
        try:
            df, label = _SOURCES[name](inst, start, end)
        except VendorNotConfiguredError as exc:
            unconfigured.append(f"{name}: {exc}")
            continue
        except VendorUnavailableError as exc:
            logger.warning("india/%s unavailable for %s: %s", name, inst.key, exc)
            unavailable.append(f"{name}: {exc}")
            continue
        except NoMarketDataError as exc:
            no_data.append(f"{name}: {exc.detail or exc}")
            continue
        if df is None or df.empty or "Close" not in df:
            no_data.append(f"{name}: empty")
            continue
        df = df.copy()
        df[_SOURCE_COL] = label
        logger.info("india: %s served by %s (%d bars)", inst.key, label, len(df))
        return df
    # A throttled/failed source never said whether it has the symbol, so it wins;
    # sources that answered "no data" outrank ones that were never set up; and
    # when nothing was set up (e.g. MCX with no Kite keys) the reasons say how to fix it.
    detail = "; ".join(unavailable + no_data + unconfigured)
    if unavailable or not no_data:
        raise VendorUnavailableError(f"no Indian data source served {inst.display}: {detail}")
    raise NoMarketDataError(inst.key, inst.key, detail)


def load_frame(inst: IndiaInstrument, as_of_date: str, fill_gaps: bool = True) -> tuple[pd.DataFrame, str]:
    """Bars up to ``as_of_date`` (inclusive) and the label of the source that served them."""
    path = _cache_file(inst)
    now = pd.Timestamp.today()
    as_of_dt = pd.to_datetime(as_of_date).normalize()
    data = None
    if os.path.exists(path):
        try:
            cached = pd.read_csv(path)
            if not cached.empty and "Close" in cached and _cache_is_fresh(path, as_of_dt, now):
                data = cached
        except Exception:  # noqa: BLE001 — corrupt cache is a miss
            data = None
    if data is None:
        data = _download(inst)
        frame = data
        replace_file(path, lambda tmp: frame.to_csv(tmp, index=False, encoding="utf-8"))

    label = str(data[_SOURCE_COL].dropna().iloc[0]) if _SOURCE_COL in data and data[_SOURCE_COL].notna().any() else "unknown"
    extra = [c for c in ("OI",) if c in data.columns]
    data = data.drop(columns=[_SOURCE_COL], errors="ignore")
    data = _clean_dataframe(data)
    data = data[data["Date"] <= as_of_dt]
    if data.empty:
        raise NoMarketDataError(inst.key, inst.key, f"no bars on or before {as_of_dt.date()}")
    data = _fill_price_gaps(data) if fill_gaps else data.dropna(subset=["Close"]).copy()
    for col in extra:
        data[col] = pd.to_numeric(data[col], errors="coerce")
    _assert_ohlcv_not_stale(data, as_of_date, inst.key, inst.key)
    return data.reset_index(drop=True), label


def _require(symbol: str) -> IndiaInstrument:
    inst = resolve(symbol)
    if inst is None:
        raise NoMarketDataError(symbol, symbol, "not an Indian (NSE/BSE/MCX/NFO) instrument")
    return inst


# --------------------------------------------------------------- tools
def get_india_stock_data(symbol: str, start_date: str, end_date: str) -> str:
    datetime.strptime(start_date, "%Y-%m-%d")
    datetime.strptime(end_date, "%Y-%m-%d")
    inst = _require(symbol)
    data, label = load_frame(inst, end_date, fill_gaps=False)
    data = data[data["Date"] >= pd.Timestamp(start_date)]
    if data.empty:
        raise NoMarketDataError(symbol, inst.key, f"no bars between {start_date} and {end_date}")
    cols = [c for c in ("Open", "High", "Low", "Close", "Volume", "OI") if c in data.columns]
    out = data.set_index("Date")[cols]
    for c in ("Open", "High", "Low", "Close"):
        out[c] = out[c].round(2)
    out.index = out.index.strftime("%Y-%m-%d")
    header = (
        f"# Stock data for {inst.display} [{inst.key}] (from {symbol}) from {start_date} to {end_date}\n"
        f"# Source: {label} | Currency: {inst.currency}"
        + (" | Index: volume not reported" if inst.kind == "index" else "")
        + (" | Futures: continuous near-month, OI = open interest" if inst.kind == "future" else "")
        + f"\n# Total records: {len(out)}\n\n"
    )
    return header + out.to_csv()


def get_india_indicators(symbol: str, indicator: str, as_of_date: str, look_back_days: int) -> str:
    if indicator not in INDICATOR_DESCRIPTIONS:
        raise ValueError(f"Indicator {indicator} is not supported. Please choose from: {list(INDICATOR_DESCRIPTIONS)}")
    inst = _require(symbol)
    data, label = load_frame(inst, as_of_date)
    no_volume = inst.kind == "index" or float(data["Volume"].abs().sum()) == 0.0
    df = wrap(data[["Date", "Open", "High", "Low", "Close", "Volume"]].copy())
    df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
    df[indicator]
    values = dict(zip(df["Date"], df[indicator], strict=True))

    as_of = datetime.strptime(as_of_date, "%Y-%m-%d")
    before = as_of - timedelta(days=look_back_days)
    lines, cur = [], as_of
    while cur >= before:
        d = cur.strftime("%Y-%m-%d")
        if d not in values:
            v = "N/A: Not a trading day (weekend or holiday)"
        elif indicator in ("vwma", "mfi") and no_volume:
            v = "N/A: volume not reported for this instrument"
        else:
            v = "N/A" if pd.isna(values[d]) else str(values[d])
        lines.append(f"{d}: {v}")
        cur -= timedelta(days=1)
    return (
        f"## {indicator} values for {inst.display} from {before:%Y-%m-%d} to {as_of_date} (source: {label}):\n\n"
        + "\n".join(lines) + "\n\n\n" + INDICATOR_DESCRIPTIONS[indicator]
    )


def get_closes(symbol: str, start_date: str, end_date: str) -> pd.Series:
    """Daily closes in [start_date, end_date); Indian instruments via this chain, else Yahoo."""
    inst = resolve(symbol)
    if inst is None:
        return yahoo_get_closes(symbol, start_date, end_date)
    last = (pd.Timestamp(end_date) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        data, _ = load_frame(inst, last, fill_gaps=False)
    except VendorError:
        return pd.Series(dtype=float)
    data = data[data["Date"] >= pd.Timestamp(start_date)]
    return pd.Series(data["Close"].to_numpy(), index=pd.DatetimeIndex(data["Date"]), name="Close")
