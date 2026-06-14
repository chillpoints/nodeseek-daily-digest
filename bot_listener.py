import time
import logging
import threading
import json
import re
from curl_cffi import requests
import database

logger = logging.getLogger("bot_listener")

def start_listener():
    """在后台线程中启动 Telegram Bot 消息交互监听守护任务"""
    thread = threading.Thread(target=_polling_loop, daemon=True)
    thread.start()
    logger.info("🤖 后台 Telegram 消息交互监听器线程已启动成功。")

def _polling_loop():
    offset = 0
    logger.info("📡 [TG监听] 启动长轮询消息更新 loop...")
    while True:
        try:
            config = database.get_config()
            token = config.get("tg_bot_token", "").strip()
            if not token:
                logger.warning("⚠️ [TG监听] tg_bot_token 未配置，请登录控制面板进行配置。")
                time.sleep(20)
                continue
                
            url = f"https://api.telegram.org/bot{token}/getUpdates"
            params = {
                "offset": offset,
                "timeout": 20
            }
            
            logger.debug(f"📡 [TG监听] 发送 getUpdates 请求，offset={offset}...")
            res = requests.get(url, params=params, impersonate="chrome120", timeout=35)
            
            if res.status_code == 200:
                data = res.json()
                if data.get("ok"):
                    updates = data.get("result", [])
                    if updates:
                        logger.info(f"📡 [TG监听] 获取到 {len(updates)} 个原始更新项")
                    for update in updates:
                        update_id = update["update_id"]
                        offset = update_id + 1
                        
                        logger.info(f"📡 [TG监听] 正在解析更新项: {json.dumps(update, ensure_ascii=False)}")
                        
                        # 兼容处理 message 和 edited_message 
                        msg_type = "message" if "message" in update else ("edited_message" if "edited_message" in update else None)
                        
                        if msg_type:
                            _handle_message(update[msg_type], token)
                        elif "callback_query" in update:
                            _handle_callback(update["callback_query"], token)
                else:
                    logger.error(f"❌ [TG监听] getUpdates 响应状态不为 ok: {res.text}")
                    time.sleep(5)
            elif res.status_code == 401:
                logger.error("❌ [TG监听] TG Bot Token 无效，请重新确认配置！")
                time.sleep(60)
            else:
                logger.error(f"❌ [TG监听] 监听更新请求失败，HTTP 状态码: {res.status_code}")
                time.sleep(10)
        except Exception as e:
            logger.error(f"❌ [TG监听] 监听更新循环异常: {e}")
            time.sleep(10)

def _handle_callback(callback_query, token):
    try:
        # 每次回调读取最新配置
        config = database.get_config()
        callback_id = callback_query["id"]
        data_str = callback_query.get("data", "")
        message = callback_query.get("message")
        
        if not data_str or not message:
            return
            
        chat_id = message["chat"]["id"]
        message_id = message["message_id"]
        
        # 1. 验证权限：仅限绑定的 tg_chat_id 交互，防越权调配
        tg_chat_id = str(config.get("tg_chat_id", "")).strip()
        current_chat_str = str(chat_id).strip()
        
        # 支持逗号分隔的多个 ID 校验
        allowed_ids = [x.strip() for x in re.split(r'[,\uff0c]', tg_chat_id) if x.strip()]
        if not tg_chat_id:
            logger.warning("⚠️ [TG鉴权] 数据库中 tg_chat_id 未配置。当前临时放行配置点按。")
        elif current_chat_str not in allowed_ids:
            logger.warning(f"⚠️ [TG回调拦截] 拦截非绑定 Chat ID ({current_chat_str}) 的按钮回调。当前绑定: ({tg_chat_id})")
            _answer_callback(token, callback_id, "⚠️ 权限不足：您无权对此 Bot 进行调配。")
            return
            
        # 过滤已禁用的占位按钮
        if data_str == "disabled:action":
            _answer_callback(token, callback_id, "ℹ️ 该操作已被锁定。")
            return
            
        # 2. 路由分发配置相关回调（以 cfg: 为前缀）
        if data_str.startswith("cfg:"):
            _handle_config_callback(token, chat_id, message_id, callback_id, data_str)
            return
            
        # 解析数据 payload，形式为 "cmd:post_id"
        parts = data_str.split(":", 1)
        if len(parts) != 2:
            return
            
        cmd, post_id = parts
        
        # 3. 处理交互按钮回调
        if cmd == "star":
            db_post = database.get_post_by_id(post_id)
            title = db_post["title"] if db_post else "NodeSeek 帖子"
            url = db_post["url"] if db_post else f"https://www.nodeseek.com/post-{post_id}-1"
            
            if database.is_starred(post_id):
                database.remove_star(post_id)
                alert_msg = "⭐ 已从收藏列表中移除！"
            else:
                database.add_star(post_id, title, url)
                alert_msg = "⭐ 收藏成功！发送 /stars 即可调取收藏列表。"
                
            _answer_callback(token, callback_id, alert_msg)
            # 反转按钮高亮状态
            _update_message_star_button(token, chat_id, message_id, callback_query.get("message", {}).get("reply_markup"), data_str)
            return
            
        elif cmd == "content":
            _answer_callback(token, callback_id, "📖 正在拉取正文预览...")
            # 异步执行详情拉取发送，防接口超时
            thread = threading.Thread(target=_run_send_post_content, args=(token, chat_id, post_id), daemon=True)
            thread.start()
            return
            
        elif cmd == "block":
            category, author_uid = database.get_post_category_and_uid(post_id)
            if not author_uid:
                _answer_callback(token, callback_id, "⚠️ 数据库中没有此发帖人的 UID，无法屏蔽。")
                return
                
            author_uid = str(author_uid)
            blocked_uids = [str(x) for x in config.get("blocked_uids", [])]
            is_blocking = False
            
            if author_uid not in blocked_uids:
                blocked_uids.append(author_uid)
                database.update_config({"blocked_uids": blocked_uids})
                alert_msg = f"🚫 已成功屏蔽该作者 (UID: {author_uid})！"
                is_blocking = True
            else:
                blocked_uids.remove(author_uid)
                database.update_config({"blocked_uids": blocked_uids})
                alert_msg = f"✅ 已成功取消屏蔽该作者 (UID: {author_uid})！"
                is_blocking = False
                
            _answer_callback(token, callback_id, alert_msg)
            _update_message_block_button(token, chat_id, message_id, callback_query.get("message", {}).get("reply_markup"), data_str, is_blocking)
            return
            
    except Exception as e:
        logger.error(f"❌ 处理回调异常: {e}")

def _answer_callback(token, callback_id, text):
    """弹出通知给用户客户端以实现即时响应反馈"""
    url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
    payload = {
        "callback_query_id": callback_id,
        "text": text,
        "show_alert": False
    }
    try:
        requests.post(url, json=payload, impersonate="chrome120", timeout=10)
    except Exception as e:
        logger.error(f"❌ 响应回调答复失败: {e}")

