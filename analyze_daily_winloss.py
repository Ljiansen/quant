#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分析最优参数（ta=2%,ts=1%,hs=3%,ss=2%,td=5）在各年度的
每日盈亏天数统计。
"""
import sys, json, os
sys.path.insert(0, 'd:/miniqmt_quant')

import run_backtest_5min_live_sim as sim

# ── 注入最优参数 ───────────────────────────────────────────────────────────────
def apply_best():
    sim.TRAILING_ACTIVATE      = 0.02
    sim.TRAILING_STOP          = 0.01
    sim.HARD_STOP_LOSS         = 0.03
    sim.STAR_HARD_STOP_LOSS    = 0.03
    sim.SOFT_STOP_LOSS         = 0.02
    sim.STAR_SOFT_STOP_LOSS    = 0.02
    sim.TIME_STOP_DAYS         = 5
    sim.STAR_TIME_STOP_DAYS    = 5

# ── 捕获 equity_curve ─────────────────────────────────────────────────────────
_captured_nav = []

def _hook_save(nav_series, trades, *args, **kwargs):
    _captured_nav.clear()
    _captured_nav.extend(nav_series)

# ── 年度配置 ──────────────────────────────────────────────────────────────────
YEAR_CFG = {
    '2022': ('20220101', '20221231', 'D:/5min_data_2022', 'D:/daily_data_2021_all'),
    '2023': ('20230101', '20231231', 'D:/5min_data_2023', None),
    '2024': ('20240101', '20241231', 'D:/5min_data_2024', None),
    '2025': ('20250101', '20260430', None,                None),
}

# 先尝试从现有 sim_results JSON 读取 equity_curve
SIM_DIR = 'd:/miniqmt_quant/sim_results'
JSON_MAP = {
    '2022': '20260508_2022_p2_4y_rank1_2022-01-01_2022-12-31.json',
    '2025': '20260508_2025_p2_4y_rank1_2025-01-01_2026-04-30.json',
}

def load_nav_from_json(year):
    fname = JSON_MAP.get(year)
    if not fname:
        return None
    fpath = os.path.join(SIM_DIR, fname)
    if not os.path.exists(fpath):
        return None
    with open(fpath, 'r', encoding='utf-8') as f:
        d = json.load(f)
    ec = d.get('equity_curve', [])
    if ec:
        return ec
    return None

def run_and_get_nav(year):
    """跑回测并返回 nav_series（每日资产列表）"""
    start, end, fivemin_dir, extra_daily = YEAR_CFG[year]
    apply_best()
    sim._save_reports = _hook_save
    sim.run_simulation(start, end, 300000,
                       fivemin_dir=fivemin_dir,
                       extra_daily_dir=extra_daily,
                       buy_price_mode='close')
    return list(_captured_nav)

def analyze(nav_list, year_label):
    """统计每日涨跌，返回汇总"""
    if not nav_list:
        return None

    # equity_curve 格式: [{'date': '2022-01-04', 'total_value': 300000, ...}, ...]
    # 或者 nav_series 格式相同
    vals = []
    for item in nav_list:
        tv = item.get('total_value', item.get('nav', 0))
        dt = item.get('date', '')
        vals.append((dt, float(tv)))

    if len(vals) < 2:
        return None

    profit_days = 0
    loss_days   = 0
    flat_days   = 0
    max_gain    = 0.0
    max_loss    = 0.0

    for i in range(1, len(vals)):
        prev_v = vals[i-1][1]
        curr_v = vals[i][1]
        chg    = (curr_v - prev_v) / prev_v * 100 if prev_v > 0 else 0
        if chg > 0.001:
            profit_days += 1
            max_gain = max(max_gain, chg)
        elif chg < -0.001:
            loss_days += 1
            max_loss = min(max_loss, chg)
        else:
            flat_days += 1

    total = profit_days + loss_days + flat_days
    win_rate = profit_days / total * 100 if total > 0 else 0

    final_val  = vals[-1][1]
    total_ret  = (final_val - 300000) / 300000 * 100

    return {
        'year': year_label,
        'total_days': total,
        'profit_days': profit_days,
        'loss_days': loss_days,
        'flat_days': flat_days,
        'win_rate_pct': round(win_rate, 1),
        'max_gain_pct': round(max_gain, 2),
        'max_loss_pct': round(max_loss, 2),
        'total_return_pct': round(total_ret, 2),
        'final_value': round(final_val),
    }

# ── 主程序 ────────────────────────────────────────────────────────────────────
print("=" * 70)
print("  最优参数（ta=2%,ts=1%,hs=3%,ss=2%,td=5）逐年每日盈亏统计")
print("=" * 70)

results = []
for year in ['2022', '2023', '2024', '2025']:
    label = year if year != '2025' else '2025~2026.4'
    print(f"\n>>> {label}...", end=' ', flush=True)

    nav = load_nav_from_json(year)
    if nav:
        print("(从缓存读取)", end=' ', flush=True)
    else:
        print("(重新回测)", end=' ', flush=True)
        nav = run_and_get_nav(year)

    r = analyze(nav, label)
    if r:
        results.append(r)
        print(f"完成 ({r['total_days']}个交易日)")
    else:
        print("无数据")

# ── 打印汇总表 ────────────────────────────────────────────────────────────────
print("\n")
print(f"{'年份':<14} {'总天数':>6} {'盈利天':>6} {'亏损天':>6} {'平盘天':>6} "
      f"{'胜率%':>7} {'最大单日涨%':>10} {'最大单日跌%':>10} {'年度总收益%':>10}")
print("-" * 80)
for r in results:
    print(f"{r['year']:<14} {r['total_days']:>6} {r['profit_days']:>6} "
          f"{r['loss_days']:>6} {r['flat_days']:>6} "
          f"{r['win_rate_pct']:>7.1f} {r['max_gain_pct']:>10.2f} "
          f"{r['max_loss_pct']:>10.2f} {r['total_return_pct']:>10.2f}")
print("-" * 80)
