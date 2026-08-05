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
    build_event_slug, fetch_market_by_slug, parse_outcomes,
    build_polymarket_us_slug, fetch_polymarket_us_event, parse_polymarket_us_outcomes,
    extract_max_for_date, parse_bulletin_issue_time,
)
from history import load_history, save_history, record_run, recent_values
from scoring import (
    get_txn_bias, get_sigma, compute_bucket_probabilities,
    find_best_edge_bucket, evaluate_edge, ScoreResult,
    MIN_PRICE_FOR_EDGE, MAX_PRICE_FOR_EDGE,
)
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


def _no_bucket_reason(outcomes, txn) -> str:
    """Explains WHY no bucket was selected -- distinguishes genuinely
    missing data from every real bucket being excluded by the price
    trust band (which is informative: it means the market has data, but
    none of it was trustworthy enough to act on)."""
    if not outcomes:
        return "no valid market bucket to evaluate -- missing event or no buckets parsed"
    if txn is None:
        return "no valid market bucket to evaluate -- missing TXN"
    priced = [price for _, _, _, price in outcomes if price is not None]
    all_too_cheap = priced and all(p < MIN_PRICE_FOR_EDGE for p in priced)
    all_too_expensive = priced and all(p > MAX_PRICE_FOR_EDGE for p in priced)
    all_outside_band = priced and all(p < MIN_PRICE_FOR_EDGE or p > MAX_PRICE_FOR_EDGE for p in priced)
    if all_too_cheap:
        return (f"every bucket priced below {MIN_PRICE_FOR_EDGE:.0%} -- market likely too thin/new "
                f"to trust yet, not treating any of them as real edge")
    if all_too_expensive:
        return (f"every bucket priced above {MAX_PRICE_FOR_EDGE:.0%} -- near-certainty this far out "
                f"is just as likely a thin-liquidity artifact as a near-zero price, not trusting it")
    if all_outside_band:
        return (f"every bucket priced outside the {MIN_PRICE_FOR_EDGE:.0%}-{MAX_PRICE_FOR_EDGE:.0%} "
                f"trust band -- market likely too thin/new to trust yet")
    return "no valid market bucket to evaluate"


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
        if NBM_CYCLE == "01":
            # Evening run: predicts the day that's about to start.
            target_date = issue_time_et.date() + timedelta(days=1)
        else:
            # Morning/midday run: happens ON the day it should be
            # evaluating (the same day last night's run predicted), not
            # the day after. Using the bulletin's own issue date (not
            # wall-clock) keeps this robust to scheduling delays the
            # same way the evening case is.
            target_date = issue_time_et.date()
    else:
        # Fallback if no bulletin could be parsed at all (e.g. total NBM
        # fetch failure) -- wall-clock is a reasonable last resort here
        # since there's no bulletin data to anchor to anyway.
        print("WARNING: could not parse any bulletin issue time -- "
              "falling back to wall-clock date (less robust to scheduling delays).")
        target_date = today_et + timedelta(days=1) if NBM_CYCLE == "01" else today_et

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

        sigma, sigma_source = get_sigma(code, latest_xnd)

        slug = build_event_slug(city["slug"], target_date)
        event = fetch_market_by_slug(slug)
        outcomes = parse_outcomes(event) if event else []

        bucket_label = bucket_low = bucket_high = market_price = None
        website_best = None
        if outcomes and corrected_txn is not None:
            bucket_probs = compute_bucket_probabilities(outcomes, corrected_txn, sigma)
            website_best = find_best_edge_bucket(bucket_probs)
            if website_best:
                bucket_label, bucket_low, bucket_high, market_price, _, _ = website_best

        # App side (Polymarket US) -- separate platform, separate order
        # book, resolves against NWS CLI instead of the website's source.
        # Confirmed different station for Chicago (mdw) and NYC (nyc).
        us_station_slug = US_STATION_SLUG.get(code)
        app_bucket_label = app_market_price = None
        app_best = None
        if us_station_slug:
            app_slug = build_polymarket_us_slug(us_station_slug, target_date)
            app_event = fetch_polymarket_us_event(app_slug)
            app_outcomes = parse_polymarket_us_outcomes(app_event) if app_event else []
            if app_outcomes and corrected_txn is not None:
                app_bucket_probs = compute_bucket_probabilities(app_outcomes, corrected_txn, sigma)
                app_best = find_best_edge_bucket(app_bucket_probs)
                if app_best:
                    app_bucket_label, _, _, app_market_price, _, _ = app_best

        # Website drives the primary recommendation/confidence/notes that
        # get logged -- app is tracked in parallel (see calibrate.py) but
        # doesn't yet drive its own separate recommendation stream.
        result = evaluate_edge(code, website_best, sigma, sigma_source)
        if result is None:
            # No valid website bucket to evaluate -- still need a
            # placeholder so logging/display below doesn't crash. This
            # replaces the old "WATCH, missing market bucket" case.
            result = ScoreResult(
                city_code=code, confidence=0.0, raw_score=0.0, hard_skip=False,
                recommendation="WATCH",
                notes=[_no_bucket_reason(outcomes, corrected_txn)],
            )

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
        if metar_data.get(station) is not None:
            stat_bits.append(f"now {metar_data.get(station)}°F")
        if gridpoint is not None:
            stat_bits.append(f"grid {gridpoint}°F")
        if bucket_label:
            stat_bits.append(f"bucket {html.escape(bucket_label)} @ {market_price:.2f}")
        lines.append("   " + " · ".join(stat_bits) if stat_bits else "   no data")

        if app_bucket_label:
            lines.append(f"   app: {html.escape(app_bucket_label)} @ {app_market_price:.2f}")

        if bias:
            lines.append(f"   🔄 bias-corrected TXN by {bias:+.1f}°F (learned from history)")

        if reused_from_last_night:
            lines.append(f"   ↻ TXN reused from last night's run (not re-derived)")

        # Under the edge-based system, notes are always meaningful (model
        # probability, edge, sigma source, or the no-data fallback) --
        # unlike the old system's many routine confirmations, there's no
        # clutter to filter out, so just show all of them.
        for n in result.notes:
            lines.append(f"   • {html.escape(n)}")

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
