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
        return [r for r in csv.DictReader(f) if r["outcome_win"] not in ("", None)]


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
