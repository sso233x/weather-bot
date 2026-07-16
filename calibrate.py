"""
calibrate.py — run this once you have a meaningful number of resolved
predictions (30+ minimum, 50+ ideally) to see whether your confidence
scores actually track real win rate, and get a suggested confidence
cutoff for hitting a target win rate like 70%.

Usage: python3 calibrate.py
"""

import csv
import json
import os
from datetime import datetime, timezone

from log import LOG_FILE


def load_resolved_rows():
    if not os.path.exists(LOG_FILE):
        print(f"No {LOG_FILE} found yet.")
        return []
    with open(LOG_FILE) as f:
        all_rows = [r for r in csv.DictReader(f) if r["outcome_win"] not in ("", None)]

    # Historical duplicate protection: before log.py was fixed to upsert,
    # (and for any manual re-runs) the same real-world market could have
    # multiple logged rows for the same (city, target_date), each
    # independently resolved. Counting all of them inflates n and
    # corrupts win-rate stats. Keep only the LATEST snapshot per
    # (city, target_date) -- that's the one closest to what you'd have
    # actually acted on.
    latest_by_market = {}
    for r in all_rows:
        key = (r["city"], r["target_date"])
        existing = latest_by_market.get(key)
        if existing is None or r["logged_at"] > existing["logged_at"]:
            latest_by_market[key] = r

    deduped = list(latest_by_market.values())
    dropped = len(all_rows) - len(deduped)
    if dropped:
        print(f"(Deduped {dropped} duplicate same-market rows -- "
              f"{len(all_rows)} logged rows -> {len(deduped)} unique markets)\n")
    return deduped


