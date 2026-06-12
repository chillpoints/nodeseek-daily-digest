#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import re
from datetime import datetime
from curl_cffi import requests
from bs4 import BeautifulSoup

# 请求头，模拟真实的普通浏览器请求
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"
}

def load_config():
    """读取本地 config.json 或通过环境变量注入配置"""
    config = {
        "tg_bot_token": "",
        "tg_chat_id": "",
        "nodeseek_url": "https://www.nodeseek.com",
        "max_pages": 5,
        "lucky_keywords": ["抽奖", "送码", "送鸡腿", "卡密", "免费送", "送个", "福利", "送台"]
    }
    
    # 尝试加载本地 config.json
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                local_config = json.load(f)
                config.update(local_config)
        except Exception as e:
            print(f"警告: 读取 config.json 失败: {e}")
            
    # 环境变量覆盖（用于 Docker/Actions 等平台部署）
    if os.environ.get("TG_BOT_TOKEN"):
        config["tg_bot_token"] = os.environ.get("TG_BOT_TOKEN")
    if os.environ.get("TG_CHAT_ID"):
        config["tg_chat_id"] = os.environ.get("TG_CHAT_ID")
    if os.environ.get("NODESEEK_URL"):
        config["nodeseek_url"] = os.environ.get("NODESEEK_URL")
        
    return config

def parse_count(text):
    """提取浏览量与评论数中的数字"""
    text = text.lower().strip()
    match = re.search(r'([\d\.]+)\s*([万w千k]?)', text)
    if not match:
        return 0
    val = float(match.group(1))
    unit = match.group(2)
    factor = 10000 if unit in ["万", "w"] else (1000 if unit in ["千", "k"] else 1)
    return int(val * factor)

def is_recent(time_text):
    """过滤24小时以外的帖子，只保留近期活跃贴"""
    time_text = time_text.lower().strip()
    if "d ago" in time_text and not time_text.startswith("1d"):
        return False
    if "w ago" in time_text or "month" in time_text or "year" in time_text:
        return False
    return True

def fetch_hot_posts(config):
    posts = []
    max_pages = config["max_pages"]
    nodeseek_url = config["nodeseek_url"]
    lucky_keywords = config["lucky_keywords"]
    
    for page in range(1, max_pages + 1):
        url = f"{nodeseek_url}/page-{page}" if page > 1 else nodeseek_url
        print(f"正在抓取页面: {url}")
        
        try:
            # 关键点：使用 curl_cffi.requests 并指定 impersonate='chrome120'，提供完整的 TLS/JA3 指纹模拟，绕过 Cloudflare 阻拦
            res = requests.get(url, headers=HEADERS, impersonate="chrome120", timeout=15)
            if res.status_code != 200:
                print(f"抓取页面失败: HTTP {res.status_code}")
                continue
        except Exception as e:
            print(f"请求发生异常: {e}")
            continue
            
        soup = BeautifulSoup(res.text, 'html.parser')
        items = soup.select('li.post-list-item')
        
        for item in items:
            link_el = item.select_one('.post-title a[href]')
            if not link_el:
                continue
            title = link_el.text.strip()
            href = link_el.get('href')
            post_id = re.search(r'post-(\d+)', href).group(1) if re.search(r'post-(\d+)', href) else ""
            
            # 1. 过滤抽奖贴
            if any(kw in title.lower() for kw in lucky_keywords):
                continue
                
            # 2. 提取阅读数与评论数
            views_el = item.select_one('.info-views')
            comments_el = item.select_one('.info-comments-count')
            views = parse_count(views_el.text) if views_el else 0
            comments = parse_count(comments_el.text) if comments_el else 0
            
            # 3. 时间合法性过滤
            time_el = item.select_one('.info-last-comment-time time')
            time_text = time_el.text if time_el else "1s ago"
            if not is_recent(time_text):
                continue
                
            # 4. 计算热度分数：评论*5 + 浏览*0.2
            hot_score = round(comments * 5.0 + views * 0.2, 1)
            
            posts.append({
                "id": post_id,
                "title": title,
                "url": nodeseek_url + href,
                "views": views,
                "comments": comments,
                "score": hot_score
            })
            
    # 去重并以热度降序排序，取前10条
    unique_posts = {p['id']: p for p in posts}.values()
    sorted_posts = sorted(unique_posts, key=lambda x: x['score'], reverse=True)[:10]
    return sorted_posts

def send_to_telegram(posts, config):
    if not posts:
        print("未筛选出今日热帖。")
        return
        
    tg_token = config["tg_bot_token"]
    chat_id = config["tg_chat_id"]
    
    if not tg_token or not chat_id:
        print("错误: 缺少 tg_bot_token 或 tg_chat_id 配置，无法完成推送！")
        return
        
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    html_msg = f"<b>🔥 NodeSeek 今日高热度贴总结</b>\n"
    html_msg += f"<i>📅 生成时间: {date_str} (已自动过滤抽奖帖)</i>\n\n"
    
    for index, post in enumerate(posts):
        html_msg += f"{index + 1}. <b><a href='{post['url']}'>{post['title']}</a></b>\n"
        html_msg += f"    👀 {post['views']} 阅读 | 💬 {post['comments']} 评论 | 📈 热度值: <b>{post['score']}</b>\n\n"
        
    telegram_url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": html_msg,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    
    try:
        res = requests.post(telegram_url, json=payload, timeout=10)
        if res.status_code == 200:
            print("Telegram 消息推送成功！")
        else:
            print(f"推送失败，Telegram 响应: {res.text}")
    except Exception as e:
        print(f"推送请求发生异常: {e}")

if __name__ == "__main__":
    config = load_config()
    print("已成功载入配置。")
    hot_posts = fetch_hot_posts(config)
    send_to_telegram(hot_posts, config)
