import sqlite3
import os
from datetime import datetime

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nodeseek_digest.db")

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
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
        "page_delay": "2"             # 防风控间隔 (秒)
    }
    
    for k, v in default_config.items():
        cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()

def get_config():
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
        elif key in ["max_pages", "cron_hour", "cron_minute", "interval_hours", "page_delay"]:
            config[key] = int(val) if val.isdigit() else 0
        else:
            config[key] = val
    return config

def update_config(new_config):
    conn = get_db()
    cursor = conn.cursor()
    for k, v in new_config.items():
        if k == "lucky_keywords" and isinstance(v, list):
            v = ",".join(v)
        cursor.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (k, str(v)))
    conn.commit()
    conn.close()

def save_posts(posts):
    conn = get_db()
    cursor = conn.cursor()
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for post in posts:
        cursor.execute("""
        INSERT OR REPLACE INTO posts (id, title, url, views, comments, score, crawler_date, push_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (post["id"], post["title"], post["url"], post["views"], post["comments"], post["score"], date_str, 1))
    conn.commit()
    conn.close()

def get_posts_history(limit=50):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, url, views, comments, score, crawler_date FROM posts ORDER BY crawler_date DESC LIMIT ?", (limit,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

# 初始化数据库
init_db()
