#!/usr/bin/env bash
# PriceTracker updater. Default: prepare only; no live change or Telegram send.
set -Eeuo pipefail
PYTHON_BIN="${PRICE_TRACKER_PYTHON:-python3}"
command -v "$PYTHON_BIN" >/dev/null || { echo 'Python 3.10+ is required.' >&2; exit 1; }
exec "$PYTHON_BIN" - "$@" <<'PY'
import argparse
import ast
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import runpy
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import uuid
import zipfile

PINNED_COMMIT = 'e58e0b0844307266521c2f1669d029b8a011ab99'
JSON_NAMES = ('analysis_db.json', 'rich_analysis_db.json', 'daily_prices.json', 'finance_db.json')
SECRET_NAMES = ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID', 'FINANCE_BOT_TOKEN')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name+'.')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name!='nt':
            directory=os.open(path.parent,os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def within(path, parent):
    return Path(path).resolve().is_relative_to(Path(parent).resolve())


def file_hashes(directory):
    directory = Path(directory)
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(directory.glob('*.json')) if p.name in JSON_NAMES}


def sqlite_copy(source, target):
    with closing(sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro', uri=True)) as src:
        with closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('SQLite integrity check failed')


def copy_data(source, target):
    source, target = Path(source), Path(target)
    target.mkdir(parents=True, exist_ok=False)
    before = file_hashes(source)
    for name in JSON_NAMES:
        path = source/name
        if path.exists():
            content = path.read_bytes()
            if not isinstance(json.loads(content.decode('utf-8-sig')), dict):
                raise ValueError('Invalid database schema: '+name)
            (target/name).write_bytes(content)
    if (source/'tracker.sqlite3').exists():
        sqlite_copy(source/'tracker.sqlite3', target/'tracker.sqlite3')
    if file_hashes(source) != before:
        raise RuntimeError('Source data changed during snapshot; pause writers and retry')
    if not any((target/name).exists() for name in ('analysis_db.json','rich_analysis_db.json','tracker.sqlite3')):
        raise RuntimeError('No analysis history found; refusing to create an empty replacement')
    return before


def extract_archive(archive, code, commit):
    code = Path(code)
    code.mkdir(parents=True, exist_ok=False)
    expected = 'PriceTracker-'+commit
    with zipfile.ZipFile(archive) as package:
        if sum(info.file_size for info in package.infolist()) > 100*1024*1024:
            raise ValueError('Source archive too large')
        for info in package.infolist():
            parts = PurePosixPath(info.filename).parts
            if not parts or parts[0] != expected or '..' in parts or '\\' in info.filename:
                raise ValueError('Unexpected archive path')
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError('Archive symlinks are not accepted')
            if len(parts) == 1 or info.is_dir():
                continue
            target = code.joinpath(*parts[1:])
            if not within(target, code):
                raise ValueError('Unsafe archive path')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(package.read(info))
    for name in ('pipeline.py','migrate_databases.py','requirements.txt','tracker_store.py','tests/test_review_findings.py'):
        if not (code/name).is_file():
            raise ValueError('Incomplete source archive: '+name)


def credentials(source, previous=None):
    values = dict(previous or {})
    for file in ('price_tracker.py','finance_bot_listener.py'):
        path = Path(source)/file
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding='utf-8-sig'))
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                for name in node.targets:
                    if isinstance(name, ast.Name):
                        key = 'FINANCE_BOT_TOKEN' if name.id == 'BOT_TOKEN' else name.id
                        if key in SECRET_NAMES and node.value.value and 'YOUR_' not in node.value.value:
                            values[key] = node.value.value
    # Read literals only. Never import old code or source shell/.env files.
    for key in SECRET_NAMES:
        if os.environ.get(key):
            values[key] = os.environ[key]
    return {key: str(values[key]) for key in SECRET_NAMES if values.get(key)}


def running_writers(paths):
    prefixes = [str(Path(path).resolve())+os.sep for path in paths]
    found = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit() or int(entry.name) in (os.getpid(), os.getppid()):
            continue
        try:
            args = (entry/'cmdline').read_bytes().decode(errors='replace').split('\0')
        except (OSError, ProcessLookupError):
            continue
        try:
            cwd=str((entry/'cwd').resolve())+os.sep
        except OSError:
            cwd=''
        if any(prefix in arg for arg in args for prefix in prefixes) or any(cwd.startswith(prefix) for prefix in prefixes):
            found.append(int(entry.name))
    return sorted(found)


