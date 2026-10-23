#!/usr/bin/env python3
"""
gold-tracker-bot/rich_db.py

Rich database for the new collector-based system.
Stores full market snapshots (not just prices) and evaluates forecasts properly.

Key difference from json_db.py:
  - Stores the FULL collector snapshot with each forecast (prices, technicals, news, hobab, calendar)
  - Evaluation checks: (a) direction correctness, (b) whether actual price fell within predicted range,
    (c) whether a BUY signal would have made money
  - Lessons are structured, not just string messages
"""

import json
import os
from datetime import datetime, timezone, timedelta

DB_PATH = "/root/gold-tracker-bot/rich_analysis_db.json"
TZ_TEHRAN = timezone(timedelta(hours=3, minutes=30))


def load_db() -> dict:
    if not os.path.exists(DB_PATH):
        return {"snapshots": [], "forecasts": [], "lessons": [], "stats": {}}
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"snapshots": [], "forecasts": [], "lessons": [], "stats": {}}


def save_db(db: dict) -> None:
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def now_iso() -> str:
    return datetime.now(TZ_TEHRAN).isoformat()


def add_snapshot(db: dict, collector_data: dict) -> None:
    """Store a collector snapshot. Keep last 1000."""
    entry = {
        "timestamp": now_iso(),
        "data": collector_data,
    }
    db.setdefault("snapshots", []).append(entry)
    if len(db["snapshots"]) > 1000:
        db["snapshots"] = db["snapshots"][-1000:]
    save_db(db)


def add_forecast(
    db: dict,
    forecast_text: str,
    snapshot: dict,
    predicted_direction: str,
    predicted_ranges: dict,
    buy_signal: str,
    confidence: int,
    reasoning: str,
) -> None:
    """Store a forecast with full context for later evaluation.

    predicted_ranges: {"4h": {"min": int, "max": int, "direction": str}, "24h": {...}, "1w": {...}}
    buy_signal: "STRONG_BUY" | "BUY" | "WAIT" | "STRONG_WAIT"
    confidence: 0-100
    reasoning: one-line explanation
    """
    entry = {
        "timestamp": now_iso(),
        "forecast_text": forecast_text,
        "snapshot": snapshot,
        "predicted_direction": predicted_direction,
        "predicted_ranges": predicted_ranges,
        "buy_signal": buy_signal,
        "confidence": confidence,
        "reasoning": reasoning,
        "evaluation": None,
    }
    db.setdefault("forecasts", []).append(entry)
    if len(db["forecasts"]) > 2000:
        db["forecasts"] = db["forecasts"][-2000:]
    save_db(db)


