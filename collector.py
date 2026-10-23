#!/usr/bin/env python3
"""
gold-tracker-bot/collector.py

What it does:
  - Collects ALL market data from every available source in one pass
  - Returns a single structured JSON with everything the LLM analyst needs

This is NOT a prediction engine. It's a data collector.
The LLM agent reads this output and does the actual analysis.

Usage:
  python3 collector.py                 # full collection
  python3 collector.py --no-news       # skip news (faster)
  python3 collector.py --technical      # include technical indicators from history
"""

import json
import os
import re
import sys
import urllib.request
import html as html_mod
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sys

import jdatetime
from price_tracker import (
    _fetch_tala_price, TALA_IR_MAZNEH, TALA_IR_18K,
    _fetch_all_homepage_prices, _fetch_tick_price, _to_toman,
    _fetch_usdt_price, _fetch_gold_silver_prices,
    _fetch_html, _fa_digits, _load_daily_db, _get_morning_price,
    TGJU_LIVE_API
)
from news_scraper import get_all_news

TZ_TEHRAN = timezone(timedelta(hours=3, minutes=30))

# Extra news channels beyond what news_scraper already has
EXTRA_NEWS_CHANNELS = [
    "TahlilBazar",
    "BoursePress",
]


def _fetch_tgju_history(grade: str, length: int = 30) -> list[dict]:
    """Fetch daily OHLC history from TGJU API. Returns list of {date, open, high, low, close} in Toman."""
    url = f"https://api.tgju.org/v1/market/indicator/summary-table-data/{grade}?lang=fa&order_dir=desc&start=0&length={length}&convert_to_ad=1"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"https://www.tgju.org/profile/{grade}/history"
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
        rows = []
        for r in data["data"][:length]:
            # columns: [open, low, high, close, change_amt, change_pct, date_ad, date_sh]
            open_p = float(r[0].replace(",", "")) / 10
            low = float(r[1].replace(",", "")) / 10
            high = float(r[2].replace(",", "")) / 10
            close = float(r[3].replace(",", "")) / 10
            date_sh = r[7]
            rows.append({"date": date_sh, "open": int(open_p), "high": int(high),
                          "low": int(low), "close": int(close)})
        return rows
    except Exception as e:
        print(f"[collector] TGJU history error for {grade}: {e}", file=sys.stderr)
        return []


def _compute_technicals(history: list[dict]) -> dict:
    """Compute basic technical indicators from daily OHLC data."""
    if len(history) < 5:
        return {}

    closes = [h["close"] for h in reversed(history)]  # oldest to newest

    result = {}

    # Moving Averages
    for period in [5, 10, 20]:
        if len(closes) >= period:
            ma = sum(closes[-period:]) / period
            result[f"ma{period}"] = int(ma)
            result[f"ma{period}_dist_pct"] = round((closes[-1] - ma) / ma * 100, 2)

    # RSI (14 period, simplified)
    if len(closes) >= 15:
        gains, losses = [], []
        recent = closes[-15:]
        for i in range(1, len(recent)):
            chg = recent[i] - recent[i-1]
            if chg > 0:
                gains.append(chg)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(chg))
        avg_gain = sum(gains) / 14
        avg_loss = sum(losses) / 14
        if avg_loss == 0:
            rsi = 100 if avg_gain > 0 else 50
        else:
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))
        result["rsi14"] = round(rsi, 1)

    # Bollinger Bands (20 period, 2 std)
    if len(closes) >= 20:
        ma20 = sum(closes[-20:]) / 20
        variance = sum((c - ma20) ** 2 for c in closes[-20:]) / 20
        std = variance ** 0.5
        result["bb_upper"] = int(ma20 + 2 * std)
        result["bb_lower"] = int(ma20 - 2 * std)
        result["bb_mid"] = int(ma20)

    # Recent range
    last7 = history[:7] if len(history) >= 7 else history
    week_high = max(h["high"] for h in last7)
    week_low = min(h["low"] for h in last7)
    result["week_high"] = week_high
    result["week_low"] = week_low
    result["dist_from_week_high_pct"] = round((closes[-1] - week_high) / week_high * 100, 2)
    result["dist_from_week_low_pct"] = round((closes[-1] - week_low) / week_low * 100, 2)

    # Momentum: rate of change
    for period_name, period in [("roc_1d", 1), ("roc_3d", 3), ("roc_7d", 7)]:
        if len(closes) > period:
            roc = (closes[-1] - closes[-1 - period]) / closes[-1 - period] * 100
            result[period_name] = round(roc, 2)

    return result


