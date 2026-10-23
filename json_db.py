"""Legacy signatures backed by the same store as rich_db."""
from rich_db import load_db, save_db, now_iso, get_performance_stats
from rich_db import add_forecast as _add_forecast, evaluate_forecasts
from tracker_store import get_store
from config import STORE_PATH
DB_PATH = str(STORE_PATH)

def add_price_entry(db, prices):
    store = get_store()
    store.observe(now_iso(), prices)
    db.update(store.export())

def add_forecast(db, forecast_text, snapshot_prices, predicted_direction=None,
                 predicted_4h=None, predicted_24h=None):
    ranges = {key: value for key,value in [('4h',predicted_4h),('24h',predicted_24h)] if value}
    return _add_forecast(db, forecast_text, {'prices': snapshot_prices}, predicted_direction, ranges)

def evaluate_old_forecasts(db, current_prices):
    return evaluate_forecasts(db, current_prices)

def get_win_rate(db):
    return get_performance_stats(db)
