#!/usr/bin/env python3
"""Finish the two Hermes connections for the verified 08ff97bb core. Inspect by default.

Uses the official Hermes CLI for job mutations. Never triggers a job or sends a
message. Backups are private; --rollback restores only these integration changes.
"""
import argparse
import base64
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time

CORE = '08ff97bb4dd41f00cb65c0cd530800a1f0b33976'
LIVE_PATH = '/root/gold-tracker-bot'
SKILL = 'price-tracker-structured-analysis'
CONFIG_KEY = 'gold_analysis_retry_config_v1'
SCHEDULE = '0 3,6,8,11,14,17 * * *'
EXPECTED = {
    'analysis_cycles.py': '938c17be1880c658c105d8902eab8fe4b6936096ce593ec1608a81c6a708e46f',
    'retry_watcher.py': 'e62a82de990d80f5428dc422624879fca566277b96804c710fbf6d4c27beac54',
    'analysis_engine.py': '438c70c4ade078374d0c4db985ea23d9201a72e2d7fec09870fd55ea1ce9a38b',
    'forecast_schema.py': '31067546435ed8fafd83bd4f3876057731f9716a84862f226768de6d195f1abe',
    'tracker_store.py': '82d2ef2d548e5a0d839ca3c665c68e2cfec93a49566fe739e4bcec22e9f621bd',
    'tools/check_runtime.py': '928abefe312d18659f45a20582e8f7d1dad2d814bdc40d15852ba2ffd22d7c9d',
    'HERMES_LLM_JOB_PROMPT_FA.txt': 'fdb4441abe779c31b60fb0cc5958b7624ac63fc78ad04dba7bd50036b23a3e1b',
    'ops/price-tracker-structured-analysis/SKILL.md': '6c5d6d7518025f55845a1165750ecf8414f14c92f48ff7235378b9edcaa48678',
    'ops/gold-analysis-retry.py': '1e3628030b08556ff33860e5530b1a2a3a46ab274ef85ce797bf2a6d5752f779',
}
STABLE = ('id', 'name', 'schedule', 'timezone', 'deliver', 'origin', 'delivery',
          'failure_deliver', 'script', 'no_agent', 'interpreter', 'model', 'provider',
          'model_provider', 'reasoning_effort', 'workdir', 'repeat', 'context_from',
          'monitor_script', 'monitor_url', 'pinned')


class Refused(RuntimeError):
    """Only fixed, nonsecret messages may be surfaced to stdout."""


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def sha(content):
    return hashlib.sha256(content).hexdigest()


