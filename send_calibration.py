#!/usr/bin/env python3
"""Sends the contents of calibration_output.txt to Telegram, splitting
into multiple messages if it's too long for one -- the report has grown
as more sections were added, and the old fixed text[:3800] truncation
was silently dropping everything past that point (confirmed 2026-07-31:
an entire report section, plus the learned-adjustments write
confirmation, were being cut off without any indication)."""
import html
import os
import requests

# Telegram's hard limit is 4096 chars per message. Leave headroom for
# the <pre>/<b> wrapper tags and a "(part N/M)" header on each chunk.
MAX_CHUNK_CHARS = 3500


def chunk_text(text: str, max_chars: int):
    """Splits text into chunks up to max_chars each, breaking only at
    line boundaries so no line is ever cut mid-word. A single line
    longer than max_chars becomes its own (oversized) chunk rather than
    being lost or splitting mid-word."""
    lines = text.splitlines(keepends=True)
    chunks = []
    current = ""
    for line in lines:
        if len(current) + len(line) > max_chars and current:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return chunks


def send_message(token: str, chat_id: str, text: str) -> None:
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        timeout=15,
    )
    resp.raise_for_status()


def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    with open("calibration_output.txt") as f:
        full_text = f.read()

    chunks = chunk_text(full_text, MAX_CHUNK_CHARS)

    for i, chunk in enumerate(chunks, start=1):
        safe_chunk = html.escape(chunk)
        if len(chunks) == 1:
            header = "📈 <b>Calibration results</b>\n\n"
        else:
            header = f"📈 <b>Calibration results (part {i}/{len(chunks)})</b>\n\n"
        message = header + "<pre>" + safe_chunk + "</pre>"
        send_message(token, chat_id, message)

    print(f"Sent to Telegram in {len(chunks)} message(s).")


if __name__ == "__main__":
    main()
