"""Build sources/data/eth_usd.csv (daily UTC close) from Coinbase Exchange public candles. No key needed.

Fallback for CoinGecko's free tier, which caps history at about 365 days.
"""
import csv
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "sources" / "data" / "eth_usd.csv"
URL = "https://api.exchange.coinbase.com/products/ETH-USD/candles?granularity=86400&start={s}&end={e}"


def main(start="2020-01-01"):
    day = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = {}
    while day < end:
        stop = min(day + timedelta(days=299), end)
        req = urllib.request.Request(URL.format(s=day.isoformat(), e=stop.isoformat()),
                                     headers={"User-Agent": "leakreport/0.1"})
        with urllib.request.urlopen(req, timeout=60) as r:
            for t, _low, _high, _open, close, _vol in json.loads(r.read()):
                rows[datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat()] = close
        day = stop + timedelta(days=1)
        time.sleep(0.4)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "close"])
        for d in sorted(rows):
            w.writerow([d, rows[d]])
    print(f"wrote {len(rows)} days to {OUT}")


if __name__ == "__main__":
    main(*sys.argv[1:])
