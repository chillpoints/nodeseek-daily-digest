# NodeSeek 每日热贴推送助手 (NodeSeek Daily Digest)

本工具是一个部署在 Linux 服务器（如 Debian 12）上的轻量级 Python 自动化工具。它能够每日定时抓取 NodeSeek 论坛的高人气热帖，计算热度，自动过滤掉抽奖无意义帖，并每天自动将 Top 10 热帖的精美简报推送到您的 Telegram 频道或对话中。

---

## ✨ 项目功能特性

*   🛡️ **强力防 CF 拦截**：基于 `curl_cffi` 库，深度模拟 Chrome 等真实浏览器的 TLS / JA3 握手指纹和 HTTP/2 特征，完美避开 Cloudflare 5秒盾与 WAF 拦截。
*   🚫 **无用信息过滤**：根据关键词（如“抽奖”、“送码”、“送鸡腿”等）自动排除各种无价值的刷水抽奖贴。
*   📈 **科学热度排序**：根据评论数与浏览量进行综合加权计算（热度公式：`评论数 * 5 + 浏览量 * 0.2`），仅推荐 24 小时内最活跃的优质帖子。
*   🔗 **精美格式推送**：将每日精选帖子以 HTML 格式生成目录与指标（阅读数、评论数、热度值），并直接推送到 Telegram。
*   ⚙️ **配置灵活**：支持读取本地 `config.json`，同时也支持通过环境变量（`TG_BOT_TOKEN`, `TG_CHAT_ID`）在 Docker/云环境进行无感知部署。

---

## 🛠️ 部署使用步骤

### 1. 克隆项目与安装依赖
在您的 Debian / Ubuntu 服务器上克隆本仓库，进入目录并安装依赖包：

```bash
git clone https://github.com/chillpoints/nodeseek-daily-digest.git
cd nodeseek-daily-digest

# 安装依赖
sudo apt update
sudo apt install -y python3 python3-pip
pip3 install -r requirements.txt
```

### 2. 参数配置
将目录下的 `config.json.example` 复制为 `config.json`：

```bash
cp config.json.example config.json
```

编辑 `config.json` 填入您的 Telegram 机器人凭证：

```json
{
  "tg_bot_token": "您的_TELEGRAM_BOT_TOKEN",
  "tg_chat_id": "您的_TELEGRAM_CHAT_ID",
  "nodeseek_url": "https://www.nodeseek.com",
  "max_pages": 5,
  "lucky_keywords": ["抽奖", "送码", "送鸡腿", "卡密", "免费送", "送个", "福利", "送台"]
}
```

> [!TIP]
> * **Telegram Token** 可通过 [@BotFather](https://t.me/BotFather) 发送 `/newbot` 获取。
> * **Chat ID** 可通过 [@userinfobot](https://t.me/userinfobot) 获取。创建完机器人后，请确保在 Telegram 中主动与机器人发起过一次聊天（点击 Start），否则机器人将无权向您推送消息。

### 3. 测试运行
```bash
python3 nodeseek_digest.py
```
若配置正确，您的 Telegram 将立即收到一封今日热帖排版总结。

---

## 📅 设置每日定时自动执行

我们使用 Linux 的 `crontab` 实现每日自动推送。

1. 打开 Cron 任务编辑器：
   ```bash
   crontab -e
   ```
2. 在文件末尾添加以下配置（设置每天晚上 **21:30** 自动抓取并推送，注意将路径替换为您的真实物理路径）：
   ```text
   30 21 * * * /usr/bin/python3 /path/to/nodeseek-daily-digest/nodeseek_digest.py > /dev/null 2>&1
   ```
3. 保存并退出即可。

---

## 📄 开源许可证

本项目基于 MIT 许可证开源。
