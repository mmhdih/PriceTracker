"""Bounded, profile-scoped pre-commit retry; dry-run by default. No Telegram sends."""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

from analysis_cycles import slot_bounds, cycle_key, SCHEDULE
from config import DATA_DIR, STORE_PATH, TZ_TEHRAN
from forecast_schema import timestamp
from safe_json import file_lock
from tracker_store import get_store, encoded

CONFIG_KEY = 'gold_analysis_retry_config_v1'
PROTOCOL = 'PRICE_TRACKER_STRUCTURED_CYCLE_V1'


def readonly(path):
    path = Path(path).resolve(strict=True)
    con = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA query_only=ON')
    con.execute('BEGIN')
    return con


def aware(value):
    dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('Hermes execution timestamps must have a timezone')
    return dt.astimezone(timezone.utc)


def job_record(home, job_id):
    value = json.loads((Path(home)/'cron/jobs.json').read_text(encoding='utf-8'))
    jobs = value.get('jobs') if isinstance(value, dict) else value
    if not isinstance(jobs, list):
        raise ValueError('Unsupported Hermes jobs schema')
    matches = [j for j in jobs if isinstance(j, dict) and j.get('id') == job_id]
    if len(matches) != 1 or matches[0].get('name') != 'gold-analysis-8h':
        raise ValueError('Analysis job identity differs from configuration')
    job = matches[0]
    if PROTOCOL not in str(job.get('prompt', '')) or 'analysis_cycles.py' not in str(job.get('prompt', '')):
        raise ValueError('Install the structured-cycle prompt before enabling this watcher')
    schedule = job.get('schedule')
    expr = schedule.get('expr') if isinstance(schedule, dict) else schedule
    if expr != SCHEDULE:
        raise ValueError('Retry schedule assumptions differ; inspect the job timezone and expression')
    if job.get('timezone') not in (None, '', 'UTC', 'Etc/UTC'):
        raise ValueError('A per-job timezone needs separate verification')
    if isinstance(schedule, dict) and schedule.get('timezone') not in (None, '', 'UTC', 'Etc/UTC'):
        raise ValueError('Schedule timezone differs from UTC')
    return job


def read_executions(home, job_id):
    with closing(readonly(Path(home)/'cron/executions.db')) as con:
        columns = {r['name'] for r in con.execute('PRAGMA table_info(executions)')}
        required = {'id', 'job_id', 'status', 'claimed_at', 'finished_at'}
        if not required <= columns:
            raise ValueError('Unsupported Hermes executions schema; no retry was requested')
        fields = 'id,status,claimed_at,finished_at'
        fields += ',delivery_outcome' if 'delivery_outcome' in columns else ',NULL AS delivery_outcome'
        # No error/prompt/credential columns are loaded or printed.
        rows = [dict(r) for r in con.execute('SELECT '+fields+' FROM executions WHERE job_id=? ORDER BY claimed_at DESC,id DESC', (job_id,))]
    return rows


def project_state(database, key, job_id, failure):
    with closing(readonly(database)) as con:
        cycle = con.execute('SELECT payload FROM analysis_cycles WHERE id=?', (key,)).fetchone()
        claim_id = job_id + ':' + key
        claim = con.execute('SELECT payload FROM retry_claims WHERE id=?', (claim_id,)).fetchone()
        start = (aware(failure['claimed_at']) - timedelta(seconds=30)).astimezone(TZ_TEHRAN).isoformat()
        end = (aware(failure['finished_at']) + timedelta(seconds=60)).astimezone(TZ_TEHRAN).isoformat()
        saved = con.execute('SELECT COUNT(*) FROM forecasts WHERE timestamp>=? AND timestamp<=?', (start,end)).fetchone()[0]
    return json.loads(cycle[0]) if cycle else None, json.loads(claim[0]) if claim else None, saved


