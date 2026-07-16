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