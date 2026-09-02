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
import re
from datetime import datetime, timezone

from log import LOG_FILE

MIN_SAMPLE = 15  # below this, flag instead of trusting the number
CONFIDENCE_THRESHOLD_MIN_SAMPLE = 50  # higher bar: controls every GO/WATCH/SKIP call
SIGMA_STRATUM_MIN_SAMPLE = 8  # per (city, XND) -- smaller since it's a finer split than MIN_SAMPLE


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


def is_edge_based_row(row) -> bool:
    """Detects whether a row came from the step-3 edge-based scoring
    system (evaluate_edge) vs. the old hand-tuned composite-score
    system. Uses the exact note text evaluate_edge always produces
    ("model probability X% ... -> edge Y%") rather than a hardcoded
    date cutoff -- the cutoff would be fragile (depends on exactly when
    each file got pasted into the live repo), while this is a direct,
    reliable fingerprint of which code actually produced the row.
    Matters because "confidence" means something different under each
    system (old: arbitrary composite score; new: a real probability) --
    mixing them in the same calibration (especially the confidence
    threshold) is comparing apples to oranges."""
    notes = row.get("notes", "")
    return "model probability" in notes and "-> edge" in notes


def is_price_band_excluded(row) -> bool:
    """True if this row's recommendation came from main.py's
    _no_bucket_reason fallback (every bucket priced outside the trusted
    5%-95% band) rather than a genuine edge evaluation. Added 2026-08-13
    to check a real hypothesis: does the price band -- which fixes a
    real thin-liquidity problem -- also suppress genuine GO opportunities
    by excluding exactly the extreme-priced buckets where real edge is
    more likely to concentrate? Distinguishing this from a genuine
    low-edge SKIP/WATCH is necessary to answer that with evidence
    instead of guessing."""
    notes = row.get("notes", "")
    return "priced below" in notes or "priced above" in notes or "priced outside the" in notes


_SIGMA_NOTE_RE = re.compile(r"sigma=([\d.]+)F \(source: (\w+)\)")


def parse_sigma_from_notes(notes: str):
    """Extracts (sigma, source) from an edge-based row's notes, or
    (None, None) if not found. Lets us cross-tabulate GO/edge outcomes
    against which sigma source produced them, without needing dedicated
    columns for historical rows logged before this diagnostic existed."""
    m = _SIGMA_NOTE_RE.search(notes)
    if not m:
        return None, None
    return float(m.group(1)), m.group(2)


def get_sigma_for_row(row):
    """Prefers the explicit sigma_used/sigma_source columns (added
    2026-08-18); falls back to parsing notes text for older rows logged
    before those columns existed."""
    if row.get("sigma_used") and row.get("sigma_source"):
        try:
            return float(row["sigma_used"]), row["sigma_source"]
        except (ValueError, TypeError):
            pass
    return parse_sigma_from_notes(row.get("notes", ""))


def print_go_diagnostic(app_rows_evaluated):
    """Lists every individual GO call from the edge-based system, with
    its city/XND/sigma/sigma-source/edge/outcome. Added 2026-08-18 to
    investigate a real pattern: GO calls were 0-for-9 while SKIP (19.3%)
    and WATCH (16.7%) both won more often -- an inverted pattern that
    looks more like a calibration issue than 'not enough samples yet'.
    At this sample size, showing every row individually is more useful
    than an aggregate breakdown."""
    go_rows = [r for r in app_rows_evaluated if r.get("recommendation") == "GO"]
    print(f"\n{'=' * 60}\nGO diagnostic -- every individual GO call (n={len(go_rows)})\n{'=' * 60}")
    if not go_rows:
        print("No GO calls in the edge-based system yet.")
        return
    wins = sum(int(r["outcome_win"]) for r in go_rows)
    print(f"GO win rate: {wins}/{len(go_rows)} = {wins/len(go_rows):.1%}")
    print("\nlogged_at              city  xnd  sigma   source    edge     outcome")
    for r in sorted(go_rows, key=lambda r: r["logged_at"]):
        sigma, source = get_sigma_for_row(r)
        edge_match = re.search(r"-> edge ([+-]?[\d.]+)%", r.get("notes", ""))
        edge = edge_match.group(1) + "%" if edge_match else "?"
        outcome = "WIN" if r["outcome_win"] == "1" else "LOSS"
        sigma_str = f"{sigma:.2f}F" if sigma is not None else "?"
        print(f"  {r['logged_at'][:16]}  {r['city']:4s}  {r['xnd']:>3s}  "
              f"{sigma_str:7s} {source or '?':9s} {edge:>7s}  {outcome}")

    # Cross-tab: does a specific sigma source or a tight sigma correlate
    # with the losses? Small n, but worth surfacing directly.
    by_source = {}
    for r in go_rows:
        _, source = get_sigma_for_row(r)
        by_source.setdefault(source or "unknown", []).append(int(r["outcome_win"]))
    print("\nGO win rate by sigma source:")
    for source, outcomes in sorted(by_source.items()):
        wr = sum(outcomes) / len(outcomes)
        print(f"  {source:9s}: n={len(outcomes)}  win rate={wr:.1%}")


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


