"""Data-derived statistical baseline and one Persian report template.

No fabricated news, calibrated confidence, or autonomous buy recommendations.
Ranges estimate endpoint prices, not intraperiod high/low coverage.
"""
from datetime import datetime
import hashlib
import json
import math
import statistics
import jdatetime
from config import TZ_TEHRAN, MAX_SNAPSHOT_AGE_SECONDS
from forecast_schema import prices, timestamp, number, HORIZONS
from tracker_store import performance

ASSETS = {'mazneh': ('مظنه', 'تومان'), 'geram18': ('طلای ۱۸ عیار', 'تومان'),
          'dollar': ('دلار آزاد', 'تومان'), 'usdt': ('تتر', 'تومان'),
          'gold_ounce': ('انس طلا', 'دلار'), 'silver_ounce': ('انس نقره', 'دلار'), 'sekee': ('سکه امامی', 'تومان')}


def validate_snapshot(snapshot, now=None):
    now = now or datetime.now(TZ_TEHRAN)
    age = (now-timestamp(snapshot['collection_time'])).total_seconds()
    if age < -60 or age > MAX_SNAPSHOT_AGE_SECONDS:
        raise ValueError('Collector snapshot is stale or in the future; collect fresh data')
    normalized = prices(snapshot.get('prices'))
    if 'geram18' not in normalized and 'mazneh' not in normalized:
        raise ValueError('No valid gold quote in collector snapshot')
    return {**snapshot, 'prices': normalized}


def statistical_ranges(current, history):
    rows = {}
    for row in history or []:
        try:
            close = number(row['close'])
            date = str(row['date']).replace('-', '/')
            if not date:
                continue
            rows[date] = close
        except (KeyError, ValueError, TypeError):
            continue
    ordered = sorted(rows.items())
    # Need at least 7 distinct daily closing observations. Do not invent data.
    if len(ordered) < 7:
        return {}
    closes = [x[1] for x in ordered][-30:]
    returns = [math.log(b/a) for a,b in zip(closes,closes[1:])]
    drift, volatility = statistics.mean(returns), statistics.stdev(returns)
    result = {}
    for horizon, hours in HORIZONS.items():
        days = hours / 24
        center = current * math.exp(drift * days)
        spread = volatility * math.sqrt(days)
        change = (center/current-1)*100
        result[horizon] = {'min': max(1, round(center*math.exp(-spread))),
                           'max': max(1, round(center*math.exp(spread))),
                           'direction': 'صعودی' if change > .3 else 'نزولی' if change < -.3 else 'نوسانی'}
    return result


def build_forecast(snapshot):
    asset = 'geram18' if snapshot['prices'].get('geram18') else 'mazneh'
    history = snapshot.get(asset+'_history') or snapshot.get(asset+'_recent_7d') or []
    ranges = statistical_ranges(snapshot['prices'][asset], history)
    for value in ranges.values():
        value['asset'] = asset
    identity = json.dumps([snapshot['collection_time'], asset, ranges, 'log-return-baseline-v1'], sort_keys=True)
    return {'id': hashlib.sha256(identity.encode()).hexdigest()[:32],
            'timestamp': snapshot['collection_time'], 'snapshot': snapshot, 'asset': asset,
            'predicted_ranges': ranges, 'predicted_direction': ranges.get('24h',{}).get('direction'),
            'buy_signal': None, 'confidence': None, 'model_version': 'log-return-baseline-v1',
            'reasoning': 'برآورد آماری بر پایه میانگین و انحراف معیار بازده روزانه؛ دقت کالیبره نشده است.'}


def fmt(value, decimals=0):
    return f'{value:,.{decimals}f}'.translate(str.maketrans('0123456789,', '۰۱۲۳۴۵۶۷۸۹،'))


