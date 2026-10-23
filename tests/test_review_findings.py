"""Regression tests for repaired defects. Entire suite is offline."""
import concurrent.futures
import copy
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from config import TZ_TEHRAN
from forecast_schema import normalize_forecast, explicit_ranges
from tracker_store import Store, performance
from safe_json import update_json, read_json, save_json
from migrate_databases import migrate
from analysis_engine import build_forecast, render, validate_snapshot
from pipeline import produce
from forecast_io import load_forecast
import collector
import daily_summary_bot
import jdatetime

START = datetime(2026, 1, 1, 10, tzinfo=TZ_TEHRAN)

def raw(start=START, **extra):
    result = {'timestamp': start.isoformat(), 'forecast_text': 'forecast', 'asset': 'mazneh',
              'snapshot': {'collection_time': start.isoformat(), 'prices': {'mazneh': 100}},
              'predicted_ranges': {'4h': {'min': 90, 'max': 110, 'direction': 'صعودی'},
                                   '24h': {'min': 90, 'max': 110, 'direction': 'صعودی'},
                                   '1w': {'min': 90, 'max': 110, 'direction': 'صعودی'}}}
    result.update(extra)
    return result

def snapshot(now=None, multiplier=1):
    now = now or datetime.now(TZ_TEHRAN)
    history = [{'date': f'1404/10/{i:02}', 'close': multiplier*(100+i), 'open': multiplier*(100+i),
                'low': multiplier*(99+i), 'high': multiplier*(102+i)} for i in range(1,11)]
    return {'collection_time': now.isoformat(), 'prices': {'geram18': 112*multiplier, 'gold_ounce': 2000},
            'geram18_history': history, 'geram18_technicals': {'rsi14': 25, 'ma20': 115*multiplier},
            'news': {'fixture': ['خبر آزمایشی بدون توصیه']} }

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name)/'tracker.sqlite3')
        self.store.put(raw())

    def test_independent_horizons_and_endpoint_price(self):
        self.store.observe((START+timedelta(hours=4)).isoformat(), {'mazneh': 105})
        self.store.evaluate(START+timedelta(hours=4))
        f=self.store.export()['forecasts'][0]
        self.assertEqual(set(f['evaluations']), {'4h'})
        self.assertIsNone(f['evaluation'])
        self.store.observe((START+timedelta(hours=24)).isoformat(), {'mazneh': 80})
        self.store.evaluate(START+timedelta(hours=24))
        db=self.store.export()
        self.assertEqual(db['forecasts'][0]['evaluation']['direction_result'], 'LOSS')
        self.assertEqual(performance(db,'4h')['wins'],1)
        self.assertEqual(performance(db,'24h')['wins'],0)

    def test_late_current_price_is_not_historical_endpoint(self):
        self.store.observe((START+timedelta(days=10)).isoformat(), {'mazneh': 200})
        self.store.evaluate(START+timedelta(days=10))
        self.assertEqual(performance(self.store.export())['total'],0)
        self.assertEqual(self.store.export()['forecasts'][0]['evaluations']['24h']['status'],'missing_observation')

    def test_later_backfill_can_score_missing_endpoint(self):
        self.store.evaluate(START+timedelta(days=2))
        self.store.observe((START+timedelta(hours=24,minutes=3)).isoformat(), {'mazneh': 105})
        self.store.evaluate(START+timedelta(days=2))
        self.assertEqual(performance(self.store.export())['wins'],1)

    def test_scored_result_immutable(self):
        self.store.observe((START+timedelta(hours=24)).isoformat(), {'mazneh': 105})
        self.store.evaluate(START+timedelta(hours=24))
        original=self.store.export()['forecasts'][0]['evaluation']
        self.store.observe((START+timedelta(hours=24,minutes=5)).isoformat(), {'mazneh': 80})
        self.store.evaluate(START+timedelta(hours=25))
        self.assertEqual(self.store.export()['forecasts'][0]['evaluation'],original)

    def test_sideways_miss_counts_as_loss(self):
        other=raw(START+timedelta(days=10), forecast_text='sideways', predicted_ranges={'24h': {'min':95,'max':105,'direction':'نوسانی'}})
        self.store.put(other)
        self.store.observe((START+timedelta(days=11)).isoformat(), {'mazneh':130})
        self.store.evaluate(START+timedelta(days=11))
        f=self.store.export()['forecasts'][-1]
        self.assertEqual(f['evaluation']['direction_result'],'LOSS')

    def test_duplicate_and_stale_save_do_not_erase_results(self):
        self.store.put(raw())
        stale=self.store.export()['forecasts'][0]
        self.store.observe((START+timedelta(hours=24)).isoformat(), {'mazneh':105})
        self.store.evaluate(START+timedelta(hours=24))
        self.store.put(stale)
        self.assertEqual(len(self.store.export()['forecasts']),1)
        self.assertEqual(performance(self.store.export())['wins'],1)

    def test_legacy_results_not_counted(self):
        f=raw(START+timedelta(days=50), evaluation={'result':'WIN','overall_score':1})
        self.store.put(f)
        self.assertEqual(performance(self.store.export())['total'],0)
        self.assertEqual(performance(self.store.export())['archived_legacy_results'],1)

    def test_transaction_rolls_back(self):
        with self.assertRaises(RuntimeError):
            with self.store.connection() as con:
                self.store.put(raw(START+timedelta(days=2)),con)
                raise RuntimeError('interruption')
        self.assertEqual(len(self.store.export()['forecasts']),1)

    def test_full_history_count_and_win_rate_beyond_old_limits(self):
        with self.store.connection() as con:
            for i in range(2,2102):
                self.store.put(raw(START+timedelta(days=i),forecast_text=f'forecast {i}'),con)
        self.store.observe((START+timedelta(hours=24)).isoformat(), {'mazneh':105})
        self.store.evaluate(START+timedelta(hours=24))
        before=performance(self.store.export())
        self.assertEqual(before['all_forecasts'],2101)
        self.assertEqual(before['total'],1)
        self.assertEqual(before['win_rate'],100)
        self.store.evaluate(START+timedelta(hours=24))
        after=performance(self.store.export())
        self.assertEqual(after,before)

    def test_concurrent_writers_preserve_all_forecasts(self):
        def put(i):
            self.store.put(raw(START+timedelta(days=i),forecast_text=f'forecast {i}'))
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            list(executor.map(put,range(1,21)))
        self.assertEqual(len(self.store.export()['forecasts']),21)

