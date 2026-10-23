"""Single transactional database for forecasts, observations and horizon results."""
from contextlib import contextmanager, closing
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3

from config import STORE_PATH, DATA_DIR, TZ_TEHRAN, EVALUATION_TOLERANCE_SECONDS
from forecast_schema import HORIZONS, normalize_forecast, prices, timestamp


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class Store:
    def __init__(self, path=STORE_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as con:
            con.execute('PRAGMA journal_mode=WAL')
            con.executescript('''
                CREATE TABLE IF NOT EXISTS forecasts(id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS observations(timestamp TEXT NOT NULL, asset TEXT NOT NULL, price REAL NOT NULL CHECK(price>0), source TEXT NOT NULL, PRIMARY KEY(timestamp,asset));
                CREATE TABLE IF NOT EXISTS evaluations(forecast_id TEXT NOT NULL, horizon TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(forecast_id,horizon), FOREIGN KEY(forecast_id) REFERENCES forecasts(id));
                CREATE TABLE IF NOT EXISTS imports(source TEXT NOT NULL, row_key TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(source,row_key));
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS observations_asset_time ON observations(asset,timestamp);
                CREATE INDEX IF NOT EXISTS forecasts_issue_time ON forecasts(timestamp);
            ''')

    @contextmanager
    def connection(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.execute('PRAGMA foreign_keys=ON')
        con.execute('PRAGMA synchronous=FULL')
        try:
            with con:
                yield con
        finally:
            con.close()

    def observe(self, observed_at, values, source='collector', con=None):
        ts = timestamp(observed_at).isoformat()
        if con is None:
            with self.connection() as con:
                return self.observe(ts, values, source, con)
        for asset, value in prices(values).items():
            con.execute('INSERT OR IGNORE INTO observations VALUES(?,?,?,?)', (ts, asset, value, source))

    def put(self, raw, con=None):
        if con is None:
            with self.connection() as con:
                return self.put(raw, con)
        record = normalize_forecast(raw)
        # Duplicate legacy/rich writers can save the same forecast seconds apart.
        # Merge only equal text, same known price set and within 2 minutes.
        start = timestamp(record['timestamp'])
        near = con.execute('SELECT id,payload FROM forecasts WHERE timestamp BETWEEN ? AND ?',
                           ((start-timedelta(minutes=2)).isoformat(), (start+timedelta(minutes=2)).isoformat())).fetchall()
        for existing_id, payload in near:
            existing = json.loads(payload)
            if (' '.join(existing['forecast_text'].split()) == ' '.join(record['forecast_text'].split())
                    and existing['snapshot']['prices'] == record['snapshot']['prices']):
                record['id'] = existing_id
                # Preserve original issue time and all structured horizon fields.
                record['timestamp'] = existing['timestamp']
                record['predicted_ranges'] = {**existing['predicted_ranges'], **record['predicted_ranges']}
                if not record.get('asset'):
                    record['asset'] = existing.get('asset')
                if not record.get('legacy_evaluation'):
                    record['legacy_evaluation'] = existing.get('legacy_evaluation')
                break
        current = con.execute('SELECT payload FROM forecasts WHERE id=?', (record['id'],)).fetchone()
        if current:
            prior = json.loads(current[0])
            # A stale load/save must never overwrite an existing forecast.
            if raw.get('id') and prior != record:
                return record['id']
        con.execute('INSERT INTO forecasts VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                    (record['id'], record['timestamp'], encoded(record)))
        snap = record['snapshot']
        quote_time = snap.get('collection_time') or record['timestamp']
        self.observe(quote_time, snap['prices'], 'forecast-snapshot', con)
        return record['id']

    def export(self):
        with self.connection() as con:
            records = [json.loads(row[0]) for row in con.execute('SELECT payload FROM forecasts ORDER BY timestamp,id')]
            results = {(r[0], r[1]): json.loads(r[2]) for r in con.execute('SELECT * FROM evaluations')}
            history = {}
            for ts, asset, value in con.execute('SELECT timestamp,asset,price FROM observations ORDER BY timestamp'):
                history.setdefault(ts, {})[asset] = value
        for record in records:
            record['evaluations'] = {h: results[(record['id'], h)] for h in HORIZONS if (record['id'], h) in results}
            summary = record['evaluations'].get('24h')
            record['evaluation'] = summary if summary and summary.get('status') == 'scored' else None
            record['snapshot_prices'] = record['snapshot']['prices']
        return {'schema_version': 2, 'forecasts': records, 'snapshots': [], 'lessons': [], 'stats': {},
                'price_history': [{'timestamp': ts, **p, 'mesghal_toman': p.get('mazneh', 0),
                                   'gold_18_toman': p.get('geram18', 0), 'usd_toman': p.get('dollar', 0),
                                   'tether_toman': p.get('usdt', 0)} for ts, p in history.items()], 'daily_summaries': []}

    def evaluate(self, now=None, tolerance=EVALUATION_TOLERANCE_SECONDS):
        now = timestamp(now or datetime.now(TZ_TEHRAN).isoformat())
        changed = []
        with self.connection() as con:
            for fid, payload in con.execute('SELECT id,payload FROM forecasts').fetchall():
                record = json.loads(payload)
                start = timestamp(record['timestamp'])
                for horizon, bounds in record['predicted_ranges'].items():
                    prior = con.execute('SELECT payload FROM evaluations WHERE forecast_id=? AND horizon=?', (fid, horizon)).fetchone()
                    if prior and json.loads(prior[0]).get('status') == 'scored':
                        continue
                    due = start + timedelta(hours=HORIZONS[horizon])
                    if now < due:
                        continue
                    asset = bounds.get('asset')
                    baseline = record['snapshot']['prices'].get(asset)
                    outcome = {'status': 'unscorable', 'horizon': horizon, 'asset': asset,
                               'due_at': due.isoformat(), 'evaluated_at': now.isoformat(), 'version': 2}
                    # A stale snapshot cannot be assigned a fresh forecast issue time.
                    quote_time = timestamp(record['snapshot'].get('collection_time') or record['timestamp'])
                    if baseline and abs((start-quote_time).total_seconds()) <= 1800:
                        endpoint = min(now, due + timedelta(seconds=tolerance))
                        row = con.execute('SELECT timestamp,price FROM observations WHERE asset=? AND timestamp>=? AND timestamp<=? ORDER BY timestamp LIMIT 1',
                                          (asset, due.isoformat(), endpoint.isoformat())).fetchone()
                        outcome['status'] = 'missing_observation'
                        if row:
                            observed_at, final = row
                            change = (final / baseline - 1) * 100
                            direction = bounds['direction']
                            actual = 'صعودی' if change > .3 else 'نزولی' if change < -.3 else 'نوسانی'
                            correct = direction == actual
                            direction_result = 'WIN' if correct else 'LOSS'
                            in_range = bounds['min'] <= final <= bounds['max']
                            signal = record.get('buy_signal')
                            signal_result = None
                            if signal in ('BUY', 'STRONG_BUY'):
                                signal_result = 'CORRECT' if change > 0 else 'WRONG'
                            elif signal in ('WAIT', 'STRONG_WAIT'):
                                signal_result = 'CORRECT' if change <= 0 else 'WRONG'
                            score = ((.5 * correct + .3 * in_range + (.2 * (signal_result == 'CORRECT') if signal_result else 0))
                                     / (1 if signal_result else .8))
                            outcome.update(status='scored', observed_at=observed_at, reference_price=baseline, actual_price=final,
                                           age_hours=round((timestamp(observed_at)-start).total_seconds()/3600, 3),
                                           predicted_direction=direction, actual_change_pct=round(change, 4),
                                           direction_result=direction_result, result=direction_result,
                                           range_result='IN_RANGE' if in_range else 'OUT_RANGE', buy_result=signal_result,
                                           overall_score=round(score, 4), signal_basis='price-change-before-costs')
                    con.execute('INSERT INTO evaluations VALUES(?,?,?) ON CONFLICT(forecast_id,horizon) DO UPDATE SET payload=excluded.payload',
                                (fid, horizon, encoded(outcome)))
                    changed.append({'forecast_id': fid, **outcome})
        return changed

    def backup(self, destination):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as source, closing(sqlite3.connect(destination)) as target:
            source.backup(target)


def get_store():
    store = Store()
    legacy = [DATA_DIR / name for name in ('analysis_db.json', 'rich_analysis_db.json')]
    with store.connection() as con:
        migrated = con.execute("SELECT value FROM metadata WHERE key='migration_completed'").fetchone()
    if any(p.exists() for p in legacy) and not migrated:
        raise RuntimeError('Existing JSON history detected. Run: python migrate_databases.py --source-dir <data-dir> --apply')
    return store


def performance(db, horizon='24h'):
    records = db.get('forecasts', [])
    eligible = [f for f in records if horizon in f.get('predicted_ranges', {})]
    scored = [(f, f.get('evaluations', {}).get(horizon)) for f in eligible]
    scored = [(f, e) for f, e in scored if e and e.get('status') == 'scored' and e.get('version') == 2]
    stats = {'all_forecasts': len(records), 'total': len(scored), 'eligible': len(eligible), 'unscored': len(eligible)-len(scored), 'horizon': horizon,
             'archived_legacy_results': sum(bool(f.get('legacy_evaluation')) for f in records),
             'wins': sum(e['direction_result'] == 'WIN' for f, e in scored)}
    stats.update(losses=stats['total']-stats['wins'], neutral=0,
                 win_rate=round(stats['wins']/stats['total']*100, 1) if stats['total'] else 0,
                 avg_score=round(sum(e['overall_score'] for f,e in scored)/len(scored), 4) if scored else 0)
    for d in ('صعودی', 'نزولی', 'نوسانی'):
        group = [e for f,e in scored if e['predicted_direction'] == d]
        wins = sum(e['direction_result'] == 'WIN' for e in group)
        stats['dir_'+d] = {'total': len(group), 'wins': wins, 'losses': len(group)-wins, 'neutral': 0,
                          'rate': round(wins/len(group)*100,1) if group else 0}
    inside = sum(e['range_result'] == 'IN_RANGE' for f,e in scored)
    stats['range'] = {'in': inside, 'out': len(scored)-inside, 'rate': round(inside/len(scored)*100,1) if scored else 0}
    return stats
