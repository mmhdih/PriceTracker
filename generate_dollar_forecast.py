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
        return list(reversed(result))[:length]
    except Exception as e:
        print(f"[dollar_forecast] HTML parse error: {e}", file=sys.stderr)
        return []


def generate_dollar_forecast():
    """Generate forecast stats based on current USD price + historical dynamics."""
    homepage = _fetch_all_homepage_prices()
    current_rial = homepage.get("price_dollar_rl")
    if current_rial is None:
        current_rial = _fetch_tick_price("price_dollar_rl")
    
    if current_rial is None:
        return None

    current = _to_toman(current_rial)

    history = _fetch_dollar_history_html(length=30)
    if len(history) < 3:
        return None

    last7 = history[-7:] if len(history) >= 7 else history[-3:]

    daily_ranges_pct = [(r["high"] - r["low"]) / r["close"] * 100 for r in last7 if r["close"] > 0]
    avg_daily_range_pct = sum(daily_ranges_pct) / len(daily_ranges_pct) if daily_ranges_pct else 0.5

    closes = [r["close"] for r in last7]
    returns = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            returns.append((closes[i] - closes[i - 1]) / closes[i - 1] * 100)

    avg_return_pct = sum(returns) / len(returns) if returns else 0

    if len(closes) >= 2 and closes[0] > 0:
        week_change_pct = (closes[-1] - closes[0]) / closes[0] * 100
    else:
        week_change_pct = 0

    typical_4h_pct = min(avg_daily_range_pct * 0.15, 0.5)
    trend_4h = avg_return_pct * 0.10
    center_4h = current * (1 + trend_4h / 100)
    spread_4h = current * typical_4h_pct / 100
    f4_min = int(center_4h - spread_4h)
    f4_max = int(center_4h + spread_4h)

    typical_24h_pct = min(avg_daily_range_pct * 0.35, 1.0)
    trend_24h = avg_return_pct * 0.50
    center_24h = current * (1 + trend_24h / 100)
    spread_24h = current * typical_24h_pct / 100
    f24_min = int(center_24h - spread_24h)
    f24_max = int(center_24h + spread_24h)

    typical_7d_pct = min(0.8 + avg_daily_range_pct * 0.7, 2.5)
    if week_change_pct > 2.5:
        trend_7d = -0.3
    elif week_change_pct < -2.5:
        trend_7d = 0.5
    else:
        trend_7d = avg_return_pct * 1.5

    center_7d = current * (1 + trend_7d / 100)
    spread_7d = current * typical_7d_pct / 100
    f7d_min = int(center_7d - spread_7d)
    f7d_max = int(center_7d + spread_7d)

    dir_4h = "صعودی" if avg_return_pct > 0.2 else "نزولی" if avg_return_pct < -0.2 else "نوسانی"
    dir_24h = "صعودی" if avg_return_pct > 0.2 else "نزولی" if avg_return_pct < -0.2 else "نوسانی"
    dir_7d = "صعودی" if trend_7d > 0.3 else "نزولی" if trend_7d < -0.3 else "نوسانی"

    return {
        "current_dollar": current,
        "week_change_pct": round(week_change_pct, 2),
        "avg_daily_range_pct": round(avg_daily_range_pct, 2),
        "avg_daily_return_pct": round(avg_return_pct, 2),
        "forecast_4h": {"min": f4_min, "max": f4_max, "direction": dir_4h},
        "forecast_24h": {"min": f24_min, "max": f24_max, "direction": dir_24h},
        "forecast_7d": {"min": f7d_min, "max": f7d_max, "direction": dir_7d},
    }


def main():
    fc = generate_dollar_forecast()
    if not fc:
        print("Failed to generate dollar forecast.", file=sys.stderr)
        return

    print("=== DOLLAR STATISTICAL FORECAST (HTML Parsed) ===")
    print(f"Current USD Price: {fc['current_dollar']:,} Toman")
    print(f"7-Day Change: {fc['week_change_pct']:+.2f}%")
    print(f"Avg Daily Range: {fc['avg_daily_range_pct']:.2f}%")
    print()
    print(f"⏰ 4h Forecast: {fc['forecast_4h']['direction']} | Range: {fc['forecast_4h']['min']:,} - {fc['forecast_4h']['max']:,} Toman")
    print(f"⏰ 24h Forecast: {fc['forecast_24h']['direction']} | Range: {fc['forecast_24h']['min']:,} - {fc['forecast_24h']['max']:,} Toman")
    print(f"📅 1w Forecast: {fc['forecast_7d']['direction']} | Range: {fc['forecast_7d']['min']:,} - {fc['forecast_7d']['max']:,} Toman")


if __name__ == "__main__":
    main()
