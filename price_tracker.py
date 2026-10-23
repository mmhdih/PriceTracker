#!/usr/bin/env python3
"""
gold-tracker-bot/price_tracker.py

هر ۵ دقیقه از tgju.org چهار قیمت مهم را می‌گیرد و مستقیم به کانال تلگرامی تعیین شده می‌فرستد.
قیمت‌ها به تومان تبدیل شده‌اند. تاریخ شمسی است.
"""

from __future__ import annotations

import re
import sys
import json
import os
import html as html_mod
import urllib.request
import urllib.error
import jdatetime
from datetime import datetime, timezone, timedelta

# --- daily history storage --------------------------------------------------
from config import DATA_DIR
from safe_json import read_json, save_json, update_json
DAILY_DB_PATH = str(DATA_DIR / "daily_prices.json")

TZ_TEHRAN = timezone(timedelta(hours=3, minutes=30))


def _load_daily_db() -> dict:
    return read_json(DAILY_DB_PATH, {"days": {}})


def _save_daily_db(db: dict) -> None:
    save_json(DAILY_DB_PATH, db)


def _save_price_to_daily(prices: dict) -> None:
    now = jdatetime.datetime.now(TZ_TEHRAN)
    today = now.strftime("%Y-%m-%d")
    def append(db):
        db["days"].setdefault(today, []).append({"time": now.strftime("%H:%M"), "prices": prices})
        for day in sorted(db["days"])[:-7]:
            del db["days"][day]
    update_json(DAILY_DB_PATH, {"days": {}}, append)
    from tracker_store import get_store
    get_store().observe(datetime.now(TZ_TEHRAN).isoformat(), prices, 'price-tracker')


# --- تنظیمات کانال و توکن ربات ------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

TGJU_URL = "https://www.tgju.org/"
NAGHDP_URL = "https://telegram.me/s/NaghdP"

TARGETS = [
    ("مثقال طلا", "mesghal", True, "🥇"),
    ("طلا ۱۸ عیار", "geram18", True, "🥈"),
    ("دلار", "price_dollar_rl", True, "💵"),
    ("تتر", "crypto-tether-irr", True, "₮"),
]


def _fetch_json(url: str, timeout: int = 10) -> dict | None:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"[fetch error] {e}", file=sys.stderr)
        return None


def _fetch_html(url: str, timeout: int = 15) -> str | None:
    """دانلود صفحه HTML (برای NaghdP و fallback)"""
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "fa,en;q=0.9",
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    except Exception as e:
        print(f"[fetch error] {e}", file=sys.stderr)
        return None


# --- قیمت لحظه‌ای از API tgju.org -------------------------------------------
# endpoint: https://api.tgju.org/v1/market/indicator/today-table-data/{grade}?lang=fa
# خروجی: {"data": [[price_rial, time, change_html, change_pct_html], ...]}
# اولین ردیف = آخرین قیمت لحظه‌ای

TGJU_LIVE_API = "https://api.tgju.org/v1/market/indicator/today-table-data/{grade}?lang=fa"

TGJU_HOMEPAGE = "https://www.tgju.org/"

TALA_IR_18K = "https://www.tala.ir/price/18k"
TALA_IR_MAZNEH = "https://www.tala.ir/price/mazaneh"

HOMEPAGE_GRADES = [
    ("price_dollar_rl", "دلار", True, "💵"),
]

# مظنه و طلای ۱۸ از tala.ir (قیمت تومانی)
TALA_GRADES = [
    ("mazneh", "مظنه", True, "🥇", TALA_IR_MAZNEH),
    ("geram18", "طلا ۱۸ عیار", True, "🥈", TALA_IR_18K),
]


def _fetch_tala_price(url: str) -> int | None:
    """خواندن آخرین قیمت از صفحه tala.ir"""
    html = _fetch_html(url, timeout=12)
    if not html:
        return None
    # پیدا کردن قیمت از bg-green-light (آخرین قیمت)
    match = re.search(r'bg-green-light[^>]*>([\d,۰-۹]+)', html)
    if match:
        raw = match.group(1)
        raw = raw.translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹', '0123456789'))
        try:
            return int(raw.replace(",", ""))
        except ValueError:
            return None
    return None


