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


def try_get(path: str):
    print(f"\n--- GET {BASE_URL}{path} ---")
    try:
        headers = auth_headers("GET", path)
        resp = requests.get(BASE_URL + path, headers=headers, timeout=20)
        print(f"Status: {resp.status_code}")
        print(f"Body (first 500 chars): {resp.text[:500]}")
    except Exception as e:
        print(f"Request failed: {e}")


if __name__ == "__main__":
    print(f"Testing against KEY_ID={KEY_ID[:8]}...")

    # 1. Simplest possible authenticated call -- confirms the signing
    #    scheme itself works before worrying about market-specific paths.
    try_get("/v1/whoami")
    try_get("/whoami")

    # 2. A couple of plausible shapes for listing/searching events, based
    #    on the endpoint names in the docs index ("Get Events", "Get
    #    Market By Slug"). Real path is still unconfirmed -- this is
    #    exactly what we're probing for.
    try_get("/v1/events")
    try_get("/events")
    try_get("/v1/markets")
    try_get("/markets")