def _update_message_star_button(token, chat_id, message_id, reply_markup, clicked_data):
    """点击收藏后，反转该收藏按钮的显示状态（⭐ 收藏 -> ★ 已收藏）"""
    if not reply_markup or "inline_keyboard" not in reply_markup:
        return
        
    inline_keyboard = reply_markup["inline_keyboard"]
    updated = False
    
    for row in inline_keyboard:
        for btn in row:
            if btn.get("callback_data") == clicked_data:
                text = btn.get("text", "")
                if "已收藏" in text or "★" in text:
                    # 原来是已收藏，现在变成未收藏
                    num_match = re.search(r'#(\d+)', text)
                    num = num_match.group(1) if num_match else ""
                    btn["text"] = f"⭐ #{num} 收藏"
                else:
                    # 原来是未收藏，现在变成已收藏
                    num_match = re.search(r'#(\d+)', text)
                    num = num_match.group(1) if num_match else ""
                    btn["text"] = f"★ #{num} 已收藏"
                updated = True
                break
        if updated:
            break
            
    if updated:
        url = f"https://api.telegram.org/bot{token}/editMessageReplyMarkup"
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": {"inline_keyboard": inline_keyboard}
        }
        try:
            requests.post(url, json=payload, impersonate="chrome120", timeout=10)
        except Exception as e:
            logger.error(f"❌ 更新原消息键盘失败: {e}")

def _update_message_block_button(token, chat_id, message_id, reply_markup, clicked_data, is_blocking):
    """点击屏蔽后，将被点击按钮设为对应状态（已屏蔽 🚫 或 🚫 #x 屏蔽），保持可用"""
    if not reply_markup or "inline_keyboard" not in reply_markup:
        return
        
    inline_keyboard = reply_markup["inline_keyboard"]
    updated = False
    
    for row in inline_keyboard:
        target_btn = None
        number = None
        for btn in row:
            cb_data = btn.get("callback_data", "")
            if cb_data.startswith("star:") or cb_data.startswith("content:"):
                match = re.search(r'#(\d+)', btn.get("text", ""))
                if match:
                    number = match.group(1)
            if cb_data == clicked_data:
                target_btn = btn
                
        if target_btn:
            if is_blocking:
                target_btn["text"] = "已屏蔽 🚫"
            else:
                num_str = number if number else "1"
                target_btn["text"] = f"🚫 #{num_str} 屏蔽"
            updated = True
            break
            
    if updated:
        url = f"https://api.telegram.org/bot{token}/editMessageReplyMarkup"
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": {"inline_keyboard": inline_keyboard}
        }
        try:
            requests.post(url, json=payload, impersonate="chrome120", timeout=10)
        except Exception as e:
            logger.error(f"❌ 更新原消息键盘失败: {e}")

# ==========================================
# 🆕 以下为新增的 Telegram Bot 控制面板逻辑
# ==========================================

def _handle_message(message, token):
    """处理普通的文本命令"""
    try:
        chat_id = message["chat"]["id"]
        text = message.get("text", "").strip()
        if not text:
            return
            
        logger.info(f"📩 [TG消息] ChatID: {chat_id} 发送了文本: {text}")
        
        # 1. 鉴权：读取配置并对比 tg_chat_id
        config = database.get_config()
        tg_chat_id = str(config.get("tg_chat_id", "")).strip()
        current_chat_str = str(chat_id).strip()
        
        logger.info(f"🔑 [TG鉴权] 绑定 ID: '{tg_chat_id}' | 当前 ID: '{current_chat_str}'")
        
        # 支持逗号分隔的多个 ID 校验
        allowed_ids = [x.strip() for x in re.split(r'[,\uff0c]', tg_chat_id) if x.strip()]
        if not tg_chat_id:
            logger.warning("⚠️ [TG鉴权] 数据库中 tg_chat_id 为空。当前临时放行所有控制大盘及指令。")
        elif current_chat_str not in allowed_ids:
            logger.warning(f"⚠️ [TG拦截] 拦截到非绑定 Chat ID ({current_chat_str}) 的指令请求。当前绑定: ({tg_chat_id})")
            return
            
        # 2. 匹配指令
        if text.startswith("/config"):
            logger.info("⚙️ 匹配到 /config 指令，开始发送大盘主菜单")
            _send_main_menu(token, chat_id)
        elif text.startswith("/stars"):
            logger.info("⭐ 匹配到 /stars 指令，开始获取收藏列表")
            _send_stars_list(token, chat_id, page=1)
        elif text.startswith("/daily"):
            logger.info("📅 匹配到 /daily 指令，开始生成24小时热帖的 Markdown 打包文件")
            thread = threading.Thread(target=_run_send_daily_digest, args=(token, chat_id), daemon=True)
            thread.start()
        elif text.startswith("/content"):
            logger.info(f"📖 匹配到 /content 详情请求: {text}")
            content_match = re.search(r'/content(?:_|-|#)?(\d+)', text)
            if content_match:
                post_id = content_match.group(1)
                thread = threading.Thread(target=_run_send_post_content, args=(token, chat_id, post_id), daemon=True)
                thread.start()
            else:
                _send_message(token, chat_id, "⚠️ 格式不正确，请输入 <code>/content#12345</code> (12345 为帖子 ID)")
            
    except Exception as e:
        logger.error(f"❌ 处理文本消息异常: {e}")

def _send_main_menu(token, chat_id):
    """发送控制大盘主菜单"""
    text, reply_markup = _get_main_menu_data()
    _send_message(token, chat_id, text, reply_markup)

def _send_message(token, chat_id, text, reply_markup=None):
    """发送 TG 消息辅助函数"""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        res = requests.post(url, json=payload, impersonate="chrome120", timeout=10)
        if res.status_code != 200:
            logger.error(f"❌ 发送 TG 消息失败，状态码: {res.status_code}, 响应: {res.text}")
    except Exception as e:
        logger.error(f"❌ 发送 TG 消息异常: {e}")

def _edit_message(token, chat_id, message_id, text, reply_markup=None):
    """编辑 TG 消息及键盘辅助函数"""
    url = f"https://api.telegram.org/bot{token}/editMessageText"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    else:
        # 传入空键盘清除旧的 Inline Keyboard 按钮，防止 Telegram API 报错
        payload["reply_markup"] = {"inline_keyboard": []}
    try:
        res = requests.post(url, json=payload, impersonate="chrome120", timeout=10)
        if res.status_code != 200:
            logger.error(f"❌ 编辑 TG 消息失败，状态码: {res.status_code}, 响应: {res.text}")
    except Exception as e:
        logger.error(f"❌ 编辑 TG 消息异常: {e}")

