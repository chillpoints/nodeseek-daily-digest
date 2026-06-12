#!/bin/bash

# ==================================================
# NodeSeek Daily Digest 交互式部署脚本
# 支持: 安装、卸载、重启、状态、完全清除、重装
# ==================================================

# 颜色控制
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;36m'
PLAIN='\033[0m'

# 当前脚本绝对路径作为项目根目录
PROJECT_DIR=$(cd "$(dirname "$0")"; pwd)
SERVICE_NAME="nodeseek-digest"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

# 必须以 root 权限运行
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}错误：必须使用 root 权限运行此脚本！${PLAIN}"
    exit 1
fi

# 获取随机空闲端口 (20000 - 30000)
get_random_port() {
    while true; do
        port=$((RANDOM % 10001 + 20000))
        # 检测端口占用
        if ! ss -ant | grep -q ":$port "; then
            echo "$port"
            return
        fi
    done
}

# 菜单主界面
show_menu() {
    echo -e "${BLUE}==================================================${PLAIN}"
    echo -e "      ${GREEN}NodeSeek Daily Digest 一键安装管理脚本${PLAIN}"
    echo -e "      项目目录: ${YELLOW}${PROJECT_DIR}${PLAIN}"
    echo -e "${BLUE}==================================================${PLAIN}"
    echo -e " ${GREEN}1.${PLAIN} 安装/升级服务 (Install)"
    echo -e " ${GREEN}2.${PLAIN} 卸载服务 (Uninstall) - 保留配置与数据库"
    echo -e " ${GREEN}3.${PLAIN} 彻底删除项目 (Purge) - 删除所有代码及数据库"
    echo -e " ${GREEN}4.${PLAIN} 重启服务 (Restart)"
    echo -e " ${GREEN}5.${PLAIN} 查看服务状态与实时日志 (Status/Logs)"
    echo -e " ${GREEN}6.${PLAIN} 退出脚本"
    echo -e "${BLUE}==================================================${PLAIN}"
    read -p "请输入对应的数字 [1-6]: " choice
    case $choice in
        1) install_service ;;
        2) uninstall_service ;;
        3) purge_all ;;
        4) restart_service ;;
        5) check_status ;;
        6) exit 0 ;;
        *) echo -e "${RED}错误：请输入 1 到 6 之间的有效数字！${PLAIN}"; sleep 1; show_menu ;;
    esac
}

