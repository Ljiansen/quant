# -*- coding: utf-8 -*-
"""
Dashboard 守护进程

功能：
- 每 30 秒检查一次 Dashboard 是否存活（HTTP GET /api/status）
- 连续 2 次失败判定为已挂，发钉钉告警
- 自动重启 run_dashboard.py（新子进程）
- 启动后最多等待 30 秒确认恢复，发钉钉恢复通知
- 如多次重启失败，每 5 分钟再发一次告警，不再重试（防止死循环）

用法：
    python watchdog_dashboard.py

建议随实盘引擎一起在后台启动。
"""

import os
import sys
import time
import subprocess
import urllib.request
import urllib.error
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.notifier import notify_system

# ── 配置 ──────────────────────────────────────────────────────────────────────
DASHBOARD_URL   = 'http://localhost:8088/api/status'   # 健康检查地址
CHECK_INTERVAL  = 30     # 正常检查间隔（秒）
FAIL_THRESHOLD  = 2      # 连续失败次数判定为挂掉
RESTART_TIMEOUT = 40     # 等待重启后恢复的超时（秒）
MAX_RESTARTS    = 5      # 最多自动重启次数（超过后进入告警静默）
ALERT_INTERVAL  = 300    # 超出重启上限后，再次告警间隔（秒）
PROJECT_DIR     = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_SCRIPT = os.path.join(PROJECT_DIR, 'run_dashboard.py')


# ── 工具 ──────────────────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _log(msg: str):
    print(f"[{_now()}] [watchdog] {msg}", flush=True)


def _check_dashboard() -> bool:
    """发 HTTP 请求检查 Dashboard 是否正常响应，返回 True/False"""
    try:
        req = urllib.request.Request(DASHBOARD_URL, method='GET')
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status == 200
    except Exception:
        return False


def _start_dashboard() -> subprocess.Popen:
    """启动 run_dashboard.py，返回 Popen 对象"""
    _log("启动 Dashboard...")
    proc = subprocess.Popen(
        [sys.executable, DASHBOARD_SCRIPT],
        cwd=PROJECT_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP  # Windows 独立进程组
    )
    return proc


def _wait_for_recovery(timeout: int = RESTART_TIMEOUT) -> bool:
    """等待 Dashboard 恢复响应，每 5 秒检查一次，超时返回 False"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(5)
        if _check_dashboard():
            return True
    return False


# ── 主循环 ────────────────────────────────────────────────────────────────────
def run():
    _log(f"Dashboard 守护进程启动，检查间隔={CHECK_INTERVAL}s，"
         f"失败阈值={FAIL_THRESHOLD}，最大重启次数={MAX_RESTARTS}")

    fail_count    = 0    # 连续失败计数
    restart_count = 0    # 累计重启次数
    last_alert_ts = 0    # 超限后的上次告警时间

    # 启动时发一条上线通知
    notify_system('守护进程上线', f'Dashboard 监控已启动\n地址：{DASHBOARD_URL}', level='info')

    while True:
        alive = _check_dashboard()

        if alive:
            if fail_count > 0:
                _log(f"Dashboard 已恢复（此前连续失败 {fail_count} 次）")
            fail_count = 0
            time.sleep(CHECK_INTERVAL)
            continue

        # ── 检测到失败 ──────────────────────────────────────────────────────
        fail_count += 1
        _log(f"Dashboard 无响应，连续失败 {fail_count}/{FAIL_THRESHOLD}")

        if fail_count < FAIL_THRESHOLD:
            # 还没到阈值，等 10 秒再检查一次
            time.sleep(10)
            continue

        # ── 确认挂掉，判断是否可以重启 ──────────────────────────────────────
        _log(f"Dashboard 已确认挂掉（连续 {fail_count} 次失败）")

        if restart_count >= MAX_RESTARTS:
            # 超出重启上限，进入告警静默模式
            now_ts = time.time()
            if now_ts - last_alert_ts >= ALERT_INTERVAL:
                notify_system(
                    'Dashboard 持续离线',
                    f'已重启 {restart_count} 次均失败，请手动检查！\n'
                    f'当前状态：连续无响应',
                    level='error'
                )
                last_alert_ts = now_ts
                _log(f"已超出最大重启次数({MAX_RESTARTS})，发告警后等待下一轮")
            time.sleep(ALERT_INTERVAL)
            continue

        # ── 发告警 + 执行重启 ───────────────────────────────────────────────
        notify_system(
            'Dashboard 已挂 ⚠️',
            f'连续 {fail_count} 次无响应，正在自动重启...\n'
            f'已重启次数：{restart_count}/{MAX_RESTARTS}',
            level='warn'
        )

        _start_dashboard()
        restart_count += 1
        fail_count = 0
        _log(f"已发起重启（第 {restart_count} 次），等待恢复...")

        # ── 等待恢复 ────────────────────────────────────────────────────────
        if _wait_for_recovery():
            _log("Dashboard 重启成功，已恢复正常")
            notify_system(
                'Dashboard 已恢复 ✅',
                f'自动重启成功（第 {restart_count} 次）\n'
                f'地址：http://localhost:8088',
                level='info'
            )
        else:
            _log(f"Dashboard 重启后仍未恢复（{RESTART_TIMEOUT}s 超时）")
            notify_system(
                'Dashboard 重启失败',
                f'第 {restart_count} 次重启后 {RESTART_TIMEOUT}s 内未响应\n'
                f'将继续监控并重试...',
                level='error'
            )

        time.sleep(CHECK_INTERVAL)


if __name__ == '__main__':
    try:
        run()
    except KeyboardInterrupt:
        _log("守护进程已手动停止")
        notify_system('守护进程停止', 'watchdog_dashboard 已手动停止', level='warn')
