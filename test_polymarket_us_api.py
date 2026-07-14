#!/usr/bin/env python3
"""
test_polymarket_us_api.py -- ONE-TIME DIAGNOSTIC, not part of the bot pipeline.

Verifies that POLYMARKET_US_KEY_ID / POLYMARKET_US_SECRET_KEY actually
authenticate against the Polymarket US API, and probes a couple of
likely endpoint shapes so we can confirm the real path structure before
wiring anything into check_outcomes.py.

Signing method is taken directly from docs.polymarket.us/api-reference/
authentication: Ed25519, message = f"{timestamp_ms}{method}{path}",
headers X-PM-Access-Key / X-PM-Timestamp / X-PM-Signature.

Run this once, locally or as a manual GitHub Action step, and paste the
full output back -- that tells us the real endpoint shape instead of
guessing further.
"""

import base64
import os
import time

import requests
from cryptography.hazmat.primitives.asymmetric import ed25519

KEY_ID = os.environ["POLYMARKET_US_KEY_ID"]
SECRET_KEY = os.environ["POLYMARKET_US_SECRET_KEY"]

BASE_URL = "https://api.polymarket.us"

private_key = ed25519.Ed25519PrivateKey.from_private_bytes(
    base64.b64decode(SECRET_KEY)[:32]
)


def auth_headers(method: str, path: str) -> dict:
    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}{method}{path}"
    signature = base64.b64encode(private_key.sign(message.encode())).decode()
    return {
        "X-PM-Access-Key": KEY_ID,
        "X-PM-Timestamp": timestamp,
        "X-PM-Signature": signature,
    }


def try_get(full_path: str):
    """full_path may include a query string (e.g. '/v1/markets?limit=5').
    Per the docs, only the PATH portion is part of the signed message --
    the query string is not. Signing the whole thing would produce a
    valid-looking but wrong signature on any request with query params,
    which would show up as a 401 that looks like 'wrong endpoint' instead
    of what it actually is: a signing bug."""
    path_only = full_path.split("?", 1)[0]
    print(f"\n--- GET {BASE_URL}{full_path} ---")
    try:
        headers = auth_headers("GET", path_only)
        resp = requests.get(BASE_URL + full_path, headers=headers, timeout=20)
        print(f"Status: {resp.status_code}")
        print(f"Body (first 500 chars): {resp.text[:500]}")
    except Exception as e:
        print(f"Request failed: {e}")


if __name__ == "__main__":
    import json

    print(f"Testing against KEY_ID={KEY_ID[:8]}...")

    # Round 1 confirmed: bare GET /v1/markets works (200, real data).
    # Round 2 showed ?category=weather and ?limit=200 returned byte-
    # identical output to the unfiltered call -- those params are very
    # likely being silently ignored, not proof weather markets don't
    # exist. Round 3: stop guessing param names, actually parse the
    # response -- check pagination shape and search for weather content
    # directly instead of hoping a filter name is right.

    path = "/v1/markets"
    print(f"\n--- GET {BASE_URL}{path} (parsed) ---")
    headers = auth_headers("GET", path)
    resp = requests.get(BASE_URL + path, headers=headers, timeout=20)
    print(f"Status: {resp.status_code}")

    try:
        data = resp.json()
    except Exception as e:
        print(f"Could not parse JSON: {e}")
        print(f"Raw body (first 1000 chars): {resp.text[:1000]}")
        raise SystemExit

    # Show the TOP-LEVEL keys of the response -- tells us if there's a
    # pagination cursor, total count, etc. we should be using.
    print(f"Top-level response keys: {list(data.keys())}")

    markets = data.get("markets", [])
    print(f"Number of markets in this page: {len(markets)}")

    # Distinct categories actually present in this page
    categories = sorted({m.get("category") for m in markets if m.get("category")})
    print(f"Distinct categories seen: {categories}")

    # Search this page for anything weather/temperature-flavored
    weather_hits = [
        m for m in markets
        if "weather" in (m.get("category") or "").lower()
        or "temperature" in (m.get("question") or "").lower()
        or "temperature" in (m.get("slug") or "").lower()
        or "high" in (m.get("slug") or "").lower()
    ]
    print(f"\nWeather-looking markets found on this page: {len(weather_hits)}")
    for m in weather_hits[:10]:
        print(f"  slug={m.get('slug')!r} question={m.get('question')!r} category={m.get('category')!r}")

    if not weather_hits:
        print("\nNo weather markets on this page. Showing first 3 markets' full")
        print("keys (not just category) so we can see the actual schema shape:")
        for m in markets[:3]:
            print(f"  {json.dumps(m, indent=2)[:600]}")