def _fetch_all_homepage_prices() -> dict[str, float | None]:
    """خواندن قیمت‌ها از صفحه اصلی tgju.org"""
    html = _fetch_html(TGJU_HOMEPAGE, timeout=10)
    if not html:
        return {}
    result = {}
    for key, label, is_rial, emoji in HOMEPAGE_GRADES:
        match = re.search(r'data-market-row="' + re.escape(key) + r'"[^>]*', html)
        if match:
            chunk = html[match.start():match.start()+2000]
            price_match = re.search(r'data-price="([^"]+)"', chunk)
            if price_match:
                try:
                    result[key] = float(price_match.group(1).replace(",", ""))
                except ValueError:
                    result[key] = None
            else:
                result[key] = None
        else:
            result[key] = None
    return result


def _fetch_tick_price(grade: str) -> float | None:
    """خواندن قیمت لحظه‌ای از API tick data (هر ۳ ثانیه آپدیت)"""
    url = TGJU_LIVE_API.format(grade=grade)
    data = _fetch_json(url)
    if not data or "data" not in data or not data["data"]:
        return None
    latest = data["data"][0]
    try:
        return float(latest[0].replace(",", ""))
    except (ValueError, IndexError):
        return None


def _fetch_price_from_profile(grade: str) -> float | None:
    """خواندن قیمت از صفحه پروفایل tgju.org (fallback نهایی)"""
    url = f"https://www.tgju.org/profile/{grade}"
    html = _fetch_html(url, timeout=10)
    if not html:
        return None
    prices = re.findall(r'data-col="info\.last_trade\.PDrCotVal"[^>]*>([\d,]+)', html)
    if not prices:
        prices = re.findall(r'class="price[^"]*"[^>]*>([\d,]+)', html)
    if prices:
        try:
            return float(prices[0].replace(",", ""))
        except ValueError:
            return None
    return None


def _fetch_all_prices() -> dict[str, float | None]:
    """دریافت تمام قیمت‌ها — ترکیب سریع‌ترین منابع"""
    result = {}

    # مظنه و طلای ۱۸ از tala.ir
    for key, label, is_rial, emoji, url in TALA_GRADES:
        price_toman = _fetch_tala_price(url)
        if price_toman is not None:
            # tala.ir already returns price in Toman, but we store in Rial for consistency
            result[key] = float(price_toman) * 10  # Convert Toman to Rial

    # دلار از صفحه اصلی tgju
    homepage = _fetch_all_homepage_prices()
    for key_item in HOMEPAGE_GRADES:
        k = key_item[0]
        if k in homepage and homepage[k] is not None:
            result[k] = homepage[k]

    return result


def _parse_row(html: str, row_key: str) -> tuple[float | None, str | None]:
    """فallback: اگر API جواب نداد، از HTML قدیمی بخوان"""
    row_start_re = re.compile(
        r'data-market-row\s*=\s*["\']' + re.escape(row_key) + r'["\']'
    )
    sm = row_start_re.search(html)
    if not sm:
        return None, None
    chunk = html[sm.start() : sm.start() + 3000]
    pm = re.search(r'data-price\s*=\s*["\']([0-9,]+(?:\.[0-9]+)?)["\']', chunk)
    if not pm:
        return None, None
    try:
        price = float(pm.group(1).replace(",", ""))
    except ValueError:
        return None, None
    pct_m = re.search(r'([+\-]?[0-9]+(?:\.[0-9]+)?)\s*%', chunk)
    pct_str = f"{pct_m.group(1)}%" if pct_m else None
    return price, pct_str


def _fa_digits(s: str) -> str:
    return s.translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def _to_toman(price_rial: float) -> int:
    return int(price_rial / 10)


def _fmt(v: int) -> str:
    return f"{v:,}".replace(",", "،")


def _fmt_fa(v: int) -> str:
    return _fa_digits(_fmt(v)) + " تومان"


# --- NaghdP scraper ---------------------------------------------------------

_NAGHDP_SELL_RE = re.compile(
    r"([\d,]+)\s+⏳\s+باحواله\s+🔴\s+فروش\s+گرم:\s+([\d,]+)"
)
_NAGHDP_SELL_NOBA_RE = re.compile(
    r"([\d,]+)\s+⏳\s+🔴\s+فروش\s+گرم:\s+([\d,]+)"
)


