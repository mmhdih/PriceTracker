# تکمیل اتصال‌های نصب فعلی، بدون آپدیت دوباره کد

اگر نصب core در `08ff97bb4dd41f00cb65c0cd530800a1f0b33976` انجام شده، ارزیابی کار می‌کند ولی prompt/skill قدیمی و watcher قبلی حفظ شده‌اند، نصب هنوز کامل نیست. `scheduled` یعنی job زمان‌بندی شده است؛ موفقیت تحلیل و تحویل را اثبات نمی‌کند.

ابزار [complete_hermes_install.py](complete_hermes_install.py) فقط اتصال تحلیل ساختاریافته و retry محدود را تکمیل می‌کند. این ابزار مستقل و مبتنی بر کتابخانه استاندارد است؛ فایل را بیرون از پروژه، مثلاً `/tmp/price-tracker-complete.py` دریافت کنید. به updater یا کد فعال دوباره دست نمی‌زند. نسخه دانلود را به commit انتشار معرفی‌شده مالک پین کنید؛ فایل از منبع ناشناخته اجرا نشود.

## دستور آماده برای Hermes

هدف نصب واقعی اتصال‌های باقی‌مانده است، نه فقط تهیه گزارش. مسیر فعلی پروژه `/root/gold-tracker-bot` است. از نسخه پین‌شده ابزار تکمیل نصب استفاده کن. ابتدا profile واقعی، ID job تحلیل `gold-analysis-8h`، ID watcher `gold-analysis-retry-watcher` و مسیر source نصب Hermes را از نصب موجود تعیین کن. مقادیر ID یا مقصد را منتشر نکن.

فایل دانلودشده را ابتدا بدون `--apply` و با آرگومان‌های واقعی اجرا کن:

```text
python3 /tmp/price-tracker-complete.py --hermes-home ACTUAL_HOME --hermes-source ACTUAL_SOURCE --analysis-job-id ACTUAL_ANALYSIS_ID --watcher-job-id ACTUAL_WATCHER_ID
```

`ACTUAL_*`ها جای‌نگهدارند؛ قبل از اجرا با مقادیر نصب موجود جایگزین شوند. مسیرهای پیش‌فرض live و source به‌ترتیب `/root/gold-tracker-bot` و `/usr/local/lib/hermes-agent` هستند؛ profile اجباری است تا نصب دیگری به‌اشتباه انتخاب نشود.

در `READY_TO_INSTALL` همان فرمان را با افزودن `--apply` اجرا کن. نیاز به تأیید دیگری برای همین محدوده نیست. در `ALREADY_INSTALLED` دوباره چیزی نصب نکن. در `STOPPED` یا خروج غیرصفر، علت دقیق و مسیر بکاپ را بررسی کن؛ شرط نسخه/schema را حذف نکن. اگر lifecycle guard اجرای ابزار را مسدود کرد، هیچ گاردی را دور نزن و gateway را restart نکن؛ فرمان دقیق را برای مالک در SSH عادی آماده کن.

## تغییرات محدود ابزار

- hash فایل‌های اصلی نصب‌شده را با core تأییدشده تطبیق می‌دهد؛ markerهای scheduler، امکان جایگزینی skills در CLI نصب‌شده، schema اجرای jobها و SQLite واقعی را پیش از تغییر بررسی می‌کند.
- فقط تحلیل و watcher را با CLI رسمی موقتاً pause می‌کند و پایان executionهای فعال همین دو را بررسی می‌کند. هیچ فرایندی را kill نمی‌کند.
- از دو config، wrapper و تنها کلید metadata مربوط به watcher، بکاپ خصوصی می‌گیرد. هیچ دیتابیس یا تاریخچه‌ای را با بکاپ قدیمی جایگزین نمی‌کند.
- skill جدید `price-tracker-structured-analysis` و prompt دقیق بسته را متصل می‌کند؛ skill قدیمی حذف یا بازنویسی نمی‌شود.
- watcher را برای همان profile/analysis پیکربندی و فقط wrapper Python واقعی همان job را اتمیک جایگزین می‌کند. wrapper به helper مدیریت‌شده پروژه اشاره می‌کند.
- dry-run watcher را در حالت paused بررسی و وضعیت فعال قبلی همین دو job را بازمی‌گرداند. jobی که قبلاً عمداً paused بوده، paused می‌ماند.
- schedule، deliver، مقصد، مدل و سایر تنظیمات را تغییر نمی‌دهد؛ tracker، خلاصه روزانه و ربات‌های دیگر را pause نمی‌کند. `jobs.json` و `executions.db` را فقط می‌خواند؛ ویرایش job از CLI رسمی است.
- هیچ `cron run`، اجرای `begin`، retry دستی، ارسال یا پین آزمایشی ندارد. resume ممکن است طبق سیاست scheduler یک اجرای عادیِ سررسیدشده را فعال کند؛ ابزار آن را اجرای تست نمی‌داند.

