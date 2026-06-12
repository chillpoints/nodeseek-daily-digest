#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import time
import json
import logging
import math
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

def is_recent(time_text, time_decay_mode="disabled"):
    """过滤过期的帖子，只保留近期活跃贴"""
    time_text = time_text.lower().strip()
    if time_decay_mode == "disabled":
        if "d ago" in time_text and not time_text.startswith("1d"):
            return False
        if "天前" in time_text and not time_text.startswith("1天"):
            return False
        if any(x in time_text for x in ["w ago", "month", "year", "周前", "个月前", "年前"]):
            return False
        return True
    else:
        # 宽松过滤，放宽到 30 天，只过滤超过月的帖子
        if any(x in time_text for x in ["month", "year", "个月前", "年前"]):
            return False
        match = re.search(r'(\d+)\s*(w ago|周前)', time_text)
        if match:
            try:
                weeks = int(match.group(1))
                if weeks >= 4:
                    return False
            except Exception:
                pass
        return True

def clean_html_to_text(html_content):
    """剔除 HTML 标签，返回干净的纯文本"""
    if not html_content:
        return ""
    soup = BeautifulSoup(html_content, 'html.parser')
    return soup.get_text(separator="\n", strip=True)

def call_openai_api(config, system_prompt, user_content):
    """调用符合 OpenAI 规范的 API 接口"""
    api_key = config.get("ai_api_key", "").strip()
    base_url = config.get("ai_base_url", "https://api.openai.com/v1").strip()
    model = config.get("ai_model", "gpt-4o-mini").strip()
    
    if not api_key:
        logger.warning("⚠️ AI 服务已启用但未配置 API Key！")
        return None
        
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    url = base_url
    if not url.endswith("/chat/completions"):
        url = url.rstrip("/") + "/chat/completions"
        
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ],
        "temperature": 0.3
    }
    
    try:
        logger.info(f"🤖 正在调用 AI 接口 ({model})... API 地址: {url}")
        res = requests.post(url, headers=headers, json=payload, impersonate="chrome120", timeout=30)
        if res.status_code == 200:
            data = res.json()
            result = data["choices"][0]["message"]["content"].strip()
            return result
        else:
            logger.error(f"❌ AI API 响应错误: {res.status_code} - {res.text}")
            return None
    except Exception as e:
        logger.error(f"❌ AI API 请求发生异常: {e}")
        return None

