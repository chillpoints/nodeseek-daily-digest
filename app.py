import os
import logging
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
import database
import scheduler
import nodeseek_digest

# 内存日志缓冲处理器，用于在前端控制台实时展示抓取状态
class MemoryLogHandler(logging.Handler):
    def __init__(self, capacity=100):
        super().__init__()
        self.capacity = capacity
        self.buffer = []
        
    def emit(self, record):
        try:
            msg = self.format(record)
            self.buffer.append(msg)
            if len(self.buffer) > self.capacity:
                self.buffer.pop(0)
        except Exception:
            self.handleError(record)

# 配置 FastAPI 应用
app = FastAPI(title="NodeSeek Daily Digest Dashboard")

# 初始化日志体系并添加内存处理器
log_handler = MemoryLogHandler()
log_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S'))
logging.getLogger().addHandler(log_handler)
logging.getLogger().setLevel(logging.INFO)

# 避让一些第三方库的冗长日志
logging.getLogger("uvicorn").setLevel(logging.INFO)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(STATIC_DIR, exist_ok=True)

class ConfigSchema(BaseModel):
    tg_bot_token: str
    tg_chat_id: str
    nodeseek_url: str
    cookie: str
    max_pages: int
    lucky_keywords: list
    schedule_mode: str
    cron_hour: int
    cron_minute: int
    interval_hours: int
    crawler_engine: str
    page_delay: int
    verbose_log: bool
    blocked_uids: list
    category_weights: dict
    push_limit: int

@app.on_event("startup")
def on_startup():
    # 数据库初始化
    database.init_db()
    # 自动调度器初始化与载入
    scheduler.init_scheduler()

@app.get("/", response_class=HTMLResponse)
def serve_index():
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return HTMLResponse("<h2>控制面板前端 static/index.html 尚未创建，请稍候。</h2>")

@app.get("/api/config")
def fetch_config():
    return database.get_config()

@app.post("/api/config")
def save_config(cfg: ConfigSchema):
    database.update_config(cfg.dict())
    # 重新加载 APScheduler 定时配置
    scheduler.reload_scheduler()
    return {"status": "success", "message": "配置更新成功，抓取调度任务已重载。"}

@app.get("/api/history")
def fetch_history():
    return database.get_posts_history()

@app.get("/api/logs")
def fetch_logs():
    return {"logs": log_handler.buffer}

@app.post("/api/run")
def trigger_run(background_tasks: BackgroundTasks):
    logging.info("⚡ 控制面板触发手动执行抓取任务...")
    background_tasks.add_task(nodeseek_digest.run_digest_job)
    background_tasks.add_task(nodeseek_digest.push_pending_digests)
    return {"status": "success", "message": "抓取与推送任务已成功在后台顺序启动。"}

@app.post("/api/push")
def trigger_push(background_tasks: BackgroundTasks):
    logging.info("⚡ 控制面板触发手动执行单独推送历史热帖...")
    background_tasks.add_task(nodeseek_digest.push_recent_hot_posts)
    return {"status": "success", "message": "历史热帖推送任务已成功在后台启动。"}

@app.post("/api/posts/clear")
def clear_local_posts():
    try:
        database.clear_posts()
        logging.info("🧹 已清空本地数据库中的历史热贴数据。")
        return {"status": "success", "message": "本地历史热帖已成功清空。"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"清空失败: {str(e)}")

@app.get("/api/post/{post_id}")
def fetch_post_details(post_id: str):
    details = nodeseek_digest.crawl_post_details(post_id)
    if "error" in details:
        raise HTTPException(status_code=500, detail=details["error"])
    return details
