# PriceTracker — Gold Tracker

Imported from `gold-tracker-system.zip` on 2026-10-23. Python scripts collect Iranian gold/forex quotes, build reports, store JSON history, and communicate with Telegram. The source contains experimental and legacy workflows; it is **not ready for unattended production use**. Read [REVIEW_FA.md](REVIEW_FA.md) before running it.

## Import contents

The two ZIP folders were combined into a single project root. Identical code was deduplicated; `generate_analysis.py`, present only in `gold-tracker-package`, was retained. The original archive is unchanged. `IMPORT_MANIFEST.json` records source hashes and publication changes.

Embedded Telegram bot tokens and the channel ID were replaced by environment variables. All four runtime JSON databases were excluded from this public repository, including personal finance records. No historical performance claims from the archive were independently validated. Original internal-use licensing was retained: no open-source license is granted by this upload.

## Install and configure

Requires Python 3.10 or newer. Offline review was run with Python 3.12.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN='your-token'
export TELEGRAM_CHAT_ID='your-channel-id'
export FINANCE_BOT_TOKEN='your-finance-token'
```

`.env.example` lists the settings; scripts read the process environment and do not load `.env` files automatically. Missing credentials must be configured before using Telegram functions. The finance listener currently lacks sender authorization; do not enable it until finding P1-02 is fixed.

## Existing path assumptions

This import preserves the original `/root/gold-tracker-bot` and `/tmp` paths. To run unchanged on Linux, place the checkout at `/root/gold-tracker-bot`. Other locations and Windows need path refactoring. Runtime databases are initialized by their respective code when absent, but the containing directory must exist. Original history remains in the local ZIP and was not published.

```bash
# Fetches external data and writes /tmp/gold_collector_output.json:
python collector.py --technical

# Builds /tmp/final_forecast.txt; contains fixed claims and ranges (see review):
python generate_analysis.py
```

`price_tracker.py` sends Telegram messages; `daily_summary_bot.py` sends and pins messages; `finance_bot_listener.py` starts continuous polling. No live Telegram operations were performed during review. Cron installation, deployment, model integration, and live market source validation are not included in this import. The archive's cron examples do not define a complete collector-to-LLM-to-publication system.

## Offline review evidence

```bash
python -m unittest discover -s tests -v
```

The tests are characterization tests: a passing test confirms that a documented defect was reproduced. They do not mean the system is production-correct. Requests, messages, and DB writes are mocked or confined to temporary files. See [REVIEW_FA.md](REVIEW_FA.md) for priorities and remediation.