## نتیجه و برگشت

`INSTALLED` یا `ALREADY_INSTALLED` یعنی اتصال‌ها با شرایط بررسی‌شده حاضرند؛ به معنی تولید/تحویل موفق چرخه بعدی نیست. خروجی ابزار `telegram_delivery_checked=false` دارد.

در خطای قابل‌بازگشت، ابزار prompt/skills و wrapper و metadata قبلی را برمی‌گرداند و enabled قبلی را بازسازی می‌کند. اگر برگشت خودکار هم خطا داد، خروجی وضعیت را موفق اعلام نمی‌کند؛ بکاپ خصوصی و وضعیت واقعی دو job بررسی شوند. هیچ job را کورکورانه resume نکنید.

بکاپ در `ACTUAL_HOME/price-tracker-install-backups/<timestamp>/` با دسترسی خصوصی است. قطع برق/kill ممکن است journal نیمه‌کاره باقی بگذارد؛ اجرای نصب بعدی پیش از تغییر آن را تشخیص می‌دهد. برای برگشت از همان بکاپ، همان آرگومان‌های profile/ID را همراه زیر اجرا کنید:

```text
--rollback /absolute/private/backup/directory
```

فایل journal را منتشر نکنید؛ ممکن است prompt و مقصد خصوصی قبلی در آن باشد. اگر بعداً wrapper یا prompt مستقل تغییر کرده، بازیابی خودکار به‌جای بازنویسی آن متوقف می‌شود.

## شاهد نخستین چرخه عادی

پس از نصب، بدون اجرای دستی job، نتیجه اولین اجرای برنامه‌ریزی‌شده موجود را بررسی کن: خروج موفق begin/finish، cycle/forecast متناظر، hash متن مشترک و نتیجه تحویل واقعی همان execution در Hermes. ابزار فقط‌خواندنی `tools/check_runtime.py --live-dir /root/gold-tracker-bot` بخش `structured_integration` را نشان می‌دهد؛ `committed` به‌تنهایی رسید Telegram نیست. اگر هنوز چرخه یا رسیدی مشاهده نشده، همان را صریح بنویس. job یا مانیتور تازه نساز.

ساعت‌های مورد انتظار تحلیل: ۰۶:۳۰، ۰۹:۳۰، ۱۱:۳۰، ۱۴:۳۰، ۱۷:۳۰ و ۲۰:۳۰ تهران، با عبارت UTC `0 3,6,8,11,14,17 * * *`. خلاصه: `0 17 * * *` UTC، یعنی ۲۰:۳۰ تهران همان روز. نمایش ساعت را از timestamp خام محاسبه کن.

## شواهد آزمون

۱۰ تست جدید با CLI ساختگی و SQLite خصوصی: نصب، اجرای دوباره، حفظ تنظیمات jobهای دیگر، خطا پیش/پس از configure، برگشت metadata بدون برگرداندن کل DB، حفظ pause قبلی، بازیابی صریح، تشخیص تغییر مستقل، journal نیمه‌کاره و ناسازگاری نسخه/مسیر. مجموعه فعلی روی Windows: **۶۸ موفق از ۷۰**، با دو تست symlink مخصوص Linux که skip شدند. نسخه واقعی Hermes، اجرای Linux و تحویل Telegram روی VPS در این تست‌ها اجرا نشده‌اند.

مبنای CLI: [parser رسمی Hermes](https://github.com/NousResearch/hermes-agent/blob/main/hermes_cli/subcommands/cron.py)، [ویرایش رسمی job](https://github.com/NousResearch/hermes-agent/blob/main/hermes_cli/cron.py)، [scheduler](https://github.com/NousResearch/hermes-agent/blob/main/cron/scheduler.py). وجود رفتار در نسخه سرور جداگانه توسط preflight بررسی می‌شود؛ صرف وجود در منبع آنلاین کافی نیست.
