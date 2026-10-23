#!/usr/bin/env python3
"""
gold-tracker-bot/save_final_forecast.py

این اسکریپت متن نهایی تحلیل (که شامل پیش‌بینی هوشمند تولید شده توسط LLM است) را دریافت کرده،
جهت پیش‌بینی (صعودی/نزولی/نوسانی) را استخراج می‌کند و آن را با جزئیات کامل در دیتابیس ذخیره می‌کند.
"""

import sys
import os
import json
import re

# اضافه کردن دایرکتوری به مسیر سیستم
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from json_db import load_db, add_forecast, save_db
from price_tracker import _fetch_html, _parse_row, _to_toman, TGJU_URL

TRACKED_KEYS = [
    ("mesghal", "مثقال طلا", True),
    ("geram18", "طلا ۱۸ عیار", True),
    ("price_dollar_rl", "دلار", True),
    ("crypto-tether-irr", "تتر", True),
]

def get_current_prices() -> dict:
    html = _fetch_html(TGJU_URL)
    if not html:
        return {}
    prices = {}
    for key, label, is_rial in TRACKED_KEYS:
        price, pct = _parse_row(html, key)
        if price is not None:
            toman = _to_toman(price) if is_rial else price
            prices[key] = toman
    return prices

def extract_direction(text: str) -> str:
    """جهت کلی پیش‌بینی را از متن تحلیل استخراج می‌کند."""
    # ponytail: Previously this scanned the entire forecast section (incl. news
    # impact lines) for up/down keywords; that produced "صعودی / محتمل / ..."
    # mismatches even when the explicit forecast direction was "نوسانی".
    # Best practice: explicit direction field wins. Only fall back to keyword
    # counting when no explicit field is present.

    # 1) Explicit direction line — format: "جهت پیش‌بینی: **نوسانی**"
    explicit = re.search(r"جهت پیش‌بینی[:\s]*\*?\*?(نوسانی|صعودی|نزولی)", text)
    if explicit:
        return explicit.group(1)

    # 2) Forecast-table cell — column header "جهت", value one of three words.
    # The line is like "| ۴ ساعت | ... | نوسانی |"
    forecast_section = ""
    parts = text.split("## 🔮 پیش‌بینی")
    if len(parts) > 1:
        forecast_section = parts[1].split("##\n")[0]
    else:
        forecast_section = text

    # 2b) Inline direction next to a timeframe bullet, e.g.
    #     "- **پیش‌بینی ۴ ساعته**: نوسانی ..."
    # Count which of the three words appears as the "label" for a timeframe
    # line. This is more accurate than keyword counting across the whole
    # section (where news impact lines may mention صعودی/نزولی incidentally).
    timeframe_lines = re.findall(
        r"پیش‌بینی\s+(?:۴|۴ ساعته|۲۴ ساعته|یک هفته‌ای|۱ هفته‌ای)[^\n]*",
        forecast_section,
    )
    inline_counts = {w: 0 for w in ["نوسانی", "صعودی", "نزولی"]}
    for line in timeframe_lines:
        for w in inline_counts:
            if w in line:
                inline_counts[w] += 1
    if any(inline_counts.values()):
        # pick the most common explicit direction across the timeframes
        return max(inline_counts, key=lambda k: inline_counts[k])

    table_dir = re.search(r"\|\s*(نوسانی|صعودی|نزولی)\s*\|", forecast_section)
    if table_dir:
        return table_dir.group(1)

    # 3) Legacy keyword scoring as last-resort tiebreaker
    up_keywords = ["صعودی", "افزایش", "رشد", "📈", "🟢"]
    down_keywords = ["نزولی", "کاهش", "افت", "📉", "🔴"]
    up_score = sum(1 for w in up_keywords if w in forecast_section)
    down_score = sum(1 for w in down_keywords if w in forecast_section)
    if up_score == 0 and down_score == 0:
        return "نوسانی"
    if up_score > down_score:
        return "صعودی"
    if down_score > up_score:
        return "نزولی"
    return "نوسانی"

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 save_final_forecast.py <path_to_forecast_text_file>", file=sys.stderr)
        sys.exit(1)
        
    file_path = sys.argv[1]
    if not os.path.exists(file_path):
        print(f"File not found: {file_path}", file=sys.stderr)
        sys.exit(1)
        
    with open(file_path, "r", encoding="utf-8") as f:
        forecast_text = f.read().strip()
        
    if not forecast_text:
        print("Empty forecast text, skipping.", file=sys.stderr)
        sys.exit(1)
        
    # دریافت قیمت‌های فعلی برای اسنپ‌شات
    prices = get_current_prices()
    if not prices:
        # fallback به لود آخرین قیمت دیتابیس
        db = load_db()
        if db["price_history"]:
            last = db["price_history"][-1]
            prices = {
                "mesghal": last.get("mesghal_toman", 0),
                "geram18": last.get("gold_18_toman", 0) or last.get("geram18", 0),
                "price_dollar_rl": last.get("usd_toman", 0),
                "crypto-tether-irr": last.get("tether_toman", 0)
            }
            
    # استخراج جهت
    direction = extract_direction(forecast_text)
    print(f"Extracted predicted direction: {direction}")
    
    # لود دیتابیس و ذخیره پیش‌بینی واقعی
    db = load_db()
    
    # حذف پیش‌بینی خالی آخری که توسط analyze.py (پله قبلی) ثبت شده بود تا تکراری نشود
    if db["forecasts"] and "<!-- FORECAST_PLACEHOLDER -->" in db["forecasts"][-1]["forecast_text"]:
        print("Removing the empty placeholder forecast entry...")
        db["forecasts"].pop()
        
    add_forecast(
        db, 
        forecast_text=forecast_text, 
        snapshot_prices=prices,
        predicted_direction=direction
    )
    print("✅ Final forecast with real prediction saved to DB successfully!")

if __name__ == "__main__":
    main()