def _get_calendar_info() -> dict:
    """Get Shamsi calendar info: weekday, upcoming holidays, special dates."""
    now = jdatetime.datetime.now(TZ_TEHRAN)
    today_str = now.strftime("%Y-%m-%d")

    # Persian weekday names
    weekday_names = ["شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه"]
    weekday_name = weekday_names[now.weekday()]

    # Check if today/ Tomorrow is Thursday (market close day)
    is_thursday = now.weekday() == 5  # Thursday in jdatetime
    is_friday = now.weekday() == 6   # Friday (holiday)

    # Time of day
    hour = now.hour
    if hour < 12:
        market_session = "صبح"
    elif hour < 15:
        market_session = "ظهر"
    elif hour < 18:
        market_session = "بعدازظهر"
    elif hour < 21:
        market_session = "عصر"
    else:
        market_session = "شب"

    # Upcoming special dates (simplified)
    month = now.month
    day = now.day
    special = []
    if month == 12 and day >= 20:
        special.append("نزدیک نوروز - تقاضای فصلی طلا")
    if month == 1 and day <= 13:
        special.append("نوروز - تعطیلی بازار")
    if month == 5:
        special.append("تابستان - کاهش تقاضای فصلی")

    # End of month (possible margin call pressure)
    if day >= 28:
        special.append("اواخر ماه - فشار تسویه")

    # Ramadan effect
    # (would need Hijri calendar for precise detection - skipped for now)

    return {
        "shamsi_date": now.strftime("%Y/%m/%d"),
        "weekday": weekday_name,
        "hour": hour,
        "market_session": market_session,
        "is_thursday": is_thursday,
        "is_friday": is_friday,
        "special_notes": special,
    }


def _fetch_extra_news() -> dict[str, list[str]]:
    """Fetch from additional Telegram channels."""
    results = {}
    for ch in EXTRA_NEWS_CHANNELS:
        url = f"https://telegram.me/s/{ch}"
        html = _fetch_html(url, timeout=12)
        if not html:
            continue
        posts = []
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            elements = soup.find_all(class_="tgme_widget_message_text")
            for el in elements[-4:]:
                text = el.get_text("\n").strip()
                if text:
                    posts.append(text)
        except ImportError:
            text = re.sub(r"<br\s*/?>", "\n", html)
            text = re.sub(r"<[^>]+>", "", text)
            text = html_mod.unescape(text)
            matches = re.findall(
                r'class="[^"]*tgme_widget_message_text[^"]*"[^>]*>([\s\S]*?)</div>',
                html,
            )
            for m in matches[-4:]:
                cleaned = re.sub(r"<[^>]+>", "", m).strip()
                if cleaned:
                    posts.append(cleaned)
        if posts:
            results[ch] = posts
    return results