def _handle_config_callback(token, chat_id, message_id, callback_id, data_str):
    """集中处理以 `cfg:` 前缀开头的配置面板回调命令"""
    try:
        logger.info(f"🤖 收到 TG 配置回调: {data_str}")
        
        # 1. 切换为主菜单
        if data_str == "cfg:menu_main":
            _answer_callback(token, callback_id, "")
            text, reply_markup = _get_main_menu_data()
            _edit_message(token, chat_id, message_id, text, reply_markup)
            
        # 2. 进入推送模式子菜单
        elif data_str == "cfg:menu_mode":
            _answer_callback(token, callback_id, "")
            text, reply_markup = _get_mode_menu_data()
            _edit_message(token, chat_id, message_id, text, reply_markup)
            
        # 3. 进入调度时间/周期子菜单
        elif data_str == "cfg:menu_time":
            _answer_callback(token, callback_id, "")
            text, reply_markup = _get_time_menu_data()
            _edit_message(token, chat_id, message_id, text, reply_markup)
            
        # 4. 进入读取页数子菜单
        elif data_str == "cfg:menu_pages":
            _answer_callback(token, callback_id, "")
            text, reply_markup = _get_pages_menu_data()
            _edit_message(token, chat_id, message_id, text, reply_markup)
            
        # 5. 进入推送数量子菜单
        elif data_str == "cfg:menu_limit":
            _answer_callback(token, callback_id, "")
            text, reply_markup = _get_limit_menu_data()
            _edit_message(token, chat_id, message_id, text, reply_markup)
            
        # 6. 进入订阅分类子菜单
        elif data_str == "cfg:menu_cats":
            _answer_callback(token, callback_id, "")
            text, reply_markup = _get_cats_menu_data()
            _edit_message(token, chat_id, message_id, text, reply_markup)
            
        # 7. 关闭面板
        elif data_str == "cfg:menu_close":
            _answer_callback(token, callback_id, "❌ 控制面板已关闭")
            text = "🔒 <b>NodeSeek 控制大盘已成功关闭</b>\n如需重新调配系统反馈机制，请重新发送 <code>/config</code> 指令。"
            _edit_message(token, chat_id, message_id, text, None)
            
        # 8. 更改调度模式
        elif data_str.startswith("cfg:set_mode:"):
            target_mode = data_str.replace("cfg:set_mode:", "")
            database.update_config({"schedule_mode": target_mode})
            
            # 热重载定时器
            import scheduler
            scheduler.reload_scheduler()
            
            mode_names = {"cron": "定时 (Cron)", "interval": "间隔 (Interval)", "disabled": "禁用 (Disabled)"}
            name = mode_names.get(target_mode, target_mode)
            _answer_callback(token, callback_id, f"✅ 推送模式已切换为：{name}")
            
            text, reply_markup = _get_mode_menu_data()
            _edit_message(token, chat_id, message_id, text, reply_markup)
            
        # 9. 参数微调
        # 格式：cfg:adj:<param>:<val>  例如 cfg:adj:ch:-1
        elif data_str.startswith("cfg:adj:"):
            parts = data_str.split(":")
            if len(parts) == 4:
                _, _, param, val_str = parts
                val = int(val_str)
                config = database.get_config()
                need_reload = False
                alert_text = ""
                
                if param == "ch":
                    # cron_hour: (0-23 循环)
                    current = config.get("cron_hour", 21)
                    new_val = (current + val) % 24
                    database.update_config({"cron_hour": new_val})
                    need_reload = True
                    alert_text = f"⏰ 推送时刻已微调为每日 {new_val:02d} 时"
                elif param == "cm":
                    # cron_minute: (0-59 循环)
                    current = config.get("cron_minute", 30)
                    new_val = (current + val) % 60
                    database.update_config({"cron_minute": new_val})
                    need_reload = True
                    alert_text = f"⏰ 推送分已微调为 {new_val:02d} 分"
                elif param == "ih":
                    # interval_hours: 最小为 1
                    current = config.get("interval_hours", 4)
                    new_val = max(1, current + val)
                    database.update_config({"interval_hours": new_val})
                    need_reload = True
                    alert_text = f"⏱️ 推送周期已微调为每隔 {new_val} 小时"
                elif param == "pg":
                    # max_pages: 限制 1 ~ 20 页
                    current = config.get("max_pages", 5)
                    new_val = max(1, min(20, current + val))
                    database.update_config({"max_pages": new_val})
                    alert_text = f"📄 每次爬取页数已调整为 {new_val} 页"
                elif param == "pl":
                    # push_limit: 限制 1 ~ 100 帖
                    current = config.get("push_limit", 10)
                    new_val = max(1, min(100, current + val))
                    database.update_config({"push_limit": new_val})
                    alert_text = f"🔢 每次推送上限已调整为 {new_val} 帖"
                
                if need_reload:
                    import scheduler
                    scheduler.reload_scheduler()
                    
                _answer_callback(token, callback_id, alert_text)
                
                # 刷新对应页面
                if param in ["ch", "cm", "ih"]:
                    text, reply_markup = _get_time_menu_data()
                elif param == "pg":
                    text, reply_markup = _get_pages_menu_data()
                elif param == "pl":
                    text, reply_markup = _get_limit_menu_data()
                    
                _edit_message(token, chat_id, message_id, text, reply_markup)
                
        # 10. 板块订阅状态切换
        elif data_str.startswith("cfg:cat_tgl:"):
            cat = data_str.replace("cfg:cat_tgl:", "")
            config = database.get_config()
            category_weights = config.get("category_weights", {})
            
            # 如果是 0.0 则开启订阅 (置为默认权重或 1.0)；如果大于 0.0 则关闭订阅 (置为 0.0)
            current_weight = category_weights.get(cat, 1.0)
            if current_weight > 0.0:
                new_weight = 0.0
                action_desc = "🔴 已关闭"
            else:
                new_weight = 1.0
                action_desc = "🟢 已开启"
                
            category_weights[cat] = new_weight
            database.update_config({"category_weights": category_weights})
            
            _answer_callback(token, callback_id, f"🏷️ {action_desc}【{cat}】板块订阅")
            
            text, reply_markup = _get_cats_menu_data()
            _edit_message(token, chat_id, message_id, text, reply_markup)
            
        # 11. 立即触发抓取和推送任务
        elif data_str == "cfg:action_run":
            # 异步拉起新线程跑任务，防止阻塞长轮询接口
            thread = threading.Thread(target=_run_digest_job_and_push, daemon=True)
            thread.start()
            
            _answer_callback(token, callback_id, "⚡ 立即抓取已激活，正在后台执行中...")
            
            text = (
                "⚡ <b>立即触发抓取与推送</b>\n\n"
                "已成功在后台启动抓取推送任务！\n"
                "系统将执行以下步骤：\n"
                "1. 抓取最新 NodeSeek 帖子数据；\n"
                "2. 根据板块权重和时间衰减模型计算热度得分；\n"
                "3. 将最新精选的热帖目录推送至本 Chat。\n\n"
                "请注意：为避免接口请求过快被风控，请勿频繁重复触发。"
            )
            keyboard = [
                [{"text": "🔙 返回主菜单", "callback_data": "cfg:menu_main"}]
            ]
            _edit_message(token, chat_id, message_id, text, {"inline_keyboard": keyboard})
            
        # 12. 进入系统升级菜单
        elif data_str == "cfg:menu_upgrade":
            _answer_callback(token, callback_id, "")
            text, reply_markup = _get_upgrade_menu_data()
            _edit_message(token, chat_id, message_id, text, reply_markup)
            
        # 13. 开始执行系统升级
        elif data_str == "cfg:action_update":
            _answer_callback(token, callback_id, "🚀 启动升级流，正在连接 GitHub...")
            text = (
                "🔄 <b>系统自动升级中...</b>\n\n"
                "⏳ 正在执行 <code>git pull</code> 拉取远程最新代码..."
            )
            _edit_message(token, chat_id, message_id, text, None)
            
            # 异步启动升级，防长轮询请求挂起
            thread = threading.Thread(target=_run_system_upgrade, args=(token, chat_id, message_id), daemon=True)
            thread.start()
            
        # 14. 处理收藏夹翻页
        elif data_str.startswith("cfg:stars:"):
            page_str = data_str.replace("cfg:stars:", "")
            try:
                page = int(page_str)
            except ValueError:
                page = 1
            _answer_callback(token, callback_id, "")
            text, reply_markup = _get_stars_page_data(page)
            _edit_message(token, chat_id, message_id, text, reply_markup)
            
    except Exception as e:
        logger.error(f"❌ 处理配置菜单回调发生异常: {e}")