def render(record, db):
    snap = record['snapshot']
    p = snap['prices']
    dt = jdatetime.datetime.fromgregorian(datetime=timestamp(record['timestamp'])).strftime('%Y/%m/%d %H:%M')
    lines = ['📊 تحلیل بازار طلا و ارز', '🕒 '+dt+' — تهران', '', '۱. قیمت و تغییر روزانه']
    for asset,(label,unit) in ASSETS.items():
        value = p.get(asset)
        if value is None:
            lines.append(f'• {label}: داده موجود نیست')
            continue
        change = snap.get('volatility',{}).get(asset,{}).get('change_pct')
        suffix = f' | تغییر {change:+.2f}٪' if isinstance(change,(float,int)) else ' | مبنای روزانه موجود نیست'
        lines.append(f'• {label}: {fmt(value,2 if unit=="دلار" else 0)} {unit}{suffix}')
    lines += ['', '۲. وضعیت تکنیکال']
    tech = snap.get(record['asset']+'_technicals') or {}
    rsi = tech.get('rsi14')
    if isinstance(rsi,(int,float)):
        state = 'اشباع خرید' if rsi > 70 else 'اشباع فروش' if rsi < 30 else 'میانه'
        lines.append(f'• RSI(14): {fmt(rsi,1)} — {state}')
    else:
        lines.append('• RSI: داده کافی موجود نیست')
    for period in (5,10,20):
        ma = tech.get(f'ma{period}')
        if ma:
            relation = 'بالاتر' if p[record['asset']] > ma else 'پایین‌تر' if p[record['asset']] < ma else 'برابر'
            lines.append(f'• MA{period}: {fmt(ma)} تومان؛ قیمت {relation} از میانگین')
    lines += ['', '۳. حباب نسبت به ارزش ذاتی']
    bubble = snap.get('hobab',{}).get('geram18_hobab_pct')
    lines.append(f'• طلای ۱۸ عیار: {bubble:+.2f}٪' if isinstance(bubble,(int,float)) else '• داده کافی موجود نیست')
    lines += ['', '۴. اخبار دریافت‌شده']
    news = []
    for source,posts in snap.get('news',{}).items():
        for post in posts[-2:]:
            headline = ' '.join(str(post).split())[:110]
            if headline and headline not in [x[1] for x in news]:
                news.append((source,headline))
    lines.extend([f'• @{source}: {headline}' for source,headline in news[:3]] or ['• خبری دریافت نشده است'])
    if news:
        lines.append('• نقل منبع؛ اثر جهت‌دار خبر و تازگی زمان انتشار تأیید نشده است.')
    lines += ['', f'۵. برآورد آماری قیمت پایانی — {ASSETS[record["asset"]][0]}']
    for horizon,label in [('4h','۴ ساعته'),('24h','۲۴ ساعته'),('1w','۱ هفته‌ای')]:
        bounds = record['predicted_ranges'].get(horizon)
        lines.append(f'• {label}: {bounds["direction"]} | {fmt(bounds["min"])} تا {fmt(bounds["max"])} تومان'
                     if bounds else f'• {label}: داده تاریخی کافی موجود نیست')
    lines += ['• برآورد آزمایشی؛ این بازه سطح اطمینان کالیبره‌شده ندارد.',
              '', '۶. تصمیم خرید و ریسک', '• سیگنال خرید خودکار صادر نشده؛ تصمیم نیازمند بررسی مستقل است.',
              '• ریسک: خطای منبع، داده قدیمی و تغییر ناگهانی بازار.', '', '۷. عملکرد ارزیابی معتبر']
    lines.append(f'• تعداد کل تحلیل‌های یکتا: {fmt(len(db.get("forecasts", [])))}')
    for horizon,label in [('4h','۴ ساعت'),('24h','۲۴ ساعت'),('1w','۱ هفته')]:
        stats = performance(db,horizon)
        if stats['total']:
            lines.append(f'• {label}: {stats["wins"]} برد از {stats["total"]} | دقت جهت {stats["win_rate"]}٪ | بدون نتیجه: {stats["unscored"]}')
        else:
            lines.append(f'• {label}: ارزیابی معتبر کافی نیست | بدون نتیجه: {stats["unscored"]}')
    lines.append('• نتایج قدیمی ناسازگار در آرشیو محفوظ‌اند و در این آمار شمرده نمی‌شوند.')
    text = '\n'.join(lines)
    # Telegram sendMessage has a 4096-character limit; preserve all sections.
    if len(text) > 4000:
        raise ValueError('Analysis exceeds Telegram message limit')
    return text