# 1. 安装服务
install_service() {
    echo -e "${BLUE}[+] 开始安装/升级依赖环境...${PLAIN}"
    
    # 安装 Python 虚拟环境相关组件
    apt update
    apt install -y python3 python3-pip python3-venv iproute2
    
    # 建立 python 虚拟环境防系统环境冲突
    if [ ! -d "${PROJECT_DIR}/.venv" ]; then
        echo -e "${BLUE}[+] 正在创建 Python 虚拟环境...${PLAIN}"
        python3 -m venv "${PROJECT_DIR}/.venv"
    fi
    
    # 安装依赖
    echo -e "${BLUE}[+] 正在安装 python 依赖包...${PLAIN}"
    "${PROJECT_DIR}/.venv/bin/pip" install --upgrade pip
    "${PROJECT_DIR}/.venv/bin/pip" install -r "${PROJECT_DIR}/requirements.txt"
    
    # 获取运行端口
    current_port=""
    if [ -f "${SERVICE_FILE}" ]; then
        # 如果已经安装过，读取旧端口
        current_port=$(grep -oP '(?<=--port )\d+' "${SERVICE_FILE}")
    fi
    
    if [ -n "${current_port}" ]; then
        echo -e "${YELLOW}[!] 检测到已注册服务端口: ${current_port}，将继续沿用。${PLAIN}"
        port="${current_port}"
    else
        read -p "请输入服务运行端口 (自选 1024-65535，直接回车将随机在 20000-30000 间选择): " input_port
        if [ -n "${input_port}" ]; then
            port="${input_port}"
        else
            port=$(get_random_port)
            echo -e "${GREEN}[+] 随机分配空闲端口: ${port}${PLAIN}"
        fi
    fi
    
    # 写入 systemd 服务文件
    echo -e "${BLUE}[+] 正在注册 systemd 服务进程守护...${PLAIN}"
    cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=NodeSeek Daily Digest Web Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${PROJECT_DIR}
ExecStart=${PROJECT_DIR}/.venv/bin/uvicorn app:app --host 0.0.0.0 --port ${port}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    # 重新加载配置并启动
    systemctl daemon-reload
    systemctl enable "${SERVICE_NAME}"
    systemctl restart "${SERVICE_NAME}"
    
    # 获取本机公网 IP 
    ip_addr=$(curl -s https://api.ipify.org || curl -s ipinfo.io/ip || echo "127.0.0.1")
    
    echo -e "${GREEN}==================================================${PLAIN}"
    echo -e "🎉 NodeSeek Daily Digest 控制面板安装/重载成功！"
    echo -e "   端口号: ${YELLOW}${port}${PLAIN}"
    echo -e "   控制面板网址: ${BLUE}http://${ip_addr}:${port}${PLAIN}"
    echo -e "   状态查看命令: ${YELLOW}systemctl status ${SERVICE_NAME}${PLAIN}"
    echo -e "${GREEN}==================================================${PLAIN}"
    
    read -p "按回车键返回菜单..." temp
    show_menu
}

# 2. 卸载服务
uninstall_service() {
    echo -e "${YELLOW}[!] 开始卸载进程守护服务...${PLAIN}"
    if [ -f "${SERVICE_FILE}" ]; then
        systemctl stop "${SERVICE_NAME}"
        systemctl disable "${SERVICE_NAME}"
        rm -f "${SERVICE_FILE}"
        systemctl daemon-reload
        echo -e "${GREEN}[+] 服务已成功卸载，配置与数据库已保留。${PLAIN}"
    else
        echo -e "${RED}[!] 未检测到已安装的服务！${PLAIN}"
    fi
    read -p "按回车键返回菜单..." temp
    show_menu
}

# 3. 彻底删除
purge_all() {
    read -p "⚠️ 警告：这将完全删除服务、数据库及所有项目文件！确定要继续吗？(y/n): " confirm
    if [ "${confirm}" = "y" ] || [ "${confirm}" = "Y" ]; then
        echo -e "${RED}[!] 开始完全清除所有项目资产...${PLAIN}"
        if [ -f "${SERVICE_FILE}" ]; then
            systemctl stop "${SERVICE_NAME}"
            systemctl disable "${SERVICE_NAME}"
            rm -f "${SERVICE_FILE}"
            systemctl daemon-reload
        fi
        rm -rf "${PROJECT_DIR}"
        echo -e "${GREEN}[+] 项目所有数据已完全删除！再见！${PLAIN}"
        exit 0
    else
        echo -e "${GREEN}[+] 操作已取消。${PLAIN}"
        sleep 1
        show_menu
    fi
}

# 4. 重启服务
restart_service() {
    if systemctl is-active --quiet "${SERVICE_NAME}"; then
        echo -e "${BLUE}[+] 正在重启服务...${PLAIN}"
        systemctl restart "${SERVICE_NAME}"
        echo -e "${GREEN}[+] 服务重启成功！${PLAIN}"
    else
        echo -e "${RED}[!] 服务未处于运行中，无法重启！请先进行安装/启动。${PLAIN}"
    fi
    sleep 1
    show_menu
}

# 5. 查看服务状态与日志
check_status() {
    if [ -f "${SERVICE_FILE}" ]; then
        echo -e "${BLUE}==================== 服务状态 ====================${PLAIN}"
        systemctl status "${SERVICE_NAME}"
        echo -e "${BLUE}==================== 实时日志 ====================${PLAIN}"
        echo -e "${YELLOW}(按 Ctrl+C 退出日志查看模式...)${PLAIN}"
        journalctl -u "${SERVICE_NAME}" -f -n 30
    else
        echo -e "${RED}[!] 服务未安装！${PLAIN}"
        read -p "按回车键返回菜单..." temp
        show_menu
    fi
}

# 启动菜单
show_menu