def summarize(rows):
    if not rows:
        print("No resolved rows yet.")
        return
    n = len(rows)
    wins = sum(int(r["outcome_win"]) for r in rows)
    print(f"Resolved predictions: {n}, wins: {wins}, raw win rate: {wins/n:.1%}\n")

    by_rec = {}
    for r in rows:
        by_rec.setdefault(r["recommendation"], []).append(int(r["outcome_win"]))
    print("Win rate by recommendation tier:")
    for rec, outcomes in by_rec.items():
        wr = sum(outcomes) / len(outcomes)
        print(f"  {rec:6s}: n={len(outcomes):3d}  win rate={wr:.1%}")

    scored = sorted(rows, key=lambda r: float(r["confidence"]))
    print("\nWin rate by confidence bucket (low to high):")
    bucket_size = max(1, len(scored) // 5)
    for i in range(0, len(scored), bucket_size):
        chunk = scored[i:i + bucket_size]
        if not chunk:
            continue
        wr = sum(int(r["outcome_win"]) for r in chunk) / len(chunk)
        lo, hi = float(chunk[0]["confidence"]), float(chunk[-1]["confidence"])
        print(f"  conf {lo:.2f}-{hi:.2f}: n={len(chunk):3d}  win rate={wr:.1%}")

    print(
        "\nIf win rate doesn't roughly increase with confidence, a signal "
        "is adding noise, not information -- consider down-weighting it "
        "in scoring.py's DEFAULT_WEIGHTS."
    )

    MIN_SAMPLE = 15  # below this, flag instead of trusting the number

    print("\nWin rate by city:")
    by_city = {}
    for r in rows:
        by_city.setdefault(r["city"], []).append(int(r["outcome_win"]))
    for city, outcomes in sorted(by_city.items()):
        wr = sum(outcomes) / len(outcomes)
        flag = "" if len(outcomes) >= MIN_SAMPLE else f"  <-- only {len(outcomes)}, not reliable yet"
        print(f"  {city:5s}: n={len(outcomes):3d}  win rate={wr:.1%}{flag}")

    print("\nWin rate by XND value (overall):")
    by_xnd = {}
    for r in rows:
        by_xnd.setdefault(r["xnd"], []).append(int(r["outcome_win"]))
    for xnd, outcomes in sorted(by_xnd.items()):
        wr = sum(outcomes) / len(outcomes)
        flag = "" if len(outcomes) >= MIN_SAMPLE else f"  <-- only {len(outcomes)}, not reliable yet"
        print(f"  XND={xnd}: n={len(outcomes):3d}  win rate={wr:.1%}{flag}")

    print("\nWin rate by city + XND (tests city-specific dispersion rules, e.g. SFO/XND>=3):")
    by_city_xnd = {}
    for r in rows:
        by_city_xnd.setdefault((r["city"], r["xnd"]), []).append(int(r["outcome_win"]))
    for (city, xnd), outcomes in sorted(by_city_xnd.items()):
        wr = sum(outcomes) / len(outcomes)
        flag = "" if len(outcomes) >= MIN_SAMPLE else f"  <-- only {len(outcomes)}, not reliable yet"
        print(f"  {city:5s} / XND={xnd}: n={len(outcomes):3d}  win rate={wr:.1%}{flag}")

    print("\nApp (Polymarket US) vs Website win rate, per city:")
    print("  Only counts rows where BOTH sides have resolved.")
    by_city_both = {}
    for r in rows:
        if r.get("app_outcome_win") in ("", None):
            continue  # app side not resolved (or wasn't tracked) for this row
        by_city_both.setdefault(r["city"], []).append(
            (int(r["outcome_win"]), int(r["app_outcome_win"]))
        )
    if not by_city_both:
        print("  No rows with both sides resolved yet.")
    else:
        for city, pairs in sorted(by_city_both.items()):
            site_wr = sum(p[0] for p in pairs) / len(pairs)
            app_wr = sum(p[1] for p in pairs) / len(pairs)
            flag = "" if len(pairs) >= MIN_SAMPLE else f"  <-- only {len(pairs)}, not reliable yet"
            print(f"  {city:5s}: website={site_wr:.1%}  app={app_wr:.1%}  (n={len(pairs)}){flag}")
    print("  Positive = model runs HOT for that city. Negative = runs COLD.")
    print("  Only uses rows where actual_high has been backfilled by check_outcomes.py.")
    by_city_bias = compute_city_bias(rows)
    if not by_city_bias:
        print("  No actual_high data yet -- run check_outcomes.py again to backfill it,")
        print("  then re-run this script.")
    else:
        for city, (avg, n) in sorted(by_city_bias.items()):
            flag = "" if n >= MIN_SAMPLE else f"  <-- only {n}, not reliable yet"
            print(f"  {city:5s}: {avg:+.1f}°F avg bias (n={n}){flag}")

    return by_city_bias


def compute_city_bias(rows) -> dict:
    """Returns {city: (avg_bias, n)} for cities with actual_high data."""
    by_city_bias = {}
    for r in rows:
        actual = r.get("actual_high")
        if not actual:
            continue
        try:
            diff = float(r["txn"]) - float(actual)
        except (ValueError, TypeError):
            continue
        by_city_bias.setdefault(r["city"], []).append(diff)
    return {city: (sum(diffs) / len(diffs), len(diffs)) for city, diffs in by_city_bias.items()}


def suggest_threshold(rows, target_win_rate=0.70):
    if len(rows) < 15:
        print(f"\nNeed 15+ resolved samples before a threshold suggestion means anything (have {len(rows)}).")
        return None
    scored = sorted(rows, key=lambda r: -float(r["confidence"]))
    best_cutoff = None
    for i in range(len(scored)):
        chunk = scored[:i + 1]
        wr = sum(int(r["outcome_win"]) for r in chunk) / len(chunk)
        if wr >= target_win_rate:
            best_cutoff = float(chunk[-1]["confidence"])
    if best_cutoff is not None:
        print(f"\nSuggested MIN_CONFIDENCE_TO_ACT for >= {target_win_rate:.0%} "
              f"win rate: {best_cutoff:.2f} (based on {len(rows)} samples -- "
              f"treat cautiously until 50+)")
        return best_cutoff
    else:
        print(f"\nNo cutoff in your history hits {target_win_rate:.0%} yet -- "
              "more samples needed, or the signals need reweighting.")
        return None


def write_learned_adjustments(rows, city_bias: dict, suggested_threshold):
    """Writes learned_adjustments.json for scoring.py to read. Every
    value is gated by MIN_SAMPLE (15) -- calibrate.py only ever writes a
    value once it's actually earned real confidence, never from a small
    sample. Missing keys just mean 'not enough data yet', and
    scoring.py's load_learned_adjustments() already treats that as
    'fall back to the static default', so it's always safe to omit."""
    MIN_SAMPLE = 15
    adjustments = {"generated_at": datetime.now(timezone.utc).isoformat()}

    if suggested_threshold is not None and len(rows) >= 50:
        # Extra-conservative here: the confidence threshold directly
        # controls GO/WATCH/SKIP for every city, so this gets a higher
        # bar (50) than the per-signal MIN_SAMPLE (15) used elsewhere.
        adjustments["min_confidence_to_act"] = round(suggested_threshold, 3)

    city_bias_out = {
        city: round(avg, 2)
        for city, (avg, n) in city_bias.items()
        if n >= MIN_SAMPLE
    }
    if city_bias_out:
        adjustments["city_txn_bias"] = city_bias_out

    learned_file = os.path.join(os.path.dirname(__file__), "learned_adjustments.json")
    with open(learned_file, "w") as f:
        json.dump(adjustments, f, indent=2)

    learned_keys = [k for k in adjustments if k != "generated_at"]
    if learned_keys:
        print(f"\nWrote learned_adjustments.json with: {learned_keys}")
    else:
        print(f"\nNo values crossed the sample-size bar yet -- "
              f"learned_adjustments.json written but empty (scoring.py "
              f"will use static defaults for everything).")


if __name__ == "__main__":
    rows = load_resolved_rows()
    city_bias = summarize(rows) or {}
    suggested_threshold = suggest_threshold(rows, target_win_rate=0.70)
    write_learned_adjustments(rows, city_bias, suggested_threshold)
