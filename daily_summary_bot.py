#!/usr/bin/env python3
"""
gold-tracker-bot/daily_summary_bot.py

هر شب ساعت ۲۴ تهران:
۱. آخرین قیمت‌های زنده رو از tgju می‌گیره
۲. از daily_prices.json اولین تغییر قیمت واقعی روز (زمان بازگشایی و تغییر بازار بعد از تعطیلی شبانه) را به عنوان مبنا می‌خونه
۳. کمترین و بیشترین قیمت طلای ۱۸ عیار را همراه با ساعتش محاسبه و نمایش می‌دهد
۴. میزان نوسان هر مورد را نسبت به اولین تغییر قیمت روز محاسبه و به فارسی نمایش می‌ده
۵. پیام رو مستقیم به کانال تلگرامی می‌فرسته و پین می‌کند — بدون LLM
"""

from __future__ import annotations

import sys
import os
import jdatetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from price_tracker import (
    _fetch_html,
    _parse_row,
    _fa_digits,
    _to_toman,
    _fmt_fa,
    _load_daily_db,
    TGJU_URL,
    TALA_GRADES,
    HOMEPAGE_GRADES,
    send_and_pin_to_telegram,
    TZ_TEHRAN,
)

# Every key that gets stored in daily_prices.json
DAILY_KEYS = [
    ("مظنه", "mazneh", "🥇"),
    ("طلا ۱۸ عیار", "geram18", "🥈"),
    ("دلار", "price_dollar_rl", "💵"),
    ("تتر", "usdt", "₮"),
    ("انس طلا", "gold_ounce", "🥇"),
    ("انس نقره", "silver_ounce", "🥈"),
]


def _get_baseline_and_last_prices(today_str: str) -> tuple[dict, dict, str, str]:
    """
    پیدا کردن اولین تغییر قیمت واقعی روز (زمان باز شدن بازار و خروج از حالت ثابت شبانه)
    به عنوان مبنای محاسبه تغییرات روزانه.
    """
    db = _load_daily_db()
    entries = db.get("days", {}).get(today_str, [])
    if not entries:
        return {}, {}, "—", "—"

    # اولین قیمت‌های ثبت شده برای امروز
    midnight_prices = {}
    first_valid_time = ""
    for e in entries:
        prices = e.get("prices", {})
        if any(v and v > 0 for v in prices.values()):
            midnight_prices = prices
            first_valid_time = e.get("time", "")
            break

    baseline_prices = midnight_prices.copy()
    baseline_time = first_valid_time if first_valid_time else "۰۸:۰۰"

    # پیدا کردن اولین تغییر قیمت معتبر بعد از ۰۸:۰۰ صبح
    for e in entries:
        t = e.get("time", "")
        if t < "08:00":
            continue
        prices = e.get("prices", {})
        p_geram = prices.get("geram18", 0)
        midnight_geram = midnight_prices.get("geram18", 0)
        if p_geram > 0 and midnight_geram > 0 and p_geram != midnight_geram:
            baseline_prices = prices
            baseline_time = t
            break

    # پیدا کردن آخرین ورودی معتبر
    last_prices = {}
    last_time = "۲۳:۵۹"
    for e in reversed(entries):
        prices = e.get("prices", {})
        if any(v and v > 0 for v in prices.values()):
            last_prices = prices
            last_time = e.get("time", "۲۳:۵۹")
            break

    return baseline_prices, last_prices, _fa_digits(baseline_time), _fa_digits(last_time)


def _get_min_max_geram18(today_str: str) -> tuple[tuple[int, str] | None, tuple[int, str] | None]:
    """محاسبه پایین‌ترین و بالاترین قیمت طلای ۱۸ عیار امروز به همراه ساعت."""
    db = _load_daily_db()
    entries = db.get("days", {}).get(today_str, [])
    if not entries:
        return None, None

    min_entry = None
    max_entry = None

    for e in entries:
        t = e.get("time", "")
        p = e.get("prices", {}).get("geram18")
        if p is not None and p > 0:
            if min_entry is None or p < min_entry[0]:
                min_entry = (int(p), t)
            if max_entry is None or p > max_entry[0]:
                max_entry = (int(p), t)

    return min_entry, max_entry


