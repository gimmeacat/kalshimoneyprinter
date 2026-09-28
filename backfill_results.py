import csv
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from kalshi_client import KalshiClient

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

SNAP_FILE = DATA_DIR / "snapshots_2s.csv"
RESULT_FILE = DATA_DIR / "results_2s.csv"
FIELDS = ["ticker", "close_time", "floor_strike", "result", "expiration_value"]


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def main():
    client = KalshiClient()

    done = set()
    if os.path.exists(RESULT_FILE):
        with open(RESULT_FILE, encoding="utf-8") as f:
            done = {r["ticker"] for r in csv.DictReader(f)}

    closes = {}
    with open(SNAP_FILE, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            closes[r["ticker"]] = r["close_time"]

    now = datetime.now(timezone.utc)
    todo = [
        (t, c) for t, c in closes.items()
        if t not in done and (now - parse_time(c)).total_seconds() > 90
    ]
    print("채워야 할 시장:", len(todo))

    is_new = not os.path.exists(RESULT_FILE)
    with open(RESULT_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if is_new:
            writer.writeheader()
        for ticker, close_time in todo:
            resp = client.request("GET", f"/markets/{ticker}")
            m = resp.get("market", resp)
            if m.get("result"):
                writer.writerow({
                    "ticker": ticker,
                    "close_time": close_time,
                    "floor_strike": m.get("floor_strike"),
                    "result": m.get("result"),
                    "expiration_value": m.get("expiration_value"),
                })
                print("기록:", ticker, m.get("result"))
            time.sleep(0.3)


if __name__ == "__main__":
    main()