def evaluate_forecasts(db: dict, current_prices: dict) -> list[dict]:
    """Evaluate unevaluated forecasts that are old enough (4h minimum).

    Evaluation checks 3 things:
    1. Direction: was the predicted direction (soodi/nozoli/nowsani) correct?
    2. Range: did the actual mazneh price fall within the predicted 24h range?
    3. Buy signal: if we said BUY/WAIT, was it the right call?

    Returns list of evaluation results.
    """
    results = []
    now = datetime.now(TZ_TEHRAN)
    current_mazneh = current_prices.get("mazneh") or current_prices.get("mesghal") or 0

    if current_mazneh == 0:
        return results

    for f in db.get("forecasts", []):
        if f.get("evaluation") is not None:
            continue

        age_hours = (now - datetime.fromisoformat(f["timestamp"])).total_seconds() / 3600
        if age_hours < 4:
            continue

        snap_prices = f.get("snapshot", {}).get("prices", {})
        old_mazneh = snap_prices.get("mazneh") or snap_prices.get("mesghal") or 0
        if old_mazneh == 0:
            continue

        actual_change_pct = ((current_mazneh - old_mazneh) / old_mazneh) * 100

        # --- 1. Direction evaluation ---
        pred_dir = f.get("predicted_direction", "نوسانی")
        if pred_dir == "صعودی":
            dir_correct = actual_change_pct > 0.3
            dir_result = "WIN" if dir_correct else "LOSS" if actual_change_pct < -0.3 else "NEUTRAL"
        elif pred_dir == "نزولی":
            dir_correct = actual_change_pct < -0.3
            dir_result = "WIN" if dir_correct else "LOSS" if actual_change_pct > 0.3 else "NEUTRAL"
        else:  # نوسانی
            dir_result = "WIN" if -2 < actual_change_pct < 2 else "NEUTRAL"

        # --- 2. Range evaluation (24h range) ---
        range_24h = f.get("predicted_ranges", {}).get("24h", {})
        range_result = None
        if range_24h and range_24h.get("min") and range_24h.get("max"):
            in_range = range_24h["min"] <= current_mazneh <= range_24h["max"]
            range_result = "IN_RANGE" if in_range else "OUT_RANGE"

        # --- 3. Buy signal evaluation ---
        buy_signal = f.get("buy_signal", "WAIT")
        buy_result = None
        if buy_signal in ("STRONG_BUY", "BUY"):
            # BUY was correct if price went up or stayed flat (didn't drop)
            buy_result = "CORRECT" if actual_change_pct > -0.3 else "WRONG"
        elif buy_signal in ("STRONG_WAIT", "WAIT"):
            # WAIT was correct if price dropped or stayed flat (didn't rise significantly)
            buy_result = "CORRECT" if actual_change_pct < 0.3 else "WRONG"

        # --- Overall score ---
        # Direction matters most (50%), range (30%), buy signal (20%)
        dir_score = 1 if dir_result == "WIN" else 0.5 if dir_result == "NEUTRAL" else 0
        range_score = 1 if range_result == "IN_RANGE" else 0 if range_result == "OUT_RANGE" else 0.5
        buy_score = 1 if buy_result == "CORRECT" else 0 if buy_result == "WRONG" else 0.5
        overall = dir_score * 0.5 + range_score * 0.3 + buy_score * 0.2

        eval_entry = {
            "evaluated_at": now_iso(),
            "age_hours": round(age_hours, 1),
            "old_mazneh": old_mazneh,
            "current_mazneh": current_mazneh,
            "actual_change_pct": round(actual_change_pct, 2),
            "direction_result": dir_result,
            "range_result": range_result,
            "buy_result": buy_result,
            "overall_score": round(overall, 2),
        }
        f["evaluation"] = eval_entry
        results.append(eval_entry)

        # --- Generate lesson if score is low ---
        if overall < 0.5:
            lesson = _generate_lesson(f, eval_entry)
            if lesson:
                db.setdefault("lessons", []).append(lesson)
                if len(db["lessons"]) > 50:
                    db["lessons"] = db["lessons"][-50:]

    save_db(db)
    return results


def _generate_lesson(forecast: dict, eval: dict) -> dict:
    """Generate a structured lesson from a failed forecast."""
    snap = forecast.get("snapshot", {})
    prices = snap.get("prices", {})
    technicals = snap.get("geram18_technicals", {})
    news_count = snap.get("news_total", 0)
    hobab = snap.get("hobab", {})

    return {
        "timestamp": forecast["timestamp"][:16],
        "predicted_direction": forecast.get("predicted_direction", ""),
        "buy_signal": forecast.get("buy_signal", ""),
        "actual_change": eval["actual_change_pct"],
        "overall_score": eval["overall_score"],
        "direction_result": eval["direction_result"],
        "range_result": eval["range_result"],
        "buy_result": eval["buy_result"],
        "mazneh_at_forecast": eval["old_mazneh"],
        "rsi_at_forecast": technicals.get("rsi14", 0),
        "dist_from_week_high": technicals.get("dist_from_week_high_pct", 0),
        "hobab_pct": hobab.get("geram18_hobab_pct", hobab.get("hobab_pct", 0)),
        "news_count": news_count,
        "reasoning": forecast.get("reasoning", "")[:200],
        "lesson": _diagnose_failure(forecast, eval, technicals, hobab),
    }


