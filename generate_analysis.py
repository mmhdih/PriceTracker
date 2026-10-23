# -*- coding: utf-8 -*-
import json
import re

def convert_to_persian_digits(text):
    if text is None:
        return ""
    text = str(text)
    mapping = {
        '0': '۰', '1': '۱', '2': '۲', '3': '۳', '4': '۴',
        '5': '۵', '6': '۶', '7': '۷', '8': '۸', '9': '۹'
    }
    for eng, per in mapping.items():
        text = text.replace(eng, per)
    return text

def format_price(num):
    if num is None:
        return ""
    # Format with commas
    formatted = f"{int(num):,}"
    return convert_to_persian_digits(formatted)

def format_float(num, decimals=2):
    if num is None:
        return ""
    formatted = f"{num:.{decimals}f}"
    return convert_to_persian_digits(formatted)

# Load data
with open('/tmp/gold_collector_output.json') as f:
    data = json.load(f)

prices = data['prices']
vol = data['volatility']
g18_tech = data['geram18_technicals']
m_tech = data['mazneh_technicals']
hobab = data['hobab']
calendar = data['calendar']

shamsi_time = convert_to_persian_digits(data['shamsi_time'])

# Calculate overall win-rate from rich_analysis_db.json
import sys
sys.path.insert(0, '/root/gold-tracker-bot')
from rich_db import load_db, get_performance_stats

rich_db = load_db()
stats = get_performance_stats(rich_db)

# To ensure we show the accurate historical count without truncation,
# we use the rich db stats
total_evals = stats.get('total', 0)
wins = sum(stats.get(f'dir_{d}', {}).get('wins', 0) for d in ['صعودی', 'نزولی', 'نوسانی'])
win_rate_pct = (wins / total_evals) * 100 if total_evals > 0 else 0

# Prepare values - safely get prices with defaults
mazneh_val = format_price(prices.get('mazneh', 0))
mazneh_change = format_price(abs(vol['mazneh']['change'])) if vol.get('mazneh') else ""
mazneh_change_pct = format_float(abs(vol['mazneh']['change_pct']), 1) if vol.get('mazneh') else 0
mazneh_dir = "🟢 +" if vol['mazneh']['change'] >= 0 else "🔴 -"

geram_val = format_price(prices.get('geram18', 0))
geram_change = format_price(abs(vol['geram18']['change'])) if vol.get('geram18') else ""
geram_change_pct = format_float(abs(vol['geram18']['change_pct']), 1) if vol.get('geram18') else 0
geram_dir = "🟢 +" if vol['geram18']['change'] >= 0 else "🔴 -"

dollar_val = format_price(prices.get('dollar', 0))
dollar_change = format_price(abs(vol['dollar']['change'])) if vol.get('dollar') else ""
dollar_change_pct = format_float(abs(vol['dollar']['change_pct']), 1) if vol.get('dollar') else 0
dollar_dir = "🟢 +" if vol['dollar']['change'] >= 0 else "🔴 -"

usdt_val = format_price(prices.get('usdt', 0))
usdt_change = format_price(abs(vol['usdt']['change'])) if vol.get('usdt') else ""
usdt_change_pct = format_float(abs(vol['usdt']['change_pct']), 1) if vol.get('usdt') else 0
usdt_dir = "🟢 +" if vol['usdt']['change'] >= 0 else "🔴 -"

ounce_val = format_float(prices.get('gold_ounce', 0), 2)
ounce_change_pct = format_float(abs(vol['gold_ounce']['change_pct']), 2) if vol.get('gold_ounce') else 0
ounce_dir = "🟢 +" if vol['gold_ounce']['change'] >= 0 else "🔴 -"

silver_val = format_float(prices.get('silver_ounce', 0), 2)

# sekee - handle missing gracefully
sekee_val = format_price(prices.get('sekee', 0))

rsi = format_float(g18_tech['rsi14'], 1)
rsi_emoji = "🔴" if g18_tech['rsi14'] > 70 else ("🟢" if g18_tech['rsi14'] < 30 else "⚪")

dist_high = format_float(abs(g18_tech['dist_from_week_high_pct']), 2)
dist_low = format_float(g18_tech['dist_from_week_low_pct'], 2)

hobab_g18_pct = format_float(hobab['geram18_hobab_pct'], 2)
hobab_g18_val = format_price(abs(hobab['geram18_hobab']))
hobab_g18_sign = "منفی (ارزنده)" if hobab['geram18_hobab_pct'] < 0 else "مثبت (حباب‌دار)"
hobab_g18_emoji = "🟢" if hobab['geram18_hobab_pct'] < 0 else "🔴"

hobab_sekee_pct = format_float(hobab.get('sekee', {}).get('hobab_pct', 0), 2)
hobab_sekee_val = format_price(abs(hobab.get('sekee', {}).get('hobab_amount', 0)))

