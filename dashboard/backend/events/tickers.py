"""Ticker canonicalization — the single owner of the CIK-bridge lookup key.

Dashboard-refinements item 1 / Cowork B1. `research_cik_ticker_map` is read by
several call sites (the universe loader, the Benzinga mapper, the canonical JSONL
loader) and written by the SEC bridge loader. Those sites previously compared the
ticker byte-for-byte, so a cosmetic difference (case, or the share-class separator
`.` vs `-`) caused a miss: SEC stores `BRK-B` upper-hyphen, but a pasted S&P list
commonly uses `BRK.B` dotted or lowercase, and the lookup never matched.

`normalize_ticker` defines ONE canonical form. It is applied at the bridge WRITE
site (so the stored rows are canonical) AND at every forward ticker->CIK READER
(so the bound input is canonical too). With one representation on both sides the
lookup cannot miss on a cosmetic difference, and the two forms cannot diverge
(DDIA: a system of record stores each fact in exactly one canonical form; an
application-maintained index desyncs when the write form and the read form differ).
"""
from __future__ import annotations


def normalize_ticker(raw: str) -> str:
    """Return the canonical form of a ticker for the CIK bridge.

    The canonical form is SEC's: trimmed, uppercased, with the share-class
    separator written as a hyphen (SEC's `company_tickers_exchange.json` ships
    `BRK-B`, not `BRK.B`). So `'brk.b'`, `'BRK.B'`, and `' BRK-B '` all map to
    `'BRK-B'`. An empty/blank input returns `''` (the caller decides whether an
    empty ticker is an error; this function does not raise).
    """
    s = raw.strip()                                              # drop surrounding whitespace from a pasted line
    s = s.upper()                                                # SEC tickers are uppercase
    s = s.replace(".", "-")                                      # share-class: SEC writes BRK-B, lists often write BRK.B
    return s
