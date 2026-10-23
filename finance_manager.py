#!/usr/bin/env python3
"""
gold-tracker-bot/finance_manager.py

مدیریت امور مالی شخصی حاج مهدی.
امکان ثبت درآمد، هزینه، اهداف مالی و تولید گزارش ماهانه شمسی در روز ۳۰ام هر ماه.
"""

from __future__ import annotations

import json
import os
import sys
import re
from datetime import datetime, timezone, timedelta
import jdatetime

FINANCE_DB_PATH = "/root/gold-tracker-bot/finance_db.json"
TZ_TEHRAN = timezone(timedelta(hours=3, minutes=30))

DEFAULT_DB = {
    "transactions": [],
    "goals": [],
    "monthly_budgets": {},  # سقف بودجه برای دسته‌بندی‌ها
    "savings_rate_target": 20  # درصد هدف پس‌انداز
}


def load_finance_db() -> dict:
    if not os.path.exists(FINANCE_DB_PATH):
        return DEFAULT_DB
    try:
        with open(FINANCE_DB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return DEFAULT_DB


def save_finance_db(db: dict) -> None:
    with open(FINANCE_DB_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def add_transaction(amount: int, tx_type: str, category: str, description: str) -> dict:
    """ثبت یک تراکنش جدید (درآمد یا هزینه).
    amount: به تومان
    tx_type: 'income' یا 'expense'
    category: دسته‌بندی (مثلاً: خوراک، خودرو، تفریح، طلا، حقوق و ...)
    """
    db = load_finance_db()
    now_tehran = datetime.now(TZ_TEHRAN)
    shamsi_date = jdatetime.datetime.fromgregorian(datetime=now_tehran)
    
    entry = {
        "id": len(db["transactions"]) + 1,
        "timestamp": now_tehran.isoformat(),
        "date_shamsi": shamsi_date.strftime("%Y/%m/%d"),
        "month_shamsi": shamsi_date.strftime("%Y/%m"),
        "day_shamsi": shamsi_date.day,
        "amount": amount,
        "type": tx_type,
        "category": category,
        "description": description
    }
    db["transactions"].append(entry)
    save_finance_db(db)
    return entry


def add_financial_goal(name: str, target_amount: int, deadline_shamsi: str) -> dict:
    """ثبت یک هدف مالی کوتاه مدت یا بلند مدت."""
    db = load_finance_db()
    goal = {
        "id": len(db["goals"]) + 1,
        "name": name,
        "target_amount": target_amount,
        "deadline": deadline_shamsi,
        "saved_amount": 0,
        "status": "active"  # active, achieved, failed
    }
    db["goals"].append(goal)
    save_finance_db(db)
    return goal


def get_monthly_report(month_str: str | None = None) -> str:
    """تولید گزارش کامل برای یک ماه شمسی مشخص (فرمت: YYYY/MM).
    اگر month_str فرستاده نشود، ماه شمسی فعلی ارزیابی می‌شود.
    """
    db = load_finance_db()
    if not month_str:
        now_tehran = datetime.now(TZ_TEHRAN)
        shamsi_date = jdatetime.datetime.fromgregorian(datetime=now_tehran)
        month_str = shamsi_date.strftime("%Y/%m")
        
    txs = [t for t in db["transactions"] if t["month_shamsi"] == month_str]
    
    total_income = sum(t["amount"] for t in txs if t["type"] == "income")
    total_expense = sum(t["amount"] for t in txs if t["type"] == "expense")
    net_savings = total_income - total_expense
    
    savings_rate = (net_savings / total_income * 100) if total_income > 0 else 0
    
    # تفکیک هزینه‌ها بر اساس دسته‌بندی
    categories = {}
    for t in txs:
        if t["type"] == "expense":
            cat = t["category"]
            categories[cat] = categories.get(cat, 0) + t["amount"]
            
    # مرتب‌سازی دسته‌بندی هزینه‌ها از بیشترین به کمترین
    sorted_cats = sorted(categories.items(), key=lambda x: x[1], reverse=True)
    
    lines = []
    lines.append(f"📊 **گزارش مدیریت مالی ماه شمسی {month_str}**")
    lines.append("───────────────────────")
    lines.append(f"💰 **کل درآمد:** `{total_income:,}` تومان")
    lines.append(f"💸 **کل هزینه‌ها:** `{total_expense:,}` تومان")
    lines.append(f"🏦 **پس‌انداز خالص:** `{net_savings:,}` تومان")
    lines.append(f"📈 **نرخ پس‌انداز:** `{savings_rate:.1f}%` (هدف: {db['savings_rate_target']}% )")
    lines.append("───────────────────────")
    
    if sorted_cats:
        lines.append("🔍 **مخارج به تفکیک دسته‌بندی:**")
        for cat, amt in sorted_cats:
            share = (amt / total_expense * 100) if total_expense > 0 else 0
            lines.append(f"- **{cat}**: `{amt:,}` تومان ({share:.1f}%)")
    else:
        lines.append("ℹ️ هیچ هزینه‌ای در این ماه ثبت نشده است.")
        
    lines.append("───────────────────────")
    
    # ارزیابی مخارج اضافی بر اساس اصول مدیریت مالی (قانون 50/30/20)
    # اگر سهم خواسته‌ها/تفریح یا یک فیلد خیلی زیاد بود هشدار می‌دهیم
    lines.append("🎓 **توصیه مالی و عارضه‌یابی مخارج:**")
    if savings_rate < db["savings_rate_target"]:
        lines.append("⚠️ نرخ پس‌انداز شما کمتر از حد هدف است. تلاش کنید هزینه‌های غیرضروری را تعدیل کنید.")
    else:
        lines.append("✅ آفرین! نرخ پس‌انداز شما در محدوده هدف قرار دارد. به همین روند ادامه دهید.")
        
    if sorted_cats:
        top_cat, top_amt = sorted_cats[0]
        top_share = (top_amt / total_expense * 100) if total_expense > 0 else 0
        if top_share > 40 and top_cat not in ["سرمایه گذاری", "طلا", "مسکن"]:
            lines.append(f"💡 بخش زیادی از بودجه شما ({top_share:.1f}%) صرف **{top_cat}** شده است. بررسی کنید آیا امکان کاهش مخارج این بخش وجود دارد یا خیر.")
            
    # وضعیت اهداف
    active_goals = [g for g in db["goals"] if g["status"] == "active"]
    if active_goals:
        lines.append("")
        lines.append("🎯 **وضعیت اهداف مالی شما:**")
        for g in active_goals:
            lines.append(f"- **{g['name']}**: هدف `{g['target_amount']:,}` تومان | مهلت: {g['deadline']}")
            
    return "\n".join(lines)


if __name__ == "__main__":
    # اجرای تست تست دستی
    db = load_finance_db()
    if not db["transactions"]:
        # تراکنش‌های فرضی برای تست اولیه
        add_transaction(45000000, "income", "حقوق", "درآمد دیجی کالا")
        add_transaction(12000000, "expense", "مسکن", "اجاره خانه")
        add_transaction(4000000, "expense", "خوراک", "خرید سوپرمارکتی")
        add_transaction(6000000, "expense", "تفریح", "رستوران و کافه گردی")
        add_transaction(15000000, "expense", "طلا", "خرید طلای آبشده برای پس‌انداز")
        add_financial_goal("خرید ۱۰۰ گرم طلای ۱۸ عیار آبشده", 350000000, "1405/12/29")
        
    print(get_monthly_report())
