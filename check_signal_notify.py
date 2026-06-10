# -*- coding: utf-8 -*-
"""
T/C1 信号盘后检测 + 钉钉通知

日线数据更新后自动调用，检测今日收盘是否触发 T 或 C1 信号。
如有信号，通过钉钉推送通知。

集成位置：run_live_v4.py postmarket_flow() 中 BA pool 预算之后调用
也可独立运行：python check_signal_notify.py
"""

import os
import sys
import json
import time
import numpy as np
import pandas as pd
from datetime import datetime, date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── 配置 ──────────────────────────────────────────────
DAILY_DATA_DIR = 'D:/daily_data'
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# T 策略参数
T_SH_THRESHOLD = -0.05       # 上证7日跌幅阈值
T_SH_LOOKBACK = 7            # 上证回看天数
T_SIGNAL_COOLDOWN = 10       # 信号去重天数
T_STATE_FILE = os.path.join(BASE_DIR, 'state_t_v4.json')

# C1 策略参数
C1_SH_7D_LOW = -0.05         # 上证7日跌幅下限(含)
C1_SH_7D_HIGH = -0.03        # 上证7日跌幅上限(含)

# BA G3.7 regime 参数（对齐 live_engine_v4.py G34_PARAMS）
BA_G34 = dict(
    bull_mp=5, chop_init_mp=4, chop_else_mp=3,
    init_bnd_bull=3, init_bnd_chop=5,
    panic_thr=-0.06, vol_thr=0.022,
    chop_else_ret5_min=-0.01, macro_bear_thr=-0.05,
)


def _now_str():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _load_sh_index():
    """加载上证指数日线（多路径兜底）"""
    candidates = [
        os.path.join(DAILY_DATA_DIR, 'SH', 'price_sh000001.csv'),
        os.path.join(DAILY_DATA_DIR, 'SH', 'price_000001.csv'),
        os.path.join(DAILY_DATA_DIR, 'INDEX', 'sh000001_daily.csv'),
    ]
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            df = pd.read_csv(path)
            if 'timetag' in df.columns:
                df = df.rename(columns={'timetag': 'date'})
                df['date'] = pd.to_datetime(df['date'].astype(str).str[:8], format='%Y%m%d')
            else:
                df['date'] = pd.to_datetime(df['date'])
            df = df.sort_values('date').reset_index(drop=True)
            for col in ['close']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
            return df
        except Exception:
            continue
    return None


