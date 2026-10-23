#!/bin/bash
# Full gold analysis cron runner - evaluate + fetch + save
# Each step is independently fault-tolerant

# Step 0: Run collector.py to refresh /tmp/gold_collector_output.json with FRESH data
python3 /root/gold-tracker-bot/collector.py > /tmp/collector_stdout.txt 2>/tmp/collector_stderr.txt || true

# Step 1: Run analyze.py (evaluates old forecasts, fetches prices, saves placeholder forecast)
python3 /root/gold-tracker-bot/analyze.py > /tmp/final_forecast.txt 2>&1 || true

# Step 2: Run news scraper
python3 /root/gold-tracker-bot/news_scraper.py 2>&1 || true

# Step 3: Generate forecast stats (real numbers for both gold and dollar)
python3 /root/gold-tracker-bot/generate_forecast.py 2>&1 || true
python3 /root/gold-tracker-bot/generate_dollar_forecast.py 2>&1 || true

# Step 4: Win rate from rich DB
python3 -c "
import sys, os, json
sys.path.insert(0, '/root/gold-tracker-bot')
from rich_db import load_db, get_performance_stats

try:
    db = load_db()
    stats = get_performance_stats(db)
    total = stats.get('total', 0)
    if total > 0:
        winrate_val = stats.get('dir_صعودی', {}).get('rate', 0) # Use soodi rate as main winrate
        print(f'total={total} win_rate={winrate_val:.1f}%')
    else:
        print('no data')
except Exception as e:
    print(f'winrate error: {e}', file=sys.stderr)
" 2>&1 || true

# Step 5: Evaluate forecasts in rich DB and save the final forecast
python3 -c "
import sys, os
sys.path.insert(0, '/root/gold-tracker-bot')
from rich_db import load_db, evaluate_forecasts
from price_tracker import _fetch_all_prices, _to_toman

db = load_db()
all_prices = _fetch_all_prices()
prices = {}
if all_prices.get('mazneh') is not None:
    prices['mazneh'] = _to_toman(all_prices['mazneh'])
elif all_prices.get('geram18') is not None:
    prices['geram18'] = _to_toman(all_prices['geram18'])

if prices:
    results = evaluate_forecasts(db, prices)
    print(f'[rich_db] Evaluated {len(results)} forecasts', file=sys.stderr)
" 2>&1 || true

python3 /root/gold-tracker-bot/save_final_forecast.py /tmp/final_forecast.txt 2>&1 || true
