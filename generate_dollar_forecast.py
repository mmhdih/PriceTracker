#!/usr/bin/env python3
"""
gold-tracker-bot/generate_dollar_forecast.py

Generate realistic price forecast for USD/IRR using historical data from HTML scraping
and technical indicators.
"""

import json
import urllib.request
import re
import sys
import os
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from price_tracker import _fa_digits, _fetch_all_homepage_prices, _fetch_tick_price, _to_toman, _fetch_html

TZ_TEHRAN = timezone(timedelta(hours=3, minutes=30))


def _fetch_dollar_history_html(length: int = 30) -> list[dict]:
    """Fetch daily OHLC data for USD from TGJU history HTML page."""
    url = "https://www.tgju.org/profile/price_dollar_rl/history"
    html = _fetch_html(url, timeout=15)
    if not html:
        return []
    
    try:
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL)
        result = []
        for r in rows:
            cols = [re.sub(r'<[^>]+>', '', c).strip() for c in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', r, re.DOTALL)]
            if len(cols) >= 8:
                # cols: [open, low, high, close, change_amt, change_pct, date_ad, date_sh]
                try:
                    open_p = float(cols[0].replace(",", "")) / 10
                    low = float(cols[1].replace(",", "")) / 10
                    high = float(cols[2].replace(",", "")) / 10
                    close = float(cols[3].replace(",", "")) / 10
                    date_sh = cols[7]
                    result.append({"date": date_sh, "open": open_p, "high": high, "low": low, "close": close})
                except ValueError:
                    continue
        # Result is newest first, reverse to get oldest first for technicals
        return list(reversed(result[:length]))
    except Exception as e:
        print(f"[dollar_forecast] HTML parse error: {e}", file=sys.stderr)
        return []


def generate_dollar_forecast():
    from config import COLLECTOR_PATH
    from safe_json import read_json
    from analysis_engine import validate_snapshot, statistical_ranges
    snapshot = validate_snapshot(read_json(COLLECTOR_PATH))
    current = snapshot['prices'].get('dollar')
    if not current:
        return None
    ranges = statistical_ranges(current, snapshot.get('dollar_recent_7d', []))
    return {'current_dollar': current, 'predicted_ranges': ranges}

if __name__ == '__main__':
    from pipeline import main
    raise SystemExit(main())