# Build Persian text
lines = []
lines.append(f"🔔 تحلیل جامع بازار طلا | {shamsi_time}")
lines.append("")
lines.append("📊 آخرین وضعیت نرخ‌ها و تغییرات امروز:")
lines.append(f"🏆 مظنه تهران: {mazneh_val} تومان ({mazneh_dir}{mazneh_change_pct}٪ | {mazneh_change} تومان)")
lines.append(f"🥈 طلای ۱۸ عیار: {geram_val} تومان ({geram_dir}{geram_change_pct}٪ | {geram_change} تومان)")
lines.append(f"💵 دلار آزاد: {dollar_val} تومان ({dollar_dir}{dollar_change_pct}٪ | {dollar_change} تومان)")
lines.append(f"💲 تتر: {usdt_val} تومان ({usdt_dir}{usdt_change_pct}٪ | {usdt_change} تومان)")
lines.append(f"🪙 سکه امامی: {sekee_val} تومان")
lines.append(f"🌍 انس جهانی طلا: {ounce_val} دلار ({ounce_dir}{ounce_change_pct}٪)")
lines.append(f"⚪ انس جهانی نقره: {silver_val} دلار")
lines.append("")
lines.append("📈 وضعیت فنی بازار (تکنیکال):")
lines.append(f"📊 شاخص قدرت نسبی (RSI): {rsi} ({rsi_emoji} اشباع خرید - ریسک اصلاح موقت بالا)")
lines.append(f"📈 موقعیت نسبت به میانگین‌ها: قیمت بالاتر از میانگین ۲۰ روزه قرار دارد (روند صعودی میان‌مدت)")
lines.append(f"📏 باندهای بولینگر: قیمت در نزدیکی سقف باند قرار دارد که نشان‌دهنده فشار خرید شدید است.")
lines.append(f"🎯 فاصله از سقف هفتگی: {dist_high}٪ | فاصله از کف هفتگی: {dist_low}٪ (قیمت در اوج هفتگی قرار دارد 🔴)")
lines.append("")
lines.append("🎈 تحلیل حباب (ارزش ذاتی):")
lines.append(f"💎 ارزش ذاتی طلای ۱۸ عیار: {format_price(hobab['intrinsic_geram18'])} تومان")
lines.append(f"🎈 حباب طلای ۱۸ عیار: {hobab_g18_val} تومان ({hobab_g18_pct}٪) {hobab_g18_emoji} حباب {hobab_g18_sign}")
lines.append(f"🪙 حباب سکه امامی: {hobab_sekee_val} تومان ({hobab_sekee_pct}٪)")
lines.append("")
lines.append("📰 تحلیل مهم‌ترین اخبار و جریان‌های بازار:")
lines.append("- فروش ۱۰ هزار دلار ارزی با نرخ ۲۵۹ هزار تومان در بانک‌ها آغاز شد → کنترل موقت و تعدیل هیجانات بازار ارز 🔴")
lines.append("- دولت کانادا رسما سپاه پاسداران را در فهرست تروریستی قرار داد → افزایش تنش‌های دیپلماتیک و نااطمینانی‌های سیاسی 🟢")
lines.append("- حملات هوایی سنگین به صنعا در یمن و تشدید درگیری‌ها → تداوم تنش‌های ژئوپلیتیک منطقه‌ای 🟢")
lines.append("- اظهارات تند نتانیاهو علیه ایران و ادعای بی‌ثباتی حاکمیت → تقویت تقاضای پناهگاه امن در بازار داخلی طلا 🟢")
lines.append("- روسیه از شهروندانش خواست پایتخت اوکراین را ترک کنند → تشدید تنش‌های ژئوپلیتیک جهانی و پایداری قیمت انس 🟢")
lines.append("")
lines.append("🎯 پیش‌بینی روند و محدوده قیمت‌ها:")
lines.append("🕒 بازه ۴ ساعته: 🟡 نوسانی (انتظار اصلاح جزئی پس از رشد سریع امروز)")
lines.append(f"   ▫️ محدوده نوسان: {format_price(113800000)} تا {format_price(114800000)} تومان")
lines.append("📅 بازه ۲۴ ساعته: 🟢 صعودی (تداوم تقاضا به دلیل تنش‌های سیاسی شدید)")
lines.append(f"   ▫️ محدوده نوسان: {format_price(113500000)} تا {format_price(115500000)} تومان")
lines.append("📅 بازه ۱ هفته: 🟢 صعودی (روند صعودی دلار و حباب منفی طلا محرک خرید هستند)")
lines.append(f"   ▫️ محدوده نوسان: {format_price(112500000)} تا {format_price(116500000)} تومان")
lines.append("")
lines.append("🟢 سیگنال معاملاتی پیشنهادی خرید:")
lines.append(f"🟡 سیگنال: صبر و خرید پله‌ای (WAIT) - میزان اطمینان: {convert_to_persian_digits(70)}٪")
lines.append("")
lines.append("💡 دلایل تحلیل:")
lines.append("۱. قیمت در سقف هفتگی قرار دارد و شاخص RSI در منطقه اشباع خرید (۷۷.۲) است که ریسک ورود تک‌ضرب را بالا می‌برد.")
lines.append("۲. حباب طلای ۱۸ عیار همچنان منفی (-۱.۲۵٪) است که نشان‌دهنده ارزندگی بنیادی آن نسبت به انس و دلار است.")
lines.append("۳. آغاز عرضه مداخله‌ای دلار توسط بانک مرکزی می‌تواند مانع از جهش ناگهانی دلار در کوتاه‌مدت شود.")
lines.append("۴. شدت گرفتن تنش‌های منطقه‌ای و جهانی (کانادا، یمن و اوکراین) محرک قوی برای تداوم تقاضای پناهگاه امن است.")
lines.append("")
lines.append("📋 عملکرد سیستم و یادگیری‌ها:")
lines.append(f"🎓 میزان موفقیت کل پیش‌بینی‌ها: {format_float(win_rate_pct, 1)}٪ ({convert_to_persian_digits(wins)} برد از مجموع {convert_to_persian_digits(total_evals)} ارزیابی)")
lines.append(f"🎓 درس آموخته اخیر: خرید در سقف‌های قیمتی زمانی که RSI بالای ۸۵ است ریسک بالایی دارد (میانگین شکست رسی: ۸۹.۰)")

analysis_text = "\n".join(lines)

with open('/tmp/final_forecast.txt', 'w', encoding='utf-8') as f:
    f.write(analysis_text)

print("Saved successfully to /tmp/final_forecast.txt")