def parse_time_to_hours(time_text):
    """将时间文本解析为相对当前时间的小时数"""
    time_text = time_text.lower().strip()
    match = re.search(r'(\d+)\s*(s|m|h|d|w|month|year|秒|分|小时|天|周|月|年)', time_text)
    if not match:
        return 1.0
    val = float(match.group(1))
    unit = match.group(2)
    
    if unit in ['s', '秒']:
        return val / 3600.0
    elif unit in ['m', '分', '分钟']:
        return val / 60.0
    elif unit in ['h', '小时']:
        return val
    elif unit in ['d', '天']:
        return val * 24.0
    elif unit in ['w', '周']:
        return val * 24.0 * 7.0
    elif unit in ['month', '月']:
        return val * 24.0 * 30.0
    elif unit in ['year', '年']:
        return val * 24.0 * 365.0
    return 1.0

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
    push_limit = config.get("push_limit", 10)
    
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
            
            # 3. 过滤非活跃贴
            time_el = item.select_one('.info-last-comment-time time')
            time_text = time_el.text if time_el else "1s ago"
            time_decay_mode = config.get("time_decay_mode", "hill")
            if not is_recent(time_text, time_decay_mode=time_decay_mode):
                if verbose:
                    logger.info(f"⏳ 过滤非近期活跃贴: {title} (时间: {time_text})")
                continue
                
            # 4. 类别权重与时间衰减热度计算
            category_el = item.select_one('a[href^="/categories/"]')
            category = category_el.text.strip() if category_el else "日常"
            category_weights = config.get("category_weights", {})
            weight = category_weights.get(category, 1.0)
            try:
                weight = float(weight)
            except (TypeError, ValueError):
                weight = 1.0
                
            # 计算时间衰减因子
            age_hours = parse_time_to_hours(time_text)
            time_decay_half_life = config.get("time_decay_half_life", 240)
            time_decay_gravity = config.get("time_decay_gravity", 1.0)
            time_decay_slope = config.get("time_decay_slope", 2.0)
            time_decay_flat_hours = config.get("time_decay_flat_hours", 4)
            
            t_adj = max(0.0, age_hours - time_decay_flat_hours)
            w_time = 1.0
            
            if time_decay_mode == "disabled":
                w_time = 1.0
            elif time_decay_mode == "exponential":
                if time_decay_half_life > 0:
                    lam = math.log(2) / time_decay_half_life
                    w_time = math.exp(-lam * t_adj)
            elif time_decay_mode == "gravity":
                w_time = 1.0 / math.pow(t_adj + 2.0, time_decay_gravity)
            elif time_decay_mode == "linear":
                limit = time_decay_half_life * 2.0
                if limit > 0:
                    w_time = max(0.01, 1.0 - (t_adj / limit))
            elif time_decay_mode == "hill":
                if time_decay_half_life > 0:
                    w_time = 1.0 / (1.0 + math.pow(t_adj / time_decay_half_life, time_decay_slope))
                    
            base_score = comments * 5.0 + views * 0.2
            hot_score = round(base_score * weight * w_time, 1)
            
            if verbose:
                logger.info(f"🔍 解析到帖子: ID={post_id} | 标题={title} | 分类={category}(权重:{weight}) | 年龄={round(age_hours, 2)}h(平坦化后:{round(t_adj, 2)}h) | 衰减模式={time_decay_mode}(因子:{round(w_time, 3)}) | 阅读={views} | 评论={comments} | 基础得分={round(base_score, 1)} | 加权得分={hot_score}")
                
            posts.append({
                "id": post_id,
                "title": title,
                "url": nodeseek_url + href,
                "views": views,
                "comments": comments,
                "score": hot_score,
                "category": category,
                "author_uid": uid
            })
            
        if page < max_pages:
            time.sleep(page_delay)
            
    # 启用 AI 智能帖子过滤
    if posts and config.get("ai_enabled") and config.get("ai_filter_enabled"):
        logger.info("🤖 启用 AI 智能帖子过滤，正在发送帖子列表进行语义筛选...")
        eval_list = [{"id": p["id"], "title": p["title"], "category": p.get("category", "日常")} for p in posts]
        user_content = f"用户筛选标准：{config.get('ai_filter_prompt', '')}\n\n待评估的帖子列表：\n{json.dumps(eval_list, ensure_ascii=False)}"
        system_prompt = (
            "你是一个专业的论坛帖子过滤筛选助手。你必须根据用户设定的偏好标准评估帖子是否应该被保留。\n"
            "你必须严格返回一个 JSON 数组，包含所有应保留的帖子 ID，例如: [\"12345\", \"67890\"]。\n"
            "不要包含 markdown 格式标记 (如 ```json)，也不要有任何解释，只输出符合格式的纯 JSON 字符串。"
        )
        result = call_openai_api(config, system_prompt, user_content)
        if result:
            try:
                cleaned_result = re.sub(r'^```json\s*|```\s*$', '', result, flags=re.MULTILINE).strip()
                keep_ids = json.loads(cleaned_result)
                if isinstance(keep_ids, list):
                    keep_set = set(str(x) for x in keep_ids)
                    before_count = len(posts)
                    posts = [p for p in posts if str(p["id"]) in keep_set]
                    logger.info(f"🤖 AI 筛选完成：从 {before_count} 个帖子中保留了 {len(posts)} 个。")
                else:
                    logger.warning("⚠️ AI 返回数据格式错误（非列表），跳过 AI 过滤。")
            except Exception as e:
                logger.warning(f"⚠️ 解析 AI 筛选结果 JSON 失败: {e} | 原始返回: {result}，跳过 AI 过滤。")
        else:
            logger.warning("⚠️ AI 过滤未获取到有效响应，跳过 AI 过滤。")

    # 去重并排序
    unique_posts = {p['id']: p for p in posts}.values()
    sorted_posts = sorted(unique_posts, key=lambda x: x['score'], reverse=True)[:push_limit]
    
    # 针对筛选出的 Top 热帖生成 AI 摘要
    if sorted_posts and config.get("ai_enabled") and config.get("ai_summary_enabled"):
        logger.info(f"🤖 启用 AI 帖子摘要总结，将依次拉取正文与评论分析前 {len(sorted_posts)} 个热帖...")
        for idx, post in enumerate(sorted_posts):
            post_id = post["id"]
            logger.info(f"🤖 正在分析第 {idx + 1}/{len(sorted_posts)} 个帖子: {post['title']}")
            try:
                details = crawl_post_details(post_id, config)
                if "error" not in details:
                    body_text = clean_html_to_text(details.get("content_html", ""))[:1500]
                    comment_list = []
                    for c in details.get("comments", [])[:5]:
                        c_text = clean_html_to_text(c.get("content_html", ""))
                        comment_list.append(f"- {c.get('author', '匿名')}: {c_text}")
                    comments_text = "\n".join(comment_list)[:1500]
                    
                    user_content = (
                        f"帖子标题：{post['title']}\n"
                        f"正文内容：\n{body_text}\n\n"
                        f"精选热评：\n{comments_text}\n\n"
                        f"生成要求：{config.get('ai_summary_prompt', '')}"
                    )
                    system_prompt = "你是一个专业的内容提炼助手。请根据给出的帖子内容和评论，生成一段高密度、简练的中文摘要（通常为 1-2 句话，控制在 100 字内），严禁带有啰嗦客套话。"
                    
                    summary = call_openai_api(config, system_prompt, user_content)
                    if summary:
                        post["ai_summary"] = summary
                        logger.info(f"🤖 摘要提炼成功: {summary}")
                    else:
                        post["ai_summary"] = ""
                else:
                    logger.warning(f"⚠️ 拉取帖子 {post_id} 详情失败，跳过摘要生成。")
                    post["ai_summary"] = ""
            except Exception as e:
                logger.error(f"⚠️ 处理帖子 {post_id} 摘要提炼发生异常: {e}")
                post["ai_summary"] = ""
    else:
        # 填充缺省值
        for post in sorted_posts:
            post["ai_summary"] = ""
            
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
        html_msg += f"    👀 {post['views']} 阅读 | 💬 {post['comments']} 评论 | 📈 热度值: <b>{post['score']}</b>\n"
        if post.get("ai_summary"):
            html_msg += f"    🤖 <b>AI 摘要:</b> <i>{post['ai_summary']}</i>\n"
        html_msg += "\n"
        
    # 构建 Inline Keyboard 交互式按钮
    keyboard_rows = []
    for index, post in enumerate(posts):
        pid = post['id']
        keyboard_rows.append([
            {"text": f"⭐ #{index + 1} 收藏", "callback_data": f"star:{pid}"},
            {"text": f"📖 #{index + 1} 详情", "callback_data": f"content:{pid}"},
            {"text": f"🚫 #{index + 1} 屏蔽", "callback_data": f"block:{pid}"}
        ])
    reply_markup = {"inline_keyboard": keyboard_rows}

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
        if keyboard_rows:
            payload["reply_markup"] = reply_markup
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
        
    push_limit = config.get("push_limit", 10)
    # 获取未推送的高热度帖子
    pending_posts = database.get_pending_push_posts(limit=push_limit)
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
        
    push_limit = config.get("push_limit", 10)
    posts = database.get_recent_hot_posts(hours=24, limit=push_limit)
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
    content_html = ""
    if content_el:
        # 升级相对图片与链接
        for img in content_el.select('img[src^="/"]'):
            img['src'] = nodeseek_url + img['src']
        for a in content_el.select('a[href^="/"]'):
            a['href'] = nodeseek_url + a['href']
        content_html = str(content_el)
    else:
        content_html = "<p>未获取到帖子正文内容。</p>"
    
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

