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
    while True:
        try:
            config = database.get_config()
            token = config.get("tg_bot_token", "").strip()
            if not token:
                # 若未配置 Token，休眠 20 秒后重试
                time.sleep(20)
                continue
                
            url = f"https://api.telegram.org/bot{token}/getUpdates"
            params = {
                "offset": offset,
                "timeout": 20
            }
            
            # 使用 curl_cffi 绕盾并拉取消息更新，超时设为 30s 以适配长轮询
            res = requests.get(url, params=params, impersonate="chrome120", timeout=35)
            if res.status_code == 200:
                data = res.json()
                if data.get("ok"):
                    updates = data.get("result", [])
                    for update in updates:
                        update_id = update["update_id"]
                        offset = update_id + 1
                        
                        # 处理 callback_query 回调
                        if "callback_query" in update:
                            _handle_callback(update["callback_query"], token)
            elif res.status_code == 401:
                logger.error("❌ TG Bot Token 无效，请登录后台检查配置！")
                time.sleep(60)
            else:
                logger.error(f"❌ 监听更新请求失败，HTTP 状态码: {res.status_code}")
                time.sleep(10)
        except Exception as e:
            logger.error(f"❌ 监听 Telegram 更新循环产生异常: {e}")
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
        
        # 过滤已禁用的占位按钮
        if data_str == "disabled:action":
            _answer_callback(token, callback_id, "ℹ️ 该操作已被锁定。")
            return
            
        # 解析数据 payload，形式为 "cmd:post_id"
        parts = data_str.split(":", 1)
        if len(parts) != 2:
            return
            
        cmd, post_id = parts
        
        # 从数据库中查找帖子的分类和作者 UID
        category, author_uid = database.get_post_category_and_uid(post_id)
        if not category:
            _answer_callback(token, callback_id, "⚠️ 错误：未能在本地数据库查获该帖分类详情。")
            return
            
        category_weights = config.get("category_weights", {})
        
        alert_msg = ""
        if cmd == "up":
            # 点赞：权重增加 0.1
            old_w = category_weights.get(category, 1.0)
            new_w = min(2.0, old_w + 0.1)
            category_weights[category] = round(new_w, 2)
            database.update_config({"category_weights": category_weights})
            alert_msg = f"👍 反馈成功！分类【{category}】的权重从 {old_w} 提升至 {category_weights[category]}"
            logger.info(f"🤖 用户点赞了帖子 {post_id} | 分类【{category}】权重增加到 {category_weights[category]}")
            
        elif cmd == "down":
            # 点踩：权重减少 0.1
            old_w = category_weights.get(category, 1.0)
            new_w = max(0.0, old_w - 0.1)
            category_weights[category] = round(new_w, 2)
            database.update_config({"category_weights": category_weights})
            alert_msg = f"👎 反馈成功！分类【{category}】的权重从 {old_w} 降低至 {category_weights[category]}"
            logger.info(f"🤖 用户点踩了帖子 {post_id} | 分类【{category}】权重减少到 {category_weights[category]}")
            
        elif cmd == "block":
            # 屏蔽作者：将其 UID 加入 blocked_uids
            if not author_uid:
                _answer_callback(token, callback_id, "⚠️ 未能识别该发帖人的 UID，无法一键屏蔽。")
                return
                
            blocked_uids = config.get("blocked_uids", [])
            if author_uid not in blocked_uids:
                blocked_uids.append(author_uid)
                database.update_config({"blocked_uids": blocked_uids})
                alert_msg = f"🚫 已屏蔽发帖人 UID: {author_uid}，该作者此后将不再会被抓取。"
                logger.info(f"🤖 一键拉黑成功：UID {author_uid} 已成功加入黑名单配置。")
            else:
                alert_msg = f"ℹ️ 该发帖人 UID: {author_uid} 之前已被屏蔽过。"
                logger.info(f"🤖 提示：UID {author_uid} 之前已在黑名单中。")
                
        # 1. 弹出消息响应弹窗给用户
        _answer_callback(token, callback_id, alert_msg)
        
        # 2. 动态修改原消息的按钮以展示已操作状态，提高用户体验
        _update_message_buttons(token, chat_id, message_id, callback_query.get("message", {}).get("reply_markup"), data_str, cmd)
        
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

def _update_message_buttons(token, chat_id, message_id, reply_markup, clicked_data, cmd):
    """点击某个按钮后，将该行按钮置为标记完成状态"""
    if not reply_markup or "inline_keyboard" not in reply_markup:
        return
        
    inline_keyboard = reply_markup["inline_keyboard"]
    updated = False
    
    # 查找被点击的按钮所在的那一行并修改文字
    for row in inline_keyboard:
        # 判断本行内是否含有被点击 of callback_data
        has_clicked = any(btn.get("callback_data") == clicked_data for btn in row)
        if has_clicked:
            for btn in row:
                btn_cb = btn.get("callback_data", "")
                if btn_cb == clicked_data:
                    # 被点击的按钮高亮标记
                    if cmd == "up":
                        btn["text"] = "已赞 👍"
                    elif cmd == "down":
                        btn["text"] = "已踩 👎"
                    elif cmd == "block":
                        btn["text"] = "已屏蔽 🚫"
                else:
                    # 同一行的其他按钮移出或禁用 (避免重复点击冲突)
                    btn["text"] = "---"
                    btn["callback_data"] = "disabled:action"
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
