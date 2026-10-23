"""One saver for the old and rich script names, with structured output preferred."""
import argparse
import hashlib
from pathlib import Path
import json
import sys
from analysis_engine import validate_snapshot
from config import COLLECTOR_PATH, FORECAST_JSON_PATH
from forecast_schema import normalize_forecast, explicit_ranges
from safe_json import read_json
from tracker_store import get_store


def load_forecast(path, collector_path=COLLECTOR_PATH, sidecar=FORECAST_JSON_PATH):
    path = Path(path)
    if path.suffix == '.json':
        record = read_json(path)
        if record.get('text_sha256') != hashlib.sha256(record['forecast_text'].encode()).hexdigest():
            raise ValueError('Structured forecast text checksum mismatch')
        validate_snapshot(record['snapshot'])
    else:
        text = path.read_text(encoding='utf-8').strip()
        if not text or 'FORECAST_PLACEHOLDER' in text:
            raise ValueError('Empty or placeholder forecast cannot be stored')
        if Path(sidecar).exists():
            candidate = read_json(sidecar)
        else:
            candidate = {}
        if candidate.get('text_sha256') == hashlib.sha256(text.encode()).hexdigest():
            record = candidate
            validate_snapshot(record['snapshot'])
        else:
            snapshot = validate_snapshot(read_json(collector_path))
            record = {'timestamp': snapshot['collection_time'], 'snapshot': snapshot, 'forecast_text': text,
                      'predicted_ranges': explicit_ranges(text), 'model_version': 'external-text-unverified'}
    normalized = normalize_forecast(record)
    if not normalized['predicted_ranges']:
        raise ValueError('No explicit structured forecasts; no prediction was saved')
    if any(not bounds.get('asset') for bounds in normalized['predicted_ranges'].values()):
        raise ValueError('Forecast asset is ambiguous; provide structured JSON with asset')
    return normalized


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('forecast_file', type=Path)
    parser.add_argument('--collector', type=Path, default=COLLECTOR_PATH)
    parser.add_argument('--sidecar', type=Path, default=FORECAST_JSON_PATH)
    args = parser.parse_args(argv)
    try:
        record = load_forecast(args.forecast_file, args.collector, args.sidecar)
        print('Saved forecast:', get_store().put(record))
        return 0
    except (ValueError, KeyError, OSError, RuntimeError) as error:
        print(f'[save failed] {error}', file=sys.stderr)
        return 1
