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

    # Round 3 showed: bare /v1/markets always returns the same 20 NFL
    # games regardless of ?limit or ?category=weather -- both silently
    # ignored, no pagination metadata in the response. Round 4: user says
    # the app itself labels this category "Temp", not "Weather" -- try
    # that exact value, plus common pagination param name guesses to get
    # past this fixed first page.

    def check_markets(path):
        print(f"\n--- GET {BASE_URL}{path} ---")
        path_only = path.split("?", 1)[0]
        headers = auth_headers("GET", path_only)
        resp = requests.get(BASE_URL + path, headers=headers, timeout=20)
        print(f"Status: {resp.status_code}")
        try:
            data = resp.json()
        except Exception:
            print(f"Not JSON: {resp.text[:300]}")
            return
        markets = data.get("markets", [])
        cats = sorted({m.get("category") for m in markets if m.get("category")})
        ids = [m.get("id") for m in markets[:5]]
        print(f"  n={len(markets)}  categories={cats}  first_ids={ids}")

    # Exact label from the app
    check_markets("/v1/markets?category=Temp")
    check_markets("/v1/markets?category=temp")
    check_markets("/v1/markets?tag=Temp")

    # Pagination param name guesses -- if any of these actually change
    # first_ids/categories from the baseline (sports, ids 1-5), that's
    # the real param name.
    check_markets("/v1/markets?offset=20")
    check_markets("/v1/markets?page=2")
    check_markets("/v1/markets?cursor=20")
    check_markets("/v1/markets?per_page=200")
    check_markets("/v1/markets?pageSize=200")

    # Direct slug guess based on how the app might name a temp contract
    try_get("/v1/markets/slug/temp-nyc-2026-07-14")

