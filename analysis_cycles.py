"""Durable, slot-bound LLM input -> validated analysis -> shared report. No sends."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import uuid

from analysis_engine import validate_snapshot, render
from config import DATA_DIR, RUNTIME_DIR, TZ_TEHRAN, FORECAST_PATH, FORECAST_JSON_PATH
from forecast_schema import HORIZONS, DIRECTIONS, number, timestamp
from safe_json import atomic_write, file_lock, read_json
from tracker_store import get_store, encoded, performance

UTC_HOURS = (3, 6, 8, 11, 14, 17)
SCHEDULE = '0 3,6,8,11,14,17 * * *'


def slot_bounds(now):
    now = timestamp(now).astimezone(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    slots = [midnight + timedelta(days=d, hours=h) for d in (-1, 0, 1) for h in UTC_HOURS]
    return max(s for s in slots if s <= now), min(s for s in slots if s > now)


def cycle_key(slot):
    return hashlib.sha256(('hermes-analyst-v1:' + slot.isoformat()).encode()).hexdigest()[:32]


def digest(value):
    return hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()


def get_cycle(store, key, con=None):
    if con is None:
        with store.connection() as connection:
            return get_cycle(store, key, connection)
    row = con.execute('SELECT payload FROM analysis_cycles WHERE id=?', (key,)).fetchone()
    return json.loads(row[0]) if row else None


def save_cycle(con, cycle):
    con.execute('INSERT INTO analysis_cycles VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                (cycle['id'], encoded(cycle)))


def write_bundle(cycle, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    bundle = cycle['bundle']
    path = directory / ('analyst-input-' + bundle['bundle_id'] + '.json')
    atomic_write(path, json.dumps(bundle, ensure_ascii=False, indent=2, allow_nan=False))
    return path


def begin(store, collect, directory, now=None):
    supplied_now = now is not None
    now = timestamp(now or datetime.now(TZ_TEHRAN).isoformat())
    slot, _ = slot_bounds(now)
    if (now - slot).total_seconds() > 90 * 60:
        raise ValueError('Outside the 90-minute analysis window; do not create a catch-up report')
    key = cycle_key(slot)
    cycle = get_cycle(store, key)
    if cycle and cycle['state'] == 'committed':
        return {'state': 'already_committed', 'cycle_id': key, 'forecast_id': cycle['forecast_id'],
                'delivery': 'not_checked', 'action': 'do_not_regenerate_or_resend'}
    if cycle:
        try:
            validate_snapshot(cycle['bundle']['snapshot'], now)
        except (ValueError, KeyError):
            cycle = None  # Previous uncommitted input expired; keep it archived in SQLite below.
    if not cycle:
        candidate = collect()
        if not supplied_now:
            now = timestamp(datetime.now(TZ_TEHRAN).isoformat())
        if (now - slot).total_seconds() > 90 * 60 or slot_bounds(now)[0] != slot:
            raise ValueError('Collection crossed the analysis slot/window; no forecast was created')
        snap = validate_snapshot(candidate, now)
        store.observe(snap['collection_time'], snap['prices'])
        store.evaluate(now.isoformat())
        asset = 'geram18' if snap['prices'].get('geram18') else 'mazneh'
        db = store.export()
        bundle = {'schema_version': 1, 'bundle_id': uuid.uuid4().hex, 'cycle_id': key,
                  'slot_at': slot.isoformat(), 'snapshot': snap, 'snapshot_sha256': digest(snap),
                  'asset': asset, 'unit': 'toman',
                  'performance': {h: performance(db, h) for h in HORIZONS}}
        cycle = {'id': key, 'state': 'drafting', 'bundle': bundle,
                 'created_at': now.isoformat(), 'delivery': 'not_checked'}
        with store.connection() as con:
            prior = get_cycle(store, key, con)
            if prior:
                con.execute('INSERT OR IGNORE INTO imports VALUES(?,?,?)',
                            ('analysis-cycle-inputs', prior['bundle']['bundle_id'], encoded(prior)))
            save_cycle(con, cycle)
    path = write_bundle(cycle, directory)
    return {'state': 'needs_analysis', 'cycle_id': key, 'bundle_id': cycle['bundle']['bundle_id'],
            'input_file': str(path),
            'draft_file': str(Path(directory) / ('analyst-draft-' + cycle['bundle']['bundle_id'] + '.json'))}


def clean_line(value, name, maximum, allow_empty=False):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError('Invalid or oversized analyst field: ' + name)
    if any(ord(c) < 32 for c in value) or '[SILENT]' in value or '[CRON_FAILURE]' in value:
        raise ValueError('Control characters or cron markers in analyst field: ' + name)
    value = ' '.join(value.split())
    if not value and not allow_empty:
        raise ValueError('Empty analyst field: ' + name)
    return value


def validate_draft(draft, cycle, now):
    if not isinstance(draft, dict):
        raise ValueError('Analyst output must be a JSON object')
    allowed = {'bundle_id', 'snapshot_sha256', 'asset', 'unit', 'predicted_ranges',
               'technical_summary', 'bubble_summary', 'news_summary', 'reasoning',
               'risks', 'buy_signal', 'confidence'}
    if set(draft) != allowed:
        raise ValueError('Analyst fields differ from the documented schema')
    bundle = cycle['bundle']
    if draft['bundle_id'] != bundle['bundle_id'] or draft['snapshot_sha256'] != bundle['snapshot_sha256']:
        raise ValueError('Analyst output belongs to a different input bundle')
    snapshot = validate_snapshot(bundle['snapshot'], now)
    if digest(snapshot) != bundle['snapshot_sha256']:
        raise ValueError('Stored snapshot checksum mismatch')
    if draft['asset'] != bundle['asset'] or draft['unit'] != 'toman':
        raise ValueError('Analyst changed the forecast asset or unit')
    proposed = draft['predicted_ranges']
    if not isinstance(proposed, dict) or set(proposed) != set(HORIZONS):
        raise ValueError('Provide all three horizons, using null when evidence is insufficient')
    ranges = {}
    for horizon, bounds in proposed.items():
        if bounds is None:
            continue
        if not isinstance(bounds, dict) or set(bounds) != {'min', 'max', 'direction'}:
            raise ValueError('Invalid horizon schema: ' + horizon)
        low, high = number(bounds['min']), number(bounds['max'])
        if low > high or bounds['direction'] not in DIRECTIONS:
            raise ValueError('Invalid range or direction: ' + horizon)
        ranges[horizon] = {'min': low, 'max': high, 'direction': bounds['direction'], 'asset': bundle['asset']}
    analyst = {name: clean_line(draft[name], name, maximum, allow_empty=True)
               for name, maximum in [('technical_summary', 160), ('bubble_summary', 100), ('news_summary', 160)]}
    analyst['reasoning'] = clean_line(draft['reasoning'], 'reasoning', 200)
    if not isinstance(draft['risks'], list) or not 1 <= len(draft['risks']) <= 3:
        raise ValueError('Provide one to three explicit risks')
    analyst['risks'] = [clean_line(r, 'risk', 90) for r in draft['risks']]
    signal = draft['buy_signal']
    if signal not in (None, 'BUY', 'STRONG_BUY', 'WAIT', 'STRONG_WAIT'):
        raise ValueError('Unknown buy signal')
    confidence = draft['confidence']
    if confidence is not None and (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                                   or not 0 <= confidence <= 100):
        raise ValueError('Confidence must be null or a finite number between 0 and 100')
    return {'id': cycle['id'], 'cycle_id': cycle['id'], 'bundle_sha256': bundle['snapshot_sha256'],
            'timestamp': snapshot['collection_time'], 'snapshot': snapshot, 'asset': bundle['asset'],
            'predicted_ranges': ranges, 'predicted_direction': ranges.get('24h', {}).get('direction'),
            'buy_signal': signal, 'confidence': confidence, 'reasoning': analyst['reasoning'],
            'analyst': analyst, 'model_version': 'hermes-analyst-v1'}


def finish(store, draft, directory, now=None):
    now = timestamp(now or datetime.now(TZ_TEHRAN).isoformat())
    bundle_id = draft.get('bundle_id') if isinstance(draft, dict) else None
    if not isinstance(bundle_id, str) or len(bundle_id) != 32:
        raise ValueError('Missing bundle identity')
    with store.connection() as con:
        rows = con.execute('SELECT payload FROM analysis_cycles').fetchall()
    cycle = next((json.loads(r[0]) for r in rows if json.loads(r[0])['bundle']['bundle_id'] == bundle_id), None)
    if not cycle:
        raise ValueError('Unknown or expired bundle identity')
    if cycle['state'] == 'committed':
        return {'state': 'already_committed', 'cycle_id': cycle['id'],
                'forecast_id': cycle['forecast_id'], 'delivery': 'not_checked'}
    record = validate_draft(draft, cycle, now)
    store.evaluate(now.isoformat())
    db = store.export()
    if any(f['id'] == record['id'] for f in db['forecasts']):
        raise RuntimeError('Forecast exists without a committed cycle; inspect privately before any replay')
    db['forecasts'].append(record)
    text = render(record, db)
    record['forecast_text'] = text
    record['text_sha256'] = hashlib.sha256(text.encode()).hexdigest()
    # A shared transaction binds the forecast and cycle commit; no partial saved/unsaved state.
    with store.connection() as con:
        con.execute('BEGIN IMMEDIATE')
        current = get_cycle(store, cycle['id'], con)
        if not current or current['bundle']['bundle_id'] != bundle_id:
            raise ValueError('Input bundle changed before commit')
        if current['state'] == 'committed':
            return {'state': 'already_committed', 'cycle_id': cycle['id'],
                    'forecast_id': current['forecast_id'], 'delivery': 'not_checked'}
        record['id'] = store.put(record, con)
        cycle.update(state='committed', forecast_id=record['id'], report=text,
                     text_sha256=record['text_sha256'], committed_at=now.isoformat())
        save_cycle(con, cycle)
    # A file-write failure after the commit must never regenerate the analysis or resend it.
    report_path = Path(directory) / ('analyst-report-' + bundle_id + '.txt')
    atomic_write(report_path, text)
    atomic_write(FORECAST_PATH, text)
    atomic_write(FORECAST_JSON_PATH, json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False))
    return {'state': 'committed', 'cycle_id': cycle['id'], 'forecast_id': record['id'],
            'report_file': str(report_path), 'text_sha256': record['text_sha256'], 'delivery': 'not_checked'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    start = commands.add_parser('begin')
    start.add_argument('--collector', type=Path, help='Use an existing fresh snapshot, for isolated/offline checks')
    end = commands.add_parser('finish')
    end.add_argument('draft', type=Path)
    export = commands.add_parser('export-report', help='Operator-only recovery of saved text, never delivery')
    export.add_argument('cycle_id')
    args = parser.parse_args(argv)
    try:
        with file_lock(DATA_DIR / 'analysis-cycle', timeout=1):
            store = get_store()
            directory = RUNTIME_DIR / 'gold-analysis-cycles'
            if args.action == 'begin':
                def collect():
                    if args.collector:
                        return read_json(args.collector)
                    from collector import collect as fresh
                    return fresh(technical=True)
                result = begin(store, collect, directory)
            elif args.action == 'finish':
                result = finish(store, read_json(args.draft), directory)
            else:
                if len(args.cycle_id) != 32 or any(c not in '0123456789abcdef' for c in args.cycle_id):
                    raise ValueError('Invalid cycle identity')
                cycle = get_cycle(store, args.cycle_id)
                if not cycle or cycle['state'] != 'committed':
                    raise ValueError('No committed report exists for this cycle')
                if hashlib.sha256(cycle['report'].encode()).hexdigest() != cycle['text_sha256']:
                    raise ValueError('Saved report checksum mismatch')
                path = directory / ('recovered-' + args.cycle_id + '.txt')
                atomic_write(path, cycle['report'])
                result = {'state': 'exported_for_review', 'report_file': str(path),
                          'text_sha256': cycle['text_sha256'], 'delivery': 'not_checked'}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, KeyError, TypeError, OSError, RuntimeError, TimeoutError):
        print('Analysis cycle failed; no report should be published. Inspect private inputs/state.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
