"""
gold-tracker-bot/analyze.py

اجرای کامل تحلیل: دریافت قیمت‌های زنده، اخبار، محاسبه نوسانات,
ارزیابی پیش‌بینی‌های قبلی، و خروجی نهایی برای ارسال به کاربر.

استفاده:
  python3 analyze.py          # تحلیل کامل ۸ ساعته
  python3 analyze.py --daily  # خلاصه پایان روز
"""

from __future__ import annotations

import sys
import os
import re
import json
import urllib.request
from datetime import datetime, timezone, timedelta

# Add parent dir to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from price_tracker import _fetch_tick_price, _fetch_all_prices, _to_toman, _fa_digits, _fmt_fa, _fetch_usdt_price
from news_scraper import get_all_news
from json_db import load_db, save_db, add_price_entry, add_forecast, evaluate_old_forecasts, get_win_rate, now_iso

TZ_TEHRAN = timezone(timedelta(hours=3, minutes=30))

TRACKED_KEYS = [
    ("mesghal", "مثقال طلا", True),
    ("geram18", "طلا ۱۸ عیار", True),
    ("price_dollar_rl", "دلار", True),
    ("crypto-tether-irr", "تتر", True),
]


def get_current_prices() -> dict:
    """دریافت قیمت‌های زنده از API tgju.org و تبدیل به تومان."""
    prices = {}
    # اول از همه ترکیبی سریع (tala.ir + homepage) بگیر
    all_prices = _fetch_all_prices()
    # نگاشت کلیدها: mazneh از tala.ir به mesghal، geram18 مستقیم، price_dollar_rl مستقیم
    if all_prices.get("mazneh") is not None:
        prices["mesghal"] = _to_toman(all_prices["mazneh"])
    if all_prices.get("geram18") is not None:
        prices["geram18"] = _to_toman(all_prices["geram18"])
    if all_prices.get("price_dollar_rl") is not None:
        # این قبلاً به تومان تبدیل شده در homepage اما مقیاس بهتری با tick-data
        prices["price_dollar_rl"] = _to_toman(all_prices["price_dollar_rl"])
    # fallback به tick data برای کلیدهایی که گرفتن نشدند
    for key, label, is_rial in TRACKED_KEYS:
        if key in prices:
            continue
        price_rial = _fetch_tick_price(key)
        if price_rial is not None:
            toman = _to_toman(price_rial) if is_rial else price_rial
            prices[key] = toman
    # تتر از کانال @UsdtLahze
    usdt = _fetch_usdt_price()
    if usdt is not None:
        prices["crypto-tether-irr"] = usdt
    return prices


def calculate_volatility(db: dict, current_prices: dict) -> dict:
    """محاسبه نوسان از اول صبح تا الان."""
    if not current_prices or not db["price_history"]:
        return {}
    today = datetime.now(TZ_TEHRAN).strftime("%Y-%m-%d")
    # پیدا کردن اولین قیمت امروز
    morning_prices = None
    for entry in db["price_history"]:
        if entry["timestamp"].startswith(today):
            morning_prices = entry
            break
    if not morning_prices:
        # اگر امروز داده‌ای نداریم، آخرین داده رو به عنوان مرجع استفاده کن
        morning_prices = db["price_history"][0] if db["price_history"] else None
    if not morning_prices:
        return {}
    volatilities = {}
    for key, label, _ in TRACKED_KEYS:
        old = morning_prices.get(f"{key}_toman", 0) or morning_prices.get(key, 0)
        new = current_prices.get(key, 0)
        if old > 0 and new > 0:
            change = new - old
            pct = (change / old) * 100
            volatilities[key] = {
                "label": label,
                "morning": old,
                "current": new,
                "change": change,
                "pct": round(pct, 2),
            }
    return volatilities