def quiescent(args, paths):
    if not args.quiesced:
        raise RuntimeError('Pause ONLY the gold jobs/services (including external schedulers), then add --quiesced. No jobs were stopped by this script.')
    pids = running_writers(paths)
    if pids:
        raise RuntimeError('Existing processes still use the project; PIDs: '+', '.join(map(str,pids)))


def environment(data, runtime):
    return {**os.environ, 'GOLD_DATA_DIR': str(data), 'GOLD_RUNTIME_DIR': str(runtime), 'PYTHONUTF8':'1'}


def checked(command, log, env=None, cwd=None, timeout=300):
    with Path(log).open('a', encoding='utf-8') as stream:
        result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, env=env, cwd=cwd, timeout=timeout)
    if result.returncode:
        raise RuntimeError('Check failed; private log: '+str(log))


def validate_and_merge(code, python, data, runtime, log):
    runtime.mkdir(parents=True, exist_ok=True)
    env = environment(data, runtime)
    if any((data/name).exists() for name in ('analysis_db.json','rich_analysis_db.json')):
        report_path = data.parent/('merge-'+uuid.uuid4().hex+'.json')
        command = [str(python),str(code/'migrate_databases.py'),'--source-dir',str(data),'--destination',str(data/'tracker.sqlite3'),'--apply']
        with report_path.open('w',encoding='utf-8') as output, Path(log).open('a',encoding='utf-8') as errors:
            subprocess.run(command, env=env, stdout=output, stderr=errors, check=True, timeout=300)
        report = read(report_path)
        if report['integrity_check'] != 'ok' or report['archived_invalid_forecasts']:
            raise RuntimeError('Migration needs manual review; original data is unchanged')
    checked([str(python),str(code/'rich_db.py')],log,env,code)
    with closing(sqlite3.connect(data/'tracker.sqlite3')) as con:
        if con.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('Merged DB failed integrity check')


def build_launchers(release, data, runtime, secrets):
    release = Path(release)
    code, live = release/'code', release/'live'
    live.mkdir(exist_ok=True)
    python = release/'venv/bin/python'
    settings = {'code':str(code),'python':str(python),'data':str(data),'runtime':str(runtime),
                'secrets':secrets,'python_version':list(sys.version_info[:2])}
    write(release/'runtime.json',settings)
    bootstrap = '''import json, os, pathlib, runpy, site, sys
_settings=json.loads(pathlib.Path(SETTINGS).read_text(encoding='utf-8'))
def execute(filename, namespace):
    env=dict(os.environ)
    for key,value in _settings['secrets'].items(): env.setdefault(key,value)
    env['GOLD_DATA_DIR']=_settings['data']
    env['GOLD_RUNTIME_DIR']=_settings['runtime']
    env['GOLD_PYTHON']=_settings['python']
    real=str(pathlib.Path(_settings['code'])/filename)
    if namespace['__name__']=='__main__':
        os.execve(_settings['python'],[_settings['python'],real,*sys.argv[1:]],env)
    if list(sys.version_info[:2])!=_settings['python_version']:
        raise RuntimeError('Use the updater-managed Python interpreter for imports')
    os.environ.update(env)
    site.addsitedir(str(pathlib.Path(_settings['python']).parents[1]/'lib'/('python%d.%d'%sys.version_info[:2])/'site-packages'))
    sys.path.insert(0,_settings['code'])
    namespace.update(runpy.run_path(real,run_name=namespace['__name__']))
'''.replace('SETTINGS',repr(str(release/'runtime.json')))
    (live/'_deployment_bootstrap.py').write_text(bootstrap,encoding='utf-8')
    for file in code.glob('*.py'):
        (live/file.name).write_text('from _deployment_bootstrap import execute\nexecute('+repr(file.name)+',globals())\n',encoding='utf-8')
    shell = '#!/bin/sh\nset -eu\nDIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\nexec '+shlex_quote(sys.executable)+' "$DIR/cron_run_v2.py" "$@"\n'
    (live/'cron_run.sh').write_text(shell,encoding='utf-8')
    (live/'cron_run.sh').chmod(0o700)
    for name in (*JSON_NAMES,'tracker.sqlite3'):
        link=live/name
        if link.is_symlink():
            link.unlink()
        link.symlink_to(data/name)


def shlex_quote(text):
    import shlex
    return shlex.quote(str(text))


def preserve_output_files(source, release, data):
    # This server-specific output was confirmed in the production file listing.
    # Preserve its old public path; it may be a scheduler redirection target.
    source,release,data=Path(source),Path(release),Path(data)
    for name in ('final_forecast_output.txt',):
        original=source/name
        if not original.exists():
            continue
        if not original.is_file():
            raise RuntimeError('Expected regular output file: '+name)
        before=(original.stat().st_size,original.stat().st_mtime_ns)
        shutil.copy2(original,data/name)
        after=(original.stat().st_size,original.stat().st_mtime_ns)
        if before!=after:
            raise RuntimeError('Output changed during copy; pause its writer and retry')
        (release/'live'/name).symlink_to(data/name)