class MigrationTests(unittest.TestCase):
    def test_merge_repeatability_backups_and_originals(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            old=raw(evaluation={'result':'WIN'})
            old['snapshot_prices']=old.pop('snapshot')['prices']
            rich=raw()
            rich['snapshot']['collection_time']=START.isoformat()
            (base/'analysis_db.json').write_text(json.dumps({'forecasts':[old], 'price_history':[], 'lessons':['old lesson']}))
            (base/'rich_analysis_db.json').write_text(json.dumps({'forecasts':[rich,raw(START+timedelta(days=1))], 'lessons':[{'lesson':'new lesson'}]}))
            before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in base.glob('*.json')}
            target=base/'tracker.sqlite3'
            preview=migrate([base],target)
            self.assertFalse(target.exists())
            self.assertEqual(preview['merged_unique_forecasts'],2)
            first=migrate([base],target,True)
            second=migrate([base],target,True)
            self.assertEqual(first['merged_unique_forecasts'],second['merged_unique_forecasts'])
            self.assertEqual(first['integrity_check'],'ok')
            self.assertTrue(Path(first['backup_directory']).exists())
            self.assertEqual(before,{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in base.glob('*.json')})
            with Store(target).connection() as con:
                archives=[json.loads(r[0]) for r in con.execute('SELECT payload FROM imports')]
            self.assertTrue(any(x.get('lessons')==['old lesson'] for x in archives))

    def test_corrupt_source_aborts_without_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            (base/'analysis_db.json').write_text('{broken')
            with self.assertRaises(json.JSONDecodeError): migrate([base],base/'tracker.sqlite3',True)
            self.assertFalse((base/'tracker.sqlite3').exists())

class JSONTests(unittest.TestCase):
    def test_corrupt_file_never_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'data.json';p.write_text('{broken')
            with self.assertRaises(json.JSONDecodeError):save_json(p,{'days':{}})
            self.assertEqual(p.read_text(),'{broken')

    def test_real_finance_updates_keep_unique_ids(self):
        import finance_manager
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'finance.json'
            with patch.object(finance_manager,'FINANCE_DB_PATH',str(path)):
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                    list(executor.map(lambda i: finance_manager.add_transaction(100,'expense','test',str(i)),range(12)))
                db=finance_manager.load_finance_db()
            self.assertEqual(len(db['transactions']),12)
            self.assertEqual(len({t['id'] for t in db['transactions']}),12)

    def test_concurrent_process_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'data.json'
            script='from safe_json import update_json;import sys;[(update_json(sys.argv[1], {"count":0}, lambda d: d.update(count=d["count"]+1))) for _ in range(15)]'
            processes=[subprocess.Popen([sys.executable,'-c',script,str(p)],cwd=ROOT) for _ in range(4)]
            for process in processes:self.assertEqual(process.wait(timeout=30),0)
            self.assertEqual(read_json(p)['count'],60)
            self.assertTrue(Path(str(p)+'.bak').exists())

