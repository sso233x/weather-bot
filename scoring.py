"""
scoring.py — adapted from Merritt's original signal-scoring design.
Same signal philosophy (selectivity over coverage, trend > snapshot,
continuous margin score, calibratable weights) — wired to real data
instead of the demo/synthetic CitySetup.
"""

import json
import math
import os
from dataclasses import dataclass, field
from typing import Optional

from config import XND_SKIP_THRESHOLD, MIN_CONFIDENCE_TO_ACT

WEIGHTS_FILE = os.path.join(os.path.dirname(__file__), "weights.json")
LEARNED_FILE = os.path.join(os.path.dirname(__file__), "learned_adjustments.json")

DEFAULT_WEIGHTS = {
    "bucket_convergence": 1.4,
    "txn_position": 1.2,
    "xnd_penalty": 1.6,
    "nbm_trend_consistency": 1.8,
    "gridpoint_agreement": 1.3,
    "margin_to_edge": 1.5,
    "price_band": 0.8,
}


def load_weights() -> dict:
    if os.path.exists(WEIGHTS_FILE):
        with open(WEIGHTS_FILE) as f:
            return json.load(f)
    return DEFAULT_WEIGHTS


def load_learned_adjustments() -> dict:
    """Reads calibrate.py's learned_adjustments.json, written only when a
    given value has crossed a real sample-size threshold (see
    calibrate.py). Missing file or missing keys just mean 'not enough
    data yet' -- callers should fall back to static defaults, not treat
    an empty result as an error. This is intentionally the ONLY place
    learned data enters scoring -- everything else in this file stays a
    fixed, auditable rule until calibrate.py has actually earned the
    right to override it."""
    if not os.path.exists(LEARNED_FILE):
        return {}
    try:
        with open(LEARNED_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def get_txn_bias(city_code: str) -> float:
    """Returns the learned TXN bias correction for a city (subtract this
    from raw TXN before using it for bucket lookup/scoring), or 0.0 if
    not enough data yet. Positive bias = model has been running hot for
    this city historically."""
    learned = load_learned_adjustments()
    return learned.get("city_txn_bias", {}).get(city_code, 0.0)


# Static prior for sigma (EMOS spread parameter, °F) by XND value, used
# only until enough real data exists to replace it with a learned
# estimate. Rough intuition-based starting point, not fit from data --
# every one of these gets superseded the moment calibrate.py has enough
# samples for that city (see get_sigma's fallback order below).
DEFAULT_SIGMA_BY_XND = {1: 1.5, 2: 2.5, 3: 4.0, 4: 5.5}
DEFAULT_SIGMA_FALLBACK = 3.5  # for an XND value not even in the table above


def get_sigma(city_code: str, xnd) -> tuple[float, str]:
    """Returns (sigma, source) for a city/XND combination, trying the
    most precise available estimate first:
      1. (city, XND)-specific learned stratum (most precise)
      2. Pooled city-level learned sigma, all XND combined (less
         precise, but still real data for this city)
      3. Static prior table (no real data yet at all)
    'source' is one of "stratum" / "pooled" / "prior" -- callers can
    surface this for transparency, same spirit as the existing
    CALIBRATED / bias-corrected notes in main.py."""
    learned = load_learned_adjustments()
    xnd_str = str(xnd)

    stratum = learned.get("city_sigma_by_xnd", {}).get(city_code, {})
    if xnd_str in stratum:
        return float(stratum[xnd_str]), "stratum"

    pooled = learned.get("city_sigma_pooled", {})
    if city_code in pooled:
        return float(pooled[city_code]), "pooled"

    try:
        xnd_int = int(xnd)
    except (TypeError, ValueError):
        xnd_int = None
    prior = DEFAULT_SIGMA_BY_XND.get(xnd_int, DEFAULT_SIGMA_FALLBACK)
    return prior, "prior"


# ---------------------------------------------------------------------------
# EMOS core: turn (mu, sigma) into real per-bucket probabilities via the
# Gaussian CDF, instead of "which single bucket does TXN fall in."
# ---------------------------------------------------------------------------

def normal_cdf(x: float, mu: float, sigma: float) -> float:
    """Standard normal CDF via math.erf (stdlib only, no scipy/numpy
    dependency needed). Phi(z) = 0.5 * (1 + erf(z / sqrt(2)))."""
    if sigma <= 0:
        # Degenerate case (shouldn't normally happen -- get_sigma always
        # returns a positive estimate or prior) -- treat as a point mass
        # at mu rather than dividing by zero.
        return 1.0 if x >= mu else 0.0
    z = (x - mu) / (sigma * math.sqrt(2))
    return 0.5 * (1 + math.erf(z))


def bucket_probability(lo: float, hi: float, mu: float, sigma: float) -> float:
    """P(lo <= actual_high <= hi) under N(mu, sigma^2). Open-ended
    buckets already use wide sentinel bounds (-200/300, see
    data_sources.py) which work correctly here without special-casing --
    normal_cdf(-200, ...) and normal_cdf(300, ...) naturally evaluate to
    ~0 and ~1 for any realistic mu/sigma."""
    return normal_cdf(hi, mu, sigma) - normal_cdf(lo, mu, sigma)


def compute_bucket_probabilities(outcomes, mu: float, sigma: float):
    """
    outcomes: list of (label, lo, hi, price) tuples -- same shape
    find_bucket_for_temp already consumes (from parse_outcomes /
    parse_polymarket_us_outcomes).

    Returns a list of (label, lo, hi, price, model_prob) for EVERY
    bucket in the market, not just one match. This is what replaces
    find_bucket_for_temp for decision-making -- instead of picking the
    single bucket TXN happens to fall in, this gives a real probability
    for every bucket, which is what an edge calculation (step 3) needs.
    """
    return [
        (label, lo, hi, price, bucket_probability(lo, hi, mu, sigma))
        for label, lo, hi, price in outcomes
    ]


@dataclass
class CitySetup:
    city_code: str
    target_date: str
    txn_history: list = field(default_factory=list)  # chronological TXN values, this station
    latest_xnd: Optional[int] = None
    gridpoint_max_f: Optional[float] = None
    metar_f: Optional[float] = None
    market_bucket_label: Optional[str] = None
    market_bucket_low: Optional[float] = None
    market_bucket_high: Optional[float] = None
    market_price: Optional[float] = None


@dataclass
class ScoreResult:
    city_code: str
    confidence: float
    raw_score: float
    hard_skip: bool
    notes: list
    recommendation: str


# ---------------------------------------------------------------------------
# Step 3: edge-based bucket selection and recommendation. Replaces the old
# hand-tuned DEFAULT_WEIGHTS composite score entirely. "confidence" here IS
# a real probability (model_prob), not a made-up score -- and the
# recommendation comes from a real number (edge = model_prob - price), not
# a hand-weighted blend of proxies. score_setup() below is kept for
# reference but is no longer called by main.py once this is wired in.
# ---------------------------------------------------------------------------

# Static priors, same pattern as DEFAULT_SIGMA_BY_XND -- these get
# superseded by learned, backtested values once calibrate.py has enough
# edge-based trade history to validate them (future work, not yet built).
EDGE_GO_THRESHOLD = 0.05     # model thinks a bucket is >=5pp more likely than the market says
EDGE_SKIP_THRESHOLD = 0.0    # at or below zero edge -- no advantage, don't bet


def find_best_edge_bucket(bucket_probs):
    """bucket_probs: list of (label, lo, hi, price, model_prob) from
    compute_bucket_probabilities(). Returns (label, lo, hi, price,
    model_prob, edge) for whichever bucket has the HIGHEST edge --
    i.e. the most mispriced opportunity in the whole market, not
    necessarily the bucket TXN happens to fall in. Skips buckets with
    no real price data. Returns None if no valid buckets."""
    best = None
    best_edge = None
    for label, lo, hi, price, model_prob in bucket_probs:
        if price is None:
            continue
        edge = model_prob - price
        if best_edge is None or edge > best_edge:
            best_edge = edge
            best = (label, lo, hi, price, model_prob, edge)
    return best


def classify_edge(edge: float) -> str:
    if edge >= EDGE_GO_THRESHOLD:
        return "GO"
    elif edge > EDGE_SKIP_THRESHOLD:
        return "WATCH"
    else:
        return "SKIP"


def evaluate_edge(city_code: str, best, sigma: float, sigma_source: str) -> Optional[ScoreResult]:
    """Full step-3 scoring for one city/platform, given an already-found
    best-edge bucket (label, lo, hi, price, model_prob, edge) from
    find_best_edge_bucket(). Callers keep `best` themselves for display/
    logging (bucket label, price) -- this just builds the ScoreResult
    (confidence/raw_score/recommendation/notes) from it. Returns None if
    best is None (no valid buckets to evaluate)."""
    if best is None:
        return None
    label, lo, hi, price, model_prob, edge = best

    notes = [
        f"model probability {model_prob:.1%} vs market price {price:.1%} -> edge {edge:+.1%}",
        f"sigma={sigma:.2f}F (source: {sigma_source})",
    ]
    if sigma_source == "prior":
        notes.append("sigma is a static prior, not yet learned from real data for this city/XND")

    return ScoreResult(
        city_code=city_code,
        confidence=model_prob,
        raw_score=edge,
        hard_skip=False,  # sigma already encodes dispersion; no separate hard-skip rule needed
        notes=notes,
        recommendation=classify_edge(edge),
    )


def score_setup(setup: CitySetup) -> ScoreResult:
    weights = load_weights()
    learned = load_learned_adjustments()
    min_confidence = learned.get("min_confidence_to_act", MIN_CONFIDENCE_TO_ACT)
    notes = []
    raw = 0.0
    hard_skip = False

    if "min_confidence_to_act" in learned:
        notes.append(f"using CALIBRATED confidence threshold {min_confidence:.2f} (not the static default)")

    # -- TXN position: latest TXN inside/above bucket low --
    if setup.txn_history and setup.market_bucket_low is not None:
        txn = setup.txn_history[-1]
        inside = txn >= setup.market_bucket_low
        raw += (1.0 if inside else 0.0) * weights["txn_position"]
        notes.append(f"latest TXN {txn}F is {'inside/above' if inside else 'below'} bucket")
    else:
        missing = []
        if not setup.txn_history:
            missing.append("TXN")
        if setup.market_bucket_low is None:
            missing.append("market bucket")
        notes.append(f"can't check TXN position -- missing: {', '.join(missing)}")

    # -- XND --
    if setup.latest_xnd is not None:
        xnd = setup.latest_xnd
        if xnd >= XND_SKIP_THRESHOLD:
            raw -= weights["xnd_penalty"]
            hard_skip = True
            notes.append(f"XND={xnd} -> HARD SKIP (high dispersion)")
        elif xnd in (1, 2):
            raw += weights["xnd_penalty"]
            notes.append(f"XND={xnd} -> favorable low dispersion")
        else:
            notes.append(f"XND={xnd} -> neutral")
    else:
        notes.append("no XND data")

    # -- NBM run-to-run trend consistency (needs >=2 persisted runs) --
    runs = setup.txn_history[-3:]
    if len(runs) >= 2:
        spread = max(runs) - min(runs)
        if spread <= 1.0:
            score = 1.0
        elif spread <= 2.0:
            score = 0.4
        else:
            score = -0.6
        raw += score * weights["nbm_trend_consistency"]
        notes.append(f"NBM run-to-run spread {spread:.1f}F over {len(runs)} runs")
    else:
        notes.append(f"only {len(runs)} run(s) logged so far -- trend signal needs history to build up")

    # -- gridpoint (HRRR-adjacent) agreement, day-of only --
    if setup.gridpoint_max_f is not None and setup.txn_history and setup.market_bucket_low is not None:
        nbm_side = setup.txn_history[-1] >= setup.market_bucket_low
        grid_side = setup.gridpoint_max_f >= setup.market_bucket_low
        agree = nbm_side == grid_side
        raw += (1.0 if agree else -0.8) * weights["gridpoint_agreement"]
        notes.append(
            f"gridpoint forecast {setup.gridpoint_max_f}F "
            f"{'confirms' if agree else 'contradicts'} NBM bucket side"
        )
    else:
        missing = []
        if setup.gridpoint_max_f is None:
            missing.append("gridpoint fetch")
        if not setup.txn_history:
            missing.append("TXN")
        if setup.market_bucket_low is None:
            missing.append("market bucket")
        notes.append(f"can't check gridpoint agreement -- missing: {', '.join(missing)}")

    # -- margin to nearest bucket edge --
    if setup.txn_history and setup.market_bucket_low is not None and setup.market_bucket_high is not None:
        txn = setup.txn_history[-1]
        margin = min(abs(txn - setup.market_bucket_low), abs(setup.market_bucket_high - txn))
        score = max(0.0, min(1.0, margin / 3.0))
        raw += score * weights["margin_to_edge"]
        notes.append(f"{margin:.1f}F cushion from nearest bucket edge")
    else:
        missing = []
        if not setup.txn_history:
            missing.append("TXN")
        if setup.market_bucket_low is None or setup.market_bucket_high is None:
            missing.append("market bucket")
        notes.append(f"can't calc margin to edge -- missing: {', '.join(missing)}")

    # -- price band --
    if setup.market_price is not None:
        in_band = 0.35 <= setup.market_price <= 0.59
        raw += (1.0 if in_band else 0.0) * weights["price_band"]
        notes.append(f"market price {setup.market_price:.2f} {'in' if in_band else 'outside'} 35-59% band")
    else:
        notes.append("no market price data")

    confidence = 1 / (1 + math.exp(-raw))

    if hard_skip:
        rec = "SKIP"
    elif confidence >= min_confidence:
        rec = "GO"
    elif confidence >= min_confidence - 0.15:
        rec = "WATCH"
    else:
        rec = "SKIP"

    return ScoreResult(
        city_code=setup.city_code,
        confidence=round(confidence, 3),
        raw_score=round(raw, 3),
        hard_skip=hard_skip,
        notes=notes,
        recommendation=rec,
    )