def plan(config, database=STORE_PATH, now=None):
    now = aware(now or datetime.now(timezone.utc).isoformat())
    home, job_id = config['hermes_home'], config['job_id']
    job = job_record(home, job_id)
    if job.get('enabled') is not True or job.get('state') in ('paused','completed','disabled') or job.get('paused_at'):
        return {'action': 'skip', 'reason': 'job_not_enabled'}
    if job.get('state') not in ('scheduled', 'running'):
        return {'action': 'review', 'reason': 'unrecognized_job_state'}
    if job.get('state') == 'running':
        return {'action': 'skip', 'reason': 'analysis_in_flight'}
    if job.get('quota_hold_until'):
        return {'action': 'skip', 'reason': 'provider_hold'}
    rows = read_executions(home, job_id)
    if any(r['status'] in ('running','claimed') for r in rows):
        return {'action': 'skip', 'reason': 'analysis_in_flight'}
    if not rows or rows[0]['status'] != 'failed':
        return {'action': 'skip', 'reason': 'no_confirmed_execution_failure'}
    last = rows[0]
    # For a failed execution, 'delivered' may mean its failure notice was sent.
    # A committed cycle or saved forecast below still forbids regenerating a report.
    if last['delivery_outcome'] not in (None, '', 'not_attempted', 'not_configured', 'suppressed', 'delivered', 'failed'):
        return {'action': 'review', 'reason': 'delivery_needs_reconciliation'}
    claimed, finished = aware(last['claimed_at']), aware(last['finished_at'])
    if finished < claimed or finished > now:
        raise ValueError('Invalid failure timestamps')
    age = (now-finished).total_seconds()
    if age < 600:
        return {'action': 'skip', 'reason': 'retry_delay'}
    if age > 3600:
        return {'action': 'review', 'reason': 'failure_expired'}
    slot, next_slot = slot_bounds(now)
    if slot_bounds(claimed)[0] != slot or (now-slot).total_seconds() > 90*60 or (next_slot-now).total_seconds() < 15*60:
        return {'action': 'skip', 'reason': 'outside_retry_window'}
    # A queued manual/native retry changes next_run_at away from the normal next slot.
    if aware(job.get('next_run_at')) != next_slot:
        return {'action': 'skip', 'reason': 'another_dispatch_or_native_retry_pending'}
    key = cycle_key(slot)
    cycle, claimed_before, saved = project_state(database, key, job_id, last)
    if claimed_before:
        if claimed_before.get('state') != 'dispatch_accepted_unverified':
            return {'action': 'review', 'reason': 'prior_dispatch_requires_review'}
        if claimed_before.get('failure_id') != last['id']:
            return {'action': 'review', 'reason': 'retry_failed_budget_exhausted'}
        return {'action': 'skip', 'reason': 'slot_retry_budget_used'}
    if saved or (cycle and cycle['state'] == 'committed'):
        return {'action': 'review', 'reason': 'forecast_already_saved_do_not_regenerate'}
    return {'action': 'retry', 'reason': 'confirmed_pre_commit_failure', 'cycle_id': key,
            'failure_id': last['id'], 'slot_at': slot.isoformat()}


def execute(config, store, decision, runner=subprocess.run, now=None):
    now = now or datetime.now(timezone.utc).isoformat()
    key = config['job_id'] + ':' + decision['cycle_id']
    claim = {**decision, 'state': 'dispatching', 'claimed_at': now, 'job_id': config['job_id']}
    with store.connection() as con:
        try:
            con.execute('INSERT INTO retry_claims VALUES(?,?)', (key, encoded(claim)))
        except sqlite3.IntegrityError:
            return {'action': 'skip', 'reason': 'slot_retry_budget_used'}
    # The durable claim deliberately survives a crash or an uncertain CLI result.
    # Never delete it and blindly re-run a potentially accepted dispatch.
    log_dir = Path(store.path).parent / 'retry-logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    descriptor, log_path = tempfile.mkstemp(prefix='retry-', suffix='.log', dir=log_dir)
    claim['private_log'] = log_path
    result_code = None
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as log:
            result = runner(['hermes','cron','run',config['job_id']],
                            env={**os.environ,'HERMES_HOME':config['hermes_home']},
                            stdout=log, stderr=subprocess.STDOUT, timeout=60, check=False)
        result_code = result.returncode
        claim['state'] = 'dispatch_accepted_unverified' if result_code == 0 else 'dispatch_failed_review_required'
    except (OSError, subprocess.TimeoutExpired):
        claim['state'] = 'dispatch_unknown_review_required'
    claim['returncode'] = result_code
    with store.connection() as con:
        con.execute('UPDATE retry_claims SET payload=? WHERE id=?', (encoded(claim),key))
    return {'action': 'attempted', 'state': claim['state'], 'returncode': result_code,
            'delivery': 'not_checked', 'private_log': log_path}


def load_config(database=STORE_PATH):
    with closing(readonly(database)) as con:
        row = con.execute('SELECT value FROM metadata WHERE key=?', (CONFIG_KEY,)).fetchone()
    if not row:
        raise ValueError('Configure the watcher against the verified Hermes profile first')
    return json.loads(row[0])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Permit at most one verified CLI dispatch for a slot')
    parser.add_argument('--configure', action='store_true')
    parser.add_argument('--hermes-home', type=Path)
    parser.add_argument('--job-id')
    args = parser.parse_args(argv)
    try:
        if args.configure:
            if args.apply or not args.hermes_home or not args.job_id:
                raise ValueError('Configure requires an explicit profile home and analysis job ID')
            config = {'hermes_home': str(args.hermes_home.resolve(strict=True)), 'job_id': args.job_id}
            job_record(config['hermes_home'], config['job_id'])
            read_executions(config['hermes_home'], config['job_id'])
            with get_store().connection() as con:
                con.execute('INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                            (CONFIG_KEY, encoded(config)))
            result = {'action': 'configured', 'messages_sent': False, 'dispatch_requested': False}
        else:
            if args.hermes_home or args.job_id:
                raise ValueError('Use the saved verified configuration; do not override the target during a run')
            config = load_config()
            if args.apply:
                with file_lock(DATA_DIR / 'analysis-retry', timeout=1):
                    decision = plan(config)
                    result = execute(config, get_store(), decision) if decision['action'] == 'retry' else decision
            else:
                result = plan(config)
                result['dry_run'] = True
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get('state') in ('dispatch_failed_review_required','dispatch_unknown_review_required'):
            return 1
        return 2 if result.get('action') == 'review' else 0
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error, TimeoutError):
        print('Watcher refused to act; inspect the private profile, schema and saved configuration.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