def compute_city_sigma(rows, city_bias: dict) -> dict:
    """
    EMOS-style spread estimate: how much should TXN actually be trusted
    today, given today's XND? Rather than a hard XND>=3 skip rule (which
    your own data shows has stopped producing a clean gradient at real
    sample sizes), this estimates the REAL historical spread of forecast
    error, stratified by (city, XND) -- literally "how wrong has TXN
    actually been for this city when XND was exactly this value."

    XND is treated as categorical (it only takes a few small integer
    values) rather than fit as a continuous regression -- more robust
    and more transparent with few distinct x-values.

    Returns {city: {xnd_value: (sigma, n)}}. Only includes a (city, xnd)
    entry once it has SIGMA_STRATUM_MIN_SAMPLE+ samples -- below that,
    the caller (scoring.py) falls back to a pooled city-level sigma, and
    below THAT, a static prior. Same gating philosophy as everything
    else in this file, just applied one level finer.
    """
    import statistics
    from collections import defaultdict

    residuals_by_city_xnd = defaultdict(list)
    for r in rows:
        actual = r.get("actual_high")
        xnd = r.get("xnd")
        if not actual or not xnd:
            continue
        try:
            txn = float(r["txn"])
            actual_f = float(actual)
        except (ValueError, TypeError):
            continue
        bias, _ = city_bias.get(r["city"], (0.0, 0))
        residual = (txn - bias) - actual_f  # bias-corrected forecast minus reality
        residuals_by_city_xnd[(r["city"], xnd)].append(residual)

    result = {}
    for (city, xnd), residuals in residuals_by_city_xnd.items():
        if len(residuals) < SIGMA_STRATUM_MIN_SAMPLE:
            continue
        sigma = statistics.stdev(residuals) if len(residuals) > 1 else abs(residuals[0])
        result.setdefault(city, {})[xnd] = (round(sigma, 2), len(residuals))
    return result


def compute_skewness(values):
    """Fisher-Pearson skewness coefficient (g1), population version.
    0 = symmetric. Positive = long right tail (occasional big overshoots
    hot). Negative = long left tail (occasional big undershoots cold).
    A meaningfully skewed distribution violates the Gaussian assumption
    EMOS relies on -- the model would then systematically misjudge
    probability in the tail it's NOT skewed toward, exactly the kind of
    thing that could make a well-supported ("stratum") sigma still
    produce bad edge calls."""
    n = len(values)
    if n < 3:
        return None
    mean = sum(values) / n
    variance = sum((x - mean) ** 2 for x in values) / n
    std = variance ** 0.5
    if std == 0:
        return 0.0
    return (sum((x - mean) ** 3 for x in values) / n) / (std ** 3)