def _load_t_state():
    """加载策略T状态（获取 cooldown 信息）"""
    if not os.path.exists(T_STATE_FILE):
        return {}
    try:
        with open(T_STATE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def check_signals(today_str: str = None):
    """
    检测今日 T/C1 信号，返回 dict：
    {
        'today': 'YYYY-MM-DD',
        'sh_close': float,
        'sh_ret_1d': float,
        'sh_ret_7d': float,
        't_signal': bool,
        't_reason': str,
        'c1_signal': bool,
        'c1_reason': str,
    }
    """
    sh_df = _load_sh_index()
    if sh_df is None:
        print(f"[{_now_str()}] [signal] ⚠️ 无法加载上证指数")
        return {'error': 'no_sh_index'}

    # 构建交易日历
    all_dates = sorted(sh_df['date'].dt.strftime('%Y-%m-%d').tolist())

    if today_str is None:
        today_str = date.today().strftime('%Y-%m-%d')

    # 找到今天（或最近交易日）在日历中的位置
    if today_str in all_dates:
        idx = all_dates.index(today_str)
    else:
        # 找最近的 <= today 的交易日
        prev = [d for d in all_dates if d <= today_str]
        if not prev:
            print(f"[{_now_str()}] [signal] ⚠️ {today_str} 之前无交易日")
            return {'error': 'no_trading_date'}
        today_str = prev[-1]
        idx = all_dates.index(today_str)

    if idx < T_SH_LOOKBACK:
        print(f"[{_now_str()}] [signal] ⚠️ 数据不足 {T_SH_LOOKBACK} 天")
        return {'error': 'insufficient_data'}

    today_dt = pd.to_datetime(today_str)
    today_row = sh_df[sh_df['date'] == today_dt]
    if today_row.empty:
        return {'error': 'no_today_data'}

    sh_close = float(today_row['close'].iloc[0])

    # 1日收益
    prev_dt = pd.to_datetime(all_dates[idx - 1])
    prev_row = sh_df[sh_df['date'] == prev_dt]
    sh_prev_close = float(prev_row['close'].iloc[0])
    sh_ret_1d = (sh_close - sh_prev_close) / sh_prev_close if sh_prev_close > 0 else 0

    # 7日收益
    prev7_dt = pd.to_datetime(all_dates[idx - T_SH_LOOKBACK])
    prev7_row = sh_df[sh_df['date'] == prev7_dt]
    sh_prev7_close = float(prev7_row['close'].iloc[0])
    sh_ret_7d = (sh_close - sh_prev7_close) / sh_prev7_close if sh_prev7_close > 0 else 0

    result = {
        'today': today_str,
        'sh_close': round(sh_close, 2),
        'sh_ret_1d': round(sh_ret_1d, 6),
        'sh_ret_7d': round(sh_ret_7d, 6),
    }

    # ── T 信号检测 ──
    t_signal = False
    t_reason = ''
    if sh_ret_7d < T_SH_THRESHOLD:
        # 检查冷却期
        t_state = _load_t_state()
        last_sig_idx = t_state.get('last_signal_day_idx', -999)
        elapsed = idx - last_sig_idx
        if 0 <= elapsed <= T_SIGNAL_COOLDOWN:
            remaining = T_SIGNAL_COOLDOWN - elapsed
            t_reason = f'触发但冷却中(剩{remaining}天)'
        else:
            t_signal = True
            t_reason = f'SH 7日={sh_ret_7d:+.2%} < {T_SH_THRESHOLD:.0%}，冷却已过'
    else:
        t_reason = f'SH 7日={sh_ret_7d:+.2%}，未达{T_SH_THRESHOLD:.0%}阈值'
    result['t_signal'] = t_signal
    result['t_reason'] = t_reason

    # ── C1 信号检测 ──
    c1_signal = False
    c1_reason = ''

    # 条件1: ret_7d in [-5%, -3%]
    in_range = C1_SH_7D_LOW <= sh_ret_7d <= C1_SH_7D_HIGH
    # 同时排除 T 信号日（ret_7d <= -5% 的日子）
    is_t_day = sh_ret_7d <= T_SH_THRESHOLD

    if not in_range:
        if sh_ret_7d < C1_SH_7D_LOW:
            c1_reason = f'SH 7日={sh_ret_7d:+.2%}，跌幅过深(进入T区间)'
        else:
            c1_reason = f'SH 7日={sh_ret_7d:+.2%}，跌幅不足(>{C1_SH_7D_HIGH:.0%})'
    elif is_t_day:
        c1_reason = f'SH 7日={sh_ret_7d:+.2%}，但与T信号日重叠'
    else:
        # 条件2: "首日"判定 — 前一交易日的 ret_7d 不在 C1 区间
        if idx - 1 >= T_SH_LOOKBACK:
            prev7_of_prev_dt = pd.to_datetime(all_dates[idx - 1 - T_SH_LOOKBACK])
            prev7_of_prev_row = sh_df[sh_df['date'] == prev7_of_prev_dt]
            sh_prev7_of_prev = float(prev7_of_prev_row['close'].iloc[0])
            prev_ret_7d = (sh_prev_close - sh_prev7_of_prev) / sh_prev7_of_prev if sh_prev7_of_prev > 0 else 0
            prev_in_range = C1_SH_7D_LOW <= prev_ret_7d <= C1_SH_7D_HIGH

            if prev_in_range:
                c1_reason = f'SH 7日={sh_ret_7d:+.2%} 在区间，但非首日(昨日也在区间)'
            elif sh_ret_1d <= 0:
                # 条件3: 当日上证上涨
                c1_reason = f'SH 7日={sh_ret_7d:+.2%} 在区间且首日，但当日下跌({sh_ret_1d:+.2%})'
            else:
                c1_signal = True
                c1_reason = f'SH 7日={sh_ret_7d:+.2%}，首日+当日上涨({sh_ret_1d:+.2%})'
        else:
            c1_reason = '数据不足，无法判定首日条件'

    result['c1_signal'] = c1_signal
    result['c1_reason'] = c1_reason

    # ── BA 安全网检测 ──
    ba = _check_ba_safety_net(sh_df, idx, all_dates)
    result['ba'] = ba

    return result


def _check_ba_safety_net(sh_df, idx, all_dates):
    """
    计算 BA G3.7 regime 和安全网状态。
    返回 {regime, max_pos, ret_5d, ret_30d, ret_60d, vol_30d,
          below, streak, close_gt_ma60, triggers}
    """
    p = BA_G34
    today_dt = pd.to_datetime(all_dates[idx])

    # 需要至少 61 天数据
    if idx < 61:
        return {'error': '数据不足61天'}

    # 取最近 61 天收盘价
    closes = []
    for i in range(max(0, idx - 61), idx + 1):
        dt = pd.to_datetime(all_dates[i])
        row = sh_df[sh_df['date'] == dt]
        if not row.empty:
            closes.append(float(row['close'].iloc[0]))
    if len(closes) < 61:
        return {'error': f'收盘价不足({len(closes)}/61)'}

    ca = np.array(closes)
    n = len(ca)
    cur = ca[-1]

    # MA20
    ma20 = float(np.mean(ca[-20:]))
    below = bool(cur < ma20)

    # streak: 连续 below 天数
    streak = 0
    if below:
        for j in range(n - 1, -1, -1):
            ma20_j = float(np.mean(ca[max(0, j - 19): j + 1]))
            if ca[j] < ma20_j:
                streak += 1
            else:
                break

    # ret_5d / ret_30d / ret_60d
    ret_5d = float(ca[-1] / ca[-6] - 1) if n >= 6 and ca[-6] > 0 else 0.0
    ret_30d = float(ca[-1] / ca[-31] - 1) if n >= 31 and ca[-31] > 0 else 0.0
    ret_60d = float(ca[-1] / ca[-61] - 1) if n >= 61 and ca[-61] > 0 else 0.0

    # vol_30d
    log_ret = np.log(ca[-30:] / ca[-31:-1])
    vol_30d = float(np.std(log_ret)) if len(log_ret) >= 30 else 0.0

    # MA60
    ma60 = float(np.mean(ca[-60:]))
    close_gt_ma60 = bool(cur > ma60)

    # ── Regime 判定（对齐 _g34_regime_decide）──
    triggers = []
    regime = ''
    max_pos = 0

    if ret_30d < p['panic_thr']:
        regime = 'panic'
        max_pos = 0
        triggers.append(f'ret_30d={ret_30d:+.2%}<{p["panic_thr"]:.0%}')
    elif vol_30d > p['vol_thr']:
        regime = 'vol_30d'
        max_pos = 0
        triggers.append(f'vol_30d={vol_30d:.4f}>{p["vol_thr"]:.3f}')
    elif ret_60d < p['macro_bear_thr']:
        regime = 'macro_bear_60d'
        max_pos = 0
        triggers.append(f'ret_60d={ret_60d:+.2%}<{p["macro_bear_thr"]:.0%}')
    elif not below:
        regime = 'bull'
        max_pos = p['bull_mp']
    else:
        cur_init_bnd = p['init_bnd_bull'] if close_gt_ma60 else p['init_bnd_chop']
        if streak <= cur_init_bnd:
            regime = 'chop_init'
            max_pos = p['chop_init_mp']
        elif ret_5d < p['chop_else_ret5_min']:
            regime = 'chop_else_ret5'
            max_pos = 0
            triggers.append(f'ret_5d={ret_5d:+.2%}<{p["chop_else_ret5_min"]:.0%}')
        else:
            regime = 'chop_else'
            max_pos = p['chop_else_mp']

    return {
        'regime': regime,
        'max_pos': max_pos,
        'ret_5d': round(ret_5d * 100, 2),
        'ret_30d': round(ret_30d * 100, 2),
        'ret_60d': round(ret_60d * 100, 2),
        'vol_30d': round(vol_30d, 4),
        'below_ma20': below,
        'streak': streak,
        'close_gt_ma60': close_gt_ma60,
        'triggers': triggers,
    }


def notify_signals(result: dict):
    """根据检测结果发送钉钉通知，同时打印完整日志"""
    if 'error' in result:
        return

    today = result['today']
    sh_close = result['sh_close']
    sh_1d = result['sh_ret_1d']
    sh_7d = result['sh_ret_7d']
    t_signal = result['t_signal']
    c1_signal = result['c1_signal']
    t_mark = '✅触发' if t_signal else '❌'
    c1_mark = '✅触发' if c1_signal else '❌'

    # BA 安全网
    ba = result.get('ba', {})
    ba_regime = ba.get('regime', '?')
    ba_mp = ba.get('max_pos', '?')
    ba_ret5 = ba.get('ret_5d', 0)
    ba_ret30 = ba.get('ret_30d', 0)
    ba_ret60 = ba.get('ret_60d', 0)
    ba_vol30 = ba.get('vol_30d', 0)
    ba_triggers = ba.get('triggers', [])
    ba_streak = ba.get('streak', '?')
    ba_below = ba.get('below_ma20', False)
    ba_open = ba_mp > 0 if isinstance(ba_mp, int) else False
    ba_mark = f'✅ max_pos={ba_mp}' if ba_open else f'🚫 max_pos={ba_mp}'
    ba_safety_fired = bool(ba_triggers)  # 安全网是否触发

    # 始终打印完整日志
    print(f"[{_now_str()}] [signal] {today}  上证={sh_close:.2f}  "
          f"日={sh_1d:+.2%}  7日={sh_7d:+.2%}")
    print(f"[{_now_str()}] [signal] T:  {t_mark}  {result['t_reason']}")
    print(f"[{_now_str()}] [signal] C1: {c1_mark}  {result['c1_reason']}")
    print(f"[{_now_str()}] [signal] BA: {ba_mark}  regime={ba_regime}  "
          f"streak={ba_streak}d")
    # 4层安全网逐一显示（对齐 dashboard 格式）
    ba_vol30_pct = ba_vol30 * 100  # 转为百分比显示
    sn_items = [
        ('恐慌止损',  ba_ret30,     '< -6.0%',  ba_ret30 < -6),
        ('波动过大',  ba_vol30_pct, '> 2.2%',   ba_vol30 > 0.022),
        ('宏观熊市',  ba_ret60,     '< -5.0%',  ba_ret60 < -5),
        ('5日阴跌',   ba_ret5,      '< -1.0%',  ba_ret5 < -1),
    ]
    for name, val, thr_str, fired in sn_items:
        mark = '🚫' if fired else '✅'
        if name == '波动过大':
            print(f"[{_now_str()}] [signal]   {mark} {name}: {val:.3f}% ({thr_str})")
        else:
            print(f"[{_now_str()}] [signal]   {mark} {name}: {val:+.2f}% ({thr_str})")

    # 发钉钉条件: T/C1 触发 OR BA 安全网触发
    if not t_signal and not c1_signal and not ba_safety_fired:
        return

    from utils.notifier import send_notify

    # 构建通知内容
    if t_signal or c1_signal:
        title = f"📡 信号触发 ({today})"
    else:
        title = f"🚫 BA安全网触发 ({today})"

    lines = [title]
    lines.append(f"上证: {sh_close:.2f} (日{sh_1d:+.2%} / 7日{sh_7d:+.2%})")
    lines.append("")

    if t_signal:
        lines.append(f"🔴 策略T信号触发!")
        lines.append(f"   {result['t_reason']}")
        lines.append(f"   → 明日开盘将买入5日跌幅最大Top4")
        lines.append("")

    if c1_signal:
        lines.append(f"🟡 策略C1信号触发!")
        lines.append(f"   {result['c1_reason']}")
        lines.append(f"   → 明日开盘将买入3日跌幅最大Top5")
        lines.append("")

    # BA 安全网（始终显示）
    lines.append(f"BA regime: {ba_regime}  max_pos={ba_mp}")
    lines.append(f"  streak={ba_streak}d  MA20{'下' if ba_below else '上'}")
    # 4层安全网状态（对齐 dashboard）
    sn_lines = []
    for name, val, thr_str, fired in sn_items:
        mark = '🚫' if fired else '✅'
        if name == '波动过大':
            sn_lines.append(f"  {mark} {name}: {val:.3f}% ({thr_str})")
        else:
            sn_lines.append(f"  {mark} {name}: {val:+.2f}% ({thr_str})")
    lines.extend(sn_lines)
    if not ba_open:
        lines.append(f"  → 明日禁止新建仓")
    lines.append("")

    content = '\n'.join(lines)
    send_notify(content)
    # send_notify 用 daemon 线程异步发送，脚本退出前等待完成
    time.sleep(1.5)
    print(f"[{_now_str()}] [signal] ✅ 钉钉通知已发送")


def main():
    """独立运行入口"""
    today = sys.argv[1] if len(sys.argv) > 1 else None
    result = check_signals(today)

    print(f"\n{'='*50}")
    print(f"  T/C1 信号检测  {result.get('today', '?')}")
    print(f"{'='*50}")
    if 'error' in result:
        print(f"  错误: {result['error']}")
    else:
        print(f"  上证收盘: {result['sh_close']:.2f}")
        print(f"  日收益:   {result['sh_ret_1d']:+.2%}")
        print(f"  7日收益:  {result['sh_ret_7d']:+.2%}")
        print(f"  ─────────────────────────")
        t_mark = '✅ 触发' if result['t_signal'] else '❌'
        c1_mark = '✅ 触发' if result['c1_signal'] else '❌'
        print(f"  策略T:    {t_mark}  {result['t_reason']}")
        print(f"  策略C1:   {c1_mark}  {result['c1_reason']}")
        # BA 安全网
        ba = result.get('ba', {})
        if ba and 'error' not in ba:
            print(f"  ─────────────────────────")
            print(f"  BA regime:  {ba.get('regime','?')}  max_pos={ba.get('max_pos','?')}")
            print(f"  streak:     {ba.get('streak','?')}天 (MA20{'下' if ba.get('below_ma20') else '上'}" +
                  f"  MA60: {'上' if ba.get('close_gt_ma60') else '下'})")
            # 4层安全网
            vol30_pct = ba.get('vol_30d', 0) * 100
            sn_display = [
                ('恐慌止损',  ba.get('ret_30d',0), '< -6.0%', ba.get('ret_30d',0) < -6),
                ('波动过大',  vol30_pct,           '> 2.2%',  ba.get('vol_30d',0) > 0.022),
                ('宏观熊市',  ba.get('ret_60d',0), '< -5.0%', ba.get('ret_60d',0) < -5),
                ('5日阴跌',   ba.get('ret_5d',0),  '< -1.0%', ba.get('ret_5d',0) < -1),
            ]
            for name, val, thr_str, fired in sn_display:
                mark = '🚫' if fired else '✅'
                if name == '波动过大':
                    print(f"  {mark} {name}: {val:.3f}% ({thr_str})")
                else:
                    print(f"  {mark} {name}: {val:+.2f}% ({thr_str})")
    print(f"{'='*50}\n")

    notify_signals(result)
    return result


if __name__ == '__main__':
    main()
