#!/usr/bin/env python3
"""
check_outcomes.py — for every logged prediction whose target_date has
passed and whose outcome is still blank, re-fetches that day's market
and checks whether the predicted bucket actually won. Updates
trade_log.csv in place and sends a short Telegram summary of what
resolved. Run this daily, after markets have had time to resolve.
"""

import csv
import os
import sys
from datetime import date, datetime

import requests

from config import CITIES
from data_sources import build_event_slug, fetch_market_by_slug, parse_outcomes
from log import LOG_FILE, FIELDNAMES

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def send_telegram(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(
        url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
        timeout=15,
    )
    resp.raise_for_status()


def bucket_matches(label_a: str, label_b: str) -> bool:
    """Compares two bucket labels by numeric range rather than exact text,
    since phrasing can vary slightly between fetches."""
    import re
    ra = re.search(r"(\d+)\s*-\s*(\d+)", label_a or "")
    rb = re.search(r"(\d+)\s*-\s*(\d+)", label_b or "")
    if not ra or not rb:
        return False
    return ra.groups() == rb.groups()


def check_one(city_code: str, target_date_str: str, predicted_label: str):
    """Returns True/False/None (win/loss/still unresolved)."""
    city = CITIES.get(city_code)
    if not city or not predicted_label:
        return None
    target_date = date.fromisoformat(target_date_str)
    slug = build_event_slug(city["slug"], target_date)
    event = fetch_market_by_slug(slug)
    if not event or not event.get("closed", False):
        return None  # not resolved yet (or event missing)

    outcomes = parse_outcomes(event)
    for label, lo, hi, yes_price in outcomes:
        if bucket_matches(label, predicted_label):
            if yes_price >= 0.9:
                return True
            if yes_price <= 0.1:
                return False
            return None  # ambiguous / not fully settled
    return None  # predicted bucket not found in resolved event


def main():
    if not os.path.exists(LOG_FILE):
        print("No trade_log.csv yet -- nothing to check.")
        return

    with open(LOG_FILE, newline="") as f:
        rows = list(csv.DictReader(f))

    today = date.today()
    resolved_summary = []

    for row in rows:
        if row["outcome_win"] not in ("", None):
            continue  # already resolved
        try:
            target_date = date.fromisoformat(row["target_date"])
        except ValueError:
            continue
        if target_date >= today:
            continue  # market hasn't happened yet

        outcome = check_one(row["city"], row["target_date"], row["market_bucket_label"])
        if outcome is None:
            continue
        row["outcome_win"] = int(outcome)
        resolved_summary.append(
            f"{'✅' if outcome else '❌'} {row['city']} {row['target_date']}: "
            f"predicted {row['market_bucket_label']} ({row['recommendation']}, "
            f"{float(row['confidence']):.0%}) -> {'WIN' if outcome else 'LOSS'}"
        )

    with open(LOG_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    if resolved_summary:
        send_telegram("📊 <b>Outcomes resolved</b>\n\n" + "\n".join(resolved_summary))
    else:
        print("No newly resolved outcomes this run.")


if __name__ == "__main__":
    main()
