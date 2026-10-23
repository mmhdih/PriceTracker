#!/usr/bin/env python3
"""
gold-tracker-bot/cron_run_v2.sh — MATLAB-style runner
Runs analyze.py, then generate_forecast.py for stats, feeds both to LLM.
"""

import subprocess
import sys
import os
import re

# Step 1: Run analyze.py
print("=== STEP 1: analyze.py ===", file=sys.stderr)
analyze_result = subprocess.run(
    [sys.executable, "/root/gold-tracker-bot/analyze.py"],
    capture_output=True, text=True, timeout=60
)
analysis_text = analyze_result.stdout
if analyze_result.returncode != 0:
    print(f"[analyze.py error] {analyze_result.stderr}", file=sys.stderr)

# Step 2: Run news_scraper.py
print("=== STEP 2: news_scraper.py ===", file=sys.stderr)
news_result = subprocess.run(
    [sys.executable, "/root/gold-tracker-bot/news_scraper.py"],
    capture_output=True, text=True, timeout=30
)

# Step 3: Generate forecast stats (real numbers for LLM to use)
print("=== STEP 3: generate_forecast.py ===", file=sys.stderr)
forecast_result = subprocess.run(
    [sys.executable, "/root/gold-tracker-bot/generate_forecast.py"],
    capture_output=True, text=True, timeout=30
)
forecast_stats = forecast_result.stdout if forecast_result.returncode == 0 else ""

# Step 4: Win rate
print("=== STEP 4: win-rate ===", file=sys.stderr)
try:
    with open("/root/gold-tracker-bot/analysis_db.json") as f:
        db = json.load(f)
    ev = [f for f in db.get("forecasts", []) if f.get("evaluation")]
    w = sum(1 for f in ev if f["evaluation"]["result"] == "WIN")
    l = sum(1 for f in ev if f["evaluation"]["result"] == "LOSS")
    n = sum(1 for f in ev if f["evaluation"]["result"] == "NEUTRAL")
    rate = w / len(ev) * 100 if ev else 0
    winrate = f"total={len(ev)} win={w} loss={l} neutral={n} rate={rate:.1f}%"
except:
    winrate = "no data"

# Output everything for the LLM to use
print(">>>RAW_ANALYSIS>>>")
print(analysis_text)
print(">>>FORECAST_STATS>>>")
print(forecast_stats)
print(">>>NEWS>>>")
print(news_result.stdout)
print(">>>WINRATE>>>")
print(winrate)
print(">>>END<<<")