class AnalysisTests(unittest.TestCase):
    def test_forecast_scales_with_input_and_has_no_invented_confidence(self):
        a=build_forecast(snapshot(multiplier=1)); b=build_forecast(snapshot(multiplier=10))
        self.assertGreater(b['predicted_ranges']['24h']['min'],a['predicted_ranges']['24h']['min']*9)
        self.assertIsNone(a['confidence']);self.assertIsNone(a['buy_signal'])
        text=render(a,{'forecasts':[]})
        self.assertIn('اشباع فروش',text)
        self.assertIn('پایین‌تر',text)
        self.assertIn('خبر آزمایشی',text)
        self.assertLess(len(text),4096)

    def test_missing_optional_data_renders_as_missing(self):
        s=snapshot();s.pop('geram18_history');s.pop('geram18_technicals');s.pop('news')
        text=render(build_forecast(s),{'forecasts':[]})
        self.assertIn('داده تاریخی کافی موجود نیست',text)
        self.assertIn('خبری دریافت نشده',text)

    def test_stale_and_missing_gold_rejected(self):
        with self.assertRaises(ValueError):validate_snapshot(snapshot(datetime.now(TZ_TEHRAN)-timedelta(hours=5)))
        s=snapshot();s['prices']={}
        with self.assertRaises(ValueError):validate_snapshot(s)

    def test_uniform_pipeline_and_sidecar_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);store=Store(base/'tracker.sqlite3');s=snapshot()
            record=produce(s,store,base/'report.txt',base/'report.json')
            loaded=load_forecast(base/'report.txt',sidecar=base/'report.json')
            self.assertEqual(loaded['predicted_ranges'],record['predicted_ranges'])
            self.assertEqual(loaded['asset'],'geram18')
            self.assertEqual(store.put(loaded),record['id'])
            self.assertEqual(len(store.export()['forecasts']),1)

    def test_repeated_generation_is_one_forecast(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);store=Store(base/'tracker.sqlite3');s=snapshot()
            a=produce(s,store,base/'report.txt',base/'report.json')
            b=produce(s,store,base/'report.txt',base/'report.json')
            self.assertEqual(a['id'],b['id'])
            self.assertIn('تعداد کل تحلیل‌های یکتا: ۱', a['forecast_text'])
            self.assertEqual(a['forecast_text'],b['forecast_text'])
            self.assertEqual(len(store.export()['forecasts']),1)

    def test_all_generation_entrypoints_use_same_template(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);source=base/'snapshot.json'
            source.write_text(json.dumps(snapshot(),ensure_ascii=False),encoding='utf-8')
            env={**os.environ,'GOLD_DATA_DIR':str(base/'data'),'GOLD_RUNTIME_DIR':str(base/'runtime'),'PYTHONUTF8':'1'}
            outputs=[]
            for name in ['generate_analysis.py','generate_final.py','generate_report.py','generate_forecast.py','generate_dollar_forecast.py','gen_forecast_script.py']:
                command=[sys.executable,str(ROOT/name),'--input',str(source)]
                result=subprocess.run(command,env=env,capture_output=True,text=True,encoding='utf-8',timeout=20)
                self.assertEqual(result.returncode,0,result.stderr)
                outputs.append(result.stdout)
            self.assertTrue(all('۱. قیمت و تغییر روزانه' in text and '۷. عملکرد ارزیابی معتبر' in text for text in outputs))
            self.assertEqual(len(Store(base/'data'/'tracker.sqlite3').export()['forecasts']),1)
            for name in ['save_final_forecast.py','save_rich_forecast.py']:
                result=subprocess.run([sys.executable,str(ROOT/name),str(base/'runtime'/'final_forecast.txt')],env=env,capture_output=True,text=True,encoding='utf-8',timeout=20)
                self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(len(Store(base/'data'/'tracker.sqlite3').export()['forecasts']),1)

    def test_multiline_and_table_ranges(self):
        text='۲۴ ساعته: صعودی\n محدوده: ۱۰۰ تا ۲۰۰ تومان\n۱ هفته‌ای: نزولی | ۸۰ تا ۱۹۰'
        result=explicit_ranges(text)
        self.assertEqual(result['24h']['min'],100)
        self.assertEqual(result['1w']['direction'],'نزولی')
        self.assertEqual(explicit_ranges('۴ ساعته: نوسانی | ۰ تا ۱'),{})

    def test_all_weekdays(self):
        for offset in range(7):
            date=datetime(2026,10,17+offset,12,tzinfo=TZ_TEHRAN)
            jd=jdatetime.datetime.fromgregorian(datetime=date)
            with patch.object(jdatetime,'datetime') as clock:
                clock.now.return_value=jd
                cal=collector._get_calendar_info()
            self.assertEqual(cal['is_friday'],offset==6)
            self.assertEqual(cal['is_thursday'],offset==5)
            self.assertEqual(cal['weekday'],['شنبه','یکشنبه','دوشنبه','سه‌شنبه','چهارشنبه','پنجشنبه','جمعه'][offset])

    def test_flat_price_rsi_is_neutral(self):
        rows=[{'close':100,'high':100,'low':100} for _ in range(20)]
        self.assertEqual(collector._compute_technicals(rows)['rsi14'],50)

    def test_ounce_summary_unit_is_usd(self):
        with patch.object(daily_summary_bot,'_get_baseline_and_last_prices',return_value=({'gold_ounce':2000},{'gold_ounce':2100},'08:00','20:00')), patch.object(daily_summary_bot,'_get_min_max_geram18',return_value=(None,None)), patch.object(daily_summary_bot,'send_and_pin_to_telegram',return_value=True) as send:
            self.assertEqual(daily_summary_bot.main(),0)
        block=send.call_args.args[0].split('**انس طلا**')[1].split('🥈')[0]
        self.assertIn('دلار',block);self.assertNotIn('تومان',block)

if __name__ == '__main__':
    unittest.main()