def _get_main_menu_data():
    """生成主菜单的格式化文案与 Inline 键盘"""
    config = database.get_config()
    
    # 调度模式汉化
    mode = config.get("schedule_mode", "disabled")
    mode_text = {
        "cron": "定时执行 (Cron)",
        "interval": "周期执行 (Interval)",
        "disabled": "手动调配 (Disabled)"
    }.get(mode, mode)
    
    # 定时/周期细节
    if mode == "cron":
        hour = config.get("cron_hour", 21)
        minute = config.get("cron_minute", 30)
        schedule_detail = f"每日 {hour:02d}:{minute:02d} 定点推送"
    elif mode == "interval":
        hours = config.get("interval_hours", 4)
        schedule_detail = f"每隔 {hours} 小时循环推送"
    else:
        schedule_detail = "自动推送已关闭，仅支持主动激活"
        
    max_pages = config.get("max_pages", 5)
    push_limit = config.get("push_limit", 10)
    
    # AI 拦截及总结状态
    ai_enabled = config.get("ai_enabled", False)
    ai_filter = config.get("ai_filter_enabled", False)
    ai_summary = config.get("ai_summary_enabled", False)
    
    if ai_enabled:
        parts = []
        if ai_filter:
            parts.append("AI 过滤")
        if ai_summary:
            parts.append("AI 总结")
        ai_status = "开启 (" + "+".join(parts) + ")" if parts else "开启 (未启用具体模块)"
    else:
        ai_status = "关闭"
        
    text = (
        "⚙️ <b>系统配置参数</b>\n\n"
        f"• 模式：<code>{mode_text}</code>\n"
        f"• 周期：<code>{schedule_detail}</code>\n"
        f"• 页数：<code>{max_pages} 页</code>\n"
        f"• 限制：<code>{push_limit} 帖/次</code>\n"
        f"• AI：<code>{ai_status}</code>"
    )
    
    keyboard = [
        [
            {"text": "⏱️ 推送模式", "callback_data": "cfg:menu_mode"},
            {"text": "🕒 调度时间", "callback_data": "cfg:menu_time"}
        ],
        [
            {"text": "📄 抓取页数", "callback_data": "cfg:menu_pages"},
            {"text": "🔢 推送数量", "callback_data": "cfg:menu_limit"}
        ],
        [
            {"text": "🏷️ 订阅板块", "callback_data": "cfg:menu_cats"}
        ],
        [
            {"text": "⚡ 立即抓取推送", "callback_data": "cfg:action_run"},
            {"text": "🔄 系统自动升级", "callback_data": "cfg:menu_upgrade"}
        ],
        [
            {"text": "❌ 关闭面板", "callback_data": "cfg:menu_close"}
        ]
    ]
    
    return text, {"inline_keyboard": keyboard}

def _get_mode_menu_data():
    """生成调度模式配置页"""
    config = database.get_config()
    current_mode = config.get("schedule_mode", "disabled")
    
    cron_tag = " ✅" if current_mode == "cron" else ""
    interval_tag = " ✅" if current_mode == "interval" else ""
    disabled_tag = " ✅" if current_mode == "disabled" else ""
    
    text = (
        "⚙️ <b>推送模式设置</b>\n\n"
        "• Cron：每日定点推送\n"
        "• Interval：固定间隔推送\n"
        "• Disabled：关闭自动推送"
    )
    
    keyboard = [
        [{"text": f"Cron 定时模式{cron_tag}", "callback_data": "cfg:set_mode:cron"}],
        [{"text": f"Interval 间隔模式{interval_tag}", "callback_data": "cfg:set_mode:interval"}],
        [{"text": f"禁用自动推送{disabled_tag}", "callback_data": "cfg:set_mode:disabled"}],
        [{"text": "🔙 返回主菜单", "callback_data": "cfg:menu_main"}]
    ]
    
    return text, {"inline_keyboard": keyboard}

def _get_time_menu_data():
    """生成时间/周期微调配置页"""
    config = database.get_config()
    mode = config.get("schedule_mode", "disabled")
    
    if mode == "cron":
        hour = config.get("cron_hour", 21)
        minute = config.get("cron_minute", 30)
        text = f"⚙️ <b>定时时间微调</b>\n\n当前推送时间：<b>{hour:02d}:{minute:02d}</b>"
        keyboard = [
            [
                {"text": "小时 -1", "callback_data": "cfg:adj:ch:-1"},
                {"text": "小时 +1", "callback_data": "cfg:adj:ch:1"}
            ],
            [
                {"text": "分钟 -5", "callback_data": "cfg:adj:cm:-5"},
                {"text": "分钟 +5", "callback_data": "cfg:adj:cm:5"}
            ],
            [{"text": "🔙 返回主菜单", "callback_data": "cfg:menu_main"}]
        ]
    elif mode == "interval":
        hours = config.get("interval_hours", 4)
        text = f"⚙️ <b>推送间隔微调</b>\n\n当前推送间隔：<b>{hours} 小时</b>"
        keyboard = [
            [
                {"text": "间隔 -1 小时", "callback_data": "cfg:adj:ih:-1"},
                {"text": "间隔 +1 小时", "callback_data": "cfg:adj:ih:1"}
            ],
            [{"text": "🔙 返回主菜单", "callback_data": "cfg:menu_main"}]
        ]
    else:
        text = "⚙️ <b>时间微调</b>\n\n⚠️ 自动推送已禁用。请先开启 Cron 或 Interval 模式。"
        keyboard = [
            [{"text": "🔙 返回主菜单", "callback_data": "cfg:menu_main"}]
        ]
        
    return text, {"inline_keyboard": keyboard}