def custom_entrypoints(source, code):
    return sorted(p.name for p in Path(source).iterdir()
                  if p.suffix in ('.py','.sh') and not (Path(code)/p.name).exists()
                  and p.name not in ('_deployment_bootstrap.py','safe_update.sh'))


def prepare(args, live, state):
    # A failed new attempt must not leave an older prepared release activatable.
    if (state/'prepared.json').exists():
        os.replace(state/'prepared.json',state/('prepared-superseded-'+uuid.uuid4().hex+'.json'))
    source=live.resolve()
    active=read(state/'active.json') if (state/'active.json').exists() else None
    source_data=Path(args.data_dir).resolve() if args.data_dir else Path(active['data']) if active else Path(os.environ.get('GOLD_DATA_DIR',str(source))).resolve()
    if not source.is_dir() or not (source/'price_tracker.py').is_file():
        raise ValueError('Live directory is not a recognized PriceTracker project')
    tag=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
    release=state/'releases'/tag
    release.mkdir(parents=True)
    archive=release/'source.zip'
    url='https://codeload.github.com/mmhdih/PriceTracker/zip/'+PINNED_COMMIT
    print('Downloading pinned, reviewed source; live installation is unchanged.',flush=True)
    with urllib.request.urlopen(url,timeout=60) as response, archive.open('wb') as stream:
        size=0
        while chunk:=response.read(1024*1024):
            size+=len(chunk)
            if size>50*1024*1024:
                raise ValueError('Download too large')
            stream.write(chunk)
    code=release/'code'
    extract_archive(archive,code,PINNED_COMMIT)
    unknown=custom_entrypoints(source,code)
    if unknown:
        raise RuntimeError('Custom entrypoints need review before integration: '+', '.join(unknown)+'. Preparation did not complete; do not activate or delete these files.')
    if shutil.disk_usage(state).free<300*1024*1024:
        raise RuntimeError('At least 300 MiB free disk space is required')
    log=release/'checks.log'
    checked([sys.executable,'-m','venv',str(release/'venv')],log,timeout=120)
    python=release/'venv/bin/python'
    checked([str(python),'-m','pip','install','--disable-pip-version-check','-r',str(code/'requirements.txt')],log,timeout=300)
    checked([str(python),'-m','unittest','discover','-s','tests','-v'],log,environment(release/'test-data',release/'test-runtime'),code)
    copy_data(source_data,release/'preview-data')
    validate_and_merge(code,python,release/'preview-data',release/'preview-runtime',log)
    previous=read(Path(active['release'])/'runtime.json')['secrets'] if active else {}
    secrets=credentials(source,previous)
    write(release/'credentials.json',secrets)
    manifest={'release':str(release),'commit':PINNED_COMMIT,'live':str(live),'source':str(source),
              'source_data':str(source_data),'status':'prepared','source_sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}
    write(release/'manifest.json',manifest)
    write(state/'prepared.json',manifest)
    print('PREPARED. No live files or schedules changed. Private logs: '+str(log))
    print('Pause gold schedulers/services, then run this script: activate --quiesced'+(' --live-dir '+shlex_quote(live)))


def restore_journal(live, state, journal):
    previous=journal.get('previous_active')
    restored=False
    if live.is_symlink():
        if journal['previous_kind']=='symlink' and os.readlink(live)==journal['previous_link']:
            restored=True
        elif live.resolve()!=Path(journal['new_live']).resolve():
            raise RuntimeError('Live link changed externally; refusing rollback')
        else:
            live.unlink()
    elif live.exists():
        identity=[live.stat().st_dev,live.stat().st_ino]
        if journal['previous_kind']=='directory' and identity==journal.get('original_identity'):
            restored=True
        else:
            raise RuntimeError('Live directory changed externally; refusing rollback')
    if not restored and journal['previous_kind']=='directory':
        backup=Path(journal['original_directory'])
        if not backup.is_dir():
            raise RuntimeError('Original directory not found; inspect switch-journal.json')
        os.rename(backup,live)
    elif not restored:
        live.symlink_to(journal['previous_link'],target_is_directory=True)
    if previous:
        write(state/'active.json',previous)
    elif (state/'active.json').exists():
        (state/'active.json').unlink()


def activate(args, live, state):
    if not (state/'prepared.json').exists():
        raise RuntimeError('No successful preparation exists. The live installation was not upgraded. Resolve the prepare error and run prepare again before activate.')
    prepared=read(state/'prepared.json')
    if prepared['live']!=str(live) or live.resolve()!=Path(prepared['source']):
        raise RuntimeError('Live source changed since prepare; prepare again')
    release=Path(prepared['release'])
    if not within(release,state/'releases') or read(release/'manifest.json')['status']!='prepared':
        raise RuntimeError('Invalid prepared release')
    unknown=custom_entrypoints(live.resolve(),release/'code')
    if unknown:
        raise RuntimeError('Custom entrypoints appeared since prepare; live remains unchanged: '+', '.join(unknown))
    previous=read(state/'active.json') if (state/'active.json').exists() else None
    paths=[live,prepared['source_data'],prepared['source']]+([previous['release']] if previous else [])
    quiescent(args,paths)
    secrets=credentials(live.resolve(),read(release/'credentials.json'))
    missing=[key for key in SECRET_NAMES if not secrets.get(key)]
    if missing:
        raise RuntimeError('Required bot settings were not found: '+', '.join(missing)+'. Export them in this shell; values are never printed.')
    backup=state/'backups'/release.name
    needed=sum(p.stat().st_size for p in live.resolve().rglob('*') if p.is_file() and not p.is_symlink())
    needed+=2*sum(p.stat().st_size for p in Path(prepared['source_data']).glob('*') if p.is_file())+100*1024*1024
    if shutil.disk_usage(state).free<needed:
        raise RuntimeError('Insufficient disk for full backup and data copies; live remains unchanged')
    backup.mkdir(parents=True,exist_ok=False)
    print('Taking a private full backup and a fresh data snapshot.',flush=True)
    with tarfile.open(backup/'original.tar.gz','w:gz') as archive:
        archive.add(live.resolve(),arcname='original',recursive=True)
    before=copy_data(prepared['source_data'],release/'data')
    validate_and_merge(release/'code',release/'venv/bin/python',release/'data',release/'runtime',release/'checks.log')
    if file_hashes(prepared['source_data'])!=before:
        raise RuntimeError('Source changed while updating; live remains unchanged')
    previous_runtime=read(Path(previous['release'])/'runtime.json')['runtime'] if previous else tempfile.gettempdir()
    live_runtime=Path(os.environ.get('GOLD_RUNTIME_DIR',previous_runtime)).resolve()
    if live_runtime.exists() and not live_runtime.is_dir():
        raise ValueError('GOLD_RUNTIME_DIR must be a directory')
    build_launchers(release,release/'data',live_runtime,secrets)
    preserve_output_files(live.resolve(),release,release/'data')
    quiescent(args,paths)
    journal={'new_live':str(release/'live'),'previous_active':previous,'previous_kind':'symlink' if live.is_symlink() else 'directory',
             'previous_link':os.readlink(live) if live.is_symlink() else None,'original_directory':str(backup/'original-directory'),
             'original_identity':[live.stat().st_dev,live.stat().st_ino]}
    if not live.is_symlink() and live.stat().st_dev!=state.stat().st_dev:
        raise RuntimeError('Live and backup directories must be on the same filesystem')
    temporary=live.parent/(live.name+'.new-'+uuid.uuid4().hex)
    temporary.symlink_to(release/'live',target_is_directory=True)
    write(state/'switch-journal.json',journal)
    try:
        if not live.is_symlink():
            os.rename(live,backup/'original-directory')
        os.replace(temporary,live)
        # Offline health checks through the SAME old paths schedulers use.
        checked([sys.executable,str(live/'rich_db.py')],release/'checks.log',timeout=30)
        checked([sys.executable,'-c',"import sys;sys.path.insert(0,sys.argv[1]);from rich_db import load_db;from price_tracker import _to_toman;assert load_db()['schema_version']==2;assert _to_toman(100)==10",str(live)],release/'checks.log',timeout=30)
        active={**prepared,'status':'active','data':str(release/'data'),'runtime':str(live_runtime),'journal':journal}
        write(state/'active.json',active)
    except BaseException:
        restore_journal(live,state,journal)
        (state/'switch-journal.json').unlink()
        raise
    finally:
        if temporary.is_symlink():
            temporary.unlink()
    write(release/'manifest.json',active)
    (state/'switch-journal.json').unlink()
    print('ACTIVATED. Existing entrypoint paths now use the tested venv and merged data.')
    print('Original directory/backup retained at: '+str(backup))
    print('No Telegram message was sent. Resume the same gold jobs, then verify the first real cycle.')
    print('Rollback command: run this script with rollback --quiesced and the same --live-dir.')


def rollback(args, live, state):
    if not (state/'active.json').exists():
        print('NOTHING TO ROLL BACK. No updater-managed release was activated; the existing installation is unchanged.')
        return
    active=read(state/'active.json')
    if live.resolve()!=Path(active['release'])/'live':
        raise RuntimeError('Live link changed externally; refusing rollback')
    quiescent(args,[live,active['release']])
    write(state/'switch-journal.json',{**active['journal'],'operation':'rollback'})
    restore_journal(live,state,active['journal'])
    (state/'switch-journal.json').unlink()
    print('ROLLED BACK. New code and post-update data are retained in: '+active['release'])
    print('Resume the previous gold jobs only after checking their original settings.')


def recover(args, live, state):
    if not (state/'switch-journal.json').exists():
        print('NO INTERRUPTED SWITCH. No recovery is needed.')
        return
    journal=read(state/'switch-journal.json')
    active=read(state/'active.json') if (state/'active.json').exists() else {}
    if journal.get('operation')!='rollback' and active and str(Path(active['release'])/'live')==journal['new_live'] and live.is_symlink() and live.resolve()==Path(journal['new_live']).resolve():
        (state/'switch-journal.json').unlink()
        print('Committed activation is intact; cleared completed journal.')
        return
    quiescent(args,[live,Path(journal['new_live']).parent])
    restore_journal(live,state,journal)
    (state/'switch-journal.json').unlink()
    print('Interrupted switch recovered; previous installation restored.')


@contextmanager
def manager_lock(state):
    import fcntl
    with (state/'updater.lock').open('a') as stream:
        fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield


def main():
    parser=argparse.ArgumentParser(description='Safe PriceTracker upgrade: prepare (default), activate, status, rollback, recover.')
    parser.add_argument('action',nargs='?',default='prepare',choices=('prepare','activate','status','rollback','recover'))
    parser.add_argument('--live-dir',default='/root/gold-tracker-bot')
    parser.add_argument('--data-dir',help='Legacy data directory; defaults to live directory on first install')
    parser.add_argument('--state-dir',help='Private updater directory; must be outside the live directory')
    parser.add_argument('--quiesced',action='store_true',help='Operator confirms all gold schedules/services were paused')
    args=parser.parse_args()
    if sys.platform!='linux' or sys.version_info<(3,10):
        raise RuntimeError('Linux and Python 3.10+ are required; no system packages are installed automatically')
    os.umask(0o077)
    # Do not resolve the final component: it becomes a managed symlink.
    requested=Path(args.live_dir).expanduser().absolute()
    live=requested.parent.resolve()/requested.name
    if str(live) in ('/','/root','/home','/usr','/opt','/etc','/var','/tmp'):
        raise ValueError('Refusing a system directory as --live-dir')
    state=Path(args.state_dir).expanduser().resolve() if args.state_dir else live.parent/'.price-tracker-updates'
    if state.is_relative_to(live) or live.is_relative_to(state):
        raise ValueError('Updater state and live directory must not contain each other')
    if live.exists() and within(state,live.resolve()):
        raise ValueError('Updater state must not be inside the real live-directory target')
    if live.exists() and live.stat().st_uid!=os.geteuid():
        raise RuntimeError('Run as the existing project owner; refusing to change ownership')
    marker=state/'manager.json'
    if state.exists() and not marker.exists() and any(state.iterdir()):
        raise RuntimeError('State directory is not managed by this updater')
    state.mkdir(parents=True,exist_ok=True)
    if state.stat().st_uid!=os.geteuid():
        raise RuntimeError('Updater directory belongs to another user')
    state.chmod(0o700)
    if marker.exists() and read(marker)['live']!=str(live):
        raise RuntimeError('This state directory belongs to another installation')
    write(marker,{'live':str(live),'schema':1})
    with manager_lock(state):
        if (state/'switch-journal.json').exists() and args.action not in ('recover','status'):
            raise RuntimeError('An interrupted switch exists. Inspect status, then run recover --quiesced')
        if args.action=='status':
            print(json.dumps({'live':str(live),'target':str(live.resolve()),
                              'prepared':read(state/'prepared.json') if (state/'prepared.json').exists() else None,
                              'active':read(state/'active.json') if (state/'active.json').exists() else None,
                              'interrupted_switch':(state/'switch-journal.json').exists()},indent=2))
        else:
            globals()[args.action](args,live,state)


if __name__=='__main__':
    try:
        main()
    except (Exception,KeyboardInterrupt) as error:
        print('STOPPED SAFELY: '+str(error),file=sys.stderr)
        print('No broad services were stopped. Backups/staged files are retained. Check status before retrying.',file=sys.stderr)
        raise SystemExit(1)
PY
