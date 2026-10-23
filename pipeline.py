"""Collect -> validate -> evaluate -> render -> persist. Sending is opt-in."""
import argparse
import hashlib
import json
import sys
from pathlib import Path
from datetime import datetime
from config import DATA_DIR, COLLECTOR_PATH, FORECAST_PATH, FORECAST_JSON_PATH, TZ_TEHRAN
from safe_json import read_json, atomic_write, file_lock
from tracker_store import get_store
from analysis_engine import validate_snapshot, build_forecast, render


def produce(snapshot, store=None, output=FORECAST_PATH, json_output=FORECAST_JSON_PATH):
    snapshot = validate_snapshot(snapshot)
    store = store or get_store()
    store.observe(snapshot['collection_time'], snapshot['prices'])
    store.evaluate()
    record = build_forecast(snapshot)
    db = store.export()
    # Include this analysis once in the displayed total before its text is saved.
    if not any(f['id'] == record['id'] for f in db['forecasts']):
        db['forecasts'].append(record)
    text = render(record, db)
    record['forecast_text'] = text
    record['text_sha256'] = hashlib.sha256(text.encode()).hexdigest()
    # SQLite forecast write is transactional and deduplicated across all entrypoints.
    record['id'] = store.put(record)
    atomic_write(json_output, json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False))
    atomic_write(output, text)
    return record


def main(argv=None, collect_fresh=False):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=COLLECTOR_PATH)
    parser.add_argument('--output', type=Path, default=FORECAST_PATH)
    parser.add_argument('--json-output', type=Path, default=FORECAST_JSON_PATH)
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--daily', action='store_true', help='Compatibility flag; the same report template is used')
    args = parser.parse_args(argv)
    try:
        with file_lock(DATA_DIR / 'analysis-cycle', timeout=1):
            if collect_fresh:
                from collector import collect
                snapshot = collect(technical=True)
                atomic_write(args.input, json.dumps(snapshot, ensure_ascii=False, indent=2))
            else:
                snapshot = read_json(args.input)
            record = produce(snapshot, output=args.output, json_output=args.json_output)
            if args.publish:
                from price_tracker import send_to_telegram
                if not send_to_telegram(record['forecast_text']):
                    raise RuntimeError('Telegram delivery failed')
            print(record['forecast_text'])
        return 0
    except (ValueError, KeyError, OSError, RuntimeError, TimeoutError) as error:
        print(f'[analysis failed] {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main(collect_fresh=True))
