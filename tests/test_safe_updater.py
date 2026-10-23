"""Offline safety tests for the Python helper embedded in safe_update.sh."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT=Path(__file__).resolve().parents[1]
TEXT=(ROOT/'safe_update.sh').read_text(encoding='utf-8')
HELPER=TEXT.split("<<'PY'\n",1)[1].rsplit('\nPY',1)[0]
UPDATER={'__name__':'safe_update_helper'}
exec(compile(HELPER,'safe_update.sh:helper','exec'),UPDATER)


class UpdaterTests(unittest.TestCase):
    def test_extract_rejects_parent_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);archive=root/'source.zip'
            with zipfile.ZipFile(archive,'w') as z:
                z.writestr('PriceTracker-abc/../../escape','bad')
            with self.assertRaises(ValueError):UPDATER['extract_archive'](archive,root/'code','abc')
            self.assertFalse((root/'escape').exists())

    def test_data_snapshot_preserves_sources_and_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source';source.mkdir()
            (source/'analysis_db.json').write_text('{"forecasts":[]}',encoding='utf-8')
            with closing(sqlite3.connect(source/'tracker.sqlite3')) as con:
                con.execute('CREATE TABLE entries(value)');con.execute('INSERT INTO entries VALUES(42)');con.commit()
            before=UPDATER['file_hashes'](source)
            UPDATER['copy_data'](source,root/'target')
            self.assertEqual(before,UPDATER['file_hashes'](source))
            with closing(sqlite3.connect(root/'target/tracker.sqlite3')) as con:
                self.assertEqual(con.execute('SELECT value FROM entries').fetchone()[0],42)

    def test_empty_or_corrupt_source_never_replaces_live_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source';source.mkdir()
            with self.assertRaises(RuntimeError):UPDATER['copy_data'](source,root/'empty-copy')
            (source/'analysis_db.json').write_text('{broken',encoding='utf-8')
            with self.assertRaises(json.JSONDecodeError):UPDATER['copy_data'](source,root/'broken-copy')
            self.assertEqual((source/'analysis_db.json').read_text(),'{broken')

    def test_credentials_are_parsed_without_executing_old_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'price_tracker.py').write_text("raise RuntimeError('must never run')\nTELEGRAM_BOT_TOKEN='test-literal'\nTELEGRAM_CHAT_ID='test-chat'\n",encoding='utf-8')
            with patch.dict(os.environ,{},clear=True):
                result=UPDATER['credentials'](root)
            self.assertEqual(result,{'TELEGRAM_BOT_TOKEN':'test-literal','TELEGRAM_CHAT_ID':'test-chat'})

    def test_explicit_quiescence_is_required(self):
        with self.assertRaisesRegex(RuntimeError,'Pause ONLY'):
            UPDATER['quiescent'](argparse.Namespace(quiesced=False),[])

    @unittest.skipIf(os.name=='nt','Directory symlinks require Windows privilege; requires Linux CI')
    def test_restore_original_directory_and_repeat_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);state=root/'state';state.mkdir()
            live=root/'live';live.mkdir();(live/'sentinel').write_text('original')
            identity=[live.stat().st_dev,live.stat().st_ino]
            original=state/'original';new=state/'new';new.mkdir()
            os.rename(live,original);live.symlink_to(new,target_is_directory=True)
            journal={'previous_kind':'directory','original_directory':str(original),'new_live':str(new),
                     'previous_active':None,'original_identity':identity}
            UPDATER['restore_journal'](live,state,journal)
            UPDATER['restore_journal'](live,state,journal)
            self.assertEqual((live/'sentinel').read_text(),'original')
            self.assertTrue(new.exists())

    @unittest.skipIf(os.name=='nt','Directory symlinks require Windows privilege; requires Linux CI')
    def test_restore_prior_symlink_preserves_new_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);state=root/'state';state.mkdir();old=root/'old';old.mkdir();new=root/'new';new.mkdir()
            live=root/'live';live.symlink_to(new,target_is_directory=True)
            journal={'previous_kind':'symlink','previous_link':str(old),'new_live':str(new),'previous_active':None}
            UPDATER['restore_journal'](live,state,journal)
            UPDATER['restore_journal'](live,state,journal)
            self.assertEqual(live.resolve(),old)
            self.assertTrue(new.exists())


if __name__=='__main__':unittest.main()
