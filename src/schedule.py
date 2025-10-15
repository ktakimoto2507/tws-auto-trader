from __future__ import annotations
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
import os

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

# 既存のロガー
from .utils.logger import get_logger
log = get_logger("scheduler")

# ==== 戦略呼び出し（実体がまだならDRY RUNのログだけ） ====
def run_nugt():
    log.info("[NUGT covered call] DRY RUN: would buy shares, add 6% STP, sell ATM call")

def run_tmf():
    log.info("[TMF  covered call] DRY RUN: would buy shares, add 6% STP, sell ATM call")

def run_uvix():
    log.info("[UVIX long put]    DRY RUN: would buy ATM put; if +15% underlying then stop & short P-")

def run_vix_put_spread_tday(tag: str):
    log.info(f"[VIX put spread {tag}] DRY RUN: long P @ ATM+15%, short P @ ATM; if long +15% ITM then close short")

# ==== タイムゾーン ====
TZ_LOCAL = os.getenv("TZ", "Asia/Tokyo")
tz_local = ZoneInfo(TZ_LOCAL)
tz_ny = ZoneInfo("America/New_York")  # 米東部

# ==== 週次（金曜）ジョブ ====
def add_weekly_jobs(sched: BlockingScheduler):
    """金曜の米市場オープン直後（09:35 NY）に実行"""
    hour, minute = 9, 35
    trig = CronTrigger(day_of_week="fri", hour=hour, minute=minute, timezone=tz_ny)
    sched.add_job(run_nugt, trig, id="weekly_nugt", replace_existing=True)
    sched.add_job(run_tmf,  trig, id="weekly_tmf",  replace_existing=True)
    sched.add_job(run_uvix, trig, id="weekly_uvix", replace_existing=True)
    log.info(f"Registered weekly jobs (Fri {hour:02d}:{minute:02d} NY): NUGT/TMF/UVIX")

# ==== 月次（VIX：SQ直後T+1/T+2/T+3） ====
def third_wednesday(year: int, month: int) -> datetime:
    """第三水曜(仮のSQ)の0時NYを返す"""
    d = datetime(year, month, 1, tzinfo=tz_ny)
    offset = (2 - d.weekday()) % 7  # Wed=2
    first_wed = d + timedelta(days=offset)
    return first_wed + timedelta(days=14)

def next_three_trading_days(d: datetime) -> list[datetime]:
    """dの翌営業日から3営業日（簡易：土日だけ除外）"""
    days = []
    cur = d + timedelta(days=1)
    while len(days) < 3:
        if cur.weekday() < 5:  # Mon-Fri
            days.append(cur)
        cur += timedelta(days=1)
    return days

def add_monthly_vix_jobs(sched: BlockingScheduler):
    now = datetime.now(tz_ny)
    y, m = now.year, now.month
    next_y, next_m = (y + 1, 1) if m == 12 else (y, m + 1)
    for (yy, mm) in [(y, m), (next_y, next_m)]:
        sq = third_wednesday(yy, mm)
        trg_time = time(9, 35, tzinfo=tz_ny)
        tdays = [td.replace(hour=trg_time.hour, minute=trg_time.minute) for td in next_three_trading_days(sq)]
        for tag, dt in zip(["T+1", "T+2", "T+3"], tdays):
            sched.add_job(run_vix_put_spread_tday, "date", run_date=dt, args=[tag],
                          id=f"vix_{yy}{mm:02d}_{tag}", replace_existing=True)
            log.info(f"Registered VIX job {tag} at {dt.astimezone(tz_local)} (local) / {dt} (NY)")

# ==== エントリーポイント ====
def main():
    log.info(f"Scheduler starting... TZ_LOCAL={TZ_LOCAL}")
    sched = BlockingScheduler(timezone=tz_local)
    add_weekly_jobs(sched)
    add_monthly_vix_jobs(sched)
    for job in sched.get_jobs():
        log.info(f"Job {job.id} next run at {job.next_run_time.astimezone(tz_local)} (local)")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")

if __name__ == "__main__":
    main()