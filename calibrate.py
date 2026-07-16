"""
calibrate.py — run this once you have a meaningful number of resolved
predictions (30+ minimum, 50+ ideally) to see whether your confidence
scores actually track real win rate, and get a suggested confidence
cutoff for hitting a target win rate like 70%.

APP (Polymarket US) results are treated as PRIMARY throughout -- that's
what Merritt actually trades on, and what feeds learned_adjustments.json.
WEBSITE results are kept as a secondary reference section only: app
tracking started later, so app sample sizes will lag website's for a
while, and website data still says something useful about the underlying
TXN/XND forecast signal in the meantime.

Usage: python3 calibrate.py
"""

import csv
import json
import os
from datetime import datetime, timezone

from log import LOG_FILE

MIN_SAMPLE = 15  # below this, flag instead of trusting the number
CONFIDENCE_THRESHOLD_MIN_SAMPLE = 50  # higher bar: controls every GO/WATCH/SKIP call


def _dedup_by_market(all_rows):
    """Keeps only the LATEST snapshot per (city, target_date) -- protects
    against counting the same real-world market more than once (from
    re-runs before log.py's upsert fix, or any manual re-triggers)."""
    latest_by_market = {}
    for r in all_rows:
        key = (r["city"], r["target_date"])
        existing = latest_by_market.get(key)
        if existing is None or r["logged_at"] > existing["logged_at"]:
            latest_by_market[key] = r
    return list(latest_by_market.values())


def load_all_rows():
    """All logged rows deduped to one per real market, regardless of
    resolution status on either side -- used by the comparison and TXN-
    bias sections, which do their own internal filtering for what they
    actually need (both resolved, or actual_high present)."""
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE) as f:
        all_rows = list(csv.DictReader(f))
    return _dedup_by_market(all_rows)


def load_resolved_rows(outcome_field="outcome_win"):
    """outcome_field is 'outcome_win' (website) or 'app_outcome_win'
    (app) -- returns only rows resolved on THAT side, deduped to one row
    per real market."""
    if not os.path.exists(LOG_FILE):
        print(f"No {LOG_FILE} found yet.")
        return []
    with open(LOG_FILE) as f:
        all_rows = [r for r in csv.DictReader(f) if r.get(outcome_field) not in ("", None)]
    deduped = _dedup_by_market(all_rows)
    dropped = len(all_rows) - len(deduped)
    if dropped:
        print(f"(Deduped {dropped} duplicate same-market rows on {outcome_field} -- "
              f"{len(all_rows)} logged rows -> {len(deduped)} unique markets)")
    return deduped


def breakdown(rows, outcome_field, label):
    """Prints the full set of win-rate breakdowns for one outcome source
    (website or app)."""
    print(f"\n{'=' * 60}\n{label}\n{'=' * 60}")
    if not rows:
        print("No resolved rows yet for this source.")
        return
    n = len(rows)
    wins = sum(int(r[outcome_field]) for r in rows)
    print(f"Resolved predictions: {n}, wins: {wins}, raw win rate: {wins/n:.1%}\n")

    by_rec = {}
    for r in rows:
        by_rec.setdefault(r["recommendation"], []).append(int(r[outcome_field]))
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
        wr = sum(int(r[outcome_field]) for r in chunk) / len(chunk)
        lo, hi = float(chunk[0]["confidence"]), float(chunk[-1]["confidence"])
        print(f"  conf {lo:.2f}-{hi:.2f}: n={len(chunk):3d}  win rate={wr:.1%}")

    print("\nWin rate by city:")
    by_city = {}
    for r in rows:
        by_city.setdefault(r["city"], []).append(int(r[outcome_field]))
    for city, outcomes in sorted(by_city.items()):
        wr = sum(outcomes) / len(outcomes)
        flag = "" if len(outcomes) >= MIN_SAMPLE else f"  <-- only {len(outcomes)}, not reliable yet"
        print(f"  {city:5s}: n={len(outcomes):3d}  win rate={wr:.1%}{flag}")

    print("\nWin rate by XND value (overall):")
    by_xnd = {}
    for r in rows:
        by_xnd.setdefault(r["xnd"], []).append(int(r[outcome_field]))
    for xnd, outcomes in sorted(by_xnd.items()):
        wr = sum(outcomes) / len(outcomes)
        flag = "" if len(outcomes) >= MIN_SAMPLE else f"  <-- only {len(outcomes)}, not reliable yet"
        print(f"  XND={xnd}: n={len(outcomes):3d}  win rate={wr:.1%}{flag}")

    print("\nWin rate by city + XND:")
    by_city_xnd = {}
    for r in rows:
        by_city_xnd.setdefault((r["city"], r["xnd"]), []).append(int(r[outcome_field]))
    for (city, xnd), outcomes in sorted(by_city_xnd.items()):
        wr = sum(outcomes) / len(outcomes)
        flag = "" if len(outcomes) >= MIN_SAMPLE else f"  <-- only {len(outcomes)}, not reliable yet"
        print(f"  {city:5s} / XND={xnd}: n={len(outcomes):3d}  win rate={wr:.1%}{flag}")