def print_distribution_shape_check(rows, city_bias: dict):
    """Added 2026-08-29 to investigate a real finding: all 9 losing GO
    calls so far came from 'stratum' (the most-trusted, best-supported)
    sigma source, concentrated in LAX. A well-supported sigma still
    producing bad calls suggests the Gaussian symmetry assumption itself
    might not fit -- this checks that directly against real residuals
    rather than guessing further."""
    from collections import defaultdict
    residuals_by_city = defaultdict(list)
    for r in rows:
        actual = r.get("actual_high")
        if not actual:
            continue
        try:
            txn = float(r["txn"])
            actual_f = float(actual)
        except (ValueError, TypeError):
            continue
        bias, _ = city_bias.get(r["city"], (0.0, 0))
        residuals_by_city[r["city"]].append((txn - bias) - actual_f)

    print(f"\n{'=' * 60}\nDistribution shape check -- is forecast error actually symmetric?\n{'=' * 60}")
    print("0 = symmetric (matches the Gaussian assumption EMOS relies on).")
    print("Meaningfully positive/negative = skewed -- the model would then")
    print("misjudge probability specifically in the tail it's NOT skewed")
    print("toward, which a symmetric sigma can't fix no matter how much")
    print("data supports it.\n")
    for city, residuals in sorted(residuals_by_city.items()):
        n = len(residuals)
        if n < MIN_SAMPLE:
            print(f"  {city:5s}: n={n}, not enough samples yet for a skew estimate")
            continue
        skew = compute_skewness(residuals)
        sorted_r = sorted(residuals)
        median = sorted_r[n // 2]
        flag = ""
        if abs(skew) > 0.5:
            flag = "  <-- meaningfully skewed, worth a closer look"
        print(f"  {city:5s}: skew={skew:+.2f}  min={min(residuals):+.1f}  "
              f"median={median:+.1f}  max={max(residuals):+.1f}  (n={n}){flag}")


def compute_city_sigma_pooled(rows, city_bias: dict) -> dict:
    """Fallback level 2: sigma pooled across ALL XND values for a city,
    for when a specific (city, XND) stratum doesn't have enough samples
    yet but the city overall does. Gated by MIN_SAMPLE (15), same bar as
    the rest of the per-city stats in this file."""
    import statistics
    from collections import defaultdict

    residuals_by_city = defaultdict(list)
    for r in rows:
        actual = r.get("actual_high")
        if not actual:
            continue
        try:
            txn = float(r["txn"])
            actual_f = float(actual)
        except (ValueError, TypeError):
            continue
        bias, _ = city_bias.get(r["city"], (0.0, 0))
        residuals_by_city[r["city"]].append((txn - bias) - actual_f)

    result = {}
    for city, residuals in residuals_by_city.items():
        if len(residuals) < MIN_SAMPLE:
            continue
        sigma = statistics.stdev(residuals) if len(residuals) > 1 else abs(residuals[0])
        result[city] = (round(sigma, 2), len(residuals))
    return result


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
        if len(chunk) < MIN_SAMPLE:
            # BUG FIXED 2026-07-31: without this floor, a small early
            # prefix (e.g. a handful of lucky wins among many ties at
            # the top confidence value) could satisfy the win-rate
            # target on a sample size too small to mean anything, and
            # lock in a threshold that doesn't reflect the TRUE win rate
            # at that confidence level once more samples are included.
            # Confirmed on real data: 13 rows tied at confidence=1.00
            # had a true win rate of 53.8% (well under 70%), but the old
            # code had already locked best_cutoff=1.00 from an early
            # lucky streak within those ties, before enough losses
            # accumulated to reveal the real rate.
            continue
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


def write_learned_adjustments(app_rows, city_bias: dict, app_suggested_threshold,
                               city_sigma: dict, city_sigma_pooled: dict):
    """Writes learned_adjustments.json for scoring.py to read. Uses APP
    results for the confidence threshold, since that's what actually
    drives real trading decisions. city_txn_bias and the sigma estimates
    are platform-independent. Every value is gated by a sample-size
    minimum -- missing keys just mean 'not enough data yet', and
    scoring.py already treats that as 'fall back to the static default'."""
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

    # EMOS-style spread estimates. Stratified (city, XND) sigma is more
    # precise where it exists; pooled city-level sigma is the fallback
    # for XND values that haven't individually crossed the sample bar
    # yet. scoring.py tries stratified first, then pooled, then a
    # static prior -- see get_sigma() there.
    sigma_out = {
        city: {str(xnd): sigma for xnd, (sigma, n) in xnd_map.items()}
        for city, xnd_map in city_sigma.items()
        if xnd_map
    }
    if sigma_out:
        adjustments["city_sigma_by_xnd"] = sigma_out

    pooled_out = {city: sigma for city, (sigma, n) in city_sigma_pooled.items()}
    if pooled_out:
        adjustments["city_sigma_pooled"] = pooled_out

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


def print_city_sigma(all_rows, city_bias: dict):
    print(f"\n{'=' * 60}\nEMOS spread (sigma) estimate -- how much to trust TXN, by city + XND\n{'=' * 60}")
    print("Bias-corrected forecast error, stratified by XND. Larger sigma =")
    print("more historical spread at that XND level for that city = less")
    print(f"certain today's TXN is close to the real high. Stratum needs "
          f"{SIGMA_STRATUM_MIN_SAMPLE}+ samples; below that, falls back to")
    print("a pooled city-level estimate, shown separately below.")

    city_sigma = compute_city_sigma(all_rows, city_bias)
    if not city_sigma:
        print("\nNo (city, XND) stratum has enough samples yet.")
    else:
        for city, xnd_map in sorted(city_sigma.items()):
            for xnd, (sigma, n) in sorted(xnd_map.items()):
                print(f"  {city:5s} / XND={xnd}: sigma={sigma:.2f}°F (n={n})")

    city_sigma_pooled = compute_city_sigma_pooled(all_rows, city_bias)
    print("\nPooled (all XND combined) -- fallback for strata without enough data yet:")
    if not city_sigma_pooled:
        print("  No city has enough total samples yet.")
    else:
        for city, (sigma, n) in sorted(city_sigma_pooled.items()):
            print(f"  {city:5s}: sigma={sigma:.2f}°F (n={n})")

    return city_sigma, city_sigma_pooled


if __name__ == "__main__":
    app_rows = load_resolved_rows("app_outcome_win")
    website_rows = load_resolved_rows("outcome_win")
    all_rows = load_all_rows()

    # Three-way split, not two: a row logged by the CURRENT pipeline can
    # either get a genuine edge evaluation, or get excluded entirely by
    # the price band (every bucket priced outside 5%-95%) -- these are
    # both "current system" rows, but is_edge_based_row's note-fingerprint
    # only catches the first kind, since a price-band-excluded row's
    # fallback note never mentions "model probability". Without this
    # three-way split, excluded rows were silently miscounted as
    # "pre-edge-system" in earlier reports, inflating that count.
    app_rows_evaluated = [r for r in app_rows if is_edge_based_row(r)]
    app_rows_excluded = [r for r in app_rows if is_price_band_excluded(r)]
    app_rows_pre_edge = [r for r in app_rows if not is_edge_based_row(r) and not is_price_band_excluded(r)]
    website_rows_edge = [r for r in website_rows if is_edge_based_row(r)]

    print(f"\n{'=' * 60}\nSYSTEM VERSION SPLIT\n{'=' * 60}")
    print(f"App: {len(app_rows_evaluated)} genuinely evaluated (real edge computed), "
          f"{len(app_rows_excluded)} excluded entirely by the price band (5%-95% trust "
          f"band, every bucket fell outside it), {len(app_rows_pre_edge)} from before "
          f"step 3 existed at all.")
    total_current = len(app_rows_evaluated) + len(app_rows_excluded)
    if total_current:
        excl_pct = len(app_rows_excluded) / total_current
        print(f"\nOf {total_current} current-pipeline rows, the price band excluded "
              f"{excl_pct:.0%} of them from ever being evaluated for edge at all.")
        print("If this is high, the price band -- which correctly fixes a real thin-")
        print("liquidity problem -- may ALSO be suppressing genuine GO opportunities,")
        print("since real edge often concentrates at price extremes, not just fake")
        print("edge from stale/thin markets. Worth investigating further if this stays high.")
    print("\nThe section below is the NEW system's own track record --")
    print("this is what actually tells you whether step 3 is working.")

    breakdown(app_rows_evaluated, "app_outcome_win",
              "EDGE-BASED SYSTEM ONLY (since step 3, genuinely evaluated) -- App results")

    print_go_diagnostic(app_rows_evaluated)

    breakdown(app_rows, "app_outcome_win",
              "ALL-TIME (includes pre-edge-system rows) -- App results")
    breakdown(website_rows, "outcome_win",
              "ALL-TIME (includes pre-edge-system rows) -- Website results")

    print_app_vs_website_comparison(all_rows)
    city_bias = print_txn_bias(all_rows)
    print_distribution_shape_check(all_rows, city_bias)
    city_sigma, city_sigma_pooled = print_city_sigma(all_rows, city_bias)

    print(f"\n{'=' * 60}\nConfidence threshold suggestion (edge-based app rows only)\n{'=' * 60}")
    print("Uses ONLY edge-based rows -- 'confidence' means something different")
    print("under the old system (arbitrary composite score) vs. the new one")
    print("(a real probability), so mixing them here would be comparing")
    print("apples to oranges even though both are called 'confidence'.")
    app_suggested_threshold = suggest_threshold(app_rows_evaluated, "app_outcome_win", target_win_rate=0.70)

    write_learned_adjustments(app_rows_evaluated, city_bias, app_suggested_threshold,
                               city_sigma, city_sigma_pooled)
