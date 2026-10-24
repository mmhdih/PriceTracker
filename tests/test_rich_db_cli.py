"""Exercise the actual legacy cron command against an isolated SQLite store."""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from config import TZ_TEHRAN
from tracker_store import Store


class RichDBCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.store = Store(self.data / 'tracker.sqlite3')
        now = datetime.now(TZ_TEHRAN)
        self.start = now - timedelta(days=10)
        self.due_id = self.add_forecast(self.start, 'due')
        self.missing_id = self.add_forecast(now - timedelta(days=20), 'missing')
        self.pending_id = self.add_forecast(now, 'pending')
        for hours, price in [(4, 105), (24, 95), (168, 110)]:
            self.store.observe((self.start + timedelta(hours=hours)).isoformat(), {'mazneh': price})
        self.env = {**os.environ, 'GOLD_DATA_DIR': str(self.data),
                    'GOLD_RUNTIME_DIR': str(self.data / 'runtime'), 'PYTHONUTF8': '1'}

    def add_forecast(self, start, text):
        return self.store.put({
            'timestamp': start.isoformat(), 'forecast_text': text, 'asset': 'mazneh',
            'snapshot': {'collection_time': start.isoformat(), 'prices': {'mazneh': 100}},
            'predicted_ranges': {h: {'min': 90, 'max': 110, 'direction': 'صعودی'}
                                 for h in ('4h', '24h', '1w')},
        })

    def run_command(self):
        result = subprocess.run([sys.executable, str(ROOT / 'rich_db.py')],
                                cwd=ROOT, env=self.env, capture_output=True,
                                text=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_actual_cli_evaluates_all_horizons_without_fabricating_missing_quotes(self):
        before = self.store.export()
        self.assertTrue(all(not f['evaluations'] for f in before['forecasts']))
        stats = self.run_command()
        after = self.store.export()
        records = {f['id']: f for f in after['forecasts']}
        for horizon, wins in [('4h', 1), ('24h', 0), ('1w', 1)]:
            metric = stats['by_horizon'][horizon]
            self.assertEqual(metric['all_forecasts'], 3)
            self.assertEqual(metric['total'], 1)
            self.assertEqual(metric['wins'], wins)
            self.assertEqual(metric['unscored'], 2)
            self.assertEqual(records[self.due_id]['evaluations'][horizon]['status'], 'scored')
            self.assertEqual(records[self.missing_id]['evaluations'][horizon]['status'], 'missing_observation')
        self.assertEqual(records[self.pending_id]['evaluations'], {})
        self.assertEqual(after['price_history'], before['price_history'])
        self.assertEqual(stats['wins'], 0)  # Legacy flat fields still represent 24h.
        self.assertEqual(stats['total'], 1)
        self.assertTrue(stats['evaluation_run']['performed'])
        original = records[self.due_id]['evaluations']
        again = self.run_command()
        self.assertEqual(again['by_horizon'], stats['by_horizon'])
        latest = {f['id']: f for f in self.store.export()['forecasts']}
        self.assertEqual(latest[self.due_id]['evaluations'], original)
        self.assertEqual(len(latest), 3)

    def test_library_import_and_stats_reads_do_not_run_evaluation(self):
        result = subprocess.run(
            [sys.executable, '-c',
             'import rich_db; import json; print(json.dumps(rich_db.get_performance_stats(rich_db.load_db())))'],
            cwd=ROOT, env=self.env, capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['total'], 0)
        self.assertTrue(all(not f['evaluations'] for f in self.store.export()['forecasts']))


if __name__ == '__main__':
    unittest.main()