def main() -> int:
    # تاریخ شمسی
    shamsi_now = jdatetime.datetime.now(TZ_TEHRAN)
    today_str = shamsi_now.strftime("%Y-%m-%d")
    fa_date = _fa_digits(shamsi_now.strftime("%Y/%m/%d"))

    baseline_prices, last_prices, baseline_time, last_time = _get_baseline_and_last_prices(today_str)
    min_g, max_g = _get_min_max_geram18(today_str)

    # اگه داده‌ای در دیتابیس ثبت نشده، زنده از tgju دریافت کن
    live_fallback = {}
    if not last_prices:
        html = _fetch_html(TGJU_URL)
        if html:
            for key, label, is_rial, emoji, *_ in (TALA_GRADES + HOMEPAGE_GRADES):
                p, _ = _parse_row(html, key)
                if p is not None:
                    live_fallback[key] = _to_toman(p) if is_rial else p

    if not last_prices:
        last_prices = live_fallback
        baseline_prices = {}

    if not last_prices:
        print("[daily] no prices available", file=sys.stderr)
        return 1

    # Unit-aware formatting; ounces are USD, domestic quotes are Toman.
    def amount(value, key):
        unit = "دلار" if key in ("gold_ounce", "silver_ounce") else "تومان"
        decimals = 2 if unit == "دلار" else 0
        return _fa_digits(f"{value:,.{decimals}f}") + " " + unit

    # ساخت پیام — فرمت Markdown
    lines = []
    lines.append("📋 **خلاصه پایان روز — طلا و ارز**")
    lines.append(f"🕒 {fa_date}")
    lines.append("")

    for label, key, emoji in DAILY_KEYS:
        base_val = baseline_prices.get(key)
        last_val = last_prices.get(key)
        if last_val and not base_val:
            lines.append(f"{emoji} **{label}**: {amount(last_val, key)}")
            lines.append("   مبنای مقایسه روزانه موجود نیست")
            lines.append("")
            continue

        if not base_val or not last_val:
            lines.append(f"{emoji} **{label}**: `—`")
            lines.append("")
            continue

        if base_val > 0:
            change = last_val - base_val
            pct = (change / base_val) * 100
        else:
            change = 0
            pct = 0

        if pct > 0.5:
            arrow = "🔺"
        elif pct < -0.5:
            arrow = "🔻"
        else:
            arrow = "➖"

        base_disp = amount(base_val, key)
        last_disp = amount(last_val, key)
        change_disp = amount(abs(change), key)
        change_disp = ("+" if change >= 0 else "-") + change_disp
        pct_disp = _fa_digits(f"{pct:+.2f}")

        lines.append(f"{emoji} **{label}**")
        lines.append(f"   اولین تغییر ({baseline_time}): `{base_disp}`")
        if key == "geram18" and min_g and max_g:
            min_val, min_time = min_g
            max_val, max_time = max_g
            lines.append(f"   📉 پایین‌ترین قیمت ({_fa_digits(min_time)}): `{amount(min_val, key)}`")
            lines.append(f"   📈 بالاترین قیمت ({_fa_digits(max_time)}): `{amount(max_val, key)}`")
        lines.append(f"   الان ({last_time}): `{last_disp}`")
        lines.append(f"   نوسان: `{change_disp}` ({pct_disp}%) {arrow}")
        lines.append("")

    msg = "\n".join(lines)

    # ارسال به کانال و پین کردن
    success = send_and_pin_to_telegram(msg)
    if success:
        print("Daily summary posted and pinned successfully.")
        return 0
    else:
        print("Failed to post or pin daily summary.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
