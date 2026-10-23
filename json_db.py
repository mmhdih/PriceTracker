"""
gold-tracker-bot/json_db.py

مدیریت دیتابیس JSON برای ذخیره تاریخچه قیمت‌ها، پیش‌بینی‌ها و ارزیابی آن‌ها.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta

DB_PATH = "/root/gold-tracker-bot/analysis_db.json"

TZ_TEHRAN = timezone(timedelta(hours=3, minutes=30))

def load_db() -> dict:
    if not os.path.exists(DB_PATH):
        return {"price_history": [], "forecasts": [], "lessons": [], "daily_summaries": []}
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"price_history": [], "forecasts": [], "lessons": [], "daily_summaries": []}

def save_db(db: dict) -> None:
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

def now_iso() -> str:
    return datetime.now(TZ_TEHRAN).isoformat()

def add_price_entry(db: dict, prices: dict) -> None:
    entry = {
        "timestamp": now_iso(),
        "mesghal_toman": prices.get("mesghal", 0),
        "gold_18_toman": prices.get("geram18", 0) or prices.get("gold_18", 0),
        "usd_toman": prices.get("price_dollar_rl", 0) or prices.get("usd", 0),
        "tether_toman": prices.get("crypto-tether-irr", 0) or prices.get("tether", 0),
    }
    db["price_history"].append(entry)
    # فقط آخرین ۶۷۲ مورس (آخرین هفته) را نگه دار
    if len(db["price_history"]) > 672:
        db["price_history"] = db["price_history"][-672:]
    save_db(db)

def add_forecast(db: dict, forecast_text: str, snapshot_prices: dict, 
                 predicted_direction: str = None, predicted_4h: dict = None,
                 predicted_24h: dict = None) -> None:
    """ذخیره پیش‌بینی با جهت و اعداد مشخص برای ارزیابی بعدی."""
    entry = {
        "timestamp": now_iso(),
        "forecast_text": forecast_text,
        "snapshot_prices": snapshot_prices,
        "predicted_direction": predicted_direction,  # "صعودی" / "نزولی" / "نوسانی"
        "predicted_4h": predicted_4h,    # {"min": ..., "max": ..., "direction": ...}
        "predicted_24h": predicted_24h,   # {"min": ..., "max": ..., "direction": ...}
        "evaluation": None,  # بعداً پر می‌شود
    }
    db["forecasts"].append(entry)
    # فقط آخرین ۹۶ پیش‌بینی (ماه آخر) را نگه دار
    if len(db["forecasts"]) > 96:
        db["forecasts"] = db["forecasts"][-96:]
    save_db(db)

def evaluate_old_forecasts(db: dict, current_prices: dict) -> list[str]:
    """ارزیابی پیش‌بینی‌های قدیمی که هنوز ارزیابی نشده‌اند."""
    lessons = []
    for f in db["forecasts"]:
        if f["evaluation"] is not None:
            continue
        age_hours = (datetime.now(TZ_TEHRAN) - datetime.fromisoformat(f["timestamp"])).total_seconds() / 3600
        if age_hours < 4:
            continue  # هنوز زود است — حداقل ۴ ساعت باید بگذرد
        
        snap = f["snapshot_prices"]
        old_price = snap.get("geram18", 0) or snap.get("gold_18", 0)
        new_price = current_prices.get("geram18", 0) or current_prices.get("gold_18", 0)
        
        if not (old_price > 0 and new_price > 0):
            continue
        
        actual_change_pct = ((new_price - old_price) / old_price) * 100
        
        # تحلیل جهت پیش‌بینی: اول از فیلد جدید predicted_direction استفاده کن
        # اگر نبود، از متن forecast_text کلمات کلیدی رو استخراج کن
        pred_dir = f.get("predicted_direction", "")
        if not pred_dir:
            # fallback: استخراج از متن
            if "صعودی" in f["forecast_text"] or "افزایش" in f["forecast_text"]:
                pred_dir = "صعودی"
            elif "نزولی" in f["forecast_text"] or "کاهش" in f["forecast_text"]:
                pred_dir = "نزولی"
            else:
                pred_dir = "نوسانی"
        
        actual_up = actual_change_pct > 0.3
        actual_down = actual_change_pct < -0.3
        
        if pred_dir == "صعودی":
            result = "WIN" if actual_up else "LOSS" if actual_down else "NEUTRAL"
        elif pred_dir == "نزولی":
            result = "WIN" if actual_down else "LOSS" if actual_up else "NEUTRAL"
        else:  # نوسانی
            result = "WIN" if -2 < actual_change_pct < 2 else "NEUTRAL"
        
        f["evaluation"] = {
            "evaluated_at": now_iso(),
            "actual_change_pct": round(actual_change_pct, 2),
            "predicted_direction": pred_dir,
            "result": result,
            "age_hours": round(age_hours, 1),
        }
        
        if result == "LOSS":
            lesson = (
                f"LOSS at {f['timestamp'][:16]}: forecast='{pred_dir}' "
                f"but actual={actual_change_pct:+.2f}%. Review context."
            )
            lessons.append(lesson)
            db["lessons"].append(lesson)
    
    save_db(db)
    return lessons

def get_win_rate(db: dict) -> dict:
    evaluated = [f for f in db["forecasts"] if f.get("evaluation") is not None]
    if not evaluated:
        return {"total": 0, "wins": 0, "losses": 0, "neutral": 0, "win_rate": 0}
    wins = sum(1 for f in evaluated if f["evaluation"]["result"] == "WIN")
    losses = sum(1 for f in evaluated if f["evaluation"]["result"] == "LOSS")
    neutral = sum(1 for f in evaluated if f["evaluation"]["result"] == "NEUTRAL")
    total = len(evaluated)
    win_rate = (wins / total * 100) if total > 0 else 0
    return {
        "total": total,
        "win_rate": round(win_rate, 1),
        "wins": wins,
        "losses": losses,
        "neutral": neutral,
    }

if __name__ == "__main__":
    db = load_db()
    print("Current DB stats:")
    print(f"  Price entries: {len(db['price_history'])}")
    print(f"  Forecasts: {len(db['forecasts'])}")
    print(f"  Lessons: {len(db['lessons'])}")
    print(f"  Win rate: {get_win_rate(db)}")
