#!/usr/bin/env python3
"""
gold-tracker-bot/self_improve.py

Self-improvement loop for gold forecast system.
Analyzes win/loss patterns from analysis_db.json, extracts actionable lessons,
and recommends threshold adjustments for generate_forecast.py.

Usage:
  python3 self_improve.py          # Quick post-cycle summary
  python3 self_improve.py --full   # Detailed pattern report
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from collections import Counter

DB_PATH = "/root/gold-tracker-bot/analysis_db.json"
TZ_TEHRAN = timezone(timedelta(hours=3, minutes=30))

DIRECTIONS = ["صعودی", "نزولی", "نوسانی"]


def load_db():
    with open(DB_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def compute_stats(db):
    """Compute win/loss stats by direction and overall."""
    evaluated = [f for f in db.get("forecasts", []) if f.get("evaluation")]
    if not evaluated:
        return {"total": 0, "by_direction": {}, "overall_rate": 0}

    stats = {"total": len(evaluated), "by_direction": {}, "losses": [], "wins": []}

    for d in DIRECTIONS:
        dir_f = [f for f in evaluated if f["evaluation"]["predicted_direction"] == d]
        if not dir_f:
            stats["by_direction"][d] = {"total": 0, "wins": 0, "losses": 0, "neutral": 0, "rate": 0}
            continue
        w = sum(1 for f in dir_f if f["evaluation"]["result"] == "WIN")
        l = sum(1 for f in dir_f if f["evaluation"]["result"] == "LOSS")
        n = sum(1 for f in dir_f if f["evaluation"]["result"] == "NEUTRAL")
        stats["by_direction"][d] = {
            "total": len(dir_f), "wins": w, "losses": l, "neutral": n,
            "rate": round(w / len(dir_f) * 100, 1),
        }

    stats["overall_rate"] = round(
        sum(1 for f in evaluated if f["evaluation"]["result"] == "WIN") / len(evaluated) * 100, 1
    )
    stats["losses"] = [f for f in evaluated if f["evaluation"]["result"] == "LOSS"]
    stats["wins"] = [f for f in evaluated if f["evaluation"]["result"] == "WIN"]
    return stats


def analyze_loss_patterns(losses):
    """Extract common patterns from losses."""
    patterns = {"by_hour": Counter(), "by_price_level": [], "by_direction": Counter(), "details": []}

    for f in losses:
        ev = f["evaluation"]
        ts = f["timestamp"]
        hour = int(ts[11:13])
        patterns["by_hour"][hour] += 1
        patterns["by_direction"][ev["predicted_direction"]] += 1

        snap = f.get("snapshot_prices", {})
        mesghal = snap.get("mesghal", 0) or snap.get("mazneh", 0) or 0
        patterns["by_price_level"].append(mesghal)

        patterns["details"].append({
            "timestamp": ts[:16],
            "predicted": ev["predicted_direction"],
            "actual_pct": ev["actual_change_pct"],
            "mesghal": mesghal,
            "age_hours": ev.get("age_hours", 0),
        })

    return patterns


def generate_lessons(stats, loss_patterns):
    """Generate actionable lessons from patterns."""
    lessons = []
    by_dir = stats["by_direction"]

    # Lesson: صعودی weakness
    if by_dir.get("صعودی", {}).get("losses", 0) > 3:
        rate = by_dir["صعودي" if "صعودي" in by_dir else "صعودی"].get("rate", 0)
        if rate < 35:
            lessons.append(
                f"THRESHOLD: صعودی win rate={rate}% < 35%. Raise threshold from 0.6 to 0.8 in generate_forecast.py line ~132."
            )

    # Lesson: نوسانی strength
    nosani = by_dir.get("نوسانی", {})
    if nosani.get("total", 0) > 10 and nosani.get("rate", 0) > 80:
        lessons.append(
            f"STRENGTH: نوسانی win rate={nosani['rate']}%. Keep as default when uncertain."
        )

    # Lesson: evening losses
    evening_losses = sum(c for h, c in loss_patterns["by_hour"].items() if h >= 18)
    if evening_losses > 4:
        lessons.append(
            f"TIME: {evening_losses} losses at 18:00-24:00. Consider more conservative direction in evening forecasts."
        )

    # Individual loss lessons
    for d in loss_patterns["details"][-5:]:
        if d["actual_pct"] < -2:
            lessons.append(
                f"LOSS {d['timestamp']}: pred={d['predicted']} actual={d['actual_pct']:+.1f}%. Large drop missed — check if news indicated bearish shift."
            )

    return lessons


def recommend_adjustments(stats):
    """Recommend specific generate_forecast.py threshold changes."""
    recs = []
    by_dir = stats["by_direction"]

    soodi = by_dir.get("صعودی", {})
    if soodi.get("total", 0) > 10 and soodi.get("rate", 100) < 35:
        recs.append("Raise صعودی threshold: avg_return_pct > 0.6 → > 0.8 (line ~132)")

    nozoli = by_dir.get("نزولی", {})
    if nozoli.get("total", 0) > 3 and nozoli.get("rate", 100) < 40:
        recs.append("Widen نزولی threshold: avg_return_pct < -0.6 → < -0.8")

    overall = stats.get("overall_rate", 0)
    if overall < 55:
        recs.append("Overall win rate < 55% — consider defaulting to نوسانی more aggressively")

    if not recs:
        recs.append("No threshold changes needed — current parameters performing within acceptable range.")

    return recs


def main():
    full = "--full" in sys.argv
    db = load_db()
    stats = compute_stats(db)

    if stats["total"] == 0:
        print("No evaluated forecasts yet — nothing to analyze.")
        return

    print(f"=== GOLD FORECAST SELF-IMPROVEMENT REPORT ===")
    print(f"Generated: {datetime.now(TZ_TEHRAN).strftime('%Y/%m/%d %H:%M')} Tehran")
    print()

    # Overall stats
    print(f"Overall Win Rate: {stats['overall_rate']}% ({stats['total']} evaluated)")
    print()
    print("By Direction:")
    for d in DIRECTIONS:
        s = stats["by_direction"].get(d, {})
        if s.get("total", 0) > 0:
            print(f"  {d}: {s['wins']}W / {s['losses']}L / {s['neutral']}N = {s['rate']}%")
        else:
            print(f"  {d}: no data")
    print()

    # Loss patterns
    losses = stats.get("losses", [])
    if losses:
        lp = analyze_loss_patterns(losses)
        print(f"=== LOSS PATTERNS ({len(losses)} total) ===")
        print("By Hour (Tehran):")
        for h, c in sorted(lp["by_hour"].items()):
            print(f"  {h:02d}:00 → {c}")
        print()

        if full:
            print("Loss Details:")
            for d in lp["details"]:
                print(f"  {d['timestamp']} | pred={d['predicted']:6s} | actual={d['actual_pct']:+.2f}% | mesghal={d['mesghal']:,}")
            print()

    # Generate and save lessons
    new_lessons = generate_lessons(stats, analyze_loss_patterns(losses) if losses else {"by_hour": Counter(), "details": []})

    if new_lessons:
        print("=== NEW LESSONS ===")
        for l in new_lessons:
            print(f"  - {l}")

        # Append to DB
        existing = set(db.get("lessons", []))
        added = 0
        for l in new_lessons:
            if l not in existing:
                db.setdefault("lessons", []).append(l)
                added += 1

        # Prune to last 20
        if len(db.get("lessons", [])) > 20:
            db["lessons"] = db["lessons"][-20:]

        with open(DB_PATH, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
        print(f"\nSaved {added} new lessons to DB (total: {len(db.get('lessons', []))})")
    else:
        print("No new lessons to extract.")
    print()

    # Recommendations
    print("=== THRESHOLD RECOMMENDATIONS ===")
    for r in recommend_adjustments(stats):
        print(f"  → {r}")
    print()

    # Existing lessons
    existing_lessons = db.get("lessons", [])
    if existing_lessons and full:
        print("=== EXISTING LESSONS (last 5) ===")
        for l in existing_lessons[-5:]:
            print(f"  - {l[:120]}")


if __name__ == "__main__":
    main()
