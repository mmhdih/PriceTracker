from datetime import datetime,timezone,timedelta
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing

PATH=Path(__file__).resolve().parents[1]/'tools/check_runtime.py'
SPEC=importlib.util.spec_from_file_location('check_runtime',PATH)
MODULE=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RuntimeHealthTests(unittest.TestCase):
    def setup_runtime(self,base):
        live=base/'release/live';live.mkdir(parents=True)
        data=base/'release/data';data.mkdir()
        settings={'data':str(data),'code':str(base/'release/code'),'runtime':'/tmp','python':'/private/venv/python','secrets':{'TELEGRAM_BOT_TOKEN':'PRIVATE_VALUE_DO_NOT_PRINT'}}
        (base/'release/runtime.json').write_text(json.dumps(settings),encoding='utf-8')
        return live,data

    def test_count_denominator_and_secret_suppression(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);live,data=self.setup_runtime(base)
            start=datetime(2026,1,1,tzinfo=timezone.utc)
            with closing(sqlite3.connect(data/'tracker.sqlite3')) as con:
                con.executescript('CREATE TABLE forecasts(payload TEXT);CREATE TABLE evaluations(forecast_id TEXT,horizon TEXT,payload TEXT);CREATE TABLE observations(timestamp TEXT,asset TEXT,price REAL);')
                for i in range(4):
                    record={'id':str(i),'timestamp':(start+timedelta(days=i)).isoformat(),'predicted_ranges':{'24h':{}} if i<3 else {}}
                    con.execute('INSERT INTO forecasts VALUES(?)',(json.dumps(record),))
                con.execute('INSERT INTO evaluations VALUES(?,?,?)',('0','24h',json.dumps({'status':'scored','version':2,'direction_result':'WIN'})))
                con.execute('INSERT INTO evaluations VALUES(?,?,?)',('1','24h',json.dumps({'status':'missing_observation'})))
                con.execute('INSERT INTO observations VALUES(?,?,?)',(start.isoformat(),'geram18',100))
                con.commit()
            before=(data/'tracker.sqlite3').read_bytes()
            report=MODULE.collect_health(live,start+timedelta(days=2,hours=1))
            stat=report['metrics_by_horizon']['24h']
            self.assertEqual(report['all_unique_forecasts'],4)
            self.assertEqual((stat['eligible'],stat['scored'],stat['no_structured_range']),(3,1,1))
            self.assertEqual(stat['win_rate'],100)
            self.assertEqual(stat['states']['not_due'],1)
            self.assertEqual(stat['states']['missing_observation'],1)
            self.assertNotIn('PRIVATE_VALUE_DO_NOT_PRINT',json.dumps(report))
            self.assertFalse(report['telegram_delivery_checked'])
            self.assertEqual((data/'tracker.sqlite3').read_bytes(),before)

    def test_missing_database_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);live,data=self.setup_runtime(base)
            with self.assertRaises(ValueError):MODULE.collect_health(live)
            self.assertFalse((data/'tracker.sqlite3').exists())


if __name__=='__main__':unittest.main()
