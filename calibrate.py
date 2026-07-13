"""
calibrate.py — run this once you have a meaningful number of resolved
predictions (30+ minimum, 50+ ideally) to see whether your confidence
scores actually track real win rate, and get a suggested confidence
cutoff for hitting a target win rate like 70%.

Usage: python3 calibrate.py
"""

import csv
import os

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

    print("\nAvg (TXN forecast - actual observed high) per city:")
    print("  Positive = model runs HOT for that city. Negative = runs COLD.")
    print("  Only uses rows where actual_high has been backfilled by check_outcomes.py.")
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
    if not by_city_bias:
        print("  No actual_high data yet -- run check_outcomes.py again to backfill it,")
        print("  then re-run this script.")
    else:
        for city, diffs in sorted(by_city_bias.items()):
            avg = sum(diffs) / len(diffs)
            flag = "" if len(diffs) >= MIN_SAMPLE else f"  <-- only {len(diffs)}, not reliable yet"
            print(f"  {city:5s}: {avg:+.1f}°F avg bias (n={len(diffs)}){flag}")


def suggest_threshold(rows, target_win_rate=0.70):
    if len(rows) < 15:
        print(f"\nNeed 15+ resolved samples before a threshold suggestion means anything (have {len(rows)}).")
        return
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
    else:
        print(f"\nNo cutoff in your history hits {target_win_rate:.0%} yet -- "
              "more samples needed, or the signals need reweighting.")


if __name__ == "__main__":
    rows = load_resolved_rows()
    summarize(rows)
    suggest_threshold(rows, target_win_rate=0.70)
