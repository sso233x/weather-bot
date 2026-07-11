#!/usr/bin/env python3
"""
main.py — pulls NBM + METAR + gridpoint + Polymarket data for all 5 cities,
scores each with the signal engine, updates persisted run history, and
sends a Telegram summary. Does not place trades.
"""

import os
import sys
from datetime import date, timedelta

import requests

from config import CITIES
from data_sources import (
    fetch_all_nbm, fetch_all_metar, fetch_gridpoint_max_temp_f,
    build_event_slug, fetch_market_by_slug, parse_outcomes, find_bucket_for_temp,
)
from history import load_history, save_history, record_run, recent_values
from scoring import CitySetup, score_setup

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
NBM_CYCLE = os.environ.get("NBM_CYCLE", "01")


def send_telegram(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials not set -- printing to console instead.\n")
        print(message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    # Telegram caps messages at 4096 chars; split into chunks just in case.
    for i in range(0, len(message), 3500):
        chunk = message[i:i + 3500]
        resp = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": chunk, "parse_mode": "HTML"},
            timeout=15,
        )
        resp.raise_for_status()


def main():
    try:
        nbm_data = fetch_all_nbm(NBM_CYCLE)
    except Exception as e:
        send_telegram(f"⚠️ NBM pull failed: {e}")
        sys.exit(1)

    try:
        metar_data = fetch_all_metar()
    except Exception as e:
        print(f"METAR pull failed (non-fatal): {e}")
        metar_data = {}

    history = load_history()
    target_date = date.today() + timedelta(days=1)  # "tomorrow's" market, adjust if running for today

    lines = [f"<b>Signal check — NBM {NBM_CYCLE}Z cycle — {target_date}</b>"]

    for code, city in CITIES.items():
        station = city["station"]
        nbm = nbm_data.get(station, {"TXN": [], "XND": []})
        latest_txn = nbm["TXN"][0] if nbm["TXN"] else None
        latest_xnd = nbm["XND"][0] if nbm["XND"] else None

        # persist today's TXN so tomorrow's run has trend history
        if latest_txn is not None:
            record_run(history, station, latest_txn)
        txn_hist = recent_values(history, station, n=3)

        gridpoint = fetch_gridpoint_max_temp_f(city["lat"], city["lon"])

        slug = build_event_slug(city["slug"], target_date)
        event = fetch_market_by_slug(slug)
        outcomes = parse_outcomes(event) if event else []

        bucket_label = bucket_low = bucket_high = market_price = None
        if outcomes and latest_txn is not None:
            found = find_bucket_for_temp(outcomes, latest_txn)
            if found:
                bucket_label, bucket_low, bucket_high, market_price = found

        setup = CitySetup(
            city_code=code,
            target_date=str(target_date),
            txn_history=txn_hist,
            latest_xnd=latest_xnd,
            gridpoint_max_f=gridpoint,
            metar_f=metar_data.get(station),
            market_bucket_label=bucket_label,
            market_bucket_low=bucket_low,
            market_bucket_high=bucket_high,
            market_price=market_price,
        )
        result = score_setup(setup)

        lines.append(f"\n<b>{city['name']} ({station})</b> — {result.recommendation} ({result.confidence:.0%})")
        if latest_txn is not None:
            lines.append(f"  TXN: {latest_txn}°F | XND: {latest_xnd}")
        if setup.metar_f is not None:
            lines.append(f"  METAR now: {setup.metar_f}°F")
        if gridpoint is not None:
            lines.append(f"  NWS gridpoint: {gridpoint}°F")
        if bucket_label:
            lines.append(f"  Market bucket: {bucket_label} @ {market_price:.2f}")
        elif event is None:
            lines.append(f"  Market: event not found at slug '{slug}' (wrong slug, or not posted yet)")
        elif not outcomes:
            lines.append(f"  Market: event found ({len(event.get('markets', []))} sub-markets) but none parsed as buckets")
        else:
            lines.append(f"  Market: {len(outcomes)} buckets found, but TXN {latest_txn}F didn't match any range")
        for n in result.notes:
            lines.append(f"  • {n}")

    save_history(history)

    message = "\n".join(lines)
    send_telegram(message)
    print(message)


if __name__ == "__main__":
    main()
