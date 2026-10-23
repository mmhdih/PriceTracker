#!/usr/bin/env python3
"""Save final forecast to rich DB with extracted metadata."""
import sys, os, re, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rich_db import load_db, add_forecast, save_db

if len(sys.argv) < 2:
    print("Usage: save_rich_forecast.py <forecast_file>", file=sys.stderr)
    sys.exit(1)

with open(sys.argv[1], "r", encoding="utf-8") as f:
    text = f.read().strip()

if not text:
    print("Empty forecast", file=sys.stderr)
    sys.exit(1)

# Load collector output
collector_path = "/tmp/gold_collector_output.json"
collector = {}
if os.path.exists(collector_path):
    with open(collector_path) as f:
        try:
            collector = json.load(f)
        except:
            pass

# Extract direction, buy signal, confidence, range from Persian text
def fa_to_int(s):
    for a, b in zip("۰۱۲۳۴۵۶۷۸۹", "0123456789"):
        s = s.replace(a, b)
    return int(s.replace("،", "").replace(",", "").replace(" ", "").strip())

# Clean text of markdown bold/italics and extra spaces to make regex reliable
clean_text = text.replace("*", "").replace("_", "")

dir_match = re.search(r"۲۴ ساعت.*?[:\s]*(صعودی|نزولی|نوسانی)", clean_text)
signal_match = re.search(r"سیگنال خرید.*?([A-Za-z\_ ]+)", clean_text)
if not signal_match:
    signal_match = re.search(r"خرید.*?([A-Z][A-Z\_]+)", clean_text)
conf_match = re.search(r"اطمینان[:\s]*([۰۱۲۳۴۵۶۷۸۹\d]+)", clean_text)
range_match = re.search(r"۲۴ ساعت.*?(?:بازه|محدوده)[:\s]*([۰-۹\d\u060c،,\s]+)\s*(?:تا|—|-)\s*([۰-۹\d\u060c،,\s]+)", clean_text)

direction = dir_match.group(1) if dir_match else "نوسانی"

# Extract buy signal: find the exact line containing "BUY" or "STRONG BUY" or "WAIT" or "STRONG WAIT" under a signal header
signal_raw = "WAIT"
for line in clean_text.split("\n"):
    if "سیگنال" in line or "SIGNAL" in line or "BUY" in line or "WAIT" in line:
        line_upper = line.upper()
        if "STRONG BUY" in line_upper or "STRONG_BUY" in line_upper:
            signal_raw = "STRONG_BUY"
            break
        elif "STRONG WAIT" in line_upper or "STRONG_WAIT" in line_upper:
            signal_raw = "STRONG_WAIT"
            break
        elif "BUY" in line_upper:
            signal_raw = "BUY"
            break
        elif "WAIT" in line_upper:
            signal_raw = "WAIT"
            break

confidence = int(fa_to_int(conf_match.group(1))) if conf_match else 50

ranges = {}
if range_match:
    try:
        ranges["24h"] = {"min": fa_to_int(range_match.group(1)), "max": fa_to_int(range_match.group(2)), "direction": direction}
    except:
        pass

# Extract reasoning
reasoning = ""
reasons_match = re.search(r"دلایل.*?\n(.*)", text, re.DOTALL)
if reasons_match:
    reasoning = reasons_match.group(1)[:300]

db = load_db()
add_forecast(db, text, collector, direction, ranges, signal_raw, confidence, reasoning)
print(f"Saved to rich DB: direction={direction}, signal={signal_raw}, confidence={confidence}%")
