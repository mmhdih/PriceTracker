# PriceTracker

## راهنمای فارسی و بررسی توسط Hermes

- [متن آماده اصلاح ساعت دو job طلا برای scheduler فعلی UTC، مطابق برنامه تهران](HERMES_TIMEZONE_FIX_FA.txt)
- [اصلاح واقعی ارزیابی در دستور فعلی rich_db.py، با حفظ تحلیل LLM](EVALUATOR_FIX_FA.md)
- [راهنمای اصلی: معماری، نصب، ادغام، شمارنده و حدود تأیید سلامت](OPERATIONS_FA.md)
- [مراحل بررسی زمان‌بندی، تست بدون ارسال و تشخیص تحویل تلگرام در Hermes](HERMES_RUNBOOK_FA.md)
- [متن آماده برای سپردن بررسی به Hermes](HERMES_CHECK_PROMPT_FA.txt)
- [بررسی prompt و wrapperهای واقعی: ارزیابی، تفاوت قالب و نقص retry](CRON_AUDIT_FA.md)
- [prompt پیشنهادی job تحلیل برای مسیر استاندارد تست‌شده](HERMES_ANALYSIS_JOB_PROMPT_FA.txt)
- [ابزار read-only بررسی SQLite و تفکیک نتایج بدون نمره](tools/check_runtime.py)

اگر سرور قبلاً `ACTIVATED` و تحلیل تازه داده، نصب را تکرار نکنید؛ از راهنمای بررسی شروع کنید. موفقیت اجرای دستی به‌تنهایی سلامت زمان‌بندی یا ارسال تلگرام را تأیید نمی‌کند.

Gold/forex collection, a single Persian analysis template, and a unified transactional forecast history. This version repairs the review findings except finance sender authorization, which was explicitly deferred by the owner.

## Run locally

Requires Python 3.10+. Validation in this workspace used Python 3.12 on Windows.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