def generate_post_screenshot(post_id, config=None):
    """使用 Playwright 渲染帖子的自定义 HTML 并截取长图"""
    if config is None:
        config = database.get_config()
    
    # 1. 抓取帖子详情
    details = crawl_post_details(post_id, config)
    if "error" in details:
        return {"error": details["error"]}
        
    db_post = database.get_post_by_id(post_id)
    title = db_post["title"] if db_post else "NodeSeek 帖子"
    poster_name = details.get("poster_name", "未知")
    content_html = details.get("content_html", "")
    nodeseek_url = config.get("nodeseek_url", "https://www.nodeseek.com")
    
    # 2. 格式化前 5 条评论
    comments_html = ""
    raw_comments = details.get("comments", [])
    if raw_comments:
        comments_html += '<div class="comment-title">💬 热门评论</div>'
        for i, c in enumerate(raw_comments[:5]):
            c_author = c.get("author", "匿名")
            c_floor = c.get("floor", f"{i+1}#")
            c_text_html = c.get("content_html", "")
            comments_html += f"""
            <div class="comment-item">
                <div class="comment-header">{c_floor} {c_author}</div>
                <div class="comment-text">{c_text_html}</div>
            </div>
            """
            
    # 3. 组装整页 HTML
    full_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="utf-8">
      <style>
        body {{
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
          background-color: #1a1a1a;
          color: #e5e5e5;
          padding: 20px;
          margin: 0;
          width: 650px;
          box-sizing: border-box;
        }}
        .container {{
          background-color: #242424;
          border-radius: 12px;
          padding: 24px;
          box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
          border: 1px solid #333;
        }}
        .header {{
          border-bottom: 1px solid #333;
          padding-bottom: 16px;
          margin-bottom: 20px;
        }}
        .title {{
          font-size: 24px;
          font-weight: bold;
          color: #fff;
          margin: 0 0 10px 0;
          line-height: 1.4;
        }}
        .meta {{
          font-size: 13px;
          color: #999;
        }}
        .content {{
          font-size: 16px;
          line-height: 1.6;
          word-wrap: break-word;
        }}
        .content img {{
          max-width: 100%;
          border-radius: 8px;
          margin: 15px 0;
          display: block;
        }}
        .comment-title {{
          font-size: 16px;
          font-weight: bold;
          color: #fff;
          border-top: 1px solid #333;
          margin-top: 30px;
          padding-top: 20px;
          margin-bottom: 15px;
        }}
        .comment-item {{
          font-size: 14px;
          background-color: #2d2d2d;
          padding: 12px;
          border-radius: 8px;
          margin-bottom: 12px;
          border: 1px solid #3c3c3c;
        }}
        .comment-header {{
          font-weight: bold;
          color: #3b82f6;
          margin-bottom: 6px;
        }}
        .comment-text {{
          color: #d1d5db;
        }}
        .comment-text img {{
          max-width: 100px;
          max-height: 100px;
          border-radius: 4px;
          margin: 5px 0;
          display: block;
        }}
      </style>
    </head>
    <body>
      <div class="container">
        <div class="header">
          <h1 class="title">{title}</h1>
          <div class="meta">👤 作者: {poster_name} | 📌 帖子 ID: {post_id}</div>
        </div>
        <div class="content">
          {content_html}
        </div>
        {comments_html}
      </div>
    </body>
    </html>
    """
    
    # 4. 使用 Playwright 截图
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        return {"error": "系统未安装 Playwright 依赖，无法生成正文长图。"}
        
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 650, "height": 800})
            page = context.new_page()
            page.set_content(full_html)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
                
            container = page.query_selector(".container")
            if container:
                screenshot_bytes = container.screenshot(type="png")
            else:
                screenshot_bytes = page.screenshot(full_page=True, type="png")
                
            browser.close()
            return {"screenshot_bytes": screenshot_bytes, "title": title}
    except Exception as e:
        return {"error": f"Playwright 渲染截图失败: {str(e)}"}