def _diagnose_failure(forecast: dict, eval: dict, technicals: dict, hobab: dict) -> str:
    """Diagnose WHY a forecast failed based on market context at the time."""
    reasons = []
    pred = forecast.get("predicted_direction", "")
    actual = eval["actual_change_pct"]
    rsi = technicals.get("rsi14", 50)
    dist_high = technicals.get("dist_from_week_high_pct", 0)
    hobab_pct = hobab.get("geram18_hobab_pct", hobab.get("hobab_pct", 0))

    if pred == "صعودی" and actual < -0.5:
        if rsi > 65:
            reasons.append(f"RSI={rsi} بالا بود — بازار اشباع خرید")
        if dist_high > -1:
            reasons.append(f"قیمت نزدیک سقف هفته بود ({dist_high:+.1f}%) — احتمال اصلاح")
        if hobab_pct > 5:
            reasons.append(f"حباب بالا بود ({hobab_pct}%) — ریسک کاهش حباب")
        if not reasons:
            reasons.append("سیگنال اخبار اشتباه تفسیر شد — تحلیل اخبار بازنگری شود")

    elif pred == "نزولی" and actual > 0.5:
        if rsi < 35:
            reasons.append(f"RSI={rsi} پایین بود — بازار اشباع فروش، احتمال بازگشت")
        if dist_high < -5:
            reasons.append(f"قیمت از سقف هفته دور بود ({dist_high:+.1f}%) — فضای رشد وجود داشت")

    elif eval.get("range_result") == "OUT_RANGE":
        reasons.append(f"بازه پیش‌بینی خیلی تنگ بود — قیمت واقعی از بازه خارج شد ({actual:+.1f}%)")

    if not reasons:
        reasons.append("علت روشن نیست — بررسی دستی context لازم است")

    return " | ".join(reasons)


def get_performance_stats(db: dict) -> dict:
    """Get comprehensive performance stats."""
    evaluated = [f for f in db.get("forecasts", []) if f.get("evaluation")]
    if not evaluated:
        return {"total": 0}

    stats = {"total": len(evaluated)}

    # Direction accuracy
    for d in ["صعودی", "نزولی", "نوسانی"]:
        dir_f = [f for f in evaluated if f["evaluation"]["direction_result"] and f.get("predicted_direction") == d]
        if dir_f:
            wins = sum(1 for f in dir_f if f["evaluation"]["direction_result"] == "WIN")
            losses = sum(1 for f in dir_f if f["evaluation"]["direction_result"] == "LOSS")
            stats[f"dir_{d}"] = {"total": len(dir_f), "wins": wins, "losses": losses,
                                  "rate": round(wins / len(dir_f) * 100, 1)}

    # Range accuracy
    in_range = sum(1 for f in evaluated if f["evaluation"].get("range_result") == "IN_RANGE")
    out_range = sum(1 for f in evaluated if f["evaluation"].get("range_result") == "OUT_RANGE")
    stats["range"] = {"in": in_range, "out": out_range, "rate": round(in_range / (in_range + out_range) * 100, 1) if (in_range + out_range) else 0}

    # Buy signal accuracy
    for sig in ["STRONG_BUY", "BUY", "WAIT", "STRONG_WAIT"]:
        sig_f = [f for f in evaluated if f.get("buy_signal") == sig]
        if sig_f:
            correct = sum(1 for f in sig_f if f["evaluation"].get("buy_result") == "CORRECT")
            stats[f"signal_{sig}"] = {"total": len(sig_f), "correct": correct,
                                       "rate": round(correct / len(sig_f) * 100, 1)}

    # Overall score average
    scores = [f["evaluation"]["overall_score"] for f in evaluated]
    stats["avg_score"] = round(sum(scores) / len(scores), 2)

    # RSI-based failure patterns
    rsi_failures = [f for f in evaluated if f["evaluation"]["overall_score"] < 0.5
                    and f.get("snapshot", {}).get("geram18_technicals", {}).get("rsi14")]
    if rsi_failures:
        stats["rsi_failure_avg"] = round(
            sum(f["snapshot"]["geram18_technicals"]["rsi14"] for f in rsi_failures) / len(rsi_failures), 1
        )

    return stats


if __name__ == "__main__":
    db = load_db()
    print("=== Rich DB Stats ===")
    print(f"Snapshots: {len(db.get('snapshots', []))}")
    print(f"Forecasts: {len(db.get('forecasts', []))}")
    print(f"Lessons: {len(db.get('lessons', []))}")
    print(f"Performance: {json.dumps(get_performance_stats(db), indent=2, ensure_ascii=False)}")
    print(f"\nLast 3 lessons:")
    for l in db.get("lessons", [])[-3:]:
        print(f"  {l.get('timestamp', '')} | {l.get('lesson', '')}")
