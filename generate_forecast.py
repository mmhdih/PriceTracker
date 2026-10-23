#!/usr/bin/env python3
"""
gold-tracker-bot/generate_forecast.py

Generate realistic price forecast for mesghal using historical data.
Based on ACTUAL observed market behavior:
- 4h changes: typically 0.1-0.8%, max 1.5%
- 24h changes: typically 0.3-1.5%, max 2.5%
- 1 week: typically 1.0-3.0%, max 5.0%
"""

import json
import urllib.request
import re
import sys
import os
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from price_tracker import _fa_digits, _fetch_all_prices, _to_toman

TZ_TEHRAN = timezone(timedelta(hours=3, minutes=30))


def _fetch_history():
    """Fetch last 30 days of mesghal daily data from TGJU API."""
    url = "https://api.tgju.org/v1/market/indicator/summary-table-data/mesghal?lang=fa&order_dir=asc&start=0&length=5000"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.tgju.org/profile/mesghal/history"
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        rows = []
        for r in data["data"][-30:]:
            close = float(r[3].replace(",", "")) / 10
            low = float(r[1].replace(",", "")) / 10
            high = float(r[2].replace(",", "")) / 10
            rows.append({"date": r[7], "close": close, "low": low, "high": high})
        return rows
    except Exception as e:
        print(f"[forecast] history fetch error: {e}", file=sys.stderr)
        return []


def generate_forecast():
    """Generate forecast stats based on current mesghal + 7-day history."""
    all_prices = _fetch_all_prices()
    mazneh_rial = all_prices.get("mazneh")
    if mazneh_rial is None:
        try:
            from price_tracker import _fetch_tick_price
            tick = _fetch_tick_price("mesghal")
            if tick:
                mazneh_rial = tick
        except:
            pass
    if mazneh_rial is None:
        return None

    current = _to_toman(mazneh_rial)

    # Get 7-day history
    history = _fetch_history()
    if len(history) < 3:
        return None

    last7 = history[-7:] if len(history) >= 7 else history[-3:]

    # Daily ranges (high - low) for volatility
    daily_ranges_pct = [(r["high"] - r["low"]) / r["close"] * 100 for r in last7]
    avg_daily_range_pct = sum(daily_ranges_pct) / len(daily_ranges_pct)

    # Daily returns for trend
    closes = [r["close"] for r in last7]
    returns = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            returns.append((closes[i] - closes[i - 1]) / closes[i - 1] * 100)

    avg_return_pct = sum(returns) / len(returns) if returns else 0

    # 7-day total change
    if len(closes) >= 2:
        week_change_pct = (closes[-1] - closes[0]) / closes[0] * 100
    else:
        week_change_pct = 0

    # === REALISTIC FORECAST RANGES (TIGHTENED) ===
    # Based on user feedback and actual analysis:
    # 4h: 0.1% - 0.6%
    # 24h: 0.3% - 1.2%
    # 1 week: 1.0% - 2.5%

    # 4h: tight range, capped at 0.6%
    typical_4h_pct = min(avg_daily_range_pct * 0.15, 0.6)
    trend_4h = avg_return_pct * 0.10
    center_4h = current * (1 + trend_4h / 100)
    spread_4h = current * typical_4h_pct / 100
    f4_min = int(center_4h - spread_4h)
    f4_max = int(center_4h + spread_4h)

    # 24h: capped at 1.2%
    typical_24h_pct = min(avg_daily_range_pct * 0.35, 1.2)
    trend_24h = avg_return_pct * 0.50
    center_24h = current * (1 + trend_24h / 100)
    spread_24h = current * typical_24h_pct / 100
    f24_min = int(center_24h - spread_24h)
    f24_max = int(center_24h + spread_24h)

    # 1 week: capped at 2.5% for normal market
    # If week_change_pct is very high, it means market already jumped, expect correction (reversion to mean)
    typical_7d_pct = min(1.0 + avg_daily_range_pct * 0.8, 2.5)
    
    # Mean reversion logic: if last 7 days was extremely up, expect slower growth or correction
    if week_change_pct > 3.0:
        trend_7d = -0.5  # slight correction bias
    elif week_change_pct < -3.0:
        trend_7d = 0.5   # rebound bias
    else:
        trend_7d = week_change_pct * 0.30  # moderate continuation

    center_7d = current * (1 + trend_7d / 100)
    spread_7d = current * typical_7d_pct / 100
    f7_min = int(center_7d - spread_7d)
    f7_max = int(center_7d + spread_7d)

    # Direction based on trend + mean reversion
    # Be more conservative with 'soodi' to avoid losses
    # Direction based on trend + mean reversion
    # NOTE: These thresholds produce a STATISTICAL suggestion only.
    # The LLM agent must analyze news + market context to make the FINAL call.
    # Do NOT default to nowsani just because stats are borderline — if news
    # strongly indicates a direction, say soodi or nozoli with confidence.
    if avg_return_pct > 0.5 and week_change_pct < 3.0:
        direction = "صعودی"
    elif avg_return_pct < -0.5 and week_change_pct > -3.0:
        direction = "نزولی"
    else:
        direction = "نوسانی"

    return {
        "current": current,
        "direction": direction,
        "f4": {"min": f4_min, "max": f4_max},
        "f24": {"min": f24_min, "max": f24_max},
        "f7": {"min": f7_min, "max": f7_max},
        "stats": {
            "avg_daily_range_pct": round(avg_daily_range_pct, 2),
            "avg_return_pct": round(avg_return_pct, 2),
            "week_change_pct": round(week_change_pct, 2),
            "typical_4h_pct": round(typical_4h_pct, 2),
            "typical_24h_pct": round(typical_24h_pct, 2),
            "typical_7d_pct": round(typical_7d_pct, 2),
        }
    }


