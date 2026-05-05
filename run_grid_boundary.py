#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_grid_boundary.py  —  B+A 策略参数边界探测网格搜索
===========================================================
目标：验证当前最优参数（ta=0.02, ts=0.01, hs=0.03）是否真的是边界最优，
     还是因为网格左边界截断导致的假象。

探测范围（向左扩展）：
  trailing_activate : [0.01, 0.015, 0.02]   （0.02 = 原最小值，作锚点）
  trailing_stop     : [0.005, 0.008, 0.01]  （0.01 = 原最小值，作锚点）
  hard_stop_loss    : [0.02, 0.025, 0.03]   （0.03 = 原最小值，作锚点）
  soft_stop_loss    : 0.02  （固定为当前最优）
  time_stop_days    : 5     （固定为当前最优）

验证年份：2025年（牛市） + 2022年（熊市），与主网格一致。

用法:
    python run_grid_boundary.py

结果：
  grid_results/grid_boundary_2025.csv
  grid_results/grid_boundary_2022.csv
  grid_results/grid_boundary_combined.csv  （双年加权排名）
"""

import os
import sys
import io
import json
import copy
import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, 'd:/miniqmt_quant')
import run_backtest_5min_live_sim as sim
from import_reports_to_sim import (
    read_nav, read_trades, calc_summary, LIVE_SIM_FIXED_PARAMS,
)

# ── 目录 ─────────────────────────────────────────────────────────────────────
GRID_DIR    = 'd:/miniqmt_quant/grid_results'
TEMP_DIR    = 'd:/miniqmt_quant/grid_tmp'
SIM_DIR     = 'd:/miniqmt_quant/sim_results'
INITIAL_CAP = 300000.0

os.makedirs(GRID_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(SIM_DIR,  exist_ok=True)

# ── 固定参数（不参与本次搜索）─────────────────────────────────────────────────
FIXED_SS = 0.02   # soft_stop  = 当前最优
FIXED_TD = 5      # time_stop  = 当前最优

# ── 探测网格 ──────────────────────────────────────────────────────────────────
TA_LIST = [0.010, 0.015, 0.020]   # 0.02 作锚点（与原Phase1重叠）
TS_LIST = [0.005, 0.008, 0.010]   # 0.01 作锚点
HS_LIST = [0.020, 0.025, 0.030]   # 0.03 作锚点

# ── 数据缓存 ──────────────────────────────────────────────────────────────────
_orig_load_daily   = sim.load_daily_data
_orig_load_fivemin = sim.load_fivemin_data
_cache_daily:  dict = {}
_cache_fivemin: dict = {}


def _cached_load_daily(extra_daily_dir=None):
    key = str(extra_daily_dir)
    if key not in _cache_daily:
        print(f'[缓存] 首次加载日线数据 (extra={extra_daily_dir})...')
        _cache_daily[key] = _orig_load_daily(extra_daily_dir=extra_daily_dir)
    return _cache_daily[key]


def _cached_load_fivemin(start, end, fivemin_dir=None):
    key = (start, end, str(fivemin_dir))
    if key not in _cache_fivemin:
        print(f'[缓存] 首次加载5分钟数据 ({start}~{end}, dir={fivemin_dir})...')
        _cache_fivemin[key] = _orig_load_fivemin(start, end, fivemin_dir=fivemin_dir)
    return _cache_fivemin[key]


sim.load_daily_data  = _cached_load_daily
sim.load_fivemin_data = _cached_load_fivemin

# ── 报告重定向（避免污染 reports/） ───────────────────────────────────────────
_combo_tag = 'default'


def _patched_save_reports(nav_series, trades, start_date, end_date,
                           buy_price_mode='close', prev_bar_up=False,
                           no_open_30=False, slippage=0.0):
    os.makedirs(TEMP_DIR, exist_ok=True)
    pd.DataFrame(nav_series).to_csv(
        os.path.join(TEMP_DIR, f'{_combo_tag}_nav.csv'),
        index=False, encoding='utf-8-sig')
    pd.DataFrame(trades).to_csv(
        os.path.join(TEMP_DIR, f'{_combo_tag}_trades.csv'),
        index=False, encoding='utf-8-sig')


sim._save_reports = _patched_save_reports


# ── 参数写入 ──────────────────────────────────────────────────────────────────
def apply_params(ta, ts, hs, ss, td):
    sim.TRAILING_ACTIVATE   = ta
    sim.TRAILING_STOP       = ts
    sim.HARD_STOP_LOSS      = hs
    sim.STAR_HARD_STOP_LOSS = hs
    sim.SOFT_STOP_LOSS      = ss
    sim.STAR_SOFT_STOP_LOSS = ss
    sim.TIME_STOP_DAYS      = td
    sim.STAR_TIME_STOP_DAYS = td


# ── 单次模拟 ──────────────────────────────────────────────────────────────────
def run_one(ta, ts, hs, year):
    global _combo_tag
    tag = (f'bnd_y{year}_ta{int(ta*1000)}_ts{int(ts*1000)}_hs{int(hs*1000)}')
    _combo_tag = tag
    apply_params(ta, ts, hs, FIXED_SS, FIXED_TD)

    if year == '2022':
        start, end      = '20220101', '20221231'
        fivemin_dir     = 'D:/5min_data_2022'
        extra_daily_dir = 'D:/daily_data_2021_all'
    else:
        start, end      = '20250101', '20260430'
        fivemin_dir     = None
        extra_daily_dir = None

    old_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        sim.run_simulation(start, end, INITIAL_CAP,
                           fivemin_dir=fivemin_dir,
                           extra_daily_dir=extra_daily_dir,
                           buy_price_mode='close')
    finally:
        captured = sys.stdout.getvalue()
        sys.stdout = old_stdout
        for line in captured.splitlines():
            if '[缓存]' in line:
                print(line)

    nav_path   = os.path.join(TEMP_DIR, f'{tag}_nav.csv')
    trade_path = os.path.join(TEMP_DIR, f'{tag}_trades.csv')
    if not os.path.exists(nav_path):
        return None

    curve   = read_nav(nav_path)
    trades  = read_trades(trade_path)
    summary = calc_summary(curve, trades, INITIAL_CAP)

    nav_arr = np.array([pt['total_value'] for pt in curve])
    if len(nav_arr) > 1 and nav_arr[:-1].any():
        dr = np.diff(nav_arr) / nav_arr[:-1]
        sharpe = float(dr.mean() / dr.std() * np.sqrt(250)) if dr.std() > 0 else 0.0
    else:
        sharpe = 0.0

    return {
        'year':              year,
        'trailing_activate': ta,
        'trailing_stop':     ts,
        'hard_stop_loss':    hs,
        'soft_stop_loss':    FIXED_SS,
        'time_stop_days':    FIXED_TD,
        'profit_pct':        summary.get('profit_pct', 0),
        'max_drawdown':      summary.get('max_drawdown', 0),
        'win_rate':          summary.get('win_rate', 0),
        'total_trades':      summary.get('total_trades', 0),
        'sharpe':            round(sharpe, 3),
        '_nav_path':         nav_path,
        '_trade_path':       trade_path,
    }


# ── 打印表格 ──────────────────────────────────────────────────────────────────
def _print_table(rows, top=15):
    hdr = (f"  {'#':<3} {'ta':>6} {'ts':>6} {'hs':>6} "
           f"{'profit%':>9} {'max_dd%':>8} {'wr%':>6} {'sharpe':>7}  {'备注'}")
    print(hdr)
    print('  ' + '-' * (len(hdr) - 4))
    for rank, r in enumerate(rows[:top], 1):
        ta, ts, hs = r['trailing_activate'], r['trailing_stop'], r['hard_stop_loss']
        note = ' ← 当前最优(锚点)' if (abs(ta - 0.02) < 1e-6 and
                                        abs(ts - 0.01) < 1e-6 and
                                        abs(hs - 0.03) < 1e-6) else ''
        print(f"  {rank:<3} {ta:>6.3f} {ts:>6.3f} {hs:>6.3f} "
              f"{r['profit_pct']:>+9.2f} {r['max_drawdown']:>8.2f} "
              f"{r['win_rate']:>6.1f} {r['sharpe']:>7.3f}{note}")


# ══════════════════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════════════════
def build_grid():
    """生成所有有效参数组合（ts < ta 且 soft_stop < hard_stop）"""
    grid = []
    for ta in TA_LIST:
        for ts in TS_LIST:
            if ts >= ta:
                continue   # trailing_stop 必须 < trailing_activate
            for hs in HS_LIST:
                if FIXED_SS >= hs:
                    continue  # soft_stop 必须 < hard_stop
                grid.append((ta, ts, hs))
    return grid


def main():
    grid = build_grid()
    years = ['2025', '2022']
    total = len(grid) * len(years)

    print('\n' + '=' * 72)
    print('  边界探测网格搜索 — 验证当前最优参数是否为真实最优')
    print(f'  探测参数: ta∈{TA_LIST}  ts∈{TS_LIST}  hs∈{HS_LIST}')
    print(f'  固定参数: soft={FIXED_SS}  time={FIXED_TD}天')
    print(f'  有效组合: {len(grid)} 个  ×  {len(years)} 年  =  {total} 次模拟')
    print(f'  当前最优(锚点): ta=0.02  ts=0.01  hs=0.03（含在网格中作参照）')
    print('=' * 72)

    start_t = datetime.datetime.now()
    results = []
    idx = 0

    for year in years:
        print(f'\n{"─"*72}')
        print(f'  开始 {year} 年回测...')
        print(f'{"─"*72}')
        for ta, ts, hs in grid:
            idx += 1
            elapsed = int((datetime.datetime.now() - start_t).total_seconds() // 60)
            eta = int(elapsed * (total - idx + 1) // max(idx - 1, 1)) if idx > 1 else '?'
            print(f'[{idx:03d}/{total}] {year}年 '
                  f'ta={ta:.3f} ts={ts:.3f} hs={hs:.3f} | '
                  f'已用{elapsed}分 剩余~{eta}分 | ', end='', flush=True)

            r = run_one(ta, ts, hs, year)
            if r:
                results.append(r)
                print(f'profit={r["profit_pct"]:+.2f}% '
                      f'dd={r["max_drawdown"]:.2f}% '
                      f'wr={r["win_rate"]:.1f}% '
                      f'sharpe={r["sharpe"]:.3f}')
            else:
                print('ERROR')

    # ── 保存原始结果 ──────────────────────────────────────────────────────────
    df = pd.DataFrame(results)
    df_clean = df.drop(columns=['_nav_path', '_trade_path'], errors='ignore')

    df25 = df[df['year'] == '2025'].sort_values('profit_pct', ascending=False).reset_index(drop=True)
    df22 = df[df['year'] == '2022'].sort_values('profit_pct', ascending=False).reset_index(drop=True)

    df25.drop(columns=['_nav_path','_trade_path'], errors='ignore').to_csv(
        os.path.join(GRID_DIR, 'grid_boundary_2025.csv'), index=False, encoding='utf-8-sig')
    df22.drop(columns=['_nav_path','_trade_path'], errors='ignore').to_csv(
        os.path.join(GRID_DIR, 'grid_boundary_2022.csv'), index=False, encoding='utf-8-sig')

    # ── 分年打印 ──────────────────────────────────────────────────────────────
    for yr, dfyr in [('2025', df25), ('2022', df22)]:
        print(f'\n{"=" * 72}')
        print(f'  {yr} 年结果（按 profit_pct 降序）')
        print(f'{"=" * 72}')
        _print_table(dfyr.to_dict('records'))

    # ── 双年加权排名 ──────────────────────────────────────────────────────────
    def _key(r):
        return (round(r['trailing_activate'], 4),
                round(r['trailing_stop'], 4),
                round(r['hard_stop_loss'], 4))

    by_key_25 = {_key(r): r for r in df25.to_dict('records')}
    by_key_22 = {_key(r): r for r in df22.to_dict('records')}
    common = [(k, by_key_25[k], by_key_22[k])
              for k in by_key_25 if k in by_key_22]
    common.sort(key=lambda x: x[1]['profit_pct'] * 0.6 + x[2]['profit_pct'] * 0.4,
                reverse=True)

    print(f'\n{"=" * 72}')
    print('  双年加权排名（2025×0.6 + 2022×0.4）')
    print(f'{"=" * 72}')
    hdr = (f"  {'#':<3} {'ta':>6} {'ts':>6} {'hs':>6} "
           f"{'2025%':>8} {'2022%':>8} {'加权':>8}  备注")
    print(hdr)
    print('  ' + '-' * 60)
    for rank, (k, r25, r22) in enumerate(common[:15], 1):
        weighted = r25['profit_pct'] * 0.6 + r22['profit_pct'] * 0.4
        note = ' ← 当前最优(锚点)' if (abs(k[0]-0.02)<1e-6 and
                                        abs(k[1]-0.01)<1e-6 and
                                        abs(k[2]-0.03)<1e-6) else ''
        print(f'  {rank:<3} {k[0]:>6.3f} {k[1]:>6.3f} {k[2]:>6.3f} '
              f'{r25["profit_pct"]:>+8.2f} {r22["profit_pct"]:>+8.2f} '
              f'{weighted:>+8.2f}{note}')

    # ── 保存合并结果 ──────────────────────────────────────────────────────────
    combined = []
    for k, r25, r22 in common:
        combined.append({
            'trailing_activate': k[0],
            'trailing_stop':     k[1],
            'hard_stop_loss':    k[2],
            'soft_stop_loss':    FIXED_SS,
            'time_stop_days':    FIXED_TD,
            'profit_2025':       r25['profit_pct'],
            'profit_2022':       r22['profit_pct'],
            'dd_2025':           r25['max_drawdown'],
            'dd_2022':           r22['max_drawdown'],
            'sharpe_2025':       r25['sharpe'],
            'sharpe_2022':       r22['sharpe'],
            'weighted_score':    round(r25['profit_pct']*0.6 + r22['profit_pct']*0.4, 2),
        })
    pd.DataFrame(combined).sort_values('weighted_score', ascending=False).to_csv(
        os.path.join(GRID_DIR, 'grid_boundary_combined.csv'),
        index=False, encoding='utf-8-sig')

    total_min = int((datetime.datetime.now() - start_t).total_seconds() // 60)
    print(f'\n{"=" * 72}')
    print(f'  边界探测完成，总用时约 {total_min} 分钟')
    print(f'  结果文件:')
    print(f'    grid_results/grid_boundary_2025.csv')
    print(f'    grid_results/grid_boundary_2022.csv')
    print(f'    grid_results/grid_boundary_combined.csv  ← 主要看这个')
    print(f'{"=" * 72}')
    print()

    # ── 结论提示 ──────────────────────────────────────────────────────────────
    anchor_rank = next((i+1 for i, (k,_,_) in enumerate(common)
                        if abs(k[0]-0.02)<1e-6 and abs(k[1]-0.01)<1e-6 and abs(k[2]-0.03)<1e-6),
                       None)
    if anchor_rank == 1:
        print('  结论：当前最优参数 (ta=0.02 ts=0.01 hs=0.03) 在边界探测中排名 #1')
        print('        → 原参数已是真实最优，无需调整。')
    elif anchor_rank is not None:
        print(f'  结论：当前最优参数排名 #{anchor_rank}，存在更优的参数组合！')
        best_k, best_r25, best_r22 = common[0]
        print(f'        建议新参数: ta={best_k[0]:.3f}  ts={best_k[1]:.3f}  hs={best_k[2]:.3f}')
        print(f'        加权收益: {best_r25["profit_pct"]*0.6 + best_r22["profit_pct"]*0.4:+.2f}%'
              f' (2025={best_r25["profit_pct"]:+.2f}%  2022={best_r22["profit_pct"]:+.2f}%)')
    else:
        print('  结论：锚点未出现在结果中，请检查数据。')


if __name__ == '__main__':
    main()
