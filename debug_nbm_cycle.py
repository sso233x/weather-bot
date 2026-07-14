#!/usr/bin/env python3
"""
debug_nbm_cycle.py -- ONE-TIME DIAGNOSTIC, not part of the bot pipeline.

Prints the RAW NBM text block for one station at a given cycle, plus what
extract_row() currently pulls out of it for TXN/XND -- so we can see
exactly how the midday (07Z/13Z) bulletin layout compares to the evening
(01Z) one, instead of guessing why cycle 13 produced impossible values
(forecast high below the CURRENT observed temp).

Usage (set via env var, since this runs as a GitHub Actions step):
    DEBUG_STATION=KMIA DEBUG_CYCLE=13 python debug_nbm_cycle.py
"""

import os

from config import ALL_STATIONS
from data_sources import fetch_nbm_raw, split_by_station, extract_row

station = os.environ.get("DEBUG_STATION", "KMIA")
cycle = os.environ.get("DEBUG_CYCLE", "13")

print(f"Fetching NBM raw text for cycle={cycle}...")
raw = fetch_nbm_raw(cycle)
print(f"Total raw text length: {len(raw)} chars")

blocks = split_by_station(raw, ALL_STATIONS)
block = blocks.get(station)

if not block:
    print(f"No block found for {station}! Available stations in this pull: {list(blocks.keys())}")
else:
    print(f"\n=== FULL RAW BLOCK for {station} (cycle {cycle}) ===")
    print(block)
    print(f"=== END BLOCK ===\n")

    txn = extract_row(block, "TXN")
    xnd = extract_row(block, "XND")
    print(f"extract_row('TXN') currently returns: {txn}")
    print(f"extract_row('XND') currently returns: {xnd}")
    print(f"\nmain.py takes txn[0]/xnd[0] as 'latest' -- that's {txn[0] if txn else None} / {xnd[0] if xnd else None}")