if __name__ == "__main__":
    result = generate_forecast()
    if result:
        c = result["current"]
        f4 = result["f4"]
        f24 = result["f24"]
        f7 = result["f7"]
        s = result["stats"]
        d = result["direction"]

        print(f"قیمت فعلی مظنه: {_fa_digits(f'{c:,}')} تومان")
        print(f"جهت روند: {d}")
        print()
        print(f"پیش‌بینی ۴ ساعته:")
        min_4h_str = f"{f4['min']:,}"
        max_4h_str = f"{f4['max']:,}"
        pct_4h_str = f"{s['typical_4h_pct']:.2f}"
        print(f"  بازه: {_fa_digits(min_4h_str)} — {_fa_digits(max_4h_str)} تومان")
        print(f"  حداکثر تغییر: {_fa_digits(pct_4h_str)}%")
        print()
        print(f"پیش‌بینی ۲۴ ساعته:")
        min_24h_str = f"{f24['min']:,}"
        max_24h_str = f"{f24['max']:,}"
        pct_24h_str = f"{s['typical_24h_pct']:.2f}"
        print(f"  بازه: {_fa_digits(min_24h_str)} — {_fa_digits(max_24h_str)} تومان")
        print(f"  حداکثر تغییر: {_fa_digits(pct_24h_str)}%")
        print()
        print(f"پیش‌بینی یک هفته‌ای:")
        min_7d_str = f"{f7['min']:,}"
        max_7d_str = f"{f7['max']:,}"
        pct_7d_str = f"{s['typical_7d_pct']:.2f}"
        print(f"  بازه: {_fa_digits(min_7d_str)} — {_fa_digits(max_7d_str)} تومان")
        print(f"  حداکثر تغییر: {_fa_digits(pct_7d_str)}%")
        print()
        
        avg_range_str = f"{s['avg_daily_range_pct']:.2f}"
        avg_return_str = f"{s['avg_return_pct']:+.2f}"
        week_change_str = f"{s['week_change_pct']:+.2f}"
        
        print(f"آمار بازار (۷ روز اخیر):")
        print(f"  نوسان روزانه میانگین: {_fa_digits(avg_range_str)}%")
        print(f"  بازده روزانه میانگین: {_fa_digits(avg_return_str)}%")
        print(f"  تغییر ۷ روزه: {_fa_digits(week_change_str)}%")
    else:
        print("Failed to generate forecast", file=sys.stderr)
        sys.exit(1)