def private_write(path, content, mode=0o600):
    """Same-directory replacement; preserve existing Unix owner/group."""
    path = Path(path)
    if path.is_symlink():
        raise Refused('Refusing to replace a symlink')
    old = path.stat() if path.exists() else None
    descriptor, temp = tempfile.mkstemp(prefix='.'+path.name+'-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        if old and hasattr(os, 'chown'):
            os.chown(temp, old.st_uid, old.st_gid)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def json_write(path, value):
    private_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8'))


@contextmanager
def install_lock(home):
    # Held through recovery as well; no guard or gateway lifecycle operations.
    import fcntl
    with (home/'.price-tracker-integration.lock').open('a+b') as handle:
        os.chmod(handle.name, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused('Another integration installer is running')
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class Installer:
    def __init__(self, args, runner=subprocess.run):
        self.args, self.runner = args, runner
        self.home = Path(args.hermes_home).expanduser().resolve(strict=True)
        self.live = Path(args.live_dir).resolve(strict=True)
        settings = read_json(self.live.parent/'runtime.json')
        self.code = Path(settings['code']).resolve(strict=True)
        self.python = Path(settings['python']).resolve(strict=True)
        self.database = (Path(settings['data'])/'tracker.sqlite3').resolve(strict=True)
        self.env = {**os.environ, 'HERMES_HOME': str(self.home),
                    'GOLD_DATA_DIR': str(self.database.parent),
                    'GOLD_RUNTIME_DIR': str(Path(settings['runtime']).resolve(strict=True))}
        self.ids = (args.analysis_job_id, args.watcher_job_id)
        if len(set(self.ids)) != 2:
            raise Refused('Two distinct job IDs are required')
        self.backup = None
        self.journal = None

    def command(self, argv):
        result = self.runner(argv, env=self.env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, timeout=30, check=False)
        if self.backup:
            with (self.backup/'commands.log').open('ab') as log:
                os.chmod(log.name, 0o600)
                log.write(result.stdout if isinstance(result.stdout, bytes) else str(result.stdout).encode('utf-8'))
                log.write(b'\n')
        if result.returncode != 0:
            raise Refused('A command failed; inspect the private backup log')
        return result.stdout.decode('utf-8') if isinstance(result.stdout, bytes) else result.stdout

    def cron(self, action, identity, *flags):
        self.command(['hermes', 'cron', action, identity, *flags])

    def jobs(self):
        raw = read_json(self.home/'cron/jobs.json')
        rows = raw.get('jobs') if isinstance(raw, dict) else raw
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
            raise Refused('Unsupported Hermes jobs schema')
        if len({r['id'] for r in rows}) != len(rows):
            raise Refused('Duplicate job IDs')
        return {r['id']: r for r in rows}

    def executions_idle(self):
        ledger = self.home/'cron/executions.db'
        with closing(sqlite3.connect(ledger.resolve(strict=True).as_uri()+'?mode=ro', uri=True)) as con:
            columns = {r[1] for r in con.execute('PRAGMA table_info(executions)')}
            if not {'id', 'job_id', 'status', 'claimed_at', 'finished_at'} <= columns:
                raise Refused('Unsupported Hermes execution schema')
            active = con.execute("SELECT COUNT(*) FROM executions WHERE job_id IN (?,?) AND status IN ('running','claimed')", self.ids).fetchone()[0]
        return active == 0 and all(self.jobs()[i].get('state') != 'running' for i in self.ids)

    def wait_idle(self):
        deadline = time.monotonic()+30
        while not self.executions_idle():
            if time.monotonic() >= deadline:
                raise Refused('A selected job is still executing; no files were replaced')
            time.sleep(1)

    def config_value(self):
        with closing(sqlite3.connect(self.database.as_uri()+'?mode=ro', uri=True)) as con:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'metadata', 'analysis_cycles', 'retry_claims'} <= tables:
                raise Refused('Active project schema is older than the required core')
            if con.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise Refused('Project database integrity check failed')
            row = con.execute('SELECT value FROM metadata WHERE key=?', (CONFIG_KEY,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def skills(job):
        return job.get('skills') or ([job['skill']] if job.get('skill') else [])

    @staticmethod
    def expression(job):
        value = job.get('schedule')
        return value.get('expr') if isinstance(value, dict) else value

    def preflight(self):
        for name, digest in EXPECTED.items():
            if sha((self.code/name).read_bytes()) != digest:
                raise Refused('Installed core differs from verified 08ff97bb: '+name)
        if str(self.args.live_dir).rstrip('/') != LIVE_PATH:
            raise Refused('This prompt and wrapper require the existing /root/gold-tracker-bot path')
        source = Path(self.args.hermes_source).resolve(strict=True)
        scheduler = (source/'cron/scheduler.py').read_text(encoding='utf-8')
        if not all(marker in scheduler for marker in ('[CRON_FAILURE]', '[SILENT]')):
            raise Refused('Installed Hermes does not provide the required scheduler markers')
        cli_source = (source/'hermes_cli/cron.py').read_text(encoding='utf-8')
        if not all(token in cli_source for token in ('replacement_skills', 'skills=final_skills')):
            raise Refused('Installed Hermes CLI skill replacement needs separate compatibility review')
        help_text = self.command(['hermes', 'cron', 'edit', '--help'])
        for flag in ('--prompt', '--skill', '--clear-skills'):
            if not re.search(re.escape(flag)+r'(?=[\s,=])', help_text):
                raise Refused('Installed Hermes CLI lacks a required editing flag')
        self.before = self.jobs()
        analysis, watcher = (self.before[i] for i in self.ids)
        if analysis.get('name') != 'gold-analysis-8h' or watcher.get('name') != 'gold-analysis-retry-watcher':
            raise Refused('Job identity/name mismatch')
        if self.expression(analysis) != SCHEDULE or analysis.get('deliver') != 'origin' or analysis.get('no_agent'):
            raise Refused('Analysis schedule, mode or delivery differs from the supported installation')
        for job in (analysis, watcher):
            schedule = job.get('schedule')
            if job.get('timezone') not in (None, '', 'UTC', 'Etc/UTC') or (isinstance(schedule, dict) and schedule.get('timezone') not in (None, '', 'UTC', 'Etc/UTC')):
                raise Refused('Selected job has a timezone other than the verified UTC schedule')
            if not ((job.get('enabled') is True and job.get('state') in ('scheduled', 'running')) or
                    (job.get('enabled') is False and job.get('state') == 'paused')):
                raise Refused('Selected job has an unsupported lifecycle state')
        if self.expression(watcher) != '*/5 * * * *' or watcher.get('deliver') != 'local' or watcher.get('no_agent') is not True:
            raise Refused('Watcher schedule, mode or delivery differs from the supported installation')
        if (self.home/'scripts').is_symlink():
            raise Refused('Refusing a symlinked profile scripts directory')
        scripts = (self.home/'scripts').resolve(strict=True)
        raw_wrapper = Path(str(watcher.get('script') or ''))
        candidate = raw_wrapper if raw_wrapper.is_absolute() else scripts/raw_wrapper
        self.wrapper = candidate.resolve(strict=True)
        if candidate.is_symlink() or not self.wrapper.is_relative_to(scripts) or not self.wrapper.is_file() or self.wrapper.suffix != '.py':
            raise Refused('Watcher must reference a regular Python file inside this profile scripts directory')
        self.skill_path = self.home/'skills'/SKILL/'SKILL.md'
        if (self.home/'skills').is_symlink() or self.skill_path.parent.is_symlink() or self.skill_path.is_symlink():
            raise Refused('Refusing a symlinked skill destination')
        self.prompt = (self.code/'HERMES_LLM_JOB_PROMPT_FA.txt').read_text(encoding='utf-8')
        self.skill = (self.code/'ops'/SKILL/'SKILL.md').read_bytes()
        self.wrapper_bytes = (self.code/'ops/gold-analysis-retry.py').read_bytes()
        if self.skill_path.exists() and self.skill_path.read_bytes() != self.skill:
            raise Refused('A different skill already occupies the new skill name')
        self.old_config = self.config_value()
        self.executions_idle()  # Validate schema without requiring a currently idle scheduler.
        expected_config = {'hermes_home': str(self.home), 'job_id': self.ids[0]}
        return (analysis.get('prompt') == self.prompt and self.skills(analysis) == [SKILL] and
                self.skill_path.exists() and self.wrapper.read_bytes() == self.wrapper_bytes and
                self.old_config is not None and json.loads(self.old_config) == expected_config)

    def verify(self):
        jobs = self.jobs()
        if set(jobs) != set(self.before):
            raise Refused('Job inventory changed during installation')
        for identity, old in self.before.items():
            fields = STABLE if identity == self.ids[0] else (*STABLE, 'prompt', 'skills', 'skill')
            if any(jobs[identity].get(k) != old.get(k) for k in fields):
                raise Refused('An unrelated job setting changed during installation')
        analysis = jobs[self.ids[0]]
        if analysis.get('prompt') != self.prompt or self.skills(analysis) != [SKILL]:
            raise Refused('Official CLI did not save the expected analysis prompt and skill')
        if self.wrapper.read_bytes() != self.wrapper_bytes or self.skill_path.read_bytes() != self.skill:
            raise Refused('Installed wrapper or skill content differs')
        if json.loads(self.config_value() or 'null') != {'hermes_home': str(self.home), 'job_id': self.ids[0]}:
            raise Refused('Watcher configuration was not saved correctly')

    def set_prompt(self, job):
        flags = ['--prompt', job.get('prompt') or '']
        skills = self.skills(job)
        flags += [part for skill in skills for part in ('--skill', skill)] if skills else ['--clear-skills']
        self.cron('edit', self.ids[0], *flags)

    def pause_selected(self):
        for identity in self.ids:
            job = self.jobs()[identity]
            if job.get('enabled') is True or job.get('state') != 'paused':
                self.cron('pause', identity)
            after = self.jobs()[identity]
            if after.get('enabled') is not False or after.get('state') != 'paused':
                raise Refused('Official CLI did not pause a selected job')
        self.wait_idle()

    def restore_states(self):
        for identity in self.ids:
            old = self.before[identity]
            if old['enabled']:
                self.cron('resume', identity)
            current = self.jobs()[identity]
            if current.get('enabled') != old['enabled'] or current.get('state') != ('scheduled' if old['enabled'] else 'paused'):
                raise Refused('A selected job did not return to its previous enabled state')

    def rollback(self):
        self.pause_selected()
        self.set_prompt(self.before[self.ids[0]])
        private_write(self.wrapper, base64.b64decode(self.journal['wrapper_b64']), self.journal['wrapper_mode'])
        with closing(sqlite3.connect(self.database, timeout=30)) as con, con:
            if self.journal['config'] is None:
                con.execute('DELETE FROM metadata WHERE key=?', (CONFIG_KEY,))
            else:
                con.execute('INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (CONFIG_KEY, self.journal['config']))
        if not self.journal['skill_existed'] and self.skill_path.exists():
            if sha(self.skill_path.read_bytes()) != EXPECTED['ops/price-tracker-structured-analysis/SKILL.md']:
                raise Refused('Skill changed after installation; retain backup and inspect before recovery')
            self.skill_path.unlink()
        current = self.jobs()[self.ids[0]]
        if current.get('prompt') != self.before[self.ids[0]].get('prompt') or self.skills(current) != self.skills(self.before[self.ids[0]]):
            raise Refused('Old analysis prompt could not be verified; selected jobs remain paused')
        self.restore_states()
        self.journal['phase'] = 'rolled_back'
        json_write(self.backup/'recovery.json', self.journal)

    def install(self):
        backups = self.home/'price-tracker-install-backups'
        if backups.is_symlink():
            raise Refused('Refusing a symlinked backup directory')
        for journal in backups.glob('*/recovery.json'):
            previous = read_json(journal)
            if tuple(previous.get('ids', [])) == self.ids and previous.get('phase') not in ('installed', 'rolled_back'):
                raise Refused('An interrupted integration needs --rollback using its existing private backup before retrying')
        ready = self.preflight()
        if not self.args.apply or ready:
            return {'status': 'ALREADY_INSTALLED' if ready else 'READY_TO_INSTALL',
                    'changed': False, 'core': CORE, 'telegram_delivery_checked': False}
        root = self.home/'price-tracker-install-backups'
        if root.is_symlink():
            raise Refused('Refusing a symlinked backup directory')
        root.mkdir(mode=0o700, exist_ok=True)
        os.chmod(root, 0o700)
        self.backup = Path(tempfile.mkdtemp(prefix=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-'), dir=root))
        self.journal = {'phase': 'prepared', 'jobs': {i: self.before[i] for i in self.ids},
                        'ids': self.ids, 'home': str(self.home), 'live': str(self.live),
                        'database': str(self.database), 'wrapper': str(self.wrapper),
                        'wrapper_b64': base64.b64encode(self.wrapper.read_bytes()).decode('ascii'),
                        'wrapper_mode': self.wrapper.stat().st_mode & 0o777,
                        'skill_path': str(self.skill_path), 'skill_existed': self.skill_path.exists(),
                        'config': self.old_config}
        json_write(self.backup/'recovery.json', self.journal)
        try:
            self.pause_selected()
            self.journal['phase'] = 'changing'
            json_write(self.backup/'recovery.json', self.journal)
            self.skill_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            private_write(self.skill_path, self.skill)
            self.set_prompt({'prompt': self.prompt, 'skills': [SKILL]})
            self.command([str(self.python), str(self.code/'retry_watcher.py'), '--configure', '--hermes-home', str(self.home), '--job-id', self.ids[0]])
            private_write(self.wrapper, self.wrapper_bytes, self.journal['wrapper_mode'])
            self.verify()
            decision = json.loads(self.command([str(self.python), str(self.code/'retry_watcher.py')]))
            if decision.get('action') != 'skip' or decision.get('reason') != 'job_not_enabled':
                raise Refused('Paused watcher dry-run did not produce the expected safe skip')
            self.restore_states()
            self.verify()
            self.journal['phase'] = 'installed'
            json_write(self.backup/'recovery.json', self.journal)
        except BaseException:
            try:
                self.rollback()
            except BaseException:
                raise Refused('Installation and recovery need review; do not resume selected jobs; use the private backup') from None
            raise Refused('Installation stopped; old integration and enabled states were restored') from None
        return {'status': 'INSTALLED', 'core': CORE, 'private_backup': str(self.backup),
                'prompt_sha256': sha(self.prompt.encode('utf-8')), 'skill': SKILL,
                'wrapper_sha256': sha(self.wrapper_bytes), 'watcher_configured': True,
                'previous_enabled_states_restored': True, 'jobs_triggered': False,
                'telegram_delivery_checked': False, 'normal_cycle': 'not_observed_by_installer'}

    def recover(self, directory):
        self.backup = Path(directory).resolve(strict=True)
        root = (self.home/'price-tracker-install-backups').resolve(strict=True)
        if self.backup.parent != root:
            raise Refused('Recovery must use a backup inside this profile backup directory')
        self.journal = read_json(self.backup/'recovery.json')
        if (self.journal['home'] != str(self.home) or self.journal['live'] != str(self.live) or
                self.journal['database'] != str(self.database) or tuple(self.journal['ids']) != self.ids):
            raise Refused('Recovery backup belongs to a different installation or job pair')
        self.before = self.journal['jobs']
        self.wrapper = Path(self.journal['wrapper'])
        self.skill_path = Path(self.journal['skill_path'])
        if (self.home/'scripts').is_symlink() or not self.wrapper.resolve(strict=True).is_relative_to((self.home/'scripts').resolve(strict=True)) or self.wrapper.is_symlink():
            raise Refused('Recovery wrapper path is no longer safe')
        if self.skill_path != self.home/'skills'/SKILL/'SKILL.md':
            raise Refused('Unexpected recovery skill path')
        if (self.home/'skills').is_symlink() or self.skill_path.parent.is_symlink() or self.skill_path.is_symlink():
            raise Refused('Recovery skill destination is symlinked')
        current = self.jobs()[self.ids[0]]
        old = self.before[self.ids[0]]
        new_prompt = (self.code/'HERMES_LLM_JOB_PROMPT_FA.txt').read_text(encoding='utf-8')
        if (current.get('prompt') not in (old.get('prompt'), new_prompt) or
                self.skills(current) not in (self.skills(old), [SKILL])):
            raise Refused('Analysis configuration changed independently; recovery needs review')
        if sha(self.wrapper.read_bytes()) not in (sha(base64.b64decode(self.journal['wrapper_b64'])), EXPECTED['ops/gold-analysis-retry.py']):
            raise Refused('Watcher wrapper changed independently; recovery needs review')
        current_config = self.config_value()
        new_config = {'hermes_home': str(self.home), 'job_id': self.ids[0]}
        if current_config != self.journal['config'] and json.loads(current_config or 'null') != new_config:
            raise Refused('Watcher configuration changed independently; recovery needs review')
        self.rollback()
        return {'status': 'ROLLED_BACK', 'jobs_triggered': False, 'telegram_delivery_checked': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-dir', default='/root/gold-tracker-bot')
    parser.add_argument('--hermes-home', required=True)
    parser.add_argument('--hermes-source', default='/usr/local/lib/hermes-agent')
    parser.add_argument('--analysis-job-id', required=True)
    parser.add_argument('--watcher-job-id', required=True)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--apply', action='store_true')
    group.add_argument('--rollback', type=Path)
    args = parser.parse_args(argv)
    installer = None
    try:
        if os.name != 'posix':
            raise Refused('Run this installer on the Linux VPS')
        installer = Installer(args)
        if args.apply or args.rollback:
            with install_lock(installer.home):
                result = installer.recover(args.rollback) if args.rollback else installer.install()
        else:
            result = installer.install()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (Exception, KeyboardInterrupt) as error:
        result = {'status': 'STOPPED', 'reason': str(error) if isinstance(error, Refused) else 'Preflight or installation error; inspect paths and private logs', 'jobs_triggered': False}
        if installer and installer.backup:
            result['private_backup'] = str(installer.backup)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
