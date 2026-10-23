#!/usr/bin/env python3
"""
gold-tracker-bot/finance_bot_listener.py

این اسکریپت به صورت دائم (Long Polling) روی ربات تلگرام شما گوش می‌دهد.
هر پیامی که در گروه فرستاده شود را بررسی کرده و تراکنش‌ها را ثبت می‌کند.
"""

from __future__ import annotations

import json
import os
import sys
import re
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
import jdatetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from finance_manager import add_transaction, get_monthly_report, load_finance_db, save_finance_db, add_financial_goal

BOT_TOKEN = os.environ.get("FINANCE_BOT_TOKEN", "")
TZ_TEHRAN = timezone(timedelta(hours=3, minutes=30))

# الگوهای ساده برای پارس کردن پیام‌های کاربر به زبان فارسی
# مثال: "هزینه تفریح ۱۲۰۰۰۰۰ برای رستوران" یا "درآمد حقوق ۵۰۰۰۰۰۰۰" یا "۵۰۰۰ مخارج کافه"
# فرمت پیشنهادی: [نوع: هزینه/درآمد] [دسته‌بندی] [مبلغ به تومان] [توضیحات اختیاری]
# یا به صورت ساده‌تر: "تفریح ۵۰۰۰۰" -> هزینه تفریح ۵۰,۰۰۰ تومان


def parse_and_register_message(text: str) -> str | None:
    text = text.strip()
    
    # حذف کاما و ریال/تومان برای یکسان‌سازی عددها
    text_clean = text.replace(",", "").replace("،", "").replace("تومان", "").replace("تومن", "").strip()
    
    # تبدیل ارقام فارسی به انگلیسی
    persian_to_english = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
    text_clean = text_clean.translate(persian_to_english)
    
    # الگو ۱: [مبلغ] [دسته‌بندی] [توضیحات] (مثال: ۱۲۰۰۰ تفریح کافه با دوستام)
    pattern1 = re.match(r"^(\d+)\s+(\S+)(?:\s+(.+))?$", text_clean)
    
    # الگو ۲: [دسته‌بندی] [مبلغ] [توضیحات] (مثال: تفریح ۱۲۰۰۰ کافه)
    pattern2 = re.match(r"^(\S+)\s+(\d+)(?:\s+(.+))?$", text_clean)
    
    amount = 0
    category = ""
    description = ""
    tx_type = "expense"  # پیش‌فرض هزینه است
    
    if pattern1:
        amount = int(pattern1.group(1))
        category = pattern1.group(2)
        description = pattern1.group(3) or ""
    elif pattern2:
        category = pattern2.group(1)
        amount = int(pattern2.group(2))
        description = pattern2.group(3) or ""
    else:
        # اگر فقط دستور گزارش خواست
        if text.lower() in ["گزارش", "گزارش ماه", "/report"]:
            return get_monthly_report()
        if text.startswith("هدف"):
            # ثبت هدف: "هدف خرید گوشی ۵۰۰۰۰۰۰۰ تا تاریخ ۱۴۰۵/۱۲/۲۹"
            m = re.match(r"^هدف\s+(.+)\s+(\d+)\s+تا\s+(\S+)$", text_clean)
            if m:
                g_name = m.group(1)
                g_amt = int(m.group(2))
                g_dead = m.group(3)
                add_financial_goal(g_name, g_amt, g_dead)
                return f"🎯 هدف مالی جدید با موفقیت ثبت شد:\n📌 **{g_name}**\n💰 مبلغ: `{g_amt:,}` تومان\n📅 مهلت: {g_dead}"
            return "❌ فرمت ثبت هدف اشتباه است.\nفرمت صحیح:\n`هدف [نام هدف] [مبلغ به تومان] تا [تاریخ شمسی]`\nمثال:\n`هدف خرید طلا ۵۰۰۰۰۰۰۰ تا ۱۴۰۵/۱۲/۲۹`"
        return None

    # بررسی درآمد بودن دسته‌بندی
    if category in ["درآمد", "حقوق", "واریز", "یارانه", "سود", "income"]:
        tx_type = "income"
        if category == "income":
            category = "درآمد عمومی"
    
    # ثبت تراکنش
    entry = add_transaction(amount, tx_type, category, description)
    
    tx_type_fa = "📥 درآمد" if tx_type == "income" else "💸 هزینه"
    desc_str = f" ({description})" if description else ""
    
    resp = (
        f"✅ تراکنش با موفقیت ثبت شد:\n"
        f"🔸 **نوع:** {tx_type_fa}\n"
        f"🔸 **دسته‌بندی:** {category}\n"
        f"🔸 **مبلغ:** `{amount:,}` تومان{desc_str}\n"
        f"📅 **تاریخ:** {entry['date_shamsi']}"
    )
    return resp


def send_message(chat_id: int, text: str) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            pass
    except Exception as e:
        print(f"[send error] {e}", file=sys.stderr)


def main():
    print("Bot Listener started. Polling for messages...")
    offset = 0
    # ایجاد دیتابیس در صورت نبودن
    load_finance_db()
    
    while True:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={offset}&timeout=20"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=25) as response:
                data = json.loads(response.read().decode())
                if data.get("ok") and data.get("result"):
                    for update in data["result"]:
                        offset = update["update_id"] + 1
                        
                        # بررسی پیام معمولی یا پیام گروهی
                        message = update.get("message") or update.get("edited_message")
                        if not message:
                            continue
                        
                        chat = message["chat"]
                        chat_id = chat["id"]
                        text = message.get("text")
                        
                        if not text:
                            continue
                        
                        # پردازش پیام
                        response_text = parse_and_register_message(text)
                        if response_text:
                            send_message(chat_id, response_text)
                            
        except Exception as e:
            print(f"[polling error] {e}", file=sys.stderr)
            time.sleep(3)


if __name__ == "__main__":
    main()