def _get_pages_menu_data():
    """生成爬取页数配置页"""
    config = database.get_config()
    max_pages = config.get("max_pages", 5)
    
    text = f"⚙️ <b>抓取页数微调</b>\n\n当前每次抓取：<b>{max_pages} 页</b>"
    
    keyboard = [
        [
            {"text": "页数 -1", "callback_data": "cfg:adj:pg:-1"},
            {"text": "页数 +1", "callback_data": "cfg:adj:pg:1"}
        ],
        [{"text": "🔙 返回主菜单", "callback_data": "cfg:menu_main"}]
    ]
    return text, {"inline_keyboard": keyboard}

def _get_limit_menu_data():
    """生成推送上限配置页"""
    config = database.get_config()
    push_limit = config.get("push_limit", 10)
    
    text = f"⚙️ <b>单次推送限制数微调</b>\n\n当前每次最多推送：<b>{push_limit} 帖</b>"
    
    keyboard = [
        [
            {"text": "限制数 -5", "callback_data": "cfg:adj:pl:-5"},
            {"text": "限制数 +5", "callback_data": "cfg:adj:pl:5"}
        ],
        [{"text": "🔙 返回主菜单", "callback_data": "cfg:menu_main"}]
    ]
    return text, {"inline_keyboard": keyboard}

def _get_cats_menu_data():
    """生成订阅板块的开关矩阵"""
    config = database.get_config()
    category_weights = config.get("category_weights", {})
    
    # 包含的 13 个主流版块
    cats = ["日常", "技术", "情报", "测评", "交易", "拼车", "推广", "生活", "Dev", "贴图", "曝光", "内版", "沙盒"]
    
    text = (
        "⚙️ <b>订阅板块开关</b>\n\n"
        "🟢 开启 | 🔴 关闭 | 🟡 限制"
    )
    
    keyboard = []
    row = []
    for i, cat in enumerate(cats):
        weight = category_weights.get(cat, 1.0)
        if weight == 0.0:
            status_emoji = "🔴"
        elif weight < 1.0:
            status_emoji = "🟡"
        else:
            status_emoji = "🟢"
            
        btn_text = f"{status_emoji} {cat}"
        row.append({"text": btn_text, "callback_data": f"cfg:cat_tgl:{cat}"})
        if len(row) == 3:
            keyboard.append(row)
            row = []
            
    if row:
        keyboard.append(row)
        
    keyboard.append([{"text": "🔙 返回主菜单", "callback_data": "cfg:menu_main"}])
    
    return text, {"inline_keyboard": keyboard}

def _run_digest_job_and_push():
    """后台执行拉取推送的守护逻辑"""
    logger.info("📅 TG 按钮触发手动执行抓取任务...")
    try:
        import nodeseek_digest
        nodeseek_digest.run_digest_job()
        nodeseek_digest.push_pending_digests()
        logger.info("📅 手动执行抓取与推送完毕。")
    except Exception as e:
        logger.error(f"❌ 手动任务执行失败: {e}")

def _get_upgrade_menu_data():
    """生成系统升级子菜单"""
    text = (
        "🔄 <b>系统自动升级与维护 (System Auto-Update)</b>\n\n"
        "系统将通过 Git 自动拉取远程代码并更新运行环境：\n"
        "1. <b>拉取最新提交</b> (<code>git pull</code>)；\n"
        "2. <b>增量安装 Python 依赖库</b> (<code>pip install</code>)；\n"
        "3. <b>退出当前进程</b> 触发 Systemd 定时拉起并重新上线。\n\n"
        "⚠️ <b>注意与警告：</b>\n"
        "• 请确保没有在运行环境中对核心代码进行冲突修改，否则可能导致拉取失败。\n"
        "• 重新上线依赖于 <b>Systemd</b> 的 <code>Restart=always</code> 机制。若当前服务是以普通命令行前台执行，进程退出后需要手动拉起。"
    )
    keyboard = [
        [{"text": "🚀 开始拉取并升级", "callback_data": "cfg:action_update"}],
        [{"text": "🔙 返回主菜单", "callback_data": "cfg:menu_main"}]
    ]
    return text, {"inline_keyboard": keyboard}

def _run_system_upgrade(token, chat_id, message_id):
    """在后台线程执行自动拉取、依赖安装和自重启逻辑"""
    import subprocess
    import os
    import sys
    
    project_dir = os.path.dirname(os.path.abspath(__file__))
    
    try:
        # 0. 规避 Git 安全目录检测限制
        subprocess.run(["git", "config", "--global", "--add", "safe.directory", project_dir], capture_output=True, cwd=project_dir, timeout=5)
        
        # 1. git pull 拉取代码
        logger.info("🤖 自动更新：正在拉取 GitHub 代码...")
        git_res = subprocess.run(["git", "pull"], capture_output=True, text=True, cwd=project_dir, timeout=30)
        
        if git_res.returncode != 0:
            error_msg = git_res.stderr or git_res.stdout
            logger.error(f"❌ git pull 失败: {error_msg}")
            
            text = (
                "❌ <b>自动升级失败 (Git Pull 错误)</b>\n\n"
                f"在执行 <code>git pull</code> 时发生冲突或网络异常：\n"
                f"<pre>{error_msg.strip()}</pre>\n\n"
                "⚠️ 系统未执行重启，已保持当前状态运行。"
            )
            keyboard = [[{"text": "🔙 返回主菜单", "callback_data": "cfg:menu_main"}]]
            _edit_message(token, chat_id, message_id, text, {"inline_keyboard": keyboard})
            return
            
        git_output = git_res.stdout
        logger.info(f"🤖 git pull 完成: {git_output}")
        
        # 2. 更新 pip 依赖
        pip_paths = [
            os.path.join(project_dir, ".venv", "bin", "pip"),
            os.path.join(project_dir, "venv", "bin", "pip"),
            "pip"
        ]
        pip_exe = "pip"
        for p in pip_paths:
            if os.path.exists(p):
                pip_exe = p
                break
                
        logger.info(f"🤖 正在执行 pip 依赖增量安装 (使用 {pip_exe})...")
        pip_res = subprocess.run([pip_exe, "install", "-r", "requirements.txt"], capture_output=True, text=True, cwd=project_dir, timeout=60)
        
        if pip_res.returncode != 0:
            pip_error = pip_res.stderr or pip_res.stdout
            logger.warning(f"⚠️ pip 依赖更新警告 (非致命): {pip_error}")
            
        # 2.5 自动安装 Playwright 浏览器依赖 (如果安装了 playwright 模块)
        try:
            playwright_bin = os.path.join(os.path.dirname(pip_exe), "playwright")
            if not os.path.exists(playwright_bin):
                playwright_bin = "playwright"
            logger.info("🤖 正在尝试安装 Playwright Chromium 浏览器依赖...")
            # 运行 playwright install chromium 安装无头浏览器二进制
            pw_res = subprocess.run([playwright_bin, "install", "chromium"], capture_output=True, text=True, cwd=project_dir, timeout=120)
            if pw_res.returncode == 0:
                logger.info("🎉 Playwright Chromium 安装成功！")
            else:
                logger.warning(f"⚠️ Playwright Chromium 安装可能失败: {pw_res.stderr or pw_res.stdout}")
        except Exception as ex:
            logger.warning(f"⚠️ 自动安装 Playwright 浏览器时发生异常: {ex}")
            
        # 3. 发送重启提醒并退出进程
        text = (
            "✅ <b>系统自动更新成功！</b>\n\n"
            f"ℹ️ Git 拉取信息：\n<pre>{git_output.strip()}</pre>\n"
            "🔄 <b>进程重启就绪：</b>系统即将退出当前进程。如果您使用了 <code>deploy.sh</code> 注册的服务，Systemd 将在 5 秒内自动唤醒并拉起最新的程序版本重新连接。"
        )
        _edit_message(token, chat_id, message_id, text, None)
        
        # 留足时间让网络包完整发出
        time.sleep(2)
        
        logger.info("🔄 触发程序自重启以热重载最新代码。当前进程即将退出...")
        os._exit(0)
        
    except Exception as e:
        logger.error(f"❌ 系统自动更新中抛出未捕获异常: {e}")
        text = (
            "❌ <b>系统升级遭遇严重崩溃</b>\n\n"
            f"升级线程遭遇以下内部异常：\n<code>{str(e)}</code>\n\n"
            "⚠️ 系统未执行重启，请前往终端检查运行日志。"
        )
        keyboard = [[{"text": "🔙 返回主菜单", "callback_data": "cfg:menu_main"}]]
        _edit_message(token, chat_id, message_id, text, {"inline_keyboard": keyboard})

