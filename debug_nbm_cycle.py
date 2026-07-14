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

    # Precise column-alignment check: NBM text products use a fixed-width
    # layout (5-char label field, then 3-char columns per forecast hour).
    # Print DT/UTC/FHR/TXN with an index ruler underneath so we can see
    # EXACTLY which character column each TXN value sits under, instead
    # of guessing from eyeballed spacing in a log viewer.
    print("\n=== Fixed-width column alignment check ===")
    lines_by_label = {}
    for line in block.splitlines():
        stripped = line.strip()
        for label in ("DT", "UTC", "FHR", "TXN", "XND"):
            if stripped.startswith(label + " ") or stripped == label:
                lines_by_label[label] = line
                break

    ruler = "".join(str(i % 10) for i in range(120))
    print(f"COL#  {ruler}")
    for label in ("DT", "UTC", "FHR", "TXN", "XND"):
        if label in lines_by_label:
            print(f"{label:5s} {lines_by_label[label]}")

    # Also show, for each 3-char column starting right after the 5-char
    # label field, what FHR/UTC/TXN value (if any) occupies it.
    if "FHR" in lines_by_label and "TXN" in lines_by_label:
        fhr_line = lines_by_label["FHR"]
        txn_line = lines_by_label["TXN"]
        utc_line = lines_by_label.get("UTC", "")
        print("\nPer-column breakdown (label field assumed 5 chars, then 3-char columns):")
        col = 0
        pos = 5
        while pos < len(fhr_line):
            fhr_val = fhr_line[pos:pos+3].strip()
            utc_val = utc_line[pos:pos+3].strip() if utc_line else ""
            txn_val = txn_line[pos:pos+3].strip() if pos < len(txn_line) else ""
            if fhr_val or txn_val:
                marker = "  <-- TXN HERE" if txn_val else ""
                print(f"  col{col:2d} (chars {pos}-{pos+2}): FHR={fhr_val:>3s} UTC={utc_val:>3s} TXN={txn_val:>3s}{marker}")
            col += 1
            pos += 3
