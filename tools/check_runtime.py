"""Read-only deployment/history diagnostic. No project imports, writes or sends."""
import argparse
from contextlib import closing
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sqlite3
import sys

HORIZONS={'4h':4,'24h':24,'1w':168}


def collect_health(live_dir, now=None):
    logical=Path(live_dir).expanduser().absolute()
    live=logical.resolve(strict=True)
    settings_file=live.parent/'runtime.json'
    if not settings_file.is_file():
        raise ValueError('Managed runtime settings not found; inspect the installation before continuing')
    settings=json.loads(settings_file.read_text(encoding='utf-8'))
    database=Path(settings['data'])/'tracker.sqlite3'
    if not database.is_file():
        raise ValueError('Active SQLite database does not exist')
    now=now or datetime.now(timezone.utc)
    with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True)) as con:
        con.execute('PRAGMA query_only=ON')
        con.execute('BEGIN')
        integrity=con.execute('PRAGMA quick_check').fetchone()[0]
        records=[json.loads(row[0]) for row in con.execute('SELECT payload FROM forecasts')]
        results={(row[0],row[1]):json.loads(row[2]) for row in con.execute('SELECT forecast_id,horizon,payload FROM evaluations')}
        observations=con.execute('SELECT COUNT(*) FROM observations').fetchone()[0]
        observation_times=con.execute('SELECT COUNT(DISTINCT timestamp) FROM observations').fetchone()[0]
        last={asset:ts for asset,ts in con.execute('SELECT asset,MAX(timestamp) FROM observations GROUP BY asset')}
        tables={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        cycle_rows=[json.loads(r[0]) for r in con.execute('SELECT payload FROM analysis_cycles')] if 'analysis_cycles' in tables else []
        retry_rows=[json.loads(r[0]) for r in con.execute('SELECT payload FROM retry_claims')] if 'retry_claims' in tables else []
        newest_cycle=max(cycle_rows,key=lambda r:r.get('created_at',''),default={})
        newest_retry=max(retry_rows,key=lambda r:r.get('claimed_at',''),default={})
        integration={'schema_supported': {'analysis_cycles','retry_claims'} <= tables,
                     'cycles_total':len(cycle_rows),
                     'cycles_committed':sum(r.get('state')=='committed' for r in cycle_rows),
                     'latest_cycle':{k:newest_cycle[k] for k in ('id','state','forecast_id','created_at','committed_at','text_sha256','delivery') if k in newest_cycle},
                     'retry_claims_total':len(retry_rows),
                     'latest_retry':{k:newest_retry[k] for k in ('state','claimed_at','returncode') if k in newest_retry}}
    metrics={}
    for horizon,hours in HORIZONS.items():
        states={'scored':0,'not_due':0,'due_not_processed':0,'missing_observation':0,'unscorable':0,'other':0}
        eligible=0
        wins=0
        for record in records:
            if horizon not in record.get('predicted_ranges',{}):
                continue
            eligible+=1
            result=results.get((record['id'],horizon))
            if result and result.get('status')=='scored' and result.get('version')==2:
                states['scored']+=1
                wins+=int(result.get('direction_result')=='WIN')
            elif result:
                status=result.get('status')
                states[status if status in ('missing_observation','unscorable') else 'other']+=1
            else:
                start=datetime.fromisoformat(record['timestamp'].replace('Z','+00:00'))
                if start.tzinfo is None:
                    start=start.replace(tzinfo=timezone(timedelta(hours=3,minutes=30)))
                states['not_due' if start+timedelta(hours=hours)>now else 'due_not_processed']+=1
        metrics[horizon]={'all_unique':len(records),'eligible':eligible,'no_structured_range':len(records)-eligible,
                          'wins':wins,'scored':states['scored'],
                          'win_rate':round(wins/states['scored']*100,1) if states['scored'] else None,
                          'unscored':eligible-states['scored'],'states':states}
    return {'checked_at':now.isoformat(),'live_path':str(logical),'resolved_live_path':str(live),
            'code_directory':settings['code'],'data_directory':settings['data'],
            'runtime_directory':settings['runtime'],'python':settings['python'],
            'database':str(database),'sqlite_check':integrity,'all_unique_forecasts':len(records),
            'stored_observation_prices':observations,'observation_timestamps':observation_times,
            'last_observation_by_asset':last,'metrics_by_horizon':metrics,
            'structured_integration':integration,
            'scheduler_checked':False,'telegram_delivery_checked':False,
            'note':'Observation timestamps prove recording time, not the market source last-trade time. No writes or messages were performed.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-dir',default='/root/gold-tracker-bot')
    args=parser.parse_args()
    try:
        report=collect_health(args.live_dir)
    except Exception:
        # Avoid including credentials or connection strings in an error traceback.
        print('Read-only check failed; no changes were made. Inspect the active runtime path and SQLite schema privately.',file=sys.stderr)
        return 1
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['sqlite_check']=='ok' else 1


if __name__=='__main__':
    raise SystemExit(main())