def compute_city_bias(rows) -> dict:
    """Returns {city: (avg_bias, n)} for cities with actual_high data.
    PLATFORM-INDEPENDENT: compares forecast TXN against real observed
    weather (via IEM), not against either market's outcome -- so this
    doesn't need an app/website split, unlike everything else here."""
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


def print_app_vs_website_comparison(all_rows):
    """Paired comparison: only rows where BOTH sides resolved. Direct
    test of the original app/website divergence edge."""
    print(f"\n{'=' * 60}\nApp vs Website comparison (paired, both sides resolved)\n{'=' * 60}")
    by_city_both = {}
    for r in all_rows:
        if r.get("outcome_win") in ("", None) or r.get("app_outcome_win") in ("", None):
            continue
        by_city_both.setdefault(r["city"], []).append(
            (int(r["outcome_win"]), int(r["app_outcome_win"]))
        )
    if not by_city_both:
        print("No rows with both sides resolved yet.")
        return
    for city, pairs in sorted(by_city_both.items()):
        site_wr = sum(p[0] for p in pairs) / len(pairs)
        app_wr = sum(p[1] for p in pairs) / len(pairs)
        flag = "" if len(pairs) >= MIN_SAMPLE else f"  <-- only {len(pairs)}, not reliable yet"
        print(f"  {city:5s}: website={site_wr:.1%}  app={app_wr:.1%}  (n={len(pairs)}){flag}")


def print_txn_bias(all_rows):
    print(f"\n{'=' * 60}\nTXN forecast bias vs actual observed high (platform-independent)\n{'=' * 60}")
    print("Positive = model runs HOT for that city. Negative = runs COLD.")
    by_city_bias = compute_city_bias(all_rows)
    if not by_city_bias:
        print("No actual_high data yet -- run check_outcomes.py again to backfill it.")
    else:
        for city, (avg, n) in sorted(by_city_bias.items()):
            flag = "" if n >= MIN_SAMPLE else f"  <-- only {n}, not reliable yet"
            print(f"  {city:5s}: {avg:+.1f}°F avg bias (n={n}){flag}")
    return by_city_bias


def suggest_threshold(rows, outcome_field, target_win_rate=0.70):
    if len(rows) < 15:
        print(f"\nNeed 15+ resolved samples before a threshold suggestion means anything (have {len(rows)}).")
        return None
    scored = sorted(rows, key=lambda r: -float(r["confidence"]))
    best_cutoff = None
    for i in range(len(scored)):
        chunk = scored[:i + 1]
        wr = sum(int(r[outcome_field]) for r in chunk) / len(chunk)
        if wr >= target_win_rate:
            best_cutoff = float(chunk[-1]["confidence"])
    if best_cutoff is not None:
        print(f"\nSuggested MIN_CONFIDENCE_TO_ACT for >= {target_win_rate:.0%} "
              f"win rate: {best_cutoff:.2f} (based on {len(rows)} samples -- "
              f"treat cautiously until {CONFIDENCE_THRESHOLD_MIN_SAMPLE}+)")
        return best_cutoff
    else:
        print(f"\nNo cutoff in your history hits {target_win_rate:.0%} yet -- "
              "more samples needed, or the signals need reweighting.")
        return None


def write_learned_adjustments(app_rows, city_bias: dict, app_suggested_threshold):
    """Writes learned_adjustments.json for scoring.py to read. Uses APP
    results for the confidence threshold, since that's what actually
    drives real trading decisions. city_txn_bias is platform-independent.
    Every value is gated by a sample-size minimum -- missing keys just
    mean 'not enough data yet', and scoring.py already treats that as
    'fall back to the static default'."""
    adjustments = {"generated_at": datetime.now(timezone.utc).isoformat()}

    if app_suggested_threshold is not None and len(app_rows) >= CONFIDENCE_THRESHOLD_MIN_SAMPLE:
        adjustments["min_confidence_to_act"] = round(app_suggested_threshold, 3)
        adjustments["min_confidence_to_act_source"] = "app"

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
    app_rows = load_resolved_rows("app_outcome_win")
    website_rows = load_resolved_rows("outcome_win")
    all_rows = load_all_rows()

    breakdown(app_rows, "app_outcome_win", "PRIMARY: App (Polymarket US) results")
    breakdown(website_rows, "outcome_win", "SECONDARY REFERENCE: Website results")

    print_app_vs_website_comparison(all_rows)
    city_bias = print_txn_bias(all_rows)

    print(f"\n{'=' * 60}\nConfidence threshold suggestion (app-based, drives real trades)\n{'=' * 60}")
    app_suggested_threshold = suggest_threshold(app_rows, "app_outcome_win", target_win_rate=0.70)

    write_learned_adjustments(app_rows, city_bias, app_suggested_threshold)
