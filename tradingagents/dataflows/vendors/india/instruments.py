"""Indian instrument resolution: one symbol -> where each source lists it.

The CLI hands the data layer a Yahoo-canonical symbol (``NIFTY`` -> ``^NSEI``
via ``symbols._ALIASES``), but Kite Connect and Investing.com name the same
instrument differently, and MCX/NFO futures are not on Yahoo at all. This
module maps every accepted spelling to one ``IndiaInstrument`` so each source
is queried with its own identifier.

Accepted inputs (case-insensitive):

    indices     NIFTY, NIFTY50, ^NSEI, BANKNIFTY, ^NSEBANK, FINNIFTY,
                MIDCPNIFTY, SENSEX, ^BSESN, INDIAVIX, ^INDIAVIX
    equities    RELIANCE.NS, NSE:RELIANCE, TCS.BO, BSE:TCS
    MCX futures GOLD.MCX, GOLDM.MCX, SILVER.MCX, CRUDEOIL.MCX, MCX:GOLD, MCXGOLD
    NFO futures NIFTY.NFO, BANKNIFTY.NFO, RELIANCE.NFO, NFO:BANKNIFTY

Futures resolve to the near-month contract and are priced as Kite's continuous
series. Plain ``GOLD`` stays COMEX (``GC=F``) — MCX gold must be asked for by name.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class IndiaInstrument:
    key: str                      # canonical id, safe for a cache filename
    display: str                  # human-readable name for report headers
    kind: str                     # "index" | "equity" | "future"
    kite_exchange: str            # NSE | BSE | MCX | NFO
    kite_symbol: str              # tradingsymbol (index/equity) or contract root (future)
    yahoo: str | None = None      # Yahoo symbol, if Yahoo lists it
    investing_id: int | None = None
    currency: str = "INR"


# Index rows: (aliases, kite exchange, kite tradingsymbol, yahoo, investing id, display)
# Investing.com ids are the long-standing ones for Nifty 50 / Nifty Bank; extend
# or correct them without code changes via INVESTING_IDS="KEY=ID,KEY=ID".
_INDICES = [
    (("NIFTY", "NIFTY50", "NIFTY 50", "^NSEI"), "NSE", "NIFTY 50", "^NSEI", 17940, "NIFTY 50"),
    (("BANKNIFTY", "NIFTYBANK", "NIFTY BANK", "^NSEBANK"), "NSE", "NIFTY BANK", "^NSEBANK", 17950, "NIFTY BANK"),
    (("FINNIFTY", "NIFTYFIN", "NIFTY_FIN_SERVICE.NS"), "NSE", "NIFTY FIN SERVICE", "NIFTY_FIN_SERVICE.NS", None, "NIFTY FIN SERVICE"),
    (("MIDCPNIFTY", "NIFTY_MID_SELECT.NS"), "NSE", "NIFTY MID SELECT", "NIFTY_MID_SELECT.NS", None, "NIFTY MIDCAP SELECT"),
    (("NIFTYIT", "^CNXIT"), "NSE", "NIFTY IT", "^CNXIT", None, "NIFTY IT"),
    (("INDIAVIX", "INDIA VIX", "^INDIAVIX"), "NSE", "INDIA VIX", "^INDIAVIX", None, "INDIA VIX"),
    (("SENSEX", "^BSESN"), "BSE", "SENSEX", "^BSESN", None, "BSE SENSEX"),
]

_INDEX_BY_ALIAS = {
    alias: row for row in _INDICES for alias in row[0]
}

# Kite index tradingsymbol -> the index row, so NFO roots resolve to a display name.
_NFO_ROOT_DISPLAY = {"NIFTY": "NIFTY 50 FUT", "BANKNIFTY": "NIFTY BANK FUT", "FINNIFTY": "NIFTY FIN FUT",
                     "MIDCPNIFTY": "NIFTY MIDCAP SELECT FUT"}

_ROOT = r"[A-Z0-9&_\-]{1,20}"
_SUFFIXED = re.compile(rf"^({_ROOT})\.(NS|BO|MCX|NFO)$")
_PREFIXED = re.compile(rf"^(NSE|BSE|MCX|NFO):({_ROOT})$")
_MCX_SHORT = re.compile(r"^MCX(GOLD|GOLDM|SILVER|SILVERM|CRUDEOIL|NATURALGAS|COPPER|ZINC|ALUMINIUM|LEAD|NICKEL)$")

_EXCHANGE_OF_SUFFIX = {"NS": "NSE", "BO": "BSE", "MCX": "MCX", "NFO": "NFO"}


def _investing_overrides() -> dict[str, int]:
    raw = os.getenv("INVESTING_IDS", "")
    out: dict[str, int] = {}
    for part in raw.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            if v.strip().isdigit():
                out[k.strip().upper()] = int(v.strip())
    return out


def _index(row) -> IndiaInstrument:
    aliases, exch, tsym, yahoo, inv, display = row
    key = aliases[0]
    inv = _investing_overrides().get(key, inv)
    return IndiaInstrument(key=key, display=display, kind="index", kite_exchange=exch,
                           kite_symbol=tsym, yahoo=yahoo, investing_id=inv)


def resolve(raw: str) -> IndiaInstrument | None:
    """The Indian instrument ``raw`` names, or None if it is not one."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    s = raw.strip().upper()

    if s in _INDEX_BY_ALIAS:
        return _index(_INDEX_BY_ALIAS[s])

    if m := _MCX_SHORT.match(s):
        s = f"{m.group(1)}.MCX"

    exch = root = None
    if m := _SUFFIXED.match(s):
        root, exch = m.group(1), _EXCHANGE_OF_SUFFIX[m.group(2)]
    elif m := _PREFIXED.match(s):
        exch, root = m.group(1), m.group(2)
    if exch is None:
        return None

    if exch in ("MCX", "NFO"):
        display = _NFO_ROOT_DISPLAY.get(root, f"{root} FUT") if exch == "NFO" else f"MCX {root} FUT"
        return IndiaInstrument(key=f"{root}.{exch}", display=display, kind="future",
                               kite_exchange=exch, kite_symbol=root,
                               investing_id=_investing_overrides().get(f"{root}.{exch}"))

    # NSE/BSE: an index typed with a suffix (e.g. NIFTY_FIN_SERVICE.NS) is still the index.
    if s in _INDEX_BY_ALIAS:
        return _index(_INDEX_BY_ALIAS[s])
    suffix = ".NS" if exch == "NSE" else ".BO"
    return IndiaInstrument(key=f"{root}{suffix}", display=f"{root} ({exch})", kind="equity",
                           kite_exchange=exch, kite_symbol=root, yahoo=f"{root}{suffix}",
                           investing_id=_investing_overrides().get(f"{root}{suffix}"))
