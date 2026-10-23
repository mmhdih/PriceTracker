"""Shared paths. Existing /tmp outputs remain the default on Linux."""
import os
import tempfile
from pathlib import Path
from datetime import timezone, timedelta

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('GOLD_DATA_DIR', str(ROOT))).resolve()
RUNTIME_DIR = Path(os.environ.get('GOLD_RUNTIME_DIR', tempfile.gettempdir())).resolve()
STORE_PATH = DATA_DIR / 'tracker.sqlite3'
COLLECTOR_PATH = RUNTIME_DIR / 'gold_collector_output.json'
FORECAST_PATH = RUNTIME_DIR / 'final_forecast.txt'
FORECAST_JSON_PATH = RUNTIME_DIR / 'final_forecast.json'
TZ_TEHRAN = timezone(timedelta(hours=3, minutes=30))
MAX_SNAPSHOT_AGE_SECONDS = int(os.environ.get('GOLD_MAX_SNAPSHOT_AGE_SECONDS', '1800'))
EVALUATION_TOLERANCE_SECONDS = int(os.environ.get('GOLD_EVALUATION_TOLERANCE_SECONDS', '1800'))