# ==========================================
# 🆕 以下为新增的收藏夹与详情预览功能函数
# ==========================================

def clean_html_to_text(html_content):
    """剔除 HTML 标签，返回干净的纯文本"""
    if not html_content:
        return ""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_content, 'html.parser')
    return soup.get_text(separator="\n", strip=True)

def html_to_markdown(html_content):
    """将 HTML 格式的内容转换为 Markdown 格式文本"""
    if not html_content:
        return ""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_content, 'html.parser')
    
    def _convert(node):
        if node.name is None:
            # 文本节点
            return node.string if node.string else ""
            
        tag = node.name.lower()
        
        # 1. 预格式化代码块 (pre)
        if tag == 'pre':
            code_el = node.find('code')
            code_text = code_el.get_text() if code_el else node.get_text()
            return f"\n\n```\n{code_text}\n```\n\n"
            
        # 2. 行内代码 (code)
        if tag == 'code':
            return f" `{node.get_text()}` "
            
        # 3. 先递归处理子节点
        parts = []
        for child in node.children:
            # 过滤列表/表格中的空白文本节点，避免空行堆叠
            if tag in ['ul', 'ol', 'table', 'tr', 'thead', 'tbody'] and child.name is None:
                if not child.string or not child.string.strip():
                    continue
            parts.append(_convert(child))
        inner = "".join(parts)
        
        # 4. 根据标签类型生成 markdown
        if tag == 'p':
            return f"\n\n{inner.strip()}\n\n"
        elif tag == 'br':
            return "\n"
        elif tag in ['strong', 'b']:
            return f"**{inner}**"
        elif tag in ['em', 'i']:
            return f"*{inner}*"
        elif tag in ['del', 's']:
            return f"~~{inner}~~"
        elif tag == 'a':
            href = node.get('href', '')
            txt = inner.strip()
            if not txt:
                return href
            return f"[{txt}]({href})"
        elif tag == 'img':
            src = node.get('src', '')
            alt = node.get('alt', '图片')
            return f"\n![{alt}]({src})\n"
        elif tag in ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']:
            level = int(tag[1])
            return f"\n\n{'#' * level} {inner.strip()}\n\n"
        elif tag == 'blockquote':
            lines = inner.strip().split('\n')
            quoted = [f"> {line}" for line in lines]
            return f"\n\n" + "\n".join(quoted) + "\n\n"
        elif tag == 'li':
            parent = node.parent
            if parent and parent.name.lower() == 'ol':
                siblings = [c for c in parent.children if c.name and c.name.lower() == 'li']
                try:
                    idx = siblings.index(node) + 1
                except ValueError:
                    idx = 1
                return f"\n{idx}. {inner.strip()}"
            else:
                return f"\n* {inner.strip()}"
        elif tag in ['ul', 'ol']:
            return f"\n{inner}\n"
        elif tag == 'tr':
            return f"\n| {inner} |"
        elif tag in ['td', 'th']:
            return f" {inner} |"
        else:
            return inner

    result = _convert(soup)
    # 替换连续的多个换行为双换行
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result.strip()

def clean_anchor(title):
    """格式化标题以生成 Markdown 锚点链接"""
    s = title.lower()
    # 保留字母、数字、中文和连字符，其余替换为空
    s = re.sub(r'[^\w\s\u4e00-\u9fff-]', '', s)
    # 替换空格和下划线为连字符
    s = re.sub(r'[\s_]+', '-', s)
    return s.strip('-')

