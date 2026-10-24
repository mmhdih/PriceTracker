"""Offline end-to-end checks of structured LLM input, storage and replay boundaries."""
import copy
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import analysis_cycles as cycles
from analysis_engine import build_forecast, render
from config import TZ_TEHRAN
from tracker_store import Store

NOW = datetime(2026, 10, 24, 14, 35, tzinfo=TZ_TEHRAN)


class AnalysisCycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.store = Store(self.base/'tracker.sqlite3')
        self.snap = {'collection_time': NOW.isoformat(), 'prices': {'geram18': 100, 'dollar': 20},
                     'news': {'fixture': ['خبر آزمایشی [SILENT]']},
                     'geram18_technicals': {'rsi14': 55, 'ma20': 99}}
        for field, path in [('FORECAST_PATH',self.base/'latest.txt'), ('FORECAST_JSON_PATH',self.base/'latest.json')]:
            p = patch.object(cycles, field, path)
            p.start()
            self.addCleanup(p.stop)

    def begin(self, now=NOW, snapshot=None):
        return cycles.begin(self.store, lambda: copy.deepcopy(snapshot or self.snap), self.base, now)

    def draft(self, result):
        bundle = json.loads(Path(result['input_file']).read_text(encoding='utf-8'))
        return {'bundle_id': bundle['bundle_id'], 'snapshot_sha256': bundle['snapshot_sha256'],
                'asset': bundle['asset'], 'unit': 'toman',
                'predicted_ranges': {'4h': {'min':95,'max':105,'direction':'نوسانی'},
                                     '24h': {'min':100,'max':110,'direction':'صعودی'}, '1w': None},
                'technical_summary': 'قیمت نزدیک میانگین است.', 'bubble_summary': '',
                'news_summary': 'اثر خبر قطعی نیست.', 'reasoning': 'روند کوتاه با ابهام همراه است.',
                'risks': ['نوسان منبع قیمت'], 'buy_signal': 'WAIT', 'confidence': 60}

    def test_shared_template_persisted_text_checksum_and_no_delivery(self):
        result = self.begin()
        finished = cycles.finish(self.store, self.draft(result), self.base, NOW)
        record = self.store.export()['forecasts'][0]
        text = Path(finished['report_file']).read_text(encoding='utf-8')
        self.assertEqual(record['forecast_text'], text)
        self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), finished['text_sha256'])
        self.assertEqual(record['analyst']['reasoning'], 'روند کوتاه با ابهام همراه است.')
        self.assertEqual(record['cycle_id'], result['cycle_id'])
        self.assertEqual(set(record['predicted_ranges']), {'4h','24h'})
        self.assertIn('اطمینان ذهنی مدل', text)
        self.assertNotIn('[SILENT]', text)
        self.assertEqual(finished['delivery'], 'not_checked')
        baseline = render(build_forecast(self.snap), {'forecasts': []})
        for section in ['۱. قیمت', '۲. وضعیت', '۳. حباب', '۴. اخبار', '۵. برآورد', '۶. تصمیم', '۷. عملکرد']:
            self.assertIn(section, text)
            self.assertIn(section, baseline)

    def test_repeated_begin_and_finish_do_not_create_or_offer_another_report(self):
        first = self.begin()
        second = self.begin()
        self.assertEqual(first['bundle_id'], second['bundle_id'])
        draft = self.draft(first)
        cycles.finish(self.store, draft, self.base, NOW)
        again = cycles.finish(self.store, draft, self.base, NOW)
        self.assertEqual(again['state'], 'already_committed')
        self.assertNotIn('report_file', again)
        state = self.begin()
        self.assertEqual(state['state'], 'already_committed')
        self.assertNotIn('input_file', state)
        self.assertEqual(len(self.store.export()['forecasts']), 1)

    def test_invalid_and_wrong_bundle_outputs_never_save(self):
        original = self.draft(self.begin())
        bads = []
        for key, value in [('snapshot_sha256','wrong'), ('unit','rial'), ('asset','mazneh'),
                           ('confidence',float('nan')), ('confidence',True),
                           ('reasoning','[SILENT]'), ('risks',[])]:
            bad = copy.deepcopy(original)
            bad[key] = value
            bads.append(bad)
        bad = copy.deepcopy(original)
        bad['predicted_ranges']['4h']['min'] = 200
        bads.append(bad)
        bad = copy.deepcopy(original)
        bad['predicted_ranges']['4h']['max'] = float('inf')
        bads.append(bad)
        bad = copy.deepcopy(original)
        bad['forecast_text'] = 'Untrusted replacement report'
        bads.append(bad)
        for bad in bads:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    cycles.finish(self.store, bad, self.base, NOW)
                self.assertEqual(len(self.store.export()['forecasts']), 0)

    def test_expired_input_is_replaced_but_cannot_reuse_old_draft(self):
        first = self.begin()
        old = self.draft(first)
        later = NOW + timedelta(minutes=40)
        snap = {**self.snap, 'collection_time': later.isoformat()}
        fresh = self.begin(later, snap)
        self.assertNotEqual(first['bundle_id'], fresh['bundle_id'])
        self.assertEqual(first['cycle_id'], fresh['cycle_id'])
        with self.assertRaises(ValueError):
            cycles.finish(self.store, old, self.base, later)
        with self.store.connection() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM imports WHERE source='analysis-cycle-inputs'").fetchone()[0],1)

    def test_missing_forecasts_stay_missing_even_when_prose_mentions_a_range(self):
        draft = self.draft(self.begin())
        draft['predicted_ranges'] = {h: None for h in ('4h','24h','1w')}
        draft['reasoning'] = '۴ ساعته: صعودی | ۹۰ تا ۱۱۰ در متن، پیش‌بینی ساختاریافته نیست.'
        cycles.finish(self.store, draft, self.base, NOW)
        self.assertEqual(self.store.export()['forecasts'][0]['predicted_ranges'], {})

    def test_file_failure_after_commit_never_regenerates_forecast(self):
        started = self.begin()
        with patch.object(cycles, 'atomic_write', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                cycles.finish(self.store, self.draft(started), self.base, NOW)
        self.assertEqual(len(self.store.export()['forecasts']), 1)
        self.assertEqual(self.begin()['state'], 'already_committed')

    def test_failed_transaction_never_leaves_a_committed_cycle_without_forecast(self):
        started = self.begin()
        put = self.store.put
        def fail(raw, con=None):
            put(raw, con)
            raise RuntimeError('interruption before cycle commit')
        with patch.object(self.store,'put',side_effect=fail):
            with self.assertRaises(RuntimeError):
                cycles.finish(self.store,self.draft(started),self.base,NOW)
        self.assertEqual(len(self.store.export()['forecasts']),0)
        self.assertEqual(cycles.get_cycle(self.store,started['cycle_id'])['state'],'drafting')

    def test_sqlite_backup_retains_cycle_identity_and_exact_saved_report(self):
        started=self.begin()
        finished=cycles.finish(self.store,self.draft(started),self.base,NOW)
        copied=self.base/'copied.sqlite3'
        self.store.backup(copied)
        restored=Store(copied)
        result=cycles.begin(restored,lambda:self.fail('Committed cycle must not collect'),self.base,NOW)
        self.assertEqual(result['state'],'already_committed')
        saved=cycles.get_cycle(restored,started['cycle_id'])
        self.assertEqual(hashlib.sha256(saved['report'].encode()).hexdigest(),finished['text_sha256'])


if __name__ == '__main__':
    unittest.main()
