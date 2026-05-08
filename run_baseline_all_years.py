#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_baseline_all_years.py
=========================
用当前最优参数对 2022 / 2023 / 2024 / 2025~2026-04-30 分段回测，
并输出逐年 + 合并全周期统计。

用法:
    python run_baseline_all_years.py
"""

import os, sys, io, json, datetime, copy
import numpy as np
import pandas as pd

sys.path.insert(0, 'd:/miniqmt_quant')
import config
import run_backtest_5min_live_sim as sim
from import_reports_to_sim import read_nav, read_trades, calc_summary, LIVE_SIM_FIXED_PARAMS

# ─── 目录 ────────────────────────────────────────────────────────────────────
TEMP_DIR  = 'd:/miniqmt_quant/baseline_tmp'
SIM_DIR   = 'd:/miniqmt_quant/sim_results'
LOG_FILE  = 'd:/miniqmt_quant/baseline_all_years_result.txt'
INITIAL_CAP = 300_000.0

os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(SIM_DIR,  exist_ok=True)

_log_fh = open(LOG_FILE, 'w', encoding='utf-8')

def _log(msg):
    ts   = datetime.datetime.now().strftime('%H:%M:%S')
    line = f'[{ts}] {msg}'
    _log_fh.write(line + '\n')
    _log_fh.flush()
    print(line)


# ─── 当前最优参数 ─────────────────────────────────────────────────────────────
OPT_PARAMS = dict(
    trailing_activate = config.V3_TRAILING_ACTIVATE,   # 0.005
    trailing_stop     = config.V3_TRAILING_STOP,       # 0.01
    hard_stop_loss    = config.V3_HARD_STOP_LOSS,      # 0.03
    soft_stop_loss    = config.V3_SOFT_STOP_LOSS,      # 0.02
    time_stop_days    = config.V3_TIME_STOP_DAYS,      # 5
)


def _apply_params(ta, ts, hs, ss, td):
    sim.TRAILING_ACTIVATE   = ta
    sim.TRAILING_STOP       = ts
    sim.HARD_STOP_LOSS      = hs; sim.STAR_HARD_STOP_LOSS = hs
    sim.SOFT_STOP_LOSS      = ss; sim.STAR_SOFT_STOP_LOSS = ss
    sim.TIME_STOP_DAYS      = td; sim.STAR_TIME_STOP_DAYS = td


# ─── 年份 → 数据目录映射 ──────────────────────────────────────────────────────
def _year_cfg(year_str):
    """返回 (start, end, fivemin_dir, extra_daily_dir)"""
    if year_str == '2022':
        return '20220101', '20221231', 'D:/5min_data_2022', 'D:/daily_data_2021_all'
    if year_str == '2023':
        return '20230101', '20231231', 'D:/5min_data_2023', None
    if year_str == '2024':
        return '20240101', '20241231', 'D:/5min_data_2024', None
    # 2025 ~ 2026
    return '20250101', '20260430', None, None


# ─── 数据缓存（避免重复读磁盘） ────────────────────────────────────────────────
_orig_daily   = sim.load_daily_data
_orig_fivemin = sim.load_fivemin_data
_cache_daily:   dict = {}
_cache_fivemin: dict = {}

def _cached_daily(extra_daily_dir=None):
    k = str(extra_daily_dir)
    if k not in _cache_daily:
        _log(f'  [缓存] 首次加载日线数据 extra={extra_daily_dir}')
        _cache_daily[k] = _orig_daily(extra_daily_dir=extra_daily_dir)
    return _cache_daily[k]

def _cached_fivemin(start, end, fivemin_dir=None):
    k = (start, end, str(fivemin_dir))
    if k not in _cache_fivemin:
        _log(f'  [缓存] 首次加载5分钟数据 {start}~{end} dir={fivemin_dir}')
        _cache_fivemin[k] = _orig_fivemin(start, end, fivemin_dir=fivemin_dir)
    return _cache_fivemin[k]

sim.load_daily_data  = _cached_daily
sim.load_fivemin_data = _cached_fivemin

# ─── 报告重定向 ────────────────────────────────────────────────────────────────
_combo_tag = 'default'

def _patched_save(nav_series, trades, start_date, end_date,
                  buy_price_mode='close', prev_bar_up=False,
                  no_open_30=False, slippage=0.0005):
    os.makedirs(TEMP_DIR, exist_ok=True)
    pd.DataFrame(nav_series).to_csv(
        os.path.join(TEMP_DIR, f'{_combo_tag}_nav.csv'), index=False, encoding='utf-8-sig')
    pd.DataFrame(trades).to_csv(
        os.path.join(TEMP_DIR, f'{_combo_tag}_trades.csv'), index=False, encoding='utf-8-sig')

sim._save_reports = _patched_save


# ─── 单年回测 ─────────────────────────────────────────────────────────────────
def run_year(year_str):
    global _combo_tag
    ta = OPT_PARAMS['trailing_activate']
    ts = OPT_PARAMS['trailing_stop']
    hs = OPT_PARAMS['hard_stop_loss']
    ss = OPT_PARAMS['soft_stop_loss']
    td = OPT_PARAMS['time_stop_days']

    _apply_params(ta, ts, hs, ss, td)
    _combo_tag = f'baseline_{year_str}'

    start, end, fivemin_dir, extra_daily_dir = _year_cfg(year_str)
    _log(f'  运行: {start} ~ {end}  fivemin={fivemin_dir}  extra_daily={extra_daily_dir}')

    old = sys.stdout; sys.stdout = io.StringIO()
    try:
        sim.run_simulation(start, end, INITIAL_CAP,
                           fivemin_dir=fivemin_dir,
                           extra_daily_dir=extra_daily_dir,
                           buy_price_mode='close',
                           slippage=0.0005)
    finally:
        captured = sys.stdout.getvalue()
        sys.stdout = old
        for line in captured.splitlines():
            if '[缓存]' in line:
                _log(line)

    nav_path   = os.path.join(TEMP_DIR, f'{_combo_tag}_nav.csv')
    trade_path = os.path.join(TEMP_DIR, f'{_combo_tag}_trades.csv')
    if not os.path.exists(nav_path):
        _log(f'  [警告] {year_str} nav 文件未生成')
        return None

    curve  = read_nav(nav_path)
    trades = read_trades(trade_path)
    stats  = calc_summary(curve, trades, INITIAL_CAP)

    # 计算 Sharpe
    nav_arr = np.array([pt['total_value'] for pt in curve])
    if len(nav_arr) > 1 and nav_arr[:-1].any():
        dr = np.diff(nav_arr) / nav_arr[:-1]
        sharpe = float(dr.mean() / dr.std() * np.sqrt(250)) if dr.std() > 0 else 0.0
    else:
        sharpe = 0.0

    return {
        'year':           year_str,
        'start':          start,
        'end':            end,
        'profit_pct':     stats.get('profit_pct', 0),
        'max_drawdown':   stats.get('max_drawdown', 0),
        'win_rate':       stats.get('win_rate', 0),
        'total_trades':   stats.get('total_trades', 0),
        'sell_trades':    stats.get('sell_trades', 0),
        'final_value':    stats.get('final_value', 0),
        'sharpe':         round(sharpe, 3),
        '_nav':           curve,
        '_nav_path':      nav_path,
        '_trade_path':    trade_path,
    }


# ─── 多年 NAV 合并（资金连续复利） ────────────────────────────────────────────
def _chain_nav(year_results):
    """将各年 NAV 曲线首尾相接，计算全周期累计净值序列"""
    chained = []
    running_capital = INITIAL_CAP
    for r in year_results:
        if r is None:
            continue
        nav = r['_nav']
        scale = running_capital / INITIAL_CAP      # 将当年净值缩放到真实资本
        for pt in nav:
            abs_val = pt['total_value'] * scale     # 真实资产价值
            chained.append({'date': pt['date'], 'total_value': abs_val})
        # 下一年初始资金 = 本年末资产
        running_capital = nav[-1]['total_value'] * scale if nav else running_capital
    return chained, running_capital


def _calc_combined(chained_nav, final_capital):
    if not chained_nav:
        return {}
    profit_pct = round((final_capital - INITIAL_CAP) / INITIAL_CAP * 100, 2)
    # 最大回撤
    peak = INITIAL_CAP
    max_dd = 0.0
    for pt in chained_nav:
        v = pt['total_value']
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100
        if dd > max_dd:
            max_dd = dd
    return {
        'profit_pct':   profit_pct,
        'max_drawdown': round(max_dd, 2),
        'final_value':  round(final_capital, 2),
    }


# ─── 导出到 sim_results ────────────────────────────────────────────────────────
def _export_to_sim(year_str, nav_path, trade_path):
    if not (nav_path and os.path.exists(nav_path)):
        return
    curve  = read_nav(nav_path)
    trades = read_trades(trade_path)
    stats  = calc_summary(curve, trades, INITIAL_CAP)

    ta = OPT_PARAMS['trailing_activate']
    ts = OPT_PARAMS['trailing_stop']
    hs = OPT_PARAMS['hard_stop_loss']
    ss = OPT_PARAMS['soft_stop_loss']
    td = OPT_PARAMS['time_stop_days']

    if year_str == '2022':
        s_date, e_date = '2022-01-01', '2022-12-31'
    elif year_str == '2023':
        s_date, e_date = '2023-01-01', '2023-12-31'
    elif year_str == '2024':
        s_date, e_date = '2024-01-01', '2024-12-31'
    else:
        s_date, e_date = '2025-01-01', '2026-04-30'

    params = copy.deepcopy(LIVE_SIM_FIXED_PARAMS)
    params['main_board']['trailing_activate'] = ta
    params['main_board']['trailing_stop']     = ts
    params['main_board']['hard_stop_loss']    = hs
    params['main_board']['soft_stop_loss']    = ss
    params['main_board']['time_stop_days']    = td
    params['star_board']['hard_stop_loss']    = hs
    params['star_board']['soft_stop_loss']    = ss
    params['star_board']['time_stop_days']    = td
    params['general']['buy_mode']             = '收盘价'
    params['general']['prev_bar_up']          = int(sim.PREV_BAR_UP)
    params['general']['no_chase_30']          = 0
    params['variant'] = f'baseline_{year_str}'
    params['label']   = (f'最优参数基准 {year_str} '
                         f'(ta={ta:.3f} ts={ts:.2f} hs={hs:.2f} ss={ss:.2f} td={td})')

    today   = datetime.date.today().strftime('%Y%m%d')
    run_id  = f'{today}_{year_str}_baseline'
    out_path = os.path.join(SIM_DIR, f'{run_id}_{s_date}_{e_date}.json')

    data = {
        'run_id':          run_id,
        'label':           params['label'],
        'start_date':      s_date,
        'end_date':        e_date,
        'run_time':        today + ' 00:00:00',
        'initial_capital': INITIAL_CAP,
        'params':          params,
        'equity_curve':    curve,
        'summary':         stats,
        'trades':          trades,
    }
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    _log(f'  [导出] {os.path.basename(out_path)}')
    return out_path


# ═════════════════════════════════════════════════════════════════════════════
# 主入口
# ═════════════════════════════════════════════════════════════════════════════
def main():
    _log('=' * 70)
    _log('  最优参数多年基准回测（修复移动止盈卖出价Bug后）')
    _log(f'  参数: ta={OPT_PARAMS["trailing_activate"]:.3f}  '
         f'ts={OPT_PARAMS["trailing_stop"]:.2f}  '
         f'hs={OPT_PARAMS["hard_stop_loss"]:.2f}  '
         f'ss={OPT_PARAMS["soft_stop_loss"]:.2f}  '
         f'td={OPT_PARAMS["time_stop_days"]}')
    _log(f'  初始资金: {INITIAL_CAP:,.0f} 元')
    _log('=' * 70)

    years       = ['2022', '2023', '2024', '2025']
    year_labels = {'2022': '2022全年', '2023': '2023全年',
                   '2024': '2024全年', '2025': '2025-01 ~ 2026-04'}
    results     = {}
    t0 = datetime.datetime.now()

    for yr in years:
        _log(f'\n>>> {year_labels[yr]}')
        r = run_year(yr)
        results[yr] = r
        if r:
            _log(f'  profit={r["profit_pct"]:+.2f}%  '
                 f'max_dd={r["max_drawdown"]:.2f}%  '
                 f'win_rate={r["win_rate"]:.1f}%  '
                 f'sharpe={r["sharpe"]:.3f}  '
                 f'trades={r["sell_trades"]}笔卖出')
            _export_to_sim(yr, r['_nav_path'], r['_trade_path'])
        else:
            _log(f'  [失败] {yr} 回测未产生结果')

    # ── 全周期合并统计 ────────────────────────────────────────────────────────
    valid_results = [results[y] for y in years if results.get(y)]
    chained, final_cap = _chain_nav(valid_results)
    combined = _calc_combined(chained, final_cap)

    total_min = (datetime.datetime.now() - t0).seconds // 60
    _log(f'\n{"=" * 70}')
    _log('  逐年统计汇总')
    _log(f'{"=" * 70}')
    header = f"  {'年份':<14} {'收益%':>9} {'最大回撤%':>10} {'胜率%':>7} {'Sharpe':>8} {'卖出笔数':>8}"
    _log(header)
    _log('  ' + '-' * (len(header) - 2))
    for yr in years:
        r = results.get(yr)
        if r:
            _log(f"  {year_labels[yr]:<14} {r['profit_pct']:>+9.2f} "
                 f"{r['max_drawdown']:>10.2f} {r['win_rate']:>7.1f} "
                 f"{r['sharpe']:>8.3f} {r['sell_trades']:>8}")
        else:
            _log(f"  {year_labels[yr]:<14}  [无数据]")

    _log(f'\n  全周期合并（2022-2026.4.30）:')
    _log(f"    累计收益: {combined.get('profit_pct', 'N/A'):+.2f}%")
    _log(f"    最大回撤: {combined.get('max_drawdown', 'N/A'):.2f}%")
    _log(f"    最终资产: {combined.get('final_value', 0):,.0f} 元")
    _log(f'\n  总用时: {total_min} 分钟')
    _log(f'  结果日志: {LOG_FILE}')
    _log(f'  各年 JSON 已写入: {SIM_DIR}')
    _log('  请刷新仪表盘查看各年回测结果')

    _log_fh.close()


if __name__ == '__main__':
    main()
