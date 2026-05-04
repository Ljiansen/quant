# -*- coding: utf-8 -*-
"""
钉钉通知模块

功能：
- 异步发送钉钉群机器人消息（不阻塞主交易线程）
- 支持买入成交、卖出成交、pending卖出触发三类通知
- 关键词安全模式：所有消息包含关键词"量化"
"""

import json
import threading
import urllib.request
from datetime import datetime


# ── 配置区 ────────────────────────────────────────────────────────────────────
DINGTALK_WEBHOOK = (
    "https://oapi.dingtalk.com/robot/send"
    "?access_token=9c8022d13ffeaed120d80e8ec4c41a8b65f76823cfed41cbc452702d203aa74b"
)
KEYWORD = "量化"          # 钉钉机器人安全关键词
ENABLED = True            # 可临时设为 False 关闭所有推送


# ── 工具函数 ──────────────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _get_stock_name(code: str) -> str:
    """尝试通过 xtquant 获取股票名称，失败则返回代码本身"""
    bare = str(code).split('.')[0]
    try:
        from xtquant import xtdata
        symbol = bare + '.SH' if bare.startswith('6') else bare + '.SZ'
        detail = xtdata.get_instrument_detail(symbol)
        if detail:
            name = detail.get('InstrumentName', '')
            if name:
                return name
    except Exception:
        pass
    return bare


def _do_send(content: str):
    """实际发送 HTTP 请求（在子线程中执行）"""
    try:
        payload = json.dumps(
            {"msgtype": "text", "text": {"content": content}},
            ensure_ascii=False
        ).encode('utf-8')
        req = urllib.request.Request(
            DINGTALK_WEBHOOK,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"}
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            result = json.loads(resp.read().decode('utf-8'))
            if result.get('errcode', 0) != 0:
                print(f"[notifier] 钉钉返回错误: {result}")
    except Exception as e:
        print(f"[notifier] 发送失败: {e}")


def send_notify(content: str):
    """异步推送消息，不阻塞主线程"""
    if not ENABLED:
        return
    # 确保消息包含安全关键词
    if KEYWORD not in content:
        content = f"【{KEYWORD}】\n{content}"
    t = threading.Thread(target=_do_send, args=(content,), daemon=True)
    t.start()


# ── 业务通知函数 ──────────────────────────────────────────────────────────────
def notify_buy(code: str, price: float, volume: int,
               amount: float, change_pct: float):
    """买入成交通知"""
    name = _get_stock_name(code)
    content = (
        f"【量化交易 · 买入成交 🟢】\n"
        f"股票：{name}（{code}）\n"
        f"成交价：{price:.3f} 元\n"
        f"数量：{volume:,} 股\n"
        f"金额：{amount:,.0f} 元\n"
        f"涨幅：{change_pct:+.2f}%\n"
        f"时间：{_now()}"
    )
    send_notify(content)


def notify_sell(code: str, price: float, volume: int,
                sell_type: str, buy_price: float,
                days_held: int, profit_pct: float):
    """卖出成交通知"""
    name = _get_stock_name(code)
    type_map = {
        'hard_stop':     '硬止损',
        'soft_stop':     '阴跌止损',
        'trailing_stop': '移动止盈',
        'time_stop':     '时间止损',
        'pending':       '竞价卖出',
        'auction':       '集合竞价',
        'resubmit':      '重挂卖出',
    }
    type_zh = type_map.get(sell_type, sell_type or '卖出')
    emoji = '🔴' if profit_pct < 0 else '🟡' if profit_pct < 3 else '🟢'
    content = (
        f"【量化交易 · 卖出成交 {emoji}】\n"
        f"股票：{name}（{code}）\n"
        f"类型：{type_zh}\n"
        f"成交价：{price:.3f} 元（买入：{buy_price:.3f}）\n"
        f"数量：{volume:,} 股\n"
        f"持仓天数：{days_held} 天\n"
        f"盈亏：{profit_pct:+.2f}%\n"
        f"时间：{_now()}"
    )
    send_notify(content)


def notify_pending_sell(code: str, sell_type: str,
                        days_held: int, last_price: float):
    """pending 卖出触发通知（次日集合竞价执行）"""
    name = _get_stock_name(code)
    type_map = {
        'hard_stop':     '硬止损',
        'soft_stop':     '阴跌止损',
        'trailing_stop': '移动止盈',
        'time_stop':     '时间止损',
    }
    type_zh = type_map.get(sell_type, sell_type or '止损止盈')
    content = (
        f"【量化交易 · 待卖出信号 ⏳】\n"
        f"股票：{name}（{code}）\n"
        f"触发类型：{type_zh}\n"
        f"现价：{last_price:.3f} 元\n"
        f"持仓天数：{days_held} 天\n"
        f"将于次日集合竞价卖出\n"
        f"时间：{_now()}"
    )
    send_notify(content)


def notify_system(title: str, body: str, level: str = 'info'):
    """系统级运维通知（服务启停、崩溃恢复等）

    level: 'info' | 'warn' | 'error'
    """
    emoji_map = {'info': 'ℹ️', 'warn': '⚠️', 'error': '🚨'}
    emoji = emoji_map.get(level, 'ℹ️')
    content = (
        f"【量化系统 {emoji} {title}】\n"
        f"{body}\n"
        f"时间：{_now()}"
    )
    send_notify(content)
