"""The "india" vendor: symbol resolution, source chain, caching, router integration."""

from datetime import date, timedelta

import pandas as pd
import pytest

from tradingagents.dataflows import config as dconfig
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorUnavailableError,
)
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors.india import instruments, kite, market

pytestmark = pytest.mark.unit


def _bars(n=400, end=None, volume=1000.0):
    end = end or kite.ist_today()
    days = pd.bdate_range(end=pd.Timestamp(end), periods=n)
    close = pd.Series(range(n), dtype=float) + 20000
    return pd.DataFrame({"Date": days, "Open": close, "High": close + 50, "Low": close - 50,
                         "Close": close, "Volume": volume})


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    dconfig.set_config({"data_cache_dir": str(tmp_path / "cache"), "india_data_sources": "kite,investing,yahoo"})
    monkeypatch.setenv("TRADINGAGENTS_HOME", str(tmp_path))
    for var in ("KITE_API_KEY", "KITE_ACCESS_TOKEN", "INVESTING_IDS"):
        monkeypatch.delenv(var, raising=False)
    yield


# ---------------------------------------------------------------- resolution
@pytest.mark.parametrize("raw,key,exch,sym,kind", [
    ("NIFTY", "NIFTY", "NSE", "NIFTY 50", "index"),
    ("^NSEI", "NIFTY", "NSE", "NIFTY 50", "index"),
    ("banknifty", "BANKNIFTY", "NSE", "NIFTY BANK", "index"),
    ("^BSESN", "SENSEX", "BSE", "SENSEX", "index"),
    ("NIFTY_FIN_SERVICE.NS", "FINNIFTY", "NSE", "NIFTY FIN SERVICE", "index"),
    ("RELIANCE.NS", "RELIANCE.NS", "NSE", "RELIANCE", "equity"),
    ("NSE:TCS", "TCS.NS", "NSE", "TCS", "equity"),
    ("M&M.NS", "M&M.NS", "NSE", "M&M", "equity"),
    ("TCS.BO", "TCS.BO", "BSE", "TCS", "equity"),
    ("GOLD.MCX", "GOLD.MCX", "MCX", "GOLD", "future"),
    ("MCX:GOLDM", "GOLDM.MCX", "MCX", "GOLDM", "future"),
    ("MCXSILVER", "SILVER.MCX", "MCX", "SILVER", "future"),
    ("BANKNIFTY.NFO", "BANKNIFTY.NFO", "NFO", "BANKNIFTY", "future"),
])
def test_resolve(raw, key, exch, sym, kind):
    inst = instruments.resolve(raw)
    assert (inst.key, inst.kite_exchange, inst.kite_symbol, inst.kind) == (key, exch, sym, kind)


@pytest.mark.parametrize("raw", ["AAPL", "GC=F", "GOLD", "BTC-USD", "0700.HK", "", None])
def test_non_indian_symbols_are_not_claimed(raw):
    assert instruments.resolve(raw) is None


def test_cli_aliases_land_on_resolvable_symbols():
    for alias in ("NIFTY", "BANKNIFTY", "SENSEX", "MCXGOLD", "NIFTYFUT", "FINNIFTY"):
        assert instruments.resolve(normalize_symbol(alias)) is not None
    assert normalize_symbol("GOLD") == "GC=F"  # COMEX stays COMEX


def test_investing_ids_override(monkeypatch):
    monkeypatch.setenv("INVESTING_IDS", "GOLD.MCX=12345")
    assert instruments.resolve("GOLD.MCX").investing_id == 12345
    assert instruments.resolve("NIFTY").investing_id == 17940


# ---------------------------------------------------------------- chain
def _patch_sources(monkeypatch, **impls):
    def make(name):
        impl = impls.get(name)
        def fetch(inst, start, end):
            if impl is None:
                raise VendorNotConfiguredError(f"{name} off")
            if isinstance(impl, Exception):
                raise impl
            return impl(), name.upper()
        return fetch
    monkeypatch.setattr(market, "_SOURCES", {n: make(n) for n in ("kite", "investing", "yahoo")})


