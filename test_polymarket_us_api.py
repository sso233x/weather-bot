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

    GATEWAY_URL = "https://gateway.polymarket.us"

    # CONFIRMED: search?query=temp finds live "climate" category events
    # with slug pattern temp-{station}high-{YYYY-MM-DD}. Station codes
    # confirm the original hypothesis: mdw (not ord) for Chicago, nyc
    # (not lga) for NYC. Now fetching one full event to see the actual
    # bucket/price schema.

    for slug in [
        "temp-miahigh-2026-07-14",
        "temp-laxhigh-2026-07-14",
    ]:
        print(f"\n=== Event by slug: {slug} ===")
        for path in [f"/v1/events/slug/{slug}", f"/v1/events?slug={slug}"]:
            print(f"\n--- GET {GATEWAY_URL}{path} ---")
            try:
                resp = requests.get(GATEWAY_URL + path, timeout=20)
                print(f"Status: {resp.status_code}")
                print(f"Body: {resp.text[:2000]}")
            except Exception as e:
                print(f"Request failed: {e}")