def format_analysis(prices: dict, volatilities: dict, news: dict, db: dict, mode: str = "8h") -> str:
    """ساخت پیام تحلیل نهایی."""
    now = datetime.now(TZ_TEHRAN)
    fa_date = _fa_digits(now.strftime("%Y/%m/%d %H:%M"))

    win_rate = get_win_rate(db)
    lessons = db.get("lessons", [])

    lines = []

    if mode == "daily":
        lines.append("📋 **خلاصه پایان روز — تحلیل طلا و ارز**")
    else:
        lines.append("📈 **تحلیل ۸ ساعته — طلا و ارز ایران**")
    lines.append(f"🕒 {fa_date}")
    lines.append("")

    # بخش ۱: قیمت‌های فعلی
    lines.append("## 💰 قیمت‌های فعلی")
    emojis = {"mesghal": "🥇", "geram18": "🥈", "price_dollar_rl": "💵", "crypto-tether-irr": "₮"}
    for key, label, _ in TRACKED_KEYS:
        price = prices.get(key, 0)
        vol = volatilities.get(key, {})
        chg_str = ""
        if vol:
            pct = vol["pct"]
            sign = "🔺" if pct > 0 else "🔻" if pct < 0 else "➖"
            chg_str = f"  {sign} {_fa_digits(f'{pct:+.2f}')}%"
        price_str = _fmt_fa(price) if price > 0 else "—"
        lines.append(f"{emojis.get(key, '📊')} **{label}**: `{price_str}`{chg_str}")
    lines.append("")

    # بخش ۲: نوسانات صبح تا الان
    if volatilities:
        lines.append("## 📊 نوسانات امروز (از صبح تا الان)")
        for key, vol in volatilities.items():
            change = vol["change"]
            pct = vol["pct"]
            if abs(pct) >= 2:
                alert = " ⚠️"
            else:
                alert = ""
            lines.append(
                f"- **{vol['label']}**: {_fa_digits(f'{change:+,}')} تومان "
                f"({_fa_digits(f'{pct:+.2f}')}%){alert}"
            )
        lines.append("")

    # بخش ۳: مهم‌ترین اخبار (بولت‌پوینت)
    lines.append("## 📰 مهم‌ترین اخبار")
    all_news = []
    for channel, posts in news.items():
        for post in posts[-2:]:  # آخرین ۲ پست هر کانال
            # فقط ۱۰۰ کاراکتر اول
            excerpt = post[:150].replace("\n", " ").strip()
            if len(post) > 150:
                excerpt += "..."
            all_news.append(excerpt)

    # حذف تکراری‌ها و نمایش
    seen = set()
    unique_news = []
    for n in all_news:
        if n[:50] not in seen:
            seen.add(n[:50])
            unique_news.append(n)

    for i, n in enumerate(unique_news[:8], 1):
        lines.append(f"{i}. {n}")
    lines.append("")

    # بخش ۴: پیش‌بینی‌ها — DONE بعد از ساخت قالب توسط LLM پر می‌شود
    # یک marker مشخص می‌گذاریم تا LLM بتواند این بخش را پیدا و جایگذاری کند
    lines.append("## 🔮 پیش‌بینی قیمت")
    lines.append("<!-- FORECAST_PLACEHOLDER -->")
    lines.append("")

    # بخش ۵: عملکرد قبلی
    if win_rate["total"] > 0:
        lines.append(
            f"- مجموع: {_fa_digits(str(win_rate['total']))} | "
            f"✅ برد: {_fa_digits(str(win_rate['wins']))} | "
            f"❌ باخت: {_fa_digits(str(win_rate['losses']))} | "
            f"neutral: {_fa_digits(str(win_rate['neutral']))}"
        )
        lines.append(f"- **وین‌ریت**: {_fa_digits(str(win_rate['win_rate']))}%")
        lines.append("")

    # بخش ۶: درس‌های آموخته
    if lessons:
        lines.append("## 🎓 درس‌های آموخته")
        for lesson in lessons[-3:]:  # آخرین ۳ درس
            lines.append(f"- {lesson[:120]}")
        lines.append("")

    lines.append("↳ منبع قیمت: tgju.org | منبع اخبار: کانال‌های تلگرام")

    return "\n".join(lines)


def main():
    mode = "daily" if "--daily" in sys.argv else "8h"

    # ۱. دریافت قیمت‌های زنده
    prices = get_current_prices()
    if not prices:
        print("[analyze] قیمت‌ها قابل دریافت نیستند، تحلیل لغو شد.", file=sys.stderr)
        return

    # ۲. بارگذاری دیتابیس و ذخیره قیمت جدید
    db = load_db()
    add_price_entry(db, prices)
    db = load_db()  # reload

    # ۳. محاسبه نوسانات
    volatilities = calculate_volatility(db, prices)

    # ۴. ارزیابی پیشبینیهای قبلی
    evaluate_old_forecasts(db, prices)
    db = load_db()  # reload

    # ۵. دریافت اخبار
    try:
        news = get_all_news()
    except Exception:
        news = {}

    # ۶. محاسبه نرخ برد پس از ارزیابی
    win_rate = get_win_rate(db)

    # ۷. ساخت پیام تحلیل
    msg = format_analysis(prices, volatilities, news, db, mode)

    # ۷. ذخیره پیش‌بینی در دیتابیس
    add_forecast(db, msg, prices)

    # ۸. خروجی
    print(msg)


if __name__ == "__main__":
    main()