def test_first_working_source_wins_and_is_named(monkeypatch):
    _patch_sources(monkeypatch, kite=None, investing=VendorUnavailableError("cloudflare"), yahoo=_bars)
    out = market.get_india_stock_data("^NSEI", (kite.ist_today() - timedelta(days=10)).isoformat(),
                                      kite.ist_today().isoformat())
    assert "# Source: YAHOO" in out and "NIFTY 50" in out and "volume not reported" in out


def test_kite_preferred_when_configured(monkeypatch):
    _patch_sources(monkeypatch, kite=_bars, investing=_bars, yahoo=_bars)
    out = market.get_india_stock_data("BANKNIFTY", "2020-01-01", kite.ist_today().isoformat())
    assert "# Source: KITE" in out


def test_mcx_without_kite_reports_why(monkeypatch):
    _patch_sources(monkeypatch)  # everything unconfigured
    with pytest.raises(VendorUnavailableError, match="kite: kite off"):
        market.get_india_stock_data("GOLD.MCX", "2024-01-01", kite.ist_today().isoformat())


def test_all_sources_empty_is_no_data(monkeypatch):
    _patch_sources(monkeypatch, kite=NoMarketDataError("X", "X", "not listed"),
                   yahoo=NoMarketDataError("X", "X", "no rows"))
    with pytest.raises(NoMarketDataError):
        market.get_india_stock_data("ZZZZ.NS", "2024-01-01", kite.ist_today().isoformat())


def test_non_indian_symbol_passes_to_next_vendor():
    with pytest.raises(NoMarketDataError, match="not an Indian"):
        market.get_india_stock_data("AAPL", "2024-01-01", "2024-02-01")


def test_no_lookahead_and_cache_reuse(monkeypatch):
    calls = []
    def bars():
        calls.append(1)
        return _bars()
    _patch_sources(monkeypatch, kite=bars)
    as_of = (kite.ist_today() - timedelta(days=30))
    while as_of.weekday() >= 5:
        as_of -= timedelta(days=1)
    out = market.get_india_stock_data("RELIANCE.NS", (as_of - timedelta(days=7)).isoformat(), as_of.isoformat())
    last_row = out.strip().splitlines()[-1]
    assert last_row.startswith(as_of.isoformat())
    market.get_india_stock_data("RELIANCE.NS", (as_of - timedelta(days=7)).isoformat(), as_of.isoformat())
    assert len(calls) == 1


def test_indicators(monkeypatch):
    _patch_sources(monkeypatch, kite=_bars)
    today = kite.ist_today()
    out = market.get_india_indicators("GOLD.MCX", "close_50_sma", today.isoformat(), 5)
    assert "MCX GOLD FUT" in out and "50 SMA" in out
    out = market.get_india_indicators("^NSEI", "vwma", today.isoformat(), 3)
    assert "volume not reported" in out or "Not a trading day" in out


def test_closes_for_settlement(monkeypatch):
    _patch_sources(monkeypatch, kite=_bars)
    end = kite.ist_today()
    s = market.get_closes("GOLD.MCX", (end - timedelta(days=20)).isoformat(), end.isoformat())
    assert not s.empty and s.index.max() < pd.Timestamp(end)


