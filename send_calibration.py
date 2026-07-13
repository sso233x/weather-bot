#!/usr/bin/env python3
"""Sends the contents of calibration_output.txt to Telegram."""
import html
import os
import requests

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

with open("calibration_output.txt") as f:
    text = f.read()

# The report contains literal "<" characters (e.g. "<-- only 1, not
# reliable yet"). Telegram's HTML parse_mode treats "<" as the start of
# a tag, so unescaped report text breaks the send with a 400 error.
# Escape it first, then wrap in <pre> for monospace formatting.
safe_text = html.escape(text[:3800])
message = "📈 <b>Calibration results</b>\n\n<pre>" + safe_text + "</pre>"
url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
resp = requests.post(url, data={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=15)
resp.raise_for_status()
print("Sent to Telegram.")
