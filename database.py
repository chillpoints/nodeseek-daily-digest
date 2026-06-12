import sqlite3
import os
import threading
import json
from datetime import datetime

# 全局 SQLite 连接互斥锁，确保并发读写安全
db_lock = threading.Lock()

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nodeseek_digest.db")

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db_lock:
        conn = get_db()
        cursor = conn.cursor()
    # 建立配置表
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS config (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)
    # 建立历史热贴表
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS posts (
        id TEXT PRIMARY KEY,
        title TEXT,
        url TEXT,
        views INTEGER,
        comments INTEGER,
        score REAL,
        crawler_date TEXT,
        push_status INTEGER DEFAULT 0
    )
    """)
    conn.commit()
    
    # 设定系统初始默认配置
    default_config = {
        "tg_bot_token": "",
        "tg_chat_id": "",
        "nodeseek_url": "https://www.nodeseek.com",
        "cookie": "",
        "max_pages": "5",
        "lucky_keywords": "抽奖,送码,送鸡腿,卡密,免费送,送个,福利,送台,抽个,送点",
        "schedule_mode": "cron",      # 'cron', 'interval', 'disabled'
        "cron_hour": "21",
        "cron_minute": "30",
        "interval_hours": "4",
        "crawler_engine": "curl_cffi", # 'curl_cffi', 'playwright'
        "page_delay": "2",             # 防风控间隔 (秒)
        "verbose_log": "0",            # 是否开启详细日志 (0-关闭, 1-开启)
        "blocked_uids": "",             # 拉黑过滤特定发帖人 UID (以逗号分隔)
        "category_weights": '{"日常": 0.7, "技术": 1.0, "情报": 1.0, "测评": 0.8, "交易": 0.3, "拼车": 0.3, "推广": 0.3, "生活": 0.2, "Dev": 0.6, "贴图": 0.0, "曝光": 0.5, "内版": 0.0, "沙盒": 0.0}',
        "push_limit": "10"
    }
    
    for k, v in default_config.items():
        cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()
 
def get_config():
    with db_lock:
        conn = get_db()
        cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM config")
    rows = cursor.fetchall()
    conn.close()
    config = {}
    for row in rows:
        key = row["key"]
        val = row["value"]
        if key == "lucky_keywords":
            config[key] = [x.strip() for x in val.split(",") if x.strip()]
        elif key in ["max_pages", "cron_hour", "cron_minute", "interval_hours", "page_delay", "push_limit"]:
            config[key] = int(val) if val.isdigit() else 0
        elif key == "verbose_log":
            config[key] = (val == "1" or val.lower() == "true")
        elif key == "blocked_uids":
            config[key] = [x.strip() for x in val.split(",") if x.strip()]
        elif key == "category_weights":
            try:
                config[key] = json.loads(val)
            except Exception:
                config[key] = {}
        else:
            config[key] = val
    return config

def update_config(new_config):
    with db_lock:
        conn = get_db()
        cursor = conn.cursor()
        for k, v in new_config.items():
            if k in ["lucky_keywords", "blocked_uids"] and isinstance(v, list):
                v = ",".join(v)
            elif k == "category_weights" and isinstance(v, dict):
                v = json.dumps(v, ensure_ascii=False)
            cursor.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (k, str(v)))
    conn.commit()
    conn.close()

def save_posts(posts):
    with db_lock:
        conn = get_db()
        cursor = conn.cursor()
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for post in posts:
            cursor.execute("""
            INSERT INTO posts (id, title, url, views, comments, score, crawler_date, push_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(id) DO UPDATE SET
                views=excluded.views,
                comments=excluded.comments,
                score=excluded.score,
                crawler_date=excluded.crawler_date
            """, (post["id"], post["title"], post["url"], post["views"], post["comments"], post["score"], date_str))
        conn.commit()
        conn.close()

def get_posts_history(limit=50):
    with db_lock:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT id, title, url, views, comments, score, crawler_date FROM posts ORDER BY crawler_date DESC LIMIT ?", (limit,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]

def get_pending_push_posts(limit=10):
    """获取尚未推送的高热度帖子"""
    with db_lock:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
        SELECT id, title, url, views, comments, score, crawler_date
        FROM posts
        WHERE push_status = 0
        ORDER BY score DESC
        LIMIT ?
        """, (limit,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]

def update_posts_push_status(post_ids, status):
    """更新指定帖子的推送状态"""
    if not post_ids:
        return
    with db_lock:
        conn = get_db()
        cursor = conn.cursor()
        placeholders = ",".join(["?"] * len(post_ids))
        cursor.execute(f"""
        UPDATE posts
        SET push_status = ?
        WHERE id IN ({placeholders})
        """, [status] + list(post_ids))
        conn.commit()
        conn.close()

def get_recent_hot_posts(hours=24, limit=10):
    """获取过去指定小时内录入的、热度前 limit 的帖子"""
    from datetime import datetime, timedelta
    with db_lock:
        conn = get_db()
        cursor = conn.cursor()
        threshold_time = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("""
        SELECT id, title, url, views, comments, score, crawler_date
        FROM posts
        WHERE crawler_date >= ?
        ORDER BY score DESC
        LIMIT ?
        """, (threshold_time, limit))
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]

def clear_posts():
    """清空 posts 表中的所有历史热帖数据"""
    with db_lock:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM posts")
        conn.commit()
        conn.close()

# 初始化数据库
init_db()
