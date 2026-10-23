#!/usr/bin/env python3
"""
مانیتورینگ ۱ ساعته ربات قیمت
هر دقیقه قیمت‌های tala.ir رو با پیام‌های کانال مقایسه می‌کنه
"""

import re
import time
import json
import urllib.request
import html as html_mod
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=3, minutes=30))

def fetch_tala_price(url):
    headers = {"User-Agent": "Mozilla/5.0"}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    match = re.search(r'bg-green-light[^>]*>([\d,۰-۹]+)', html)
    if match:
        raw = match.group(1).translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹', '0123456789'))
        return int(raw.replace(",", ""))
    return None

def fetch_channel_messages():
    url = "https://t.me/s/Hajmehdipricetracker"
    headers = {"User-Agent": "Mozilla/5.0"}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    texts = re.findall(r'tgme_widget_message_text[^>]*>(.*?)</div', html, re.DOTALL)
    messages = []
    for t in texts:
        clean = re.sub(r'<[^>]+>', ' ', t)
        clean = html_mod.unescape(clean).strip()
        if clean:
            messages.append(clean)
    return messages

def extract_prices_from_message(msg):
    """استخراج قیمت‌ها از متن پیام"""
    prices = {}
    # مظنه
    m = re.search(r'مظنه بازار:\s*([\d,۰-۹]+)', msg)
    if m:
        raw = m.group(1).translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹', '0123456789'))
        prices['mazneh'] = int(raw.replace(",", ""))
    # طلای ۱۸
    m = re.search(r'طلا ۱۸ عیار:\s*([\d,۰-۹]+)', msg)
    if m:
        raw = m.group(1).translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹', '0123456789'))
        prices['geram18'] = int(raw.replace(",", ""))
    # مثقال
    m = re.search(r'مثقال طلا:\s*([\d,۰-۹]+)', msg)
    if m:
        raw = m.group(1).translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹', '0123456789'))
        prices['mesghal'] = int(raw.replace(",", ""))
    # دلار
    m = re.search(r'دلار:\s*([\d,۰-۹]+)', msg)
    if m:
        raw = m.group(1).translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹', '0123456789'))
        prices['dollar'] = int(raw.replace(",", ""))
    # تتر
    m = re.search(r'تتر:\s*([\d,۰-۹]+)', msg)
    if m:
        raw = m.group(1).translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹', '0123456789'))
        prices['usdt'] = int(raw.replace(",", ""))
    return prices

print("=" * 60)
print("  مانیتورینگ ۱ ساعته ربات قیمت")
print("=" * 60)

results = []
start_time = time.time()

while time.time() - start_time < 3600:  # 1 hour
    now = datetime.now(TZ).strftime("%H:%M:%S")
    print(f"\n--- {now} ---")
    
    # دریافت قیمت از tala.ir
    tala_mazneh = fetch_tala_price("https://www.tala.ir/price/mazaneh")
    tala_geram18 = fetch_tala_price("https://www.tala.ir/price/18k")
    
    # دریافت آخرین پیام کانال
    try:
        messages = fetch_channel_messages()
        last_msg = messages[-1] if messages else ""
        ch_prices = extract_prices_from_message(last_msg)
    except Exception as e:
        ch_prices = {}
        print(f"  Channel error: {e}")
    
    # مقایسه
    comparison = {"time": now, "tala": {}, "channel": {}, "diff": {}}
    
    for key, label in [("mazneh", "مظنه"), ("geram18", "طلای ۱۸")]:
        tala_val = tala_mazneh if key == "mazneh" else tala_geram18
        ch_val = ch_prices.get(key)
        comparison["tala"][key] = tala_val
        comparison["channel"][key] = ch_val
        
        if tala_val and ch_val:
            diff = tala_val - ch_val
            comparison["diff"][key] = diff
            status = "✅" if diff == 0 else f"⚠️ diff={diff:+,}"
            print(f"  {label}: tala={tala_val:,} | channel={ch_val:,} | {status}")
        else:
            print(f"  {label}: tala={tala_val} | channel={ch_val} | ❌")
    
    results.append(comparison)
    time.sleep(60)

# خلاصه
print("\n" + "=" * 60)
print("  خلاصه ۱ ساعته")
print("=" * 60)
mazneh_diffs = [r["diff"].get("mazneh", 0) for r in results if "mazneh" in r["diff"]]
geram_diffs = [r["diff"].get("geram18", 0) for r in results if "geram18" in r["diff"]]

if mazneh_diffs:
    print(f"مظنه: {len(mazneh_diffs)} برابر برابر، {sum(1 for d in mazneh_diffs if d != 0)} بار متفاوت")
    if any(d != 0 for d in mazneh_diffs):
        print(f"  اختلافات: {mazneh_diffs}")

if geram_diffs:
    print(f"طلای ۱۸: {len(geram_diffs)} برابر برابر، {sum(1 for d in geram_diffs if d != 0)} بار متفاوت")
    if any(d != 0 for d in geram_diffs):
        print(f"  اختلافات: {geram_diffs}")
