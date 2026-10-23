#!/usr/bin/env python3
"""
gold-tracker-bot/news_scraper.py

اسکرپ کردن آخرین پیام‌های کانال‌های عمومی تلگرام بدون نیاز به API یا لاگین.
از آدرس وب t.me/s/ استفاده می‌کنه.
"""

from __future__ import annotations

import re
import sys
import urllib.request
import urllib.error
try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

CHANNELS = [
    ("goldonline2016", "کانال اطلاع‌رسانی طلا"),
    ("mazene", "کانال مظنه طلا و دلار"),
    ("VahidOnline", "وحید آنلاین (اخبار عمومی و اجتماعی)"),
]

def _fetch_channel_html(username: str, timeout: int = 15) -> str | None:
    url = f"https://telegram.me/s/{username}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    except Exception as e:
        print(f"[Scraper Error] Fetching {username} failed: {e}", file=sys.stderr)
        return None

def clean_html(text: str) -> str:
    # حذف تگ‌های HTML اضافی و برگرداندن متن تمیز
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'<[^>]+>', '', text)
    # جایگزینی کاراکترهای خاص HTML
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    return text.strip()

def get_latest_posts(username: str, limit: int = 4) -> list[str]:
    html = _fetch_channel_html(username)
    if not html:
        return []
    
    posts = []
    # در تلگرام وب، پیام‌ها داخل تگ div با کلاس tgme_widget_message_text هستند
    # با BeautifulSoup اگر نصب باشد راحت‌تر است، وگرنه با regex
    try:
        if BeautifulSoup:
            soup = BeautifulSoup(html, 'html.parser')
            elements = soup.find_all(class_="tgme_widget_message_text")
            for el in elements[-limit:]:
                text = el.get_text('\n').strip()
                if text:
                    posts.append(text)
        else:
            # Fallback to regex if bs4 is not available
            matches = re.findall(r'<div class="[^"]*tgme_widget_message_text[^"]*"[^>]*>([\s\S]*?)</div>', html)
            for m in matches[-limit:]:
                cleaned = clean_html(m)
                if cleaned:
                    posts.append(cleaned)
    except Exception as e:
        print(f"[Scraper Error] Parsing {username} failed: {e}", file=sys.stderr)
                
    # بازگرداندن پست‌ها (از قدیمی به جدید)
    return posts

def get_all_news() -> dict[str, list[str]]:
    results = {}
    for user, label in CHANNELS:
        results[user] = get_latest_posts(user)
    return results

if __name__ == "__main__":
    news = get_all_news()
    for user, posts in news.items():
        print(f"\n=================== {user} ===================")
        for i, post in enumerate(posts, 1):
            excerpt = post[:200].replace('\n', ' ') + ('...' if len(post) > 200 else '')
            print(f"{i}. {excerpt}")