def _scrape_naghdp_deal() -> tuple[str | None, str | None, str | None]:
    """برگرداند: (مظنه, گرم, نوع) — اول باحواله می‌گردد، بعد بدون باحواله."""
    html = _fetch_html(NAGHDP_URL, timeout=15)
    if not html:
        return None, None, None
    text = re.sub(r"<br\s*/?>", "\n", html)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    # اول باحواله 🔴 فروش
    matches = _NAGHDP_SELL_RE.findall(text)
    if matches:
        mazneh, gram = matches[-1]
        return mazneh, gram, "باحواله"
    # بعد بدون باحواله 🔴 فروش
    matches = _NAGHDP_SELL_NOBA_RE.findall(text)
    if matches:
        mazneh, gram = matches[-1]
        return mazneh, gram, None
    return None, None, None


# --- Telegram sender ---------------------------------------------------------

def send_to_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise ValueError("Configure TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            res_data = json.loads(response.read().decode())
            return res_data.get("ok", False)
    except Exception as e:
        print(f"[Telegram API Error] {e}", file=sys.stderr)
        return False




def send_and_pin_to_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise ValueError("Configure TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    url_send = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url_send, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            res_data = json.loads(response.read().decode())
            if not res_data.get("ok"):
                return False
            message_id = res_data.get("result", {}).get("message_id")
    except Exception as e:
        print(f"[Telegram API Error] {e}", file=sys.stderr)
        return False

    if message_id:
        url_pin = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/pinChatMessage"
        pin_payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "message_id": message_id,
            "disable_notification": False,
        }
        pin_data = json.dumps(pin_payload).encode("utf-8")
        pin_req = urllib.request.Request(
            url_pin, data=pin_data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(pin_req, timeout=10) as response:
                pin_res = json.loads(response.read().decode())
                return pin_res.get("ok", False)
        except Exception as e:
            print(f"[Telegram Pin Error] {e}", file=sys.stderr)
            return False
    return True


def _get_morning_price(db: dict, grade: str) -> int | None:
    """پیدا کردن اولین قیمت امروز (قیمت صبح) — اولین entry که کلید grade را داشته باشد"""
    today = jdatetime.datetime.now(TZ_TEHRAN).strftime("%Y-%m-%d")
    for day_key in sorted(db.get("days", {}).keys(), reverse=True):
        if day_key == today:
            for entry in db["days"][day_key]:
                val = entry["prices"].get(grade)
                if val is not None and val > 0:
                    return val
    return None


def _fetch_usdt_price() -> int | None:
    """خواندن قیمت تتر از کانال @UsdtLahze"""
    html = _fetch_html(USDT_CHANNEL, timeout=15)
    if not html:
        return None
    # پیدا کردن آخرین پیام با قیمت تتر
    prices = re.findall(r'(\d[\d,]+)\s*تومان', html)
    if prices:
        try:
            return int(prices[-1].replace(",", ""))
        except ValueError:
            return None
    return None


USDT_CHANNEL = "https://telegram.me/s/UsdtLahze"
GOLD_SILVER_CHANNEL = "https://telegram.me/s/goldsilverprice1"


def _fetch_usdt_price() -> int | None:
    """خواندن قیمت تتر از کانال @UsdtLahze"""
    html = _fetch_html(USDT_CHANNEL, timeout=15)
    if not html:
        return None
    prices = re.findall(r'(\d[\d,]+)\s*تومان', html)
    if prices:
        try:
            return int(prices[-1].replace(",", ""))
        except ValueError:
            return None
    return None

GOLD_OUNCE_CHANNEL = "https://telegram.me/s/OUNSGOLD"
SILVER_OUNCE_CHANNEL = "https://telegram.me/s/SilverOuncePrice"


def _fetch_gold_silver_prices() -> tuple[float | None, float | None]:
    """خواندن قیمت انس طلا از @OUNSGOLD و انس نقره از @SilverOuncePrice.
    بازگشت: (انس طلا, انس نقره) به دلار
    """
    gold_oz = None
    silver_oz = None

    # طلا از @OUNSGOLD — هر پیام فقط یک عدد دارد (مثل "♦️ 4382.94")
    html = _fetch_html(GOLD_OUNCE_CHANNEL, timeout=15)
    if html:
        # پیدا کردن پیام‌ها
        messages = re.findall(
            r'class="[^"]*tgme_widget_message_text[^"]*"[^>]*>([\s\S]*?)</div>',
            html,
        )
        if messages:
            # آخرین پیام‌ها را به عقب اسکن کن تا اولین پیامی که شامل عدد اعشاری (قیمت) است را پیدا کنی
            for msg in reversed(messages):
                last_msg = re.sub(r"<[^>]+>", " ", msg)
                last_msg = html_mod.unescape(last_msg)
                match = re.search(r"([0-9]+\.[0-9]+)", last_msg)
                if match:
                    try:
                        gold_oz = float(match.group(1))
                        break
                    except ValueError:
                        pass

    # نقره از @SilverOuncePrice — هر پیام فقط یک عدد دارد (مثل "🔵 65.43")
    html = _fetch_html(SILVER_OUNCE_CHANNEL, timeout=15)
    if html:
        messages = re.findall(
            r'class="[^"]*tgme_widget_message_text[^"]*"[^>]*>([\s\S]*?)</div>',
            html,
        )
        if messages:
            for msg in reversed(messages):
                last_msg = re.sub(r"<[^>]+>", " ", msg)
                last_msg = html_mod.unescape(last_msg)
                match = re.search(r"([0-9]+\.[0-9]+)", last_msg)
                if match:
                    try:
                        silver_oz = float(match.group(1))
                        break
                    except ValueError:
                        pass

    return gold_oz, silver_oz


# --- Main -------------------------------------------------------------------

def main() -> int:
    lines: list[str] = []
    prices_ok: list[bool] = []

    # بارگذاری دیتابیس روزانه
    daily_db = _load_daily_db()

    # دریافت تمام قیمت‌ها
    all_prices = _fetch_all_prices()

    # لیست نمایش (ترکیب TALA_GRADES + HOMEPAGE_GRADES)
    ALL_GRADES = TALA_GRADES + HOMEPAGE_GRADES

    for key, label, is_rial, emoji, *_ in ALL_GRADES:
        price_rial = all_prices.get(key)
        if price_rial is None:
            # fallback: پروفایل
            price_rial = _fetch_price_from_profile(key)
        if price_rial is None:
            lines.append(f"{emoji} **{label}**: `—`")
            prices_ok.append(False)
            continue
        toman = _to_toman(price_rial)
        price_disp = _fmt_fa(toman)

        # محاسبه افت/رشد نسبت به صبح
        morning = _get_morning_price(daily_db, key)
        if morning and morning > 0:
            change = toman - morning
            change_pct = (change / morning) * 100
            if change_pct > 0:
                chg = f"  🔺{_fa_digits(f'{change_pct:+.2f}')}"
            elif change_pct < 0:
                chg = f"  🔻{_fa_digits(f'{change_pct:+.2f}')}"
            else:
                chg = f"  ➖{_fa_digits(f'{change_pct:+.2f}')}"
        else:
            chg = ""

        lines.append(f"{emoji} **{label}**: `{price_disp}`{chg}")
        prices_ok.append(True)

    if not any(prices_ok):
        print("[gold-tracker] All API calls failed. Silent exit.", file=sys.stderr)
        return 1

    # تاریخ شمسی با ساعت تهران
    shamsi_now = jdatetime.datetime.now(TZ_TEHRAN)
    fa_date = _fa_digits(shamsi_now.strftime("%Y/%m/%d %H:%M"))

    # NaghdP
    mazneh, gram_bazar, deal_type = _scrape_naghdp_deal()

    # تتر از کانال @UsdtLahze
    usdt_price = _fetch_usdt_price()

    # انس طلا و نقره از کانال @goldsilverprice1
    gold_ounce, silver_ounce = _fetch_gold_silver_prices()

    # ذخیره در daily history (شامل تتر و دلار و انس)
    daily_prices = {}
    for key, label, is_rial, emoji, *_ in ALL_GRADES:
        p = all_prices.get(key)
        if p is not None:
            daily_prices[key] = _to_toman(p) if is_rial else p

    # Explicitly add dollar to daily_prices for _save_price_to_daily
    dollar_price_from_collector = all_prices.get("price_dollar_rl") # It's already in Rial here
    if dollar_price_from_collector is not None:
        dollar_toman = _to_toman(dollar_price_from_collector)
        daily_prices["price_dollar_rl"] = dollar_toman
        daily_prices["dollar"] = dollar_toman  # alias for collector volatility lookup

    if usdt_price:
        daily_prices["usdt"] = usdt_price
    if gold_ounce:
        daily_prices["gold_ounce"] = gold_ounce
    if silver_ounce:
        daily_prices["silver_ounce"] = silver_ounce
    if daily_prices:
        try:
            _save_price_to_daily(daily_prices)
        except Exception as e:
            print(f"[daily save error] {e}", file=sys.stderr)

    # ساخت پیام
    msg_parts = [
        "📊 **قیمت لحظه‌ی طلا و ارز**",
        f"🕒 {fa_date}",
        "",
        *lines,
    ]

    # نمایش تتر با نوسان
    if usdt_price:
        usdt_morning = _get_morning_price(daily_db, "usdt")
        if usdt_morning and usdt_morning > 0:
            usdt_change = usdt_price - usdt_morning
            usdt_pct = (usdt_change / usdt_morning) * 100
            if usdt_pct > 0:
                usdt_chg = f"  🔺{_fa_digits(f'{usdt_pct:+.2f}')}"
            elif usdt_pct < 0:
                usdt_chg = f"  🔻{_fa_digits(f'{usdt_pct:+.2f}')}"
            else:
                usdt_chg = f"  ➖{_fa_digits(f'{usdt_pct:+.2f}')}"
        else:
            usdt_chg = ""
        msg_parts.append(f"₮ **تتر**: `{_fmt_fa(usdt_price)}`{usdt_chg}")

    # انس طلا و نقره با نوسان
    if gold_ounce:
        gold_morning = _get_morning_price(daily_db, "gold_ounce")
        if gold_morning and gold_morning > 0:
            gold_change = gold_ounce - gold_morning
            gold_pct = (gold_change / gold_morning) * 100
            if gold_pct > 0:
                gold_chg = f"  🔺{_fa_digits(f'{gold_pct:+.2f}')}"
            elif gold_pct < 0:
                gold_chg = f"  🔻{_fa_digits(f'{gold_pct:+.2f}')}"
            else:
                gold_chg = f"  ➖{_fa_digits(f'{gold_pct:+.2f}')}"
        else:
            gold_chg = ""
        gold_disp = _fa_digits(f"${gold_ounce:,.2f}")
        msg_parts.append(f"🥇 **انس طلا**: `{gold_disp}`{gold_chg}")

    if silver_ounce:
        silver_morning = _get_morning_price(daily_db, "silver_ounce")
        if silver_morning and silver_morning > 0:
            silver_change = silver_ounce - silver_morning
            silver_pct = (silver_change / silver_morning) * 100
            if silver_pct > 0:
                silver_chg = f"  🔺{_fa_digits(f'{silver_pct:+.2f}')}"
            elif silver_pct < 0:
                silver_chg = f"  🔻{_fa_digits(f'{silver_pct:+.2f}')}"
            else:
                silver_chg = f"  ➖{_fa_digits(f'{silver_pct:+.2f}')}"
        else:
            silver_chg = ""
        silver_disp = _fa_digits(f"${silver_ounce:,.2f}")
        msg_parts.append(f"🥈 **انس نقره**: `{silver_disp}`{silver_chg}")

    if mazneh and gram_bazar:
        mazneh_fa = _fa_digits(mazneh).replace(",", "،")
        gram_fa = _fa_digits(gram_bazar).replace(",", "،")
        msg_parts += [
            "",
            f"🔹 **مظنه بازار**: {mazneh_fa} تومان",
            f"🔹 **گرم بازار**: {gram_fa} تومان",
        ]

    msg = "\n".join(msg_parts)

    success = send_to_telegram(msg)
    if success:
        print("Message successfully posted to the channel.")
    else:
        print("Failed to post the message to the channel.", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
