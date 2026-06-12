import logging
from apscheduler.schedulers.background import BackgroundScheduler
import database

# 注意：为了循环引用避让，将在任务运行时动态加载 nodeseek_digest
scheduler = BackgroundScheduler()

# 日志输出配置
logger = logging.getLogger("nodeseek_scheduler")

def run_job():
    logger.info("📅 触发自动调度抓取计划...")
    try:
        import nodeseek_digest
        nodeseek_digest.run_digest_job()
    except Exception as e:
        logger.error(f"❌ 自动调度任务执行失败: {e}")

def init_scheduler():
    if not scheduler.running:
        scheduler.start()
    reload_scheduler()

def reload_scheduler():
    # 移除旧任务
    for job in scheduler.get_jobs():
        job.remove()
        
    config = database.get_config()
    mode = config.get("schedule_mode", "disabled")
    
    if mode == "cron":
        hour = config.get("cron_hour", 21)
        minute = config.get("cron_minute", 30)
        scheduler.add_job(run_job, "cron", hour=hour, minute=minute, id="nodeseek_digest_cron")
        logger.info(f"⏰ 成功载入定时调度计划，每日 {hour:02d}:{minute:02d} 自动运行。")
    elif mode == "interval":
        hours = config.get("interval_hours", 4)
        scheduler.add_job(run_job, "interval", hours=hours, id="nodeseek_digest_interval")
        logger.info(f"⏱️ 成功载入间隔调度计划，每隔 {hours} 小时自动运行一次。")
    else:
        logger.info("💤 自动调度抓取计划已禁用。")
