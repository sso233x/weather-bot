"""
history.py — tiny JSON-file persistence for NBM run history, so the
trend-consistency signal has something to compare across days.

GitHub Actions runners are stateless between jobs, so this file must be
committed back to the repo at the end of each run (see the workflow's
"Commit history" step). This is a simple, free way to get persistence
without a database.
"""

import json
import os
from datetime import date, timedelta

HISTORY_FILE = os.path.join(os.path.dirname(__file__), "run_history.json")
MAX_DAYS_KEPT = 5  # only need last few days for trend checks


def load_history() -> dict:
    if not os.path.exists(HISTORY_FILE):
        return {}
    with open(HISTORY_FILE) as f:
        return json.load(f)


def save_history(history: dict) -> None:
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def record_run(history: dict, station: str, txn: float) -> None:
    """Appends today's TXN value for a station, pruning old entries."""
    today = str(date.today())
    history.setdefault(station, {})
    history[station][today] = txn

    cutoff = date.today() - timedelta(days=MAX_DAYS_KEPT)
    for station_key in list(history.keys()):
        for day_key in list(history[station_key].keys()):
            if date.fromisoformat(day_key) < cutoff:
                del history[station_key][day_key]


def recent_values(history: dict, station: str, n: int = 3) -> list:
    """Returns up to the last n recorded TXN values for a station, oldest first."""
    entries = history.get(station, {})
    sorted_days = sorted(entries.keys())[-n:]
    return [entries[d] for d in sorted_days]