# ---------------------------------------------------------------- kite internals
class FakeKite:
    def __init__(self):
        self.calls = []

    def instruments(self, exchange):
        today = kite.ist_today()
        return [
            {"instrument_token": 1, "tradingsymbol": "GOLD25OCTFUT", "name": "GOLD",
             "instrument_type": "FUT", "expiry": today - timedelta(days=3)},
            {"instrument_token": 2, "tradingsymbol": "GOLD25DECFUT", "name": "GOLD",
             "instrument_type": "FUT", "expiry": today + timedelta(days=40)},
            {"instrument_token": 3, "tradingsymbol": "GOLD26FEBFUT", "name": "GOLD",
             "instrument_type": "FUT", "expiry": today + timedelta(days=100)},
            {"instrument_token": 9, "tradingsymbol": "NIFTY 50", "name": "NIFTY 50",
             "instrument_type": "EQ", "expiry": None},
        ]

    def historical_data(self, token, start, end, interval, continuous=False, oi=False):
        self.calls.append((token, start, end, continuous))
        return [{"date": pd.Timestamp(start).tz_localize("Asia/Kolkata"), "open": 1, "high": 2,
                 "low": 0.5, "close": 1.5, "volume": 10, "oi": 5}]


def test_kite_near_month_continuous_and_chunking(monkeypatch):
    fake = FakeKite()
    monkeypatch.setattr(kite, "client", lambda: fake)
    inst = instruments.resolve("GOLD.MCX")
    df, label = kite.fetch_daily(inst, date(2019, 1, 1), date(2026, 1, 1))
    assert {c[0] for c in fake.calls} == {2} and all(c[3] for c in fake.calls)
    assert len(fake.calls) == 2  # 5y > one 2000-day window
    assert "GOLD25DECFUT (continuous)" in label and "OI" in df


def test_kite_token_file_expires_daily(monkeypatch):
    monkeypatch.setenv("KITE_API_KEY", "k")
    kite.save_access_token("tok")
    assert kite._access_token() == "tok"
    monkeypatch.setattr(kite, "ist_today", lambda: date(1999, 1, 1))
    assert kite._access_token() is None
    with pytest.raises(VendorNotConfiguredError, match="kite-login"):
        kite.client()


def test_router_uses_india_then_yfinance(monkeypatch):
    from tradingagents.dataflows import router
    _patch_sources(monkeypatch, kite=_bars)
    dconfig.set_config({"data_vendors": {"core_stock_apis": "india,yfinance"}})
    seen = []
    monkeypatch.setitem(router.VENDOR_METHODS["get_stock_data"], "yfinance",
                        lambda s, a, b: seen.append(s) or "YF")
    today = kite.ist_today().isoformat()
    assert "KITE" in router.route_to_vendor("get_stock_data", "^NSEI", "2025-01-01", today)
    assert router.route_to_vendor("get_stock_data", "AAPL", "2025-01-01", today) == "YF"
    assert seen == ["AAPL"]


def test_identity_falls_back_to_india_name(monkeypatch):
    from tradingagents.agents import context
    monkeypatch.setattr(context, "_identity", lambda t: {})
    ident = context.resolve_instrument_identity("GOLD.MCX")
    assert ident["company_name"] == "MCX GOLD FUT" and ident["exchange"] == "MCX"


def test_investing_parses_tvc_history(monkeypatch):
    from tradingagents.dataflows.vendors.india import investing

    class R:
        status_code = 200
        def json(self):
            return {"s": "ok", "t": [1735689600, 1735776000], "o": [1, 2], "h": [3, 4],
                    "l": [0, 1], "c": [2, 3], "v": [0, 0]}
    monkeypatch.setattr(investing.requests, "get", lambda *a, **k: R())
    df, label = investing.fetch_daily(instruments.resolve("NIFTY"), date(2025, 1, 1), date(2025, 1, 3))
    assert list(df["Close"]) == [2, 3] and label == "Investing.com id 17940"


def test_investing_cloudflare_is_an_outage(monkeypatch):
    from tradingagents.dataflows.vendors.india import investing

    class R:
        status_code = 403
    monkeypatch.setattr(investing.requests, "get", lambda *a, **k: R())
    with pytest.raises(VendorUnavailableError):
        investing.fetch_daily(instruments.resolve("NIFTY"), date(2025, 1, 1), date(2025, 1, 3))
