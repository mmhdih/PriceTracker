"""Integration installer tests with fake cron CLI and private fixture databases."""
import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import complete_hermes_install as installer
from tracker_store import Store


class CompleteInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base/'hermes'
        self.live = self.base/'release/live'
        self.code = self.base/'release/code'
        self.data = self.base/'data'
        self.runtime = self.base/'runtime'
        self.source = self.base/'hermes-source'
        for directory in (self.home/'cron', self.home/'scripts', self.live,
                          self.code, self.data, self.runtime, self.source/'cron', self.source/'hermes_cli'):
            directory.mkdir(parents=True)
        root = Path(installer.__file__).parent
        files = set(installer.EXPECTED) | {p.name for p in root.glob('*.py')}
        for filename in files:
            dest = self.code/filename
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Repository blobs and the Linux deployed source both use LF.
            dest.write_bytes((root/filename).read_text(encoding='utf-8').encode('utf-8'))
        (self.source/'cron/scheduler.py').write_text('"[CRON_FAILURE]"\n"[SILENT]"', encoding='utf-8')
        (self.source/'hermes_cli/cron.py').write_text('replacement_skills\nskills=final_skills', encoding='utf-8')
        settings = {'code':str(self.code), 'python':sys.executable, 'data':str(self.data), 'runtime':str(self.runtime), 'secrets':{'NEVER_PRINT':'private-fixture'}}
        (self.live.parent/'runtime.json').write_text(json.dumps(settings), encoding='utf-8')
        Store(self.data/'tracker.sqlite3')
        with closing(sqlite3.connect(self.home/'cron/executions.db')) as con, con:
            con.execute('CREATE TABLE executions(id TEXT,job_id TEXT,status TEXT,claimed_at TEXT,finished_at TEXT)')
        self.wrapper = self.home/'scripts/gold-analysis-retry.py'
        self.wrapper.write_text('print("old watcher")\n', encoding='utf-8')
        self.jobs = {
            'analysis': {'id':'analysis', 'name':'gold-analysis-8h', 'enabled':True,
                         'state':'scheduled', 'schedule':{'kind':'cron','expr':installer.SCHEDULE},
                         'deliver':'origin', 'origin':{'chat_id':'private-fixture'},
                         'prompt':'old prompt', 'skills':['gold-price-history-analysis']},
            'watcher': {'id':'watcher', 'name':'gold-analysis-retry-watcher', 'enabled':True,
                        'state':'scheduled', 'schedule':{'kind':'cron','expr':'*/5 * * * *'},
                        'deliver':'local', 'no_agent':True, 'script':'gold-analysis-retry.py'},
            'tracker': {'id':'tracker', 'name':'gold-price-tracker', 'enabled':True,
                        'state':'scheduled','schedule':'* * * * *','prompt':'unrelated'},
        }
        self.original = copy.deepcopy(self.jobs)
        self.save()
        self.calls = []
        self.fail_configure = False
        self.fail_resume_once = False
        self.args = SimpleNamespace(hermes_home=str(self.home), live_dir=str(self.live),
                                    hermes_source=str(self.source), analysis_job_id='analysis',
                                    watcher_job_id='watcher', apply=True)
        self.patch = patch.object(installer, 'LIVE_PATH', str(self.live))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def save(self):
        (self.home/'cron/jobs.json').write_text(json.dumps({'jobs':list(self.jobs.values())}), encoding='utf-8')

    def runner(self, argv, **kwargs):
        self.calls.append(argv)
        self.assertNotIn('run', argv)
        self.assertNotIn('--apply', argv)
        self.assertNotIn('begin', argv)
        if argv[:2] == ['hermes','cron']:
            if argv[2:] == ['edit','--help']:
                return subprocess.CompletedProcess(argv,0,b'--prompt TEXT --skill NAME --clear-skills\n')
            action, identity = argv[2:4]
            self.assertIn(identity, ('analysis','watcher'))
            job = self.jobs[identity]
            if action == 'pause':
                job.update(enabled=False,state='paused')
            elif action == 'resume':
                if self.fail_resume_once:
                    self.fail_resume_once = False
                    return subprocess.CompletedProcess(argv,1,b'fixture resume failure')
                job.update(enabled=True,state='scheduled')
            elif action == 'edit':
                flags = argv[4:]
                job['prompt'] = flags[flags.index('--prompt')+1]
                job['skills'] = [flags[i+1] for i,f in enumerate(flags) if f == '--skill']
            else:
                self.fail('Unexpected CLI action')
            self.save()
            return subprocess.CompletedProcess(argv,0,b'ok')
        if '--configure' in argv and self.fail_configure:
            return subprocess.CompletedProcess(argv,1,b'fixture configure failure')
        return subprocess.run(argv, **kwargs)

    def make(self):
        return installer.Installer(self.args, self.runner)

    def test_inspect_changes_no_job_or_config(self):
        self.args.apply = False
        result = self.make().install()
        self.assertEqual(result['status'],'READY_TO_INSTALL')
        self.assertEqual(self.jobs,self.original)
        self.assertFalse((self.home/'price-tracker-install-backups').exists())
        self.assertEqual(len(self.calls),1)

    def test_success_and_repeat_are_idempotent(self):
        first = self.make().install()
        self.assertEqual(first['status'],'INSTALLED')
        self.assertFalse(first['telegram_delivery_checked'])
        self.assertEqual(self.jobs['tracker'],self.original['tracker'])
        for identity in ('analysis','watcher'):
            self.assertEqual(self.jobs[identity]['schedule'],self.original[identity]['schedule'])
            self.assertEqual(self.jobs[identity]['deliver'],self.original[identity]['deliver'])
            self.assertTrue(self.jobs[identity]['enabled'])
        self.assertEqual(self.jobs['analysis']['skills'],[installer.SKILL])
        count = len(self.calls)
        second = self.make().install()
        self.assertEqual(second['status'],'ALREADY_INSTALLED')
        self.assertEqual(len(self.calls),count+1)

    def test_failure_restores_old_prompt_wrapper_states_and_metadata(self):
        old_wrapper = self.wrapper.read_bytes()
        self.fail_configure = True
        obj = self.make()
        with self.assertRaisesRegex(installer.Refused,'were restored'):
            obj.install()
        self.assertEqual(self.jobs,self.original)
        self.assertEqual(self.wrapper.read_bytes(),old_wrapper)
        self.assertIsNone(obj.config_value())
        self.assertFalse(obj.skill_path.exists())
        self.assertEqual(installer.read_json(obj.backup/'recovery.json')['phase'],'rolled_back')

    def test_failure_after_configure_and_wrapper_change_restores_only_metadata(self):
        with closing(sqlite3.connect(self.data/'tracker.sqlite3')) as con, con:
            con.execute('INSERT INTO metadata VALUES(?,?)',(installer.CONFIG_KEY,'{"old":true}'))
            con.execute('INSERT INTO metadata VALUES(?,?)',('unrelated','keep'))
        self.fail_resume_once = True
        obj = self.make()
        with self.assertRaisesRegex(installer.Refused,'were restored'):
            obj.install()
        self.assertEqual(self.jobs,self.original)
        self.assertEqual(obj.config_value(),'{"old":true}')
        with closing(sqlite3.connect(self.data/'tracker.sqlite3')) as con, con:
            self.assertEqual(con.execute('SELECT value FROM metadata WHERE key="unrelated"').fetchone()[0],'keep')

    def test_wrong_core_and_unsupported_hermes_fail_before_pausing(self):
        core = self.code/'analysis_cycles.py'
        core.write_bytes(core.read_bytes()+b'\n')
        with self.assertRaisesRegex(installer.Refused,'core differs'):
            self.make().install()
        self.assertEqual(self.jobs,self.original)
        self.assertEqual(self.calls,[])

    def test_previously_paused_jobs_stay_paused(self):
        for identity in ('analysis','watcher'):
            self.jobs[identity].update(enabled=False,state='paused')
        self.save()
        self.make().install()
        for identity in ('analysis','watcher'):
            self.assertFalse(self.jobs[identity]['enabled'])
        self.assertFalse(any(c[2]=='resume' for c in self.calls if c[0]=='hermes'))

    def test_unsupported_scheduler_and_outside_script_do_not_pause_jobs(self):
        scheduler = self.source/'cron/scheduler.py'
        scheduler.write_text('"[SILENT]"',encoding='utf-8')
        with self.assertRaisesRegex(installer.Refused,'markers'):
            self.make().install()
        self.assertEqual(self.calls,[])
        scheduler.write_text('"[SILENT]" "[CRON_FAILURE]"',encoding='utf-8')
        outside = self.base/'outside.py'
        outside.write_text('private unrelated script',encoding='utf-8')
        self.jobs['watcher']['script'] = str(outside)
        self.save()
        with self.assertRaisesRegex(installer.Refused,'inside this profile'):
            self.make().install()
        self.assertFalse(any(c[2]=='pause' for c in self.calls))
        self.assertEqual(outside.read_text(encoding='utf-8'),'private unrelated script')

    def test_explicit_recovery_uses_official_cli_and_preserves_other_jobs(self):
        obj = self.make()
        obj.install()
        restored = self.make().recover(obj.backup)
        self.assertEqual(restored['status'],'ROLLED_BACK')
        self.assertEqual(self.jobs,self.original)

    def test_recovery_refuses_a_later_independent_wrapper_edit(self):
        obj = self.make()
        obj.install()
        self.wrapper.write_text('later independent edit',encoding='utf-8')
        count = len(self.calls)
        with self.assertRaisesRegex(installer.Refused,'changed independently'):
            self.make().recover(obj.backup)
        self.assertEqual(len(self.calls),count)

    def test_interrupted_journal_requires_recovery_before_another_install(self):
        path = self.home/'price-tracker-install-backups/interrupted/recovery.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'ids':['analysis','watcher'],'phase':'changing'}),encoding='utf-8')
        with self.assertRaisesRegex(installer.Refused,'interrupted integration'):
            self.make().install()
        self.assertEqual(self.calls,[])
        self.assertEqual(self.jobs,self.original)


if __name__ == '__main__':
    unittest.main()
