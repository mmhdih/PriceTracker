"""Offline characterization tests: passing tests reproduce unresolved defects.

These are review evidence, not claims that production behavior is correct.
No external requests or Telegram messages are sent.
"""
import contextlib
import copy
import importlib
import io
import json
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch, mock_open

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import rich_db
import collector
import jdatetime
import daily_summary_bot
import finance_bot_listener


class ReviewEvidence(unittest.TestCase):
    def test_24h_range_finalized_after_only_four_hours(self):
        forecast = {'timestamp': (datetime.now(rich_db.TZ_TEHRAN) - timedelta(hours=4, minutes=5)).isoformat(),
                    'snapshot': {'prices': {'mazneh': 100}}, 'predicted_direction': 'صعودی',
                    'predicted_ranges': {'24h': {'min': 90, 'max': 110}}, 'buy_signal': 'BUY', 'evaluation': None}
        db = {'forecasts': [forecast], 'lessons': []}
        with patch.object(rich_db, 'save_db'):
            result = rich_db.evaluate_forecasts(db, {'mazneh': 105})
            self.assertEqual(result[0]['range_result'], 'IN_RANGE')
            self.assertLess(result[0]['age_hours'], 24)
            self.assertEqual(rich_db.evaluate_forecasts(db, {'mazneh': 200}), [])

    def test_failed_sideways_prediction_is_neutral(self):
        forecast = {'timestamp': (datetime.now(rich_db.TZ_TEHRAN) - timedelta(hours=25)).isoformat(),
                    'snapshot': {'prices': {'mazneh': 100}}, 'predicted_direction': 'نوسانی',
                    'predicted_ranges': {}, 'buy_signal': 'WAIT', 'evaluation': None}
        with patch.object(rich_db, 'save_db'):
            result = rich_db.evaluate_forecasts({'forecasts': [forecast], 'lessons': []}, {'mazneh': 130})
        self.assertEqual(result[0]['direction_result'], 'NEUTRAL')

    def test_multiline_range_is_not_parsed(self):
        text = 'بازه ۲۴ ساعته: 🟢 صعودی\n   محدوده نوسان: ۱۰۰ تا ۲۰۰ تومان\nسیگنال: BUY\nاطمینان: ۷۰٪'
        with patch('builtins.open', mock_open(read_data=text)), patch('os.path.exists', return_value=False), \
             patch.object(rich_db, 'load_db', return_value={}), patch.object(rich_db, 'add_forecast') as save, \
             patch.object(sys, 'argv', ['save_rich_forecast.py', 'fixture.txt']), contextlib.redirect_stdout(io.StringIO()):
            runpy.run_path(str(ROOT / 'save_rich_forecast.py'))
        self.assertEqual(save.call_args.args[3], 'صعودی')
        self.assertEqual(save.call_args.args[4], {})

    def test_cron_stats_silently_missing_due_to_json_import(self):
        import subprocess
        db = {'forecasts': [{'evaluation': {'result': 'WIN'}}]}
        with patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, 'fixture', '')), \
             patch('builtins.open', mock_open(read_data=json.dumps(db))), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            state = runpy.run_path(str(ROOT / 'cron_run_v2.py'))
        self.assertEqual(state['winrate'], 'no data')

    def test_calendar_uses_wrong_weekday_mapping(self):
        friday = jdatetime.datetime.fromgregorian(datetime=datetime(2026, 10, 23, 12, tzinfo=rich_db.TZ_TEHRAN))
        with patch.object(jdatetime, 'datetime') as clock:
            clock.now.return_value = friday
            result = collector._get_calendar_info()
        self.assertFalse(result['is_friday'])
        self.assertNotEqual(result['weekday'], 'جمعه')

    def test_unauthorized_sender_reaches_finance_handler(self):
        payload = {'ok': True, 'result': [{'update_id': 1, 'message': {'chat': {'id': 999999}, 'from': {'id': 999999}, 'text': '/report'}}]}
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        with patch.object(finance_bot_listener, 'load_finance_db'), \
             patch('urllib.request.urlopen', side_effect=[response, KeyboardInterrupt]), \
             patch.object(finance_bot_listener, 'parse_and_register_message', return_value='private report') as handler, \
             patch.object(finance_bot_listener, 'send_message') as send, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                finance_bot_listener.main()
        handler.assert_called_once_with('/report')
        send.assert_called_once_with(999999, 'private report')

    def test_daily_summary_labels_ounce_as_toman(self):
        with patch.object(daily_summary_bot, '_get_baseline_and_last_prices', return_value=({'gold_ounce': 2000}, {'gold_ounce': 2100}, '08:00', '20:00')), \
             patch.object(daily_summary_bot, '_get_min_max_geram18', return_value=(None, None)), \
             patch.object(daily_summary_bot, 'send_and_pin_to_telegram', return_value=True) as send, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(daily_summary_bot.main(), 0)
        ounce = send.call_args.args[0].split('**انس طلا**')[1].split('🥈')[0]
        self.assertIn('تومان', ounce)

    def test_corrupt_database_is_silently_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db.json'
            path.write_text('{"forecasts": [', encoding='utf-8')
            with patch.object(rich_db, 'DB_PATH', str(path)):
                db = rich_db.load_db()
                self.assertEqual(db['forecasts'], [])
                rich_db.save_db(db)
            self.assertEqual(json.loads(path.read_text())['forecasts'], [])

    def test_generator_crashes_on_missing_volatility(self):
        fixture = {'prices': {}, 'volatility': {}, 'geram18_technicals': {}, 'mazneh_technicals': {}, 'hobab': {}, 'calendar': {}, 'shamsi_time': 'fixture'}
        with patch('builtins.open', mock_open(read_data=json.dumps(fixture))), patch.object(rich_db, 'load_db', return_value={'forecasts': []}):
            with self.assertRaisesRegex(KeyError, 'mazneh'):
                runpy.run_path(str(ROOT / 'generate_analysis.py'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
