#!/usr/bin/env python3
"""Sends the contents of calibration_output.txt to Telegram."""
import os
import requests

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

with open("calibration_output.txt") as f:
    text = f.read()

message = "📈 <b>Calibration results</b>\n\n<pre>" + text[:3800] + "</pre>"
url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
resp = requests.post(url, data={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=15)
resp.raise_for_status()
print("Sent to Telegram.")
