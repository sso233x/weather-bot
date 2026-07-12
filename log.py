"""
log.py — persists every scored prediction to trade_log.csv so outcomes
can be checked later and weights calibrated against real results.
"""

import csv
import os
from datetime import datetime, timezone

LOG_FILE = os.path.join(os.path.dirname(__file__), "trade_log.csv")

FIELDNAMES = [
    "logged_at", "city", "station", "target_date", "txn", "xnd",
    "market_bucket_label", "market_price", "confidence", "raw_score",
    "recommendation", "outcome_win", "actual_high", "notes",
]


def log_prediction(city_code, station, target_date, txn, xnd,
                    bucket_label, market_price, result) -> None:
    file_exists = os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "city": city_code,
            "station": station,
            "target_date": target_date,
            "txn": txn if txn is not None else "",
            "xnd": xnd if xnd is not None else "",
            "market_bucket_label": bucket_label or "",
            "market_price": market_price if market_price is not None else "",
            "confidence": result.confidence,
            "raw_score": result.raw_score,
            "recommendation": result.recommendation,
            "outcome_win": "",  # filled in later by check_outcomes.py
            "actual_high": "",  # filled in later by check_outcomes.py
            "notes": " | ".join(result.notes),
        })