Set `GOLD_DATA_DIR` to your private runtime-data directory, and export `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and `FINANCE_BOT_TOKEN` when needed. `.env` files are not automatically loaded. `GOLD_RUNTIME_DIR` defaults to the OS temporary directory, preserving `/tmp/gold_collector_output.json`, `/tmp/final_forecast.txt` and `/tmp/final_forecast.json` on Linux.

**Before using old data, run the merge** (see [SERVER_UPGRADE_FA.md](SERVER_UPGRADE_FA.md)). Both old databases are merged into `tracker.sqlite3`; original JSON files remain unchanged. Never publish data directories, backups or bot credentials.

```bash
# Preview, no changes to source databases:
python migrate_databases.py --source-dir /path/to/old-data
# Backup and merge:
python migrate_databases.py --source-dir /path/to/old-data --apply
# Fresh collection + evaluation + report + structured forecast, no Telegram send:
python cron_run_v2.py
# Explicitly send the resulting analysis:
python cron_run_v2.py --publish
```

## One store, one template

`rich_db.py` and `json_db.py` retain legacy function names but read/write the same SQLite database. New forecasts, observations and results use transactions. The import archives all raw source objects, including old lessons and evaluation results; duplicate forecasts with identical text and price sets within two minutes are combined. Imported old evaluations are retained separately and excluded from new performance statistics. Repeating a merge does not duplicate history.

Running `python rich_db.py` now explicitly evaluates due forecasts against stored observations before printing statistics. The existing flat fields remain 24h statistics; `by_horizon` adds all three horizons and `evaluation_run` records completion. This command writes evaluation outcomes but does not collect quotes, create forecasts, or send messages. Importing the module or calling its stats read API does not evaluate. Use `tools/check_runtime.py` for a strictly read-only diagnostic. Existing Hermes LLM analysis can keep calling the same command after this code update.

Forecasts are evaluated independently at 4h, 24h and 1w. The endpoint is the **first observation at or after the due time**, within 30 minutes (configurable via `GOLD_EVALUATION_TOLERANCE_SECONDS`). Missing endpoint data stays unscored; a later live quote is never substituted for a historical endpoint. Missing results may be filled if actual historical observations are imported later. Scored results are immutable. Full history is counted without a row limit; repeated runs do not add duplicate analyses. The report shows the total unique analysis count separately from the scored denominator for each horizon. Direction uses a 0.3% threshold; sideways outside that threshold is a loss. Signal scoring, when an external signal exists, measures price direction before transaction costs and is not a realized trading return.

All report generation script names (`generate_analysis.py`, `generate_final.py`, `generate_report.py`, `generate_forecast.py`, `generate_dollar_forecast.py`, `gen_forecast_script.py`) use one renderer and consume the collector JSON. `analyze.py`, `cron_run_v2.py` and `cron_run.sh` collect fresh inputs first. The two saver scripts now share one saver and store one forecast, rather than writing competing databases. Generated reports get a stable ID and a checksum-bound JSON sidecar. `save_final_forecast.py` and `save_rich_forecast.py` can accept the sidecar directly.

The report's baseline model derives endpoint ranges from mean and standard deviation of log returns over at least seven daily closes. It is an **uncalibrated statistical baseline**, not an LLM prediction or validated investment strategy. No fixed news, historical claims, BUY recommendation, or fabricated confidence percentage is emitted. An external analyst/LLM can supply structured forecasts via `forecast_io.py`; the model, asset, timestamp and ranges must be explicit. See [ANALYSIS_TEMPLATE_FA.md](ANALYSIS_TEMPLATE_FA.md).

Daily ticks and finance records retain JSON format with kernel file locks, atomic replacement, corruption detection and a backup of the last valid file. Finance access control remains unchanged. `self_improve.py` reports only validated horizon results and does not automatically tune trading thresholds.

## Verification and limits

25 core offline regression tests cover horizon timing, scoring, migration idempotency, preservation of originals and raw history, transaction rollback, concurrent SQLite writers, concurrent JSON writers, corrupt inputs, source freshness, units, seven weekdays, common templates and repeated saver calls. GitHub Actions is configured for Linux/Windows with Python 3.10/3.12; results there must be checked after upload.

A local merge of both ZIP folders read 650 source forecast rows (the two database copies overlap), preserved **324 unique forecasts**, and produced **8,489 observation timestamps**. No invalid forecast rows were discarded. Repeating it produced the same counts and SQLite integrity check returned `ok`. These are validation results for the provided ZIP, not current server data. The private merged database is deliberately not in GitHub.

The workspace agent did not directly access the VPS. The owner subsequently supplied successful activation logs and a real manual report with 337 unique analyses; those are owner-provided observations at that time. Hermes scheduling, production delivery and independent market-quote validation remain unverified. Start with [the operations guide](OPERATIONS_FA.md) for an already upgraded server. Internal-use licensing from the original project is unchanged; no open-source license is granted by this upload.

## Safe Bash updater

[safe_update.sh](safe_update.sh) prepares the pinned reviewed release in an isolated venv, tests it, merges a data copy, and supports guarded activation, rollback and interrupted-switch recovery. Default mode is **prepare only**. It never stops unrelated services or sends a Telegram test message. Activation requires the operator to pause the gold schedulers/services and pass `--quiesced`; it also refuses to switch while recognized project processes are running. Existing script paths, bot credentials and `/tmp` output paths are preserved through private compatibility launchers.

Read [SAFE_UPDATE_FA.md](SAFE_UPDATE_FA.md) for exact commands and limits. Eleven updater tests, two private-inspection tests, two read-only health-check tests and two evaluator CLI tests supplement the 25 core tests. On this Windows workspace 40 passed and two directory-symlink tests were skipped; Linux CI is configured to run those and check Bash syntax. The owner reported earlier production activation; installation of the latest evaluator fix is not yet verified, and the workspace agent did not access the VPS directly.

The server-specific `gen_forecast_script.py` now uses the common pipeline; its original fixed report is replaced by data-derived output. The updater also preserves the confirmed `final_forecast_output.txt` file at its original path.
