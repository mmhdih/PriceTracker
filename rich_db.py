"""Compatibility API; active analysis history lives in tracker.sqlite3."""
from datetime import datetime
import json
from config import TZ_TEHRAN, STORE_PATH
from tracker_store import get_store, performance
DB_PATH = str(STORE_PATH)

def now_iso():
    return datetime.now(TZ_TEHRAN).isoformat()

def load_db():
    return get_store().export()

def save_db(db):
    store = get_store()
    with store.connection() as con:
        for record in db.get('forecasts', []):
            store.put(record, con)

def add_snapshot(db, collector_data):
    store = get_store()
    store.observe(collector_data['collection_time'], collector_data['prices'])
    db.update(store.export())

def add_forecast(db, forecast_text, snapshot, predicted_direction=None,
                 predicted_ranges=None, buy_signal=None, confidence=None, reasoning=''):
    store = get_store()
    fid = store.put({'timestamp': snapshot.get('collection_time') or now_iso(),
                     'forecast_text': forecast_text, 'snapshot': snapshot,
                     'predicted_direction': predicted_direction, 'predicted_ranges': predicted_ranges or {},
                     'buy_signal': buy_signal, 'confidence': confidence, 'reasoning': reasoning})
    db.update(store.export())
    return fid

def evaluate_forecasts(db, current_prices, observed_at=None):
    store = get_store()
    now = observed_at or now_iso()
    store.observe(now, current_prices)
    results = store.evaluate(now)
    db.update(store.export())
    return results

def get_performance_stats(db, horizon='24h'):
    return performance(db, horizon)

if __name__ == '__main__':
    print(json.dumps(get_performance_stats(load_db()), ensure_ascii=False, indent=2))
