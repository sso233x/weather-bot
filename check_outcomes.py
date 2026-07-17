#!/usr/bin/env python3
"""
check_outcomes.py — for every logged prediction whose target_date has
passed and whose outcome is still blank, re-fetches that day's market
and checks whether the predicted bucket actually won. Updates
trade_log.csv in place and sends a short Telegram summary of what
resolved. Run this daily, after markets have had time to resolve.
"""

import csv
import html
import os
import re
import sys
from datetime import date, datetime

import requests

from config import CITIES, US_STATION_SLUG
from data_sources import (
    build_event_slug, fetch_market_by_slug, parse_outcomes, fetch_actual_high,
    build_polymarket_us_slug, fetch_polymarket_us_event, parse_polymarket_us_outcomes,
)
from log import LOG_FILE, FIELDNAMES

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Display-only labels for app-side outcome lines, so they're visually
# distinct from the website line for the same city instead of relying
# solely on the "(app)" suffix. Matches the real station each platform
# actually uses (confirmed via the Polymarket US gateway API).
APP_DISPLAY_CODE = {
    "ORD": "MDW",
    "LGA": "NYC",
}


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
    ra = re.search(r"(\d+)\s*-\s*(\d+)", label_a or "")
    rb = re.search(r"(\d+)\s*-\s*(\d+)", label_b or "")
    if not ra or not rb:
        return False
    return ra.groups() == rb.groups()


def _resolve(outcomes, predicted_label):
    """Shared resolution logic for both website and app. Returns
    (outcome, winning_label):
      outcome: True/False/None (win/loss/still unresolved)
      winning_label: the label of whichever bucket actually won (yes
        price >= 0.9), if one has settled -- populated even on a LOSS,
        so you can see what actually happened instead of just "no"."""
    winning_label = None
    predicted_result = None
    for label, lo, hi, yes_price in outcomes:
        if yes_price >= 0.9:
            winning_label = label
        if bucket_matches(label, predicted_label):
            if yes_price >= 0.9:
                predicted_result = True
            elif yes_price <= 0.1:
                predicted_result = False
            # else: ambiguous/not fully settled -- leave as None
    return predicted_result, winning_label


def check_one(city_code: str, target_date_str: str, predicted_label: str):
    """Returns (outcome, winning_label). See _resolve()."""
    city = CITIES.get(city_code)
    if not city or not predicted_label:
        return None, None
    target_date = date.fromisoformat(target_date_str)
    slug = build_event_slug(city["slug"], target_date)
    event = fetch_market_by_slug(slug)
    if not event or not event.get("closed", False):
        return None, None  # not resolved yet (or event missing)
    outcomes = parse_outcomes(event)
    return _resolve(outcomes, predicted_label)


def check_one_app(city_code: str, target_date_str: str, predicted_label: str):
    """App-side (Polymarket US) equivalent of check_one(). Returns
    (outcome, winning_label). Uses the same >=0.9 win / <=0.1 loss
    price-threshold convention as the website side."""
    us_station_slug = US_STATION_SLUG.get(city_code)
    if not us_station_slug or not predicted_label:
        return None, None
    target_date = date.fromisoformat(target_date_str)
    slug = build_polymarket_us_slug(us_station_slug, target_date)
    event = fetch_polymarket_us_event(slug)
    if not event or not event.get("closed", False):
        return None, None  # not resolved yet (or event missing)
    outcomes = parse_polymarket_us_outcomes(event)
    return _resolve(outcomes, predicted_label)


def main():
    if not os.path.exists(LOG_FILE):
        print("No trade_log.csv yet -- nothing to check.")
        return

    with open(LOG_FILE, newline="") as f:
        rows = list(csv.DictReader(f))

    # Older rows logged before these fields were tracked won't have the
    # keys at all -- normalize before doing anything else.
    for row in rows:
        row.setdefault("actual_high", "")
        row.setdefault("app_bucket_label", "")
        row.setdefault("app_market_price", "")
        row.setdefault("app_outcome_win", "")
        row.setdefault("actual_winning_bucket", "")
        row.setdefault("actual_winning_bucket_app", "")

    today = date.today()
    resolved_summary = []

    for row in rows:
        try:
            target_date = date.fromisoformat(row["target_date"])
        except ValueError:
            continue
        if target_date >= today:
            continue  # market hasn't happened yet

        station = row.get("station") or CITIES.get(row["city"], {}).get("station")

        already_resolved = row["outcome_win"] not in ("", None)

        if not already_resolved:
            outcome, winning_label = check_one(row["city"], row["target_date"], row["market_bucket_label"])
            if outcome is not None:
                row["outcome_win"] = int(outcome)
                row["actual_winning_bucket"] = winning_label or ""
                miss_note = "" if outcome else f" (actual winner: {html.escape(winning_label or 'unknown')})"
                resolved_summary.append(
                    f"{'✅' if outcome else '❌'} {row['city']} {row['target_date']}: "
                    f"predicted {html.escape(row['market_bucket_label'])} ({row['recommendation']}, "
                    f"{float(row['confidence']):.0%}) -> {'WIN' if outcome else 'LOSS'}{miss_note}"
                )

        app_already_resolved = row.get("app_outcome_win") not in ("", None)
        if not app_already_resolved:
            if row.get("app_bucket_label"):
                app_outcome, app_winning_label = check_one_app(row["city"], row["target_date"], row["app_bucket_label"])
                if app_outcome is not None:
                    row["app_outcome_win"] = int(app_outcome)
                    row["actual_winning_bucket_app"] = app_winning_label or ""
                    app_display = APP_DISPLAY_CODE.get(row["city"], row["city"])
                    miss_note = "" if app_outcome else f" (actual winner: {html.escape(app_winning_label or 'unknown')})"
                    resolved_summary.append(
                        f"{'✅' if app_outcome else '❌'} {app_display} {row['target_date']} (app): "
                        f"predicted {html.escape(row['app_bucket_label'])} -> {'WIN' if app_outcome else 'LOSS'}{miss_note}"
                    )
            else:
                # No app bucket was ever captured for this prediction --
                # nothing to resolve. Only worth printing if this row
                # could still plausibly resolve later -- i.e. website has
                # a real bucket to check and just hasn't closed yet.
                # Rows with NO bucket on either side (market_bucket_label
                # also blank -- a WATCH row where NBM/market data never
                # lined up that day) can never resolve on the website
                # side either, so waiting for "website resolved" would
                # spam forever. Same for rows already closed out.
                never_resolvable = not row.get("market_bucket_label")
                already_closed_out = row["outcome_win"] not in ("", None)
                if never_resolvable or already_closed_out:
                    pass  # nothing left to ever log for this row
                else:
                    print(f"No app_bucket_label for {row['city']} {row['target_date']} -- "
                          f"app side was never populated for this row, so there's nothing to check.")

        # Backfill actual_high for any row missing it -- newly resolved
        # rows and previously-resolved rows logged before this field existed.
        if not row.get("actual_high") and station:
            actual = fetch_actual_high(station, target_date)
            if actual is not None:
                row["actual_high"] = actual

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
