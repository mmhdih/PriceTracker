"""Private fake Hermes ledgers; no real CLI, network, Telegram or scheduler writes."""
from contextlib import closing
import concurrent.futures
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

from analysis_cycles import slot_bounds, cycle_key, SCHEDULE
from tracker_store import Store, encoded
import retry_watcher as watcher

NOW = datetime(2026,10,24,11,25,tzinfo=timezone.utc)


class RetryWatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base/'hermes'
        (self.home/'cron').mkdir(parents=True)
        self.store = Store(self.base/'tracker.sqlite3')
        self.config = {'hermes_home':str(self.home),'job_id':'fixture-job'}
        self.job = {'id':'fixture-job','name':'gold-analysis-8h','enabled':True,'state':'scheduled',
                    'schedule':{'kind':'cron','expr':SCHEDULE},
                    'prompt':watcher.PROTOCOL+' analysis_cycles.py',
                    'next_run_at':slot_bounds(NOW)[1].isoformat()}
        self.save_job()
        self.ledger = self.home/'cron/executions.db'
        with closing(sqlite3.connect(self.ledger)) as con, con:
            con.execute('CREATE TABLE executions(id TEXT PRIMARY KEY,job_id TEXT,status TEXT,claimed_at TEXT,finished_at TEXT,delivery_outcome TEXT)')
        self.add_execution('failure', NOW-timedelta(minutes=24), NOW-timedelta(minutes=15))

    def save_job(self):
        (self.home/'cron/jobs.json').write_text(json.dumps({'jobs':[self.job]}),encoding='utf-8')

    def add_execution(self, identity, start, end=None, status='failed', delivery=None):
        with closing(sqlite3.connect(self.ledger)) as con, con:
            con.execute('INSERT INTO executions VALUES(?,?,?,?,?,?)',
                        (identity,'fixture-job',status,start.isoformat(),end.isoformat() if end else None,delivery))

    def plan(self, now=NOW):
        return watcher.plan(self.config,self.store.path,now.isoformat())

    def claim_count(self):
        with self.store.connection() as con:
            return con.execute('SELECT COUNT(*) FROM retry_claims').fetchone()[0]

    def test_dry_plan_is_read_only_and_profile_scoped(self):
        self.assertEqual(self.plan()['action'],'retry')
        self.assertEqual(self.claim_count(),0)
        # An unrelated failed job never changes the selected job's decision.
        with closing(sqlite3.connect(self.ledger)) as con, con:
            con.execute('INSERT INTO executions VALUES(?,?,?,?,?,?)',
                        ('unrelated','different-job','running',NOW.isoformat(),None,None))
        self.assertEqual(self.plan()['action'],'retry')

    def test_concurrent_watchers_can_dispatch_at_most_once_for_slot(self):
        decision = self.plan()
        calls = []
        def runner(argv, **kw):
            calls.append(argv)
            self.assertEqual(kw['env']['HERMES_HOME'],str(self.home))
            return subprocess.CompletedProcess(argv,0)
        def attempt(_):
            return watcher.execute(self.config,self.store,decision,runner,NOW.isoformat())
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            outcomes = list(pool.map(attempt,range(6)))
        self.assertEqual(len(calls),1)
        self.assertEqual(self.claim_count(),1)
        self.assertEqual(sum(o['action']=='attempted' for o in outcomes),1)
        self.assertEqual(self.plan()['reason'],'slot_retry_budget_used')

    def test_failed_cli_or_uncertain_dispatch_is_not_hidden_or_repeated(self):
        for outcome in ['nonzero','timeout','missing']:
            with self.subTest(outcome=outcome):
                with self.store.connection() as con:
                    con.execute('DELETE FROM retry_claims')
                def runner(argv, **kw):
                    if outcome=='timeout': raise subprocess.TimeoutExpired(argv,60)
                    if outcome=='missing': raise FileNotFoundError('hermes')
                    return subprocess.CompletedProcess(argv,2)
                result = watcher.execute(self.config,self.store,self.plan(),runner,NOW.isoformat())
                self.assertIn(result['state'],('dispatch_failed_review_required','dispatch_unknown_review_required'))
                self.assertEqual(self.plan()['reason'],'prior_dispatch_requires_review')

    def test_new_slot_failure_has_its_own_retry_budget(self):
        watcher.execute(self.config,self.store,self.plan(),lambda argv,**kw:subprocess.CompletedProcess(argv,0),NOW.isoformat())
        later = NOW+timedelta(hours=3)
        self.job['next_run_at'] = slot_bounds(later)[1].isoformat()
        self.save_job()
        self.add_execution('next-failure',later-timedelta(minutes=24),later-timedelta(minutes=15))
        self.assertEqual(self.plan(later)['action'],'retry')

    def test_running_paused_and_pending_native_retry_are_never_retriggered(self):
        self.job['enabled']=False
        self.save_job()
        self.assertEqual(self.plan()['reason'],'job_not_enabled')
        self.job['enabled']=True
        self.job['next_run_at']=(NOW+timedelta(minutes=5)).isoformat()
        self.save_job()
        self.assertEqual(self.plan()['reason'],'another_dispatch_or_native_retry_pending')
        self.job['next_run_at']=slot_bounds(NOW)[1].isoformat()
        self.save_job()
        self.add_execution('live',NOW-timedelta(minutes=2),None,'running')
        self.assertEqual(self.plan()['reason'],'analysis_in_flight')

    def test_completed_or_unknown_execution_is_never_retriggered(self):
        for state in ['completed','unknown']:
            self.add_execution(state,NOW-timedelta(minutes=2),NOW-timedelta(minutes=1),state,'failed')
            self.assertEqual(self.plan()['reason'],'no_confirmed_execution_failure')

    def test_committed_report_or_existing_saved_forecast_needs_review(self):
        key = cycle_key(slot_bounds(NOW)[0])
        with self.store.connection() as con:
            con.execute('INSERT INTO analysis_cycles VALUES(?,?)',(key,encoded({'state':'committed'})))
        self.assertEqual(self.plan()['reason'],'forecast_already_saved_do_not_regenerate')
        with self.store.connection() as con:
            con.execute('DELETE FROM analysis_cycles')
        ts=(NOW-timedelta(minutes=22)).isoformat()
        self.store.put({'timestamp':ts,'forecast_text':'saved before failure',
                        'snapshot':{'collection_time':ts,'prices':{'geram18':100}},'predicted_ranges':{}})
        self.assertEqual(self.plan()['reason'],'forecast_already_saved_do_not_regenerate')

    def test_missing_database_or_schema_never_creates_an_empty_hermes_store(self):
        absent = self.home/'cron/absent.db'
        with self.assertRaises(FileNotFoundError): watcher.readonly(absent)
        self.assertFalse(absent.exists())
        with closing(sqlite3.connect(self.ledger)) as con, con:
            con.execute('DROP TABLE executions')
            con.execute('CREATE TABLE executions(job_id TEXT)')
        with self.assertRaises(ValueError): self.plan()

    def test_ambiguous_delivery_and_wrong_protocol_refuse_retry(self):
        with closing(sqlite3.connect(self.ledger)) as con, con:
            con.execute("UPDATE executions SET delivery_outcome='unknown'")
        self.assertEqual(self.plan()['reason'],'delivery_needs_reconciliation')
        self.job['prompt']='legacy free-form prompt'
        self.save_job()
        with self.assertRaises(ValueError): self.plan()


if __name__=='__main__':
    unittest.main()