def _run_send_daily_digest(token, chat_id):
    """在后台线程收集过去 24 小时内的所有热帖内容，转为 Markdown 后打包发送给用户"""
    logger.info("📅 开始生成24小时热帖打包 Markdown 文件...")
    _send_message(token, chat_id, "🔍 正在为您收集并打包过去 24 小时内的所有热帖详情（包含文字、图片与前 10 条评论），这需要一些时间，请稍候...")
    
    try:
        from datetime import datetime
        # 获取过去 24 小时热度前 100 的帖子
        posts = database.get_recent_hot_posts(hours=24, limit=100)
        if not posts:
            _send_message(token, chat_id, "ℹ️ 过去 24 小时内未检测到任何热帖。")
            return
            
        config = database.get_config()
        nodeseek_url = config.get("nodeseek_url", "https://www.nodeseek.com")
        
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        md_content = f"# 🔥 NodeSeek 过去 24 小时热帖打包订阅\n"
        md_content += f"- **生成时间**: {now_str}\n"
        md_content += f"- **帖子总数**: {len(posts)} 个\n\n"
        md_content += "---\n\n"
        
        # 生成目录
        md_content += "## 目录\n"
        for idx, post in enumerate(posts):
            anchor = f"{post['id']}-{clean_anchor(post['title'])}"
            md_content += f"{idx + 1}. [{post['title']}](#{anchor}) (🔥 热度: {post['score']})\n"
        md_content += "\n---\n\n"
        
        import nodeseek_digest
        
        for idx, post in enumerate(posts):
            post_id = post["id"]
            title = post["title"]
            url = post["url"]
            views = post["views"]
            comments_count = post["comments"]
            score = post["score"]
            ai_summary = post.get("ai_summary", "")
            
            logger.info(f"正在拉取第 {idx+1}/{len(posts)} 个帖子详情: ID={post_id}, 标题={title}")
            
            details = nodeseek_digest.crawl_post_details(post_id, config)
            if "error" in details:
                anchor = f"{post_id}-{clean_anchor(title)}"
                md_content += f"## <a name=\"{anchor}\"></a> {idx + 1}. {title}\n\n"
                md_content += f"- **链接**: [{url}]({url})\n"
                md_content += f"- **数据**: 👀 {views} 阅读 | 💬 {comments_count} 评论 | 📈 热度 {score}\n\n"
                md_content += f"⚠️ **内容获取失败**: {details['error']}\n\n"
                md_content += "---\n\n"
                continue
                
            poster_name = details.get("poster_name", "未知")
            content_html = details.get("content_html", "")
            comments = details.get("comments", [])
            
            # 转换正文为 Markdown
            body_md = html_to_markdown(content_html)
            
            # 提取正文插图 URL
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(content_html, 'html.parser')
            images = []
            for img in soup.find_all('img'):
                src = img.get('src')
                if src:
                    if "emoji" in src or "avatar" in src or "/static/image/common/" in src:
                        continue
                    if src.startswith("/"):
                        src = nodeseek_url + src
                    images.append(src)
            
            anchor = f"{post_id}-{clean_anchor(title)}"
            md_content += f"## <a name=\"{anchor}\"></a> {idx + 1}. {title}\n\n"
            md_content += f"- **作者**: {poster_name}\n"
            md_content += f"- **链接**: [{url}]({url})\n"
            md_content += f"- **数据**: 👀 {views} 阅读 | 💬 {comments_count} 评论 | 📈 热度 {score}\n"
            if ai_summary:
                md_content += f"- **AI 摘要**: {ai_summary}\n"
            md_content += "\n"
            
            md_content += "### 📝 正文内容\n\n"
            if body_md:
                md_content += body_md + "\n\n"
            else:
                md_content += "*（正文无文字）*\n\n"
                
            if images:
                md_content += "### 🖼️ 图片列表\n\n"
                for i, img in enumerate(images):
                    md_content += f"- ![图片 {i+1}]({img})\n"
                md_content += "\n"
                
            md_content += "### 💬 热门评论 (前 10 条)\n\n"
            top_comments = comments[:10]
            if top_comments:
                for c in top_comments:
                    c_floor = c.get("floor", "")
                    c_author = c.get("author", "匿名")
                    c_content_html = c.get("content_html", "")
                    c_md = html_to_markdown(c_content_html)
                    # 引用格式化评论
                    quoted_c = "\n".join([f"> {line}" for line in c_md.strip().split("\n")])
                    md_content += f"**{c_floor} {c_author}**:\n{quoted_c}\n\n"
            else:
                md_content += "*（暂无评论）*\n\n"
                
            md_content += "---\n\n"
            
            # 延迟 1 秒防风控
            time.sleep(1)
            
        # 发送 Markdown 打包文件
        file_date = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"NodeSeek_Daily_Digest_{file_date}.md"
        markdown_bytes = md_content.encode("utf-8")
        
        url = f"https://api.telegram.org/bot{token}/sendDocument"
        from curl_cffi import CurlMime
        mp = CurlMime()
        mp.addpart(name="chat_id", data=str(chat_id).encode("utf-8"))
        caption = f"📅 <b>NodeSeek 24 小时热帖打包成功！</b>\n\n📊 共收录 <b>{len(posts)}</b> 个热帖详情。"
        mp.addpart(name="caption", data=caption.encode("utf-8"))
        mp.addpart(name="parse_mode", data=b"HTML")
        mp.addpart(name="document", content_type="text/markdown", filename=filename, data=markdown_bytes)
        
        res = requests.post(url, multipart=mp, impersonate="chrome120", timeout=30)
        if res.status_code == 200:
            logger.info("🎉 24小时热帖打包文件已成功发送。")
        else:
            logger.error(f"❌ 发送打包文件失败，TG 响应: {res.text}")
            _send_message(token, chat_id, f"❌ 发送打包文件失败，TG 响应: <code>{res.text}</code>")
            
    except Exception as e:
        logger.error(f"❌ 生成或发送24小时热帖打包 Markdown 发生异常: {e}")
        _send_message(token, chat_id, f"❌ 生成打包文件失败，异常: <code>{str(e)}</code>")


def _send_stars_list(token, chat_id, page=1):
    """向用户发送其收藏的帖子列表"""
    text, reply_markup = _get_stars_page_data(page)
    _send_message(token, chat_id, text, reply_markup)

def _get_stars_page_data(page=1):
    """生成收藏列表的分页文案与换页键盘"""
    starred = database.get_starred_posts()
    if not starred:
        return "⭐ <b>我的收藏列表</b>\n\n目前您还没有收藏任何帖子。在推送的消息下方点击 <b>⭐ 收藏</b> 即可将帖子保存到这里。", None
        
    total_posts = len(starred)
    per_page = 10
    total_pages = (total_posts + per_page - 1) // per_page
    
    # 页码范围校验
    page = max(1, min(total_pages, page))
    
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    page_posts = starred[start_idx:end_idx]
    
    text = f"⭐ <b>我的收藏列表 (第 {page}/{total_pages} 页，共 {total_posts} 条)</b>\n\n"
    for i, post in enumerate(page_posts):
        idx = start_idx + i + 1
        date_str = post.get("star_time", "")
        if len(date_str) > 10:
            date_str = date_str[:10]
        text += f"{idx}. <b><a href='{post['url']}'>{post['title']}</a></b>\n"
        text += f"    🔗 <code>/content_{post['post_id']}</code> (收藏于: {date_str})\n\n"
        
    text += "<i>💡 提示：点击帖子的 <code>/content_ID</code> 链接，可以直接在此处抓取并预览该帖的正文与评论内容！</i>"
    
    # 翻页键盘
    keyboard = []
    nav_row = []
    if page > 1:
        nav_row.append({"text": "⬅️ 上一页", "callback_data": f"cfg:stars:{page - 1}"})
    if page < total_pages:
        nav_row.append({"text": "下一页 ➡️", "callback_data": f"cfg:stars:{page + 1}"})
        
    if nav_row:
        keyboard.append(nav_row)
        
    # 添加返回主菜单按钮
    keyboard.append([{"text": "🔙 返回主菜单", "callback_data": "cfg:menu_main"}])
    
    return text, {"inline_keyboard": keyboard}

