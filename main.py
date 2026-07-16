#!/usr/bin/env python3
"""
main.py — pulls NBM + METAR + gridpoint + Polymarket data for all 5 cities,
scores each with the signal engine, updates persisted run history, and
sends a Telegram summary. Does not place trades.
"""

import html
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
    extract_max_for_date, parse_bulletin_issue_time,
)
from history import load_history, save_history, record_run, recent_values
from scoring import CitySetup, score_setup, get_txn_bias
from log import log_prediction, get_existing_prediction

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

    # Derive target_date from the NBM bulletin's OWN issue timestamp
    # (fixed, embedded in the data) rather than wall-clock execution time.
    # Confirmed necessary on 2026-07-15: the scheduled evening run was
    # delayed by GitHub Actions from ~9:11pm ET to 12:22am ET -- crossing
    # midnight meant wall-clock "today + 1" silently became the WRONG
    # date (predicted the 17th instead of the 16th) purely because of
    # when the job happened to execute, not because of anything about
    # the actual forecast data. The bulletin's issue time doesn't move
    # just because GitHub was slow to run the job, so anchoring to it
    # instead makes target_date immune to scheduling delays.
    issue_time_et = None
    for station_data in nbm_data.values():
        block = station_data.get("block")
        if block:
            issue_time_utc = parse_bulletin_issue_time(block)
            if issue_time_utc:
                issue_time_et = issue_time_utc.astimezone(ET)
                break

    if issue_time_et is not None:
        target_date = issue_time_et.date() + timedelta(days=1)
    else:
        # Fallback if no bulletin could be parsed at all (e.g. total NBM
        # fetch failure) -- wall-clock is a reasonable last resort here
        # since there's no bulletin data to anchor to anyway.
        print("WARNING: could not parse any bulletin issue time -- "
              "falling back to wall-clock date (less robust to scheduling delays).")
        target_date = today_et + timedelta(days=1)

    REC_EMOJI = {"GO": "🟢", "WATCH": "🟡", "SKIP": "🔴"}

    lines = [f"📅 <b>Signal Check</b> — {NBM_CYCLE}Z — {target_date}\n"]

    for code, city in CITIES.items():
        station = city["station"]
        nbm = nbm_data.get(station, {"TXN": [], "XND": [], "block": None})

        reused_from_last_night = False
        if NBM_CYCLE == "01":
            # Evening run: this IS the source of truth for TXN/XND.
            if nbm.get("block"):
                latest_txn, latest_xnd = extract_max_for_date(nbm["block"], target_date)
            else:
                latest_txn, latest_xnd = None, None
        else:
            # Morning/midday run: reuse last night's TXN instead of
            # re-deriving it. Matches the original manual process (TXN
            # taken once at night, only bucket/price re-checked in the
            # morning) -- and NBM doesn't post a distinct max for an
            # already-mostly-elapsed day anyway, so re-fetching here
            # would either fail or silently return nothing useful.
            existing = get_existing_prediction(code, str(target_date))
            if existing and existing.get("txn"):
                latest_txn = float(existing["txn"])
                latest_xnd = int(existing["xnd"]) if existing.get("xnd") else None
                reused_from_last_night = True
            elif nbm.get("block"):
                # No prior night-before row found (e.g. first-ever run,
                # or last night's run failed) -- fall back to a fresh
                # fetch so this city isn't just silently skipped.
                latest_txn, latest_xnd = extract_max_for_date(nbm["block"], target_date)
            else:
                latest_txn, latest_xnd = None, None

        # Raw TXN gets logged unmodified -- bias correction below is only
        # for bucket lookup/scoring, never for what's persisted, or the
        # bias calculation itself would drift from correcting its own output.
        raw_txn = latest_txn
        bias = get_txn_bias(code) if latest_txn is not None else 0.0
        corrected_txn = latest_txn - bias if latest_txn is not None else None
        if bias and latest_txn is not None:
            print(f"{code}: applying learned bias correction {bias:+.1f}F "
                  f"(raw TXN {raw_txn} -> corrected {corrected_txn:.1f})")

        # persist today's TXN so tomorrow's run has trend history
        if raw_txn is not None and not reused_from_last_night:
            record_run(history, station, raw_txn)
        txn_hist = recent_values(history, station, n=3)
        # Scoring's internal "is TXN inside bucket" checks use
        # txn_history[-1] -- that needs to match corrected_txn (what the
        # bucket was actually chosen against below), not the raw value,
        # or scoring would contradict its own bucket choice whenever a
        # bias correction is active. History persistence above still
        # uses raw_txn unmodified -- only this in-memory copy changes.
        if txn_hist and corrected_txn is not None:
            txn_hist = txn_hist[:-1] + [corrected_txn]

        gridpoint = fetch_gridpoint_max_temp_f(city["lat"], city["lon"])

        slug = build_event_slug(city["slug"], target_date)
        event = fetch_market_by_slug(slug)
        outcomes = parse_outcomes(event) if event else []

        bucket_label = bucket_low = bucket_high = market_price = None
        if outcomes and corrected_txn is not None:
            found = find_bucket_for_temp(outcomes, corrected_txn)
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
            if app_outcomes and corrected_txn is not None:
                app_found = find_bucket_for_temp(app_outcomes, corrected_txn)
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
            stat_bits.append(f"bucket {html.escape(bucket_label)} @ {market_price:.2f}")
        lines.append("   " + " · ".join(stat_bits) if stat_bits else "   no data")

        if app_bucket_label:
            lines.append(f"   app: {html.escape(app_bucket_label)} @ {app_market_price:.2f}")

        if bias:
            lines.append(f"   🔄 bias-corrected TXN by {bias:+.1f}°F (learned from history)")

        if reused_from_last_night:
            lines.append(f"   ↻ TXN reused from last night's run (not re-derived)")

        # Only surface the notes that actually change the picture -- skip
        # routine confirmations to keep this scannable on a phone.
        highlights = []
        for n in result.notes:
            if any(kw in n for kw in ("HARD SKIP", "contradict", "outside", "diverge",
                                       "can't", "unstable", "not found", "didn't match",
                                       "CALIBRATED")):
                highlights.append(n)
        for h in highlights:
            lines.append(f"   ⚠️ {html.escape(h)}")

        if not bucket_label:
            if event is None:
                lines.append(f"   ⚠️ market event not found (slug: {html.escape(slug)})")
            elif not outcomes:
                lines.append(f"   ⚠️ event found but no buckets parsed")

        lines.append("")

    save_history(history)

    message = "\n".join(lines)
    send_telegram(message)
    print(message)


if __name__ == "__main__":
    main()
