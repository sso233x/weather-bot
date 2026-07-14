#!/usr/bin/env python3
"""
main.py — pulls NBM + METAR + gridpoint + Polymarket data for all 5 cities,
scores each with the signal engine, updates persisted run history, and
sends a Telegram summary. Does not place trades.
"""

import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from config import CITIES, US_STATION_SLUG
from data_sources import (
    fetch_all_nbm, fetch_all_metar, fetch_gridpoint_max_temp_f,
    build_event_slug, fetch_market_by_slug, parse_outcomes, find_bucket_for_temp,
    build_polymarket_us_slug, fetch_polymarket_us_event, parse_polymarket_us_outcomes,
)
from history import load_history, save_history, record_run, recent_values
from scoring import CitySetup, score_setup
from log import log_prediction

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
    # Use US/Eastern calendar day, not the server's UTC day -- the evening
    # run fires right around the UTC midnight rollover (00:15 UTC is still
    # only ~8:15pm ET), so using UTC's date() would silently push the
    # target date a full day too far ahead on that run specifically.
    ET = ZoneInfo("America/New_York")
    today_et = datetime.now(ET).date()
    if NBM_CYCLE == "01":
        # evening run: predicting tomorrow's market
        target_date = today_et + timedelta(days=1)
    else:
        # morning-of run (07Z/13Z/etc): reconfirming today's market,
        # which is the same date the prior evening run predicted
        target_date = today_et

    REC_EMOJI = {"GO": "🟢", "WATCH": "🟡", "SKIP": "🔴"}

    lines = [f"📅 <b>Signal Check</b> — {NBM_CYCLE}Z — {target_date}\n"]

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

        # App side (Polymarket US) -- separate platform, separate order
        # book, resolves against NWS CLI instead of the website's source.
        # Confirmed different station for Chicago (mdw) and NYC (nyc).
        us_station_slug = US_STATION_SLUG.get(code)
        app_bucket_label = app_market_price = None
        if us_station_slug:
            app_slug = build_polymarket_us_slug(us_station_slug, target_date)
            app_event = fetch_polymarket_us_event(app_slug)
            app_outcomes = parse_polymarket_us_outcomes(app_event) if app_event else []
            if app_outcomes and latest_txn is not None:
                app_found = find_bucket_for_temp(app_outcomes, latest_txn)
                if app_found:
                    app_bucket_label, _, _, app_market_price = app_found

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
        log_prediction(code, station, str(target_date), latest_txn, latest_xnd,
                        bucket_label, market_price, result,
                        app_bucket_label, app_market_price)

        emoji = REC_EMOJI.get(result.recommendation, "⚪")
        lines.append(f"{emoji} <b>{city['name']}</b> — {result.recommendation} ({result.confidence:.0%})")

        stat_bits = []
        if latest_txn is not None:
            stat_bits.append(f"TXN {latest_txn}°F")
        if latest_xnd is not None:
            stat_bits.append(f"XND {latest_xnd}")
        if setup.metar_f is not None:
            stat_bits.append(f"now {setup.metar_f}°F")
        if bucket_label:
            stat_bits.append(f"bucket {bucket_label} @ {market_price:.2f}")
        lines.append("   " + " · ".join(stat_bits) if stat_bits else "   no data")

        if app_bucket_label:
            lines.append(f"   app: {app_bucket_label} @ {app_market_price:.2f}")

        # Only surface the notes that actually change the picture -- skip
        # routine confirmations to keep this scannable on a phone.
        highlights = []
        for n in result.notes:
            if any(kw in n for kw in ("HARD SKIP", "contradict", "outside", "diverge",
                                       "can't", "unstable", "not found", "didn't match")):
                highlights.append(n)
        for h in highlights:
            lines.append(f"   ⚠️ {h}")

        if not bucket_label:
            if event is None:
                lines.append(f"   ⚠️ market event not found (slug: {slug})")
            elif not outcomes:
                lines.append(f"   ⚠️ event found but no buckets parsed")

        lines.append("")

    save_history(history)

    message = "\n".join(lines)
    send_telegram(message)
    print(message)


if __name__ == "__main__":
    main()