def _run_send_post_content(token, chat_id, post_id):
    """在后台线程抓取帖子详细正文和评论并推送到 Telegram 客户端 (支持文字与图片双模式)"""
    logger.info(f"🤖 正在获取帖子 {post_id} 详情并发送...")
    try:
        # 读取系统配置
        config = database.get_config()
        db_post = database.get_post_by_id(post_id)
        title = db_post["title"] if db_post else "未知标题"
        
        forward_mode = config.get("forward_mode", "text")
        
        # 1. 长图模式：使用 Playwright 渲染并发送整张图片
        if forward_mode == "image":
            import nodeseek_digest
            res_dict = nodeseek_digest.generate_post_screenshot(post_id, config)
            if "error" in res_dict:
                _send_message(token, chat_id, f"❌ 生成长图预览失败：\n<code>{res_dict['error']}</code>")
                return
            
            photo_bytes = res_dict["screenshot_bytes"]
            url = f"https://api.telegram.org/bot{token}/sendPhoto"
            
            from curl_cffi import CurlMime
            mp = CurlMime()
            mp.addpart(name="chat_id", data=str(chat_id).encode("utf-8"))
            caption = f"📖 <b>NodeSeek 帖子正文预览</b>\n\n📌 <b>标题：</b><a href='https://www.nodeseek.com/post-{post_id}-1'>{title}</a>"
            mp.addpart(name="caption", data=caption.encode("utf-8"))
            mp.addpart(name="parse_mode", data=b"HTML")
            mp.addpart(name="photo", content_type="image/png", filename="screenshot.png", data=photo_bytes)
            
            res = requests.post(url, multipart=mp, impersonate="chrome120", timeout=30)
            if res.status_code != 200:
                logger.error(f"❌ 发送正文长图失败，状态码: {res.status_code}, 响应: {res.text}")
                _send_message(token, chat_id, f"❌ 发送截图失败，TG 响应: <code>{res.text}</code>")
            return

        # 2. 文本模式：发送纯文本并附带原始插图媒体组
        import nodeseek_digest
        details = nodeseek_digest.crawl_post_details(post_id, config)
        
        if "error" in details:
            _send_message(token, chat_id, f"❌ 抓取帖子内容失败：\n<code>{details['error']}</code>")
            return
            
        poster_name = details.get("poster_name", "未知")
        
        # 3. 提取纯文本正文并截断
        content_html = details.get("content_html", "")
        body_text = clean_html_to_text(content_html)
        if len(body_text) > 1200:
            body_text = body_text[:1200] + "\n\n...(正文较长，已自动截断)..."
            
        # 4. 提取插图 URL
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(content_html, 'html.parser')
        images = []
        for img in soup.find_all('img'):
            src = img.get('src')
            if src:
                images.append(src)
                
        # 5. 提取前 5 条评论并格式化
        comments_list = []
        raw_comments = details.get("comments", [])
        for i, c in enumerate(raw_comments[:5]):
            c_author = c.get("author", "匿名")
            c_floor = c.get("floor", f"{i+1}#")
            c_text = clean_html_to_text(c.get("content_html", ""))
            if len(c_text) > 200:
                c_text = c_text[:200] + "..."
            comments_list.append(f"<b>{c_floor} {c_author}</b>: <i>{c_text}</i>")
            
        comments_text = "\n".join(comments_list) if comments_list else "（暂无评论）"
        
        # 6. 整理插图展示与首图背景预览超链接
        if images:
            images_list_text = "\n".join([f"🖼️ <a href='{img}'>插图 {i+1}</a>" for i, img in enumerate(images[:10])])
            # 零宽字符超链接，隐藏嵌入首图以启用 Telegram 自动图片预览
            preview_prefix = f'<a href="{images[0]}">&#8203;</a>'
        else:
            images_list_text = "<i>（本帖无插图）</i>"
            preview_prefix = ""
            
        # 7. 构建消息文案
        text = (
            f"{preview_prefix}📖 <b>NodeSeek 帖子正文预览</b>\n\n"
            f"📌 <b>标题：</b><a href='https://www.nodeseek.com/post-{post_id}-1'>{title}</a>\n"
            f"👤 <b>作者：</b><code>{poster_name}</code> (ID: {post_id})\n\n"
            f"📝 <b>正文内容：</b>\n"
            f"----------------------------------------\n"
            f"{body_text}\n"
            f"----------------------------------------\n\n"
            f"🖼️ <b>正文插图链接：</b>\n"
            f"{images_list_text}\n\n"
            f"💬 <b>热门评论 (展示前 5 条)：</b>\n\n"
            f"{comments_text}"
        )
        
        # 先发送文字内容
        _send_message(token, chat_id, text)
        
        # 8. 如果有图片，并发/顺序下载并以 Media Group 或单张 Photo 发送
        if images:
            media_group = []
            files = {}
            downloaded_count = 0
            nodeseek_url = config.get("nodeseek_url", "https://www.nodeseek.com")
            
            for img_url in images[:10]: # Telegram 限制每次最多 10 张
                try:
                    if img_url.startswith("/"):
                        img_url = nodeseek_url + img_url
                    
                    # 过滤表情包和头像等小图
                    if "emoji" in img_url or "avatar" in img_url or "/static/image/common/" in img_url:
                        continue
                        
                    logger.info(f"正在下载帖子图片以供转发: {img_url}")
                    img_res = requests.get(img_url, impersonate="chrome120", timeout=10)
                    if img_res.status_code == 200 and len(img_res.content) > 1024: # 大于 1KB
                        file_key = f"file{downloaded_count}"
                        files[file_key] = (f"image_{downloaded_count}.jpg", img_res.content, "image/jpeg")
                        media_group.append({
                            "type": "photo",
                            "media": f"attach://{file_key}"
                        })
                        downloaded_count += 1
                except Exception as ex:
                    logger.warning(f"下载图片失败: {img_url} | {ex}")
                    
            if downloaded_count == 1:
                # 发送单张照片
                photo_url = f"https://api.telegram.org/bot{token}/sendPhoto"
                try:
                    from curl_cffi import CurlMime
                    mp = CurlMime()
                    mp.addpart(name="chat_id", data=str(chat_id).encode("utf-8"))
                    filename, content, content_type = files["file0"]
                    mp.addpart(name="photo", content_type=content_type, filename=filename, data=content)
                    requests.post(photo_url, multipart=mp, impersonate="chrome120", timeout=15)
                except Exception as ex:
                    logger.error(f"发送单张插图失败: {ex}")
            elif downloaded_count > 1:
                # 发送 Media Group
                album_url = f"https://api.telegram.org/bot{token}/sendMediaGroup"
                try:
                    from curl_cffi import CurlMime
                    mp = CurlMime()
                    mp.addpart(name="chat_id", data=str(chat_id).encode("utf-8"))
                    mp.addpart(name="media", data=json.dumps(media_group).encode("utf-8"))
                    for file_key, (filename, content, content_type) in files.items():
                        mp.addpart(name=file_key, content_type=content_type, filename=filename, data=content)
                    requests.post(album_url, multipart=mp, impersonate="chrome120", timeout=30)
                except Exception as ex:
                    logger.error(f"发送插图相册失败: {ex}")
                    
    except Exception as e:
        logger.error(f"❌ 发送帖子 {post_id} 内容异常: {e}")
        _send_message(token, chat_id, f"❌ 展示帖子详情时遭遇崩溃：\n<code>{str(e)}</code>")
