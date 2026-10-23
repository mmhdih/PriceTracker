"""Non-destructive, repeatable merge of old and rich JSON stores into SQLite."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile

import jdatetime
from config import DATA_DIR, STORE_PATH, TZ_TEHRAN
from safe_json import atomic_write, file_lock
from tracker_store import Store, encoded, performance

NAMES = ('analysis_db.json', 'rich_analysis_db.json', 'daily_prices.json')


def migrate(source_dirs, destination=STORE_PATH, apply=False):
    sources = []
    for directory in source_dirs:
        for name in NAMES:
            path = Path(directory).resolve() / name
            if not path.exists() or any(p[0] == path for p in sources):
                continue
            content = path.read_bytes()
            value = json.loads(content.decode('utf-8-sig'))
            if not isinstance(value, dict):
                raise ValueError(f'Invalid source root: {path}')
            key = 'days' if name == 'daily_prices.json' else 'forecasts'
            if key not in value or not isinstance(value[key], dict if key == 'days' else list):
                raise ValueError(f'Invalid source schema: {path}')
            sources.append((path, content, value))
    if not sources:
        raise ValueError('No legacy database files found')
    report = {'mode': 'apply' if apply else 'preview', 'source_files': len(sources),
              'source_forecasts': sum(len(d.get('forecasts', [])) for _, _, d in sources),
              'archived_invalid_forecasts': 0, 'source_hashes': {str(p): hashlib.sha256(b).hexdigest() for p,b,_ in sources}}
    destination = Path(destination).resolve()
    with tempfile.TemporaryDirectory() as scratch:
        target = destination if apply else Path(scratch) / 'preview.sqlite3'
        with file_lock(destination):
            if not apply and destination.exists():
                Store(destination).backup(target)
            store = Store(target)
            if apply:
                tag = datetime.now(TZ_TEHRAN).strftime('%Y%m%dT%H%M%S%f')
                backup = destination.parent / 'backups' / ('merge-' + tag)
                backup.mkdir(parents=True)
                try:
                    backup.chmod(0o700)
                except OSError:
                    pass
                store.backup(backup / 'before-merge.sqlite3')
                for i,(p,content,_) in enumerate(sources):
                    copy = backup / f'{i}-{p.name}'
                    copy.write_bytes(content)
                    try:
                        copy.chmod(0o600)
                    except OSError:
                        pass
                report['backup_directory'] = str(backup)
            with store.connection() as con:
                for path, content, value in sources:
                    source = str(path) + ':' + hashlib.sha256(content).hexdigest()
                    con.execute('INSERT OR IGNORE INTO imports VALUES(?,?,?)', (source, 'raw-file', encoded(value)))
                    for raw in value.get('forecasts', []):
                        try:
                            store.put(raw, con)
                        except (KeyError, ValueError, TypeError, AttributeError):
                            report['archived_invalid_forecasts'] += 1
                    for raw in value.get('price_history', []):
                        store.observe(raw['timestamp'], raw, str(path), con)
                    for snap in value.get('snapshots', []):
                        data = snap.get('data', snap)
                        if data.get('prices'):
                            store.observe(data.get('collection_time') or snap['timestamp'], data['prices'], str(path), con)
                    for day, entries in value.get('days', {}).items():
                        year, month, date = map(int, day.replace('/', '-').split('-'))
                        for entry in entries:
                            hour, minute = map(int, entry['time'].split(':')[:2])
                            dt = jdatetime.datetime(year, month, date, hour, minute, tzinfo=TZ_TEHRAN).togregorian()
                            store.observe(dt.isoformat(), entry['prices'], str(path), con)
                con.execute("INSERT OR REPLACE INTO metadata VALUES('migration_completed',?)", (datetime.now(TZ_TEHRAN).isoformat(),))
            store.evaluate()
            db = store.export()
            report['merged_unique_forecasts'] = len(db['forecasts'])
            report['observations'] = len(db['price_history'])
            report['performance_by_horizon'] = {h: performance(db, h) for h in ('4h', '24h', '1w')}
            with store.connection() as con:
                report['integrity_check'] = con.execute('PRAGMA integrity_check').fetchone()[0]
                report['archived_source_versions'] = con.execute('SELECT COUNT(*) FROM imports').fetchone()[0]
            if apply:
                atomic_write(backup / 'merge-report.json', json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', action='append', help='Repeat for multiple old/new directories')
    parser.add_argument('--destination', type=Path, default=STORE_PATH)
    parser.add_argument('--apply', action='store_true', help='Default is preview; originals are never changed')
    args = parser.parse_args()
    print(json.dumps(migrate(args.source_dir or [DATA_DIR], args.destination, args.apply), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
