"""
README (setup, deploy, usage)
- Python 3.13.4, Render.com free tier.
- pip install -r requirements.txt
- Set .env with DELTA_API_KEY, DELTA_API_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
- Start: gunicorn not needed; Render uses a single web service command like: python app.py
- Exposes a minimal webhook-compatible handler and also supports polling-less external trigger.
- Scheduler runs daily at 07:00 UTC (12:30 PM IST) to execute the short strangle.
- Manual command: send '/strangle' in Telegram to trigger immediately.

APIs used (Delta Exchange India)
- Market data:
  - GET /v2/products
  - GET /v2/tickers
  - GET /v2/tickers/{symbol}
- Orders (auth required):
  - POST /v2/orders (new order: market/limit/stop)
  - GET/PUT /v2/orders (query/update)
Signing/auth:
- signature = HMAC_SHA256(secret, method + timestamp + path + query_string + payload)
- Headers: api-key, timestamp, signature, Content-Type application/json

Strategy (same-day BTC options short strangle)
1) At 07:00 UTC:
   - Fetch BTC spot CMP via /v2/tickers/{symbol} (UNDERLYING_SYMBOL default BTCUSDT).
   - Find same-day expiry BTC options via /v2/products filter and symbol scheme C-BTC-<strike>-<ddmmyy> and P-BTC-<strike>-<ddmmyy>.
   - Pick strikes closest to +1% and -1% of CMP.
   - Sell 1 lot for each (short CE and short PE).
   - For each leg, set stop-loss at 1x the premium collected (based on current last price from /v2/tickers/{option_symbol}).
2) Report details in Telegram: strikes, expiry, premiums, stop-losses, order IDs/status.

Notes:
- Option symbols follow Delta India “Options Symbology”: {C|P}-BTC-<strike>-<ddmmyy>, with 5:30 PM IST expiry time [User Guide].
- Ensure correct product_id resolution for orders from /v2/products.
"""

import os
import time
import json
import hmac
import hashlib
import threading
import http.server
import socketserver
from urllib.parse import parse_qs
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

import requests
from dotenv import load_dotenv

from delta_client import DeltaClient
from strategy import run_short_strangle

load_dotenv()

DELTA_BASE_URL = os.getenv("DELTA_BASE_URL", "https://api.india.delta.exchange")
DELTA_API_KEY = os.getenv("DELTA_API_KEY", "")
DELTA_API_SECRET = os.getenv("DELTA_API_SECRET", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
UNDERLYING_SYMBOL = os.getenv("UNDERLYING_SYMBOL", "BTCUSDT")
SCHEDULE_CRON_UTC = os.getenv("SCHEDULE_CRON_UTC", "07:00")  # HH:MM in UTC

assert DELTA_API_KEY and DELTA_API_SECRET and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, "Missing required env vars."

def tg_send_message(text: str) -> None:
    """Send a Telegram message via raw Bot API"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        requests.post(url, json=payload, timeout=15)
    except Exception:
        pass

def parse_command(text: str) -> Optional[str]:
    if not text: return None
    t = text.strip().lower()
    if t.startswith("/strangle"):
        return "strangle"
    return None

class WebhookHandler(http.server.SimpleHTTPRequestHandler):
    # Minimal handler to be Render-compatible; configure Render to POST updates here.
    def do_POST(self):
        length = int(self.headers.get('content-length', 0))
        body = self.rfile.read(length)
        try:
            update = json.loads(body.decode("utf-8"))
        except Exception:
            self.send_response(400); self.end_headers(); self.wfile.write(b"Bad JSON"); return

        message = update.get("message") or update.get("edited_message") or {}
        text = message.get("text", "")
        command = parse_command(text)

        if command == "strangle":
            tg_send_message("Manual trigger received: executing short strangle...")  # notify
            try:
                client = DeltaClient(DELTA_BASE_URL, DELTA_API_KEY, DELTA_API_SECRET)
                report = run_short_strangle(client, UNDERLYING_SYMBOL)
                tg_send_message(report)
            except Exception as e:
                tg_send_message(f"Error during manual execution: {e}")
        else:
            # optional: ignore other messages
            pass

        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")

def serve_http():
    port = int(os.getenv("PORT", "10000"))
    with socketserver.TCPServer(("", port), WebhookHandler) as httpd:
        httpd.serve_forever()

def scheduler_loop():
    """Simple daily scheduler without extra libs to fit Render free tier"""
    target_hh, target_mm = map(int, SCHEDULE_CRON_UTC.split(":"))
    while True:
        now = datetime.now(timezone.utc)
        target = now.replace(hour=target_hh, minute=target_mm, second=0, microsecond=0)
        if target <= now:
            target = target.replace(day=now.day)  # today’s target passed, schedule tomorrow
            target = target + (datetime.min.replace(tzinfo=timezone.utc) - datetime.min.replace(tzinfo=timezone.utc))  # no-op
            target_ts = time.mktime(target.timetuple())
            # add 24h
            target_ts = time.time() + (24*3600 - (now.hour*3600 + now.minute*60 + now.second))
        else:
            target_ts = time.mktime(target.timetuple())
        sleep_s = max(1, int(target_ts - time.time()))
        time.sleep(sleep_s)

        # Fire job
        try:
            tg_send_message("Scheduled run: executing BTC short strangle for same‑day expiry.")
            client = DeltaClient(DELTA_BASE_URL, DELTA_API_KEY, DELTA_API_SECRET)
            report = run_short_strangle(client, UNDERLYING_SYMBOL)
            tg_send_message(report)
        except Exception as e:
            tg_send_message(f"Scheduled run failed: {e}")

def main():
    # Start scheduler in background
    threading.Thread(target=scheduler_loop, daemon=True).start()
    # Start HTTP server to receive Telegram updates
    serve_http()

if __name__ == "__main__":
    main()