def collect(no_news: bool = False, technical: bool = False) -> dict:
    """Collect all market data into a single structured dict."""
    now = datetime.now(TZ_TEHRAN)
    result = {
        "collection_time": now.isoformat(),
        "shamsi_time": jdatetime.datetime.now(TZ_TEHRAN).strftime("%Y/%m/%d %H:%M"),
    }

    # === 1. CURRENT PRICES ===
    prices = {}

    # Mazneh + Geram18 from tala.ir
    mazneh = _fetch_tala_price(TALA_IR_MAZNEH)
    geram18 = _fetch_tala_price(TALA_IR_18K)
    if mazneh:
        prices["mazneh"] = mazneh
    if geram18:
        prices["geram18"] = geram18

    # Dollar: try homepage, then tick API
    homepage = _fetch_all_homepage_prices()
    if homepage.get("price_dollar_rl"):
        prices["dollar"] = _to_toman(homepage["price_dollar_rl"])
    else:
        dollar_tick = _fetch_tick_price("price_dollar_rl")
        if dollar_tick:
            prices["dollar"] = _to_toman(dollar_tick)

    # USDT
    usdt = _fetch_usdt_price()
    if usdt:
        prices["usdt"] = usdt

    # Gold/Silver ounce
    gold_oz, silver_oz = _fetch_gold_silver_prices()
    if gold_oz:
        prices["gold_ounce"] = gold_oz
    if silver_oz:
        prices["silver_ounce"] = silver_oz

    # Coin (sekee) from TGJU
    sekee = _fetch_tick_price("sekee")
    if sekee:
        prices["sekee"] = _to_toman(sekee)

    # Mesghal from TGJU (for cross-check with tala.ir mazneh)
    mesghal_tgju = _fetch_tick_price("mesghal")
    if mesghal_tgju:
        prices["mesghal_tgju"] = _to_toman(mesghal_tgju)

    result["prices"] = prices

    # === 2. VOLATILITY (today's change from morning) ===
    daily_db = _load_daily_db()
    volatility = {}
    for key in ["mazneh", "geram18", "usdt", "gold_ounce", "silver_ounce", "dollar"]:
        morning = _get_morning_price(daily_db, key)
        current = prices.get(key)
        if morning and current:
            chg = current - morning
            pct = round(chg / morning * 100, 2)
            volatility[key] = {
                "morning": morning,
                "current": current,
                "change": chg,
                "change_pct": pct,
            }
    result["volatility"] = volatility

    # === 3. GOLD-SILVER RATIO ===
    if gold_oz and silver_oz and silver_oz > 0:
        result["gold_silver_ratio"] = round(gold_oz / silver_oz, 1)

    # === 4. DOLLAR-GOLD CORRELATION ===
    # How much of mesghal price is explained by dollar vs gold ounce
    if prices.get("dollar") and prices.get("gold_ounce") and prices.get("mazneh"):
        # Implied: if dollar * gold_ounce * conversion_factor ≈ mazneh
        # Converter ≈ mazneh / (dollar * gold_ounce)
        conv = prices["mazneh"] / (prices["dollar"] * prices["gold_ounce"])
        result["implied_converter"] = round(conv, 4)
        # If converter is high, there's a premium (hobab)
        # Historical converter is around 0.12-0.15 (varies)
        # We'll let the LLM interpret this

    # === 5. TECHNICAL INDICATORS ===
    if technical:
        # Get geram18 history for technicals
        geram18_history = _fetch_tgju_history("geram18", length=30)
        if geram18_history:
            result["geram18_technicals"] = _compute_technicals(geram18_history)
            result["geram18_history"] = geram18_history

        # Mesghal history
        mesghal_history = _fetch_tgju_history("mesghal", length=30)
        if mesghal_history:
            result["mazneh_technicals"] = _compute_technicals(mesghal_history)
            result["mazneh_history"] = mesghal_history
            result["mazneh_recent_7d"] = mesghal_history[:7]
    else:
        # Always include at least 7-day history for context
        mesghal_history = _fetch_tgju_history("mesghal", length=7)
        if mesghal_history:
            result["mazneh_recent_7d"] = mesghal_history
        geram18_history = _fetch_tgju_history("geram18", length=7)
        if geram18_history:
            result["geram18_recent_7d"] = geram18_history

    # Dollar 7-day history and technicals (using robust HTML scraper)
    try:
        from generate_dollar_forecast import _fetch_dollar_history_html
        dollar_history_30 = list(reversed(_fetch_dollar_history_html(length=30)))  # collector histories newest first
        if dollar_history_30:
            result["dollar_recent_7d"] = dollar_history_30[:7]
            if technical:
                result["dollar_technicals"] = _compute_technicals(dollar_history_30)
    except Exception as e:
        print(f"[collector] dollar history error: {e}", file=sys.stderr)

    # === 6. NEWS ===
    if not no_news:
        news = get_all_news()
        # Add extra channels
        extra = _fetch_extra_news()
        news.update(extra)
        result["news"] = news
        # Count total headlines
        total = sum(len(v) for v in news.values())
        result["news_total"] = total

    # === 7. CALENDAR ===
    result["calendar"] = _get_calendar_info()

    # === 8. HOBAB (Bubble) CALCULATION ===
    # Real bubble = market price vs intrinsic value derived from global ounce + free dollar.
    # Fetching both mazneh and geram18 from tala.ir gives a FIXED-RATIO derivative (always -6%).
    # Instead: intrinsic geram18 = (ounce * dollar * 0.750) / 31.1034768
    # hobab = market geram18 - intrinsic geram18  (positive = bubble, negative = discount)
    gold_oz = prices.get("gold_ounce")
    dollar = prices.get("dollar")
    geram18_price = prices.get("geram18")
    sekee_price = prices.get("sekee")

    if gold_oz and dollar and geram18_price:
        intrinsic_geram18 = int((gold_oz * dollar * 0.750) / 31.1034768)
        hobab_geram18 = geram18_price - intrinsic_geram18
        hobab_geram18_pct = round(hobab_geram18 / intrinsic_geram18 * 100, 2) if intrinsic_geram18 > 0 else 0

        sekee_hobab = {}
        if sekee_price:
            # 1 Sekee = 8.135g of 22k (900 purity) → 7.3215g pure gold
            intrinsic_sekee = int((gold_oz * dollar * 7.3215) / 31.1034768)
            sekee_hobab_amt = sekee_price - intrinsic_sekee
            sekee_hobab = {
                "intrinsic_value": intrinsic_sekee,
                "hobab_amount": sekee_hobab_amt,
                "hobab_pct": round(sekee_hobab_amt / intrinsic_sekee * 100, 2) if intrinsic_sekee > 0 else 0,
            }

        result["hobab"] = {
            "intrinsic_geram18": intrinsic_geram18,
            "geram18_hobab": hobab_geram18,
            "geram18_hobab_pct": hobab_geram18_pct,
            "sekee": sekee_hobab,
        }

    return result


def main():
    no_news = "--no-news" in sys.argv
    technical = "--technical" in sys.argv or not no_news  # default: include technicals

    print(f"[collector] Starting collection at {datetime.now(TZ_TEHRAN).isoformat()}", file=sys.stderr)

    data = collect(no_news=no_news, technical=technical)

    json_output = json.dumps(data, ensure_ascii=False, indent=2)

    from config import COLLECTOR_PATH
    from safe_json import atomic_write
    from tracker_store import get_store
    from analysis_engine import validate_snapshot
    data = validate_snapshot(data)
    get_store().observe(data['collection_time'], data['prices'])
    atomic_write(COLLECTOR_PATH, json.dumps(data, ensure_ascii=False, indent=2))
    print(json_output)
    print(f"\n[collector] Done. Sources: {len(data)} sections", file=sys.stderr)


if __name__ == "__main__":
    main()
