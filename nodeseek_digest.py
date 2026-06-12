#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import time
import json
import logging
from datetime import datetime
from curl_cffi import requests
from bs4 import BeautifulSoup
import database

# 网页抓取通用 User-Agent (匹配 Chrome 149)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"
}

logger = logging.getLogger("nodeseek_digest")

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

def fetch_html_playwright(url, cookie):
    """使用 Playwright 无头浏览器方式抓取网页 (解析并携带 Cookie)"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise Exception("未安装 Playwright 依赖。请登录后台或在终端中运行: pip3 install playwright && playwright install chromium") from e

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 800}
        )
        
        # 注入 NodeSeek Cookie 以获取高等级帖子
        if cookie:
            cookies_to_add = []
            for item in cookie.split(";"):
                if "=" in item:
                    k, v = item.strip().split("=", 1)
                    cookies_to_add.append({
                        "name": k,
                        "value": v,
                        "domain": ".nodeseek.com",
                        "path": "/"
                    })
            if cookies_to_add:
                context.add_cookies(cookies_to_add)
                
        page = context.new_page()
        # 访问页面，等待 DOM 加载完成即返回，提升速度并防慢速图片导致的超时
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        html_content = page.content()
        browser.close()
        return html_content

def fetch_html(url, config):
    """网络请求代理：根据配置使用 curl_cffi 或 Playwright"""
    engine = config.get("crawler_engine", "curl_cffi")
    cookie = config.get("cookie", "")
    
    headers = HEADERS.copy()
    if cookie:
        headers["Cookie"] = cookie
        
    if engine == "playwright":
        return fetch_html_playwright(url, cookie)
    else:
        # curl_cffi 模拟 Chrome JA3/H2 特征防封
        res = requests.get(url, headers=headers, impersonate="chrome120", timeout=15)
        if res.status_code == 200:
            return res.text
        else:
            raise Exception(f"HTTP 请求失败，状态码: {res.status_code}")

def run_digest_job(config=None):
    """开始一次完整的抓取及保存任务（不含推送）"""
    logger.info("🚀 启动热帖拉取任务流程...")
    if config is None:
        config = database.get_config()
        
    nodeseek_url = config["nodeseek_url"]
    max_pages = config["max_pages"]
    lucky_keywords = config["lucky_keywords"]
    page_delay = config.get("page_delay", 2)
    blocked_uids = config.get("blocked_uids", [])
    
    verbose = config.get("verbose_log", False)
    posts = []
    
    for page in range(1, max_pages + 1):
        url = f"{nodeseek_url}/page-{page}" if page > 1 else nodeseek_url
        logger.info(f"正在扫描第 {page} 页: {url}")
        
        try:
            html_text = fetch_html(url, config)
        except Exception as e:
            logger.error(f"第 {page} 页抓取失败: {e}")
            continue
            
        soup = BeautifulSoup(html_text, 'html.parser')
        items = soup.select('li.post-list-item')
        
        for item in items:
            link_el = item.select_one('.post-title a[href]')
            if not link_el:
                continue
            title = link_el.text.strip()
            href = link_el.get('href')
            post_id_match = re.search(r'post-(\d+)', href)
            post_id = post_id_match.group(1) if post_id_match else ""
            
            # 提取发帖人 UID
            uid = ""
            author_el = item.select_one('a[href^="/space/"]')
            if author_el:
                author_href = author_el.get('href', '')
                uid_match = re.search(r'/space/(\d+)', author_href)
                if uid_match:
                    uid = uid_match.group(1)
            
            # 1. 过滤特定发帖人 UID
            if uid and uid in blocked_uids:
                if verbose:
                    logger.info(f"🚫 过滤黑名单用户帖子: UID={uid} | 标题={title}")
                continue
                
            # 2. 过滤抽奖灌水帖
            if any(kw in title.lower() for kw in lucky_keywords):
                if verbose:
                    logger.info(f"🚫 过滤抽奖贴: {title}")
                continue
                
            # 2. 提取阅读与评论数
            views_el = item.select_one('.info-views')
            comments_el = item.select_one('.info-comments-count')
            views = parse_count(views_el.text) if views_el else 0
            comments = parse_count(comments_el.text) if comments_el else 0
            
            # 3. 过滤非24小时活跃贴
            time_el = item.select_one('.info-last-comment-time time')
            time_text = time_el.text if time_el else "1s ago"
            if not is_recent(time_text):
                if verbose:
                    logger.info(f"⏳ 过滤非24h活跃贴: {title} (时间: {time_text})")
                continue
                
            # 4. 热度计算
            hot_score = round(comments * 5.0 + views * 0.2, 1)
            
            if verbose:
                logger.info(f"🔍 解析到帖子: ID={post_id} | 标题={title} | 阅读={views} | 评论={comments} | 得分={hot_score}")
                
            posts.append({
                "id": post_id,
                "title": title,
                "url": nodeseek_url + href,
                "views": views,
                "comments": comments,
                "score": hot_score
            })
            
        if page < max_pages:
            time.sleep(page_delay)
            
    # 去重并排序
    unique_posts = {p['id']: p for p in posts}.values()
    sorted_posts = sorted(unique_posts, key=lambda x: x['score'], reverse=True)[:10]
    
    # 持久化保存到本地 SQLite 数据库中（默认未推送 push_status=0）
    if sorted_posts:
        database.save_posts(sorted_posts)
        logger.info(f"💾 成功保存 {len(sorted_posts)} 个热帖到本地数据库。")
    else:
        logger.warning("⚠️ 未筛选出符合热度要求的有效帖子。")

def send_to_telegram(posts, token, chat_id_str):
    """推送 HTML 目录至 Telegram 机器人 (支持逗号分隔多个 ID 广播)"""
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    html_msg = f"<b>🔥 NodeSeek 今日热帖订阅</b>\n"
    html_msg += f"<i>📅 生成时间: {date_str} (已自动过滤抽奖帖)</i>\n\n"
    
    for index, post in enumerate(posts):
        html_msg += f"{index + 1}. <b><a href='{post['url']}'>{post['title']}</a></b>\n"
        html_msg += f"    👀 {post['views']} 阅读 | 💬 {post['comments']} 评论 | 📈 热度值: <b>{post['score']}</b>\n\n"
        
    # 切分可能包含的多个 ID (支持中英文逗号)
    chat_ids = [c.strip() for c in re.split(r'[,\uff0c]', str(chat_id_str)) if c.strip()]
    if not chat_ids:
        logger.error("❌ 未配置有效的 Telegram Chat ID！")
        return False
        
    telegram_url = f"https://api.telegram.org/bot{token}/sendMessage"
    
    success_count = 0
    for cid in chat_ids:
        logger.info(f"📡 正在向目标 {cid} 发送推送通知...")
        payload = {
            "chat_id": cid,
            "text": html_msg,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        try:
            res = requests.post(telegram_url, json=payload, impersonate="chrome120", timeout=10)
            if res.status_code == 200:
                logger.info(f"🎉 目标 {cid} 推送成功！")
                success_count += 1
            else:
                logger.error(f"❌ 目标 {cid} 推送失败，TG 响应: {res.text}")
        except Exception as e:
            logger.error(f"❌ 目标 {cid} 推送请求异常: {e}")
            
    return success_count > 0

def push_pending_digests(config=None):
    """读取未推送的帖子，发送至 Telegram 并更新推送状态"""
    if config is None:
        config = database.get_config()
        
    tg_token = config.get("tg_bot_token")
    chat_id = config.get("tg_chat_id")
    
    if not tg_token or not chat_id:
        logger.info("ℹ️ 未配置 Telegram Bot 参数，跳过自动推送。")
        return
        
    # 获取未推送的高热度帖子
    pending_posts = database.get_pending_push_posts(limit=10)
    if not pending_posts:
        logger.info("ℹ️ 没有未推送的候选热帖。")
        return
        
    logger.info(f"📡 发现 {len(pending_posts)} 个待推送的候选热帖，准备推送...")
    success = send_to_telegram(pending_posts, tg_token, chat_id)
    if success:
        post_ids = [p["id"] for p in pending_posts]
        database.update_posts_push_status(post_ids, 1)
        logger.info(f"💾 已将 {len(post_ids)} 个帖子在数据库中的推送状态更新为已推送。")

def push_recent_hot_posts(config=None):
    """单独推送过去 24 小时的热帖（不重新爬取）"""
    logger.info("📡 手动触发：开始单独推送 24h 内的历史热帖...")
    if config is None:
        config = database.get_config()
        
    tg_token = config.get("tg_bot_token")
    chat_id = config.get("tg_chat_id")
    
    if not tg_token or not chat_id:
        logger.warning("ℹ️ 未配置 Telegram Bot 参数，无法执行单独推送任务。")
        return False
        
    posts = database.get_recent_hot_posts(hours=24, limit=10)
    if not posts:
        logger.warning("⚠️ 数据库中没有过去 24 小时内抓取到的历史热帖数据，跳过推送。")
        return False
        
    logger.info(f"💾 数据库已筛选出过去 24h 的 {len(posts)} 个历史热帖，准备推送...")
    success = send_to_telegram(posts, tg_token, chat_id)
    if success:
        post_ids = [p["id"] for p in posts]
        database.update_posts_push_status(post_ids, 1)
        logger.info("🎉 历史热帖已成功发送到所有指定的 Telegram ID！")
        return True
    else:
        logger.error("❌ 历史热帖推送全部失败。")
        return False

def crawl_post_details(post_id, config=None):
    """动态拉取某一帖子具体详情页（正文及评论集，保持原本 HTML 并通过 JSON 返回）"""
    if config is None:
        config = database.get_config()
    nodeseek_url = config["nodeseek_url"]
    url = f"{nodeseek_url}/post-{post_id}-1"
    
    try:
        html_text = fetch_html(url, config)
    except Exception as e:
        logger.error(f"获取帖子 {post_id} 页面失败: {e}")
        return {"error": f"抓取详情页面失败: {str(e)}"}
        
    soup = BeautifulSoup(html_text, 'html.parser')
    
    # 1. 主帖作者及头像
    poster_name = "未知"
    poster_avatar = ""
    poster_el = soup.select_one('.nsk-post .author-name')
    if poster_el:
        poster_name = poster_el.text.strip()
    avatar_el = soup.select_one('.nsk-post img.avatar-normal')
    if avatar_el:
        poster_avatar = avatar_el.get('src')
        
    # 2. 帖子正文 HTML
    content_el = soup.select_one('.nsk-post article.post-content, .nsk-post .post-content, .nsk-post .md-content')
    content_html = str(content_el) if content_el else "<p>未获取到帖子正文内容。</p>"
    
    # 3. 评论列表解析
    comments = []
    comment_items = soup.select('ul.comments li.content-item')
    
    for item in comment_items:
        floor_el = item.select_one('.floor-link')
        author_el = item.select_one('.author-name')
        c_avatar_el = item.select_one('img.avatar-normal')
        c_content_el = item.select_one('article.post-content, .post-content, .md-content')
        
        floor = floor_el.text.strip() if floor_el else ""
        author = author_el.text.strip() if author_el else "匿名"
        avatar = c_avatar_el.get('src') if c_avatar_el else ""
        
        # 将评论中的相对图片/跳转路径升级为绝对路径
        c_content_html = ""
        if c_content_el:
            # 升级相对图片
            for img in c_content_el.select('img[src^="/"]'):
                img['src'] = nodeseek_url + img['src']
            for a in c_content_el.select('a[href^="/"]'):
                a['href'] = nodeseek_url + a['href']
            c_content_html = str(c_content_el)
            
        comments.append({
            "floor": floor,
            "author": author,
            "avatar": avatar,
            "content_html": c_content_html
        })
        
    return {
        "id": post_id,
        "poster_name": poster_name,
        "poster_avatar": poster_avatar,
        "content_html": content_html,
        "comments": comments
    }
