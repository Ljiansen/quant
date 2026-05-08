#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_backtest_5min_live_sim.py
=============================
基于5分钟K线的实盘模拟回测

设计原则：
  - 完全对齐实盘引擎逻辑（全天扫描，无固定买入时间窗口）
  - 买入信号：用每根5分钟K线的 high/close 检查条件（--buy-price 控制），满足即成交
  - 止损信号：用每根5分钟K线的 low 检查硬止损 / 移动止盈
  - 收盘信号：用 14:55 K线检查阴跌止损 / 时间止损（次日开盘执行）
  - 选股池：B+A（MA20趋势过滤+信号质量），每日基于前一日数据重算，结果磁盘缓存

用法:
    python run_backtest_5min_live_sim.py --start 20250101 --end 20260430
    python run_backtest_5min_live_sim.py --start 20250101 --end 20260430 --buy-price close
    python run_backtest_5min_live_sim.py --start 20250101 --end 20260430 --capital 300000
"""

import os
import sys
import math
import glob
import argparse
import datetime
from collections import defaultdict

import numpy as np
import pandas as pd

sys.path.insert(0, 'd:/miniqmt_quant')
import config

# ─── 策略参数（完全对齐实盘 config.py） ────────────────────────────────────────
MIN_CHANGE_PCT         = config.V3_MIN_CHANGE_PCT
MAX_CHANGE_PCT         = config.V3_MAX_CHANGE_PCT
STAR_MIN_CHANGE_PCT    = config.V3_STAR_MIN_CHANGE_PCT
STAR_MAX_CHANGE_PCT    = config.V3_STAR_MAX_CHANGE_PCT
HARD_STOP_LOSS         = config.V3_HARD_STOP_LOSS
SOFT_STOP_LOSS         = config.V3_SOFT_STOP_LOSS
STAR_HARD_STOP_LOSS    = config.V3_STAR_HARD_STOP_LOSS
STAR_SOFT_STOP_LOSS    = config.V3_STAR_SOFT_STOP_LOSS
TRAILING_ACTIVATE      = config.V3_TRAILING_ACTIVATE
TRAILING_STOP          = config.V3_TRAILING_STOP
STAR_TRAILING_ACTIVATE = config.V3_STAR_TRAILING_ACTIVATE
STAR_TRAILING_STOP     = config.V3_STAR_TRAILING_STOP
TIME_STOP_DAYS         = config.V3_TIME_STOP_DAYS
STAR_TIME_STOP_DAYS    = config.V3_STAR_TIME_STOP_DAYS
MAX_POSITIONS          = config.V3_MAX_POSITIONS
TOP_N                  = config.V3_TOP_N
COMMISSION_RATE        = config.V3_COMMISSION_RATE
MIN_COMMISSION         = config.V3_MIN_COMMISSION
STAMP_TAX_RATE         = config.V3_STAMP_TAX_RATE
LIMIT_UP_MAIN          = 0.098
LIMIT_UP_STAR          = config.V3_STAR_LIMIT_UP
PREV_BAR_UP            = getattr(config, 'V3_PREV_BAR_UP', False)  # 前K线非阴线过滤，对齐实盘

# B+A 选股参数（对齐 init_rebalance_pool.py）
BA_LOOKBACK  = config.V3_REBALANCE_LOOKBACK   # 120 个交易日
BA_MA20_DAYS = 20

# 数据目录（可被 CLI --fivemin-dir / --extra-daily-dir 覆盖）
FIVEMIN_DIR = 'D:/5min_data'
DAILY_DIR   = config.V3_LOCAL_DATA_DIR        # 'D:/daily_data'

REPORTS_DIR = 'd:/miniqmt_quant/reports'
SLIPPAGE    = 0.0005  # 双向滑点（单边），买入×(1+slip)，卖出×(1-slip)
                      # 0.05%：对齐实盘路由加权均值成本（A股主板约1 tick溢价）


# ─── 板块判断辅助 ──────────────────────────────────────────────────────────────
def _is_star(code: str) -> bool:
    c = str(code).split('.')[0]
    return c.startswith('688') or c.startswith('30')

def _limit_up(code):      return LIMIT_UP_STAR          if _is_star(code) else LIMIT_UP_MAIN
def _min_chg(code):       return STAR_MIN_CHANGE_PCT    if _is_star(code) else MIN_CHANGE_PCT
def _max_chg(code):       return STAR_MAX_CHANGE_PCT    if _is_star(code) else MAX_CHANGE_PCT
def _hard_sl(code):       return STAR_HARD_STOP_LOSS    if _is_star(code) else HARD_STOP_LOSS
def _soft_sl(code):       return STAR_SOFT_STOP_LOSS    if _is_star(code) else SOFT_STOP_LOSS
def _trail_act(code):     return STAR_TRAILING_ACTIVATE if _is_star(code) else TRAILING_ACTIVATE
def _trail_stop(code):    return STAR_TRAILING_STOP     if _is_star(code) else TRAILING_STOP
def _time_stop_d(code):   return STAR_TIME_STOP_DAYS    if _is_star(code) else TIME_STOP_DAYS


# ─── 费用计算 ──────────────────────────────────────────────────────────────────
def _buy_commission(price, qty):
    actual = price * (1 + SLIPPAGE)
    return max(actual * qty * COMMISSION_RATE, MIN_COMMISSION)

def _sell_net(price, qty):
    """返回 (net_income, commission, stamp_tax)"""
    actual     = price * (1 - SLIPPAGE)
    commission = max(actual * qty * COMMISSION_RATE, MIN_COMMISSION)
    stamp_tax  = actual * qty * STAMP_TAX_RATE
    return actual * qty - commission - stamp_tax, commission, stamp_tax

def _buy_qty(cash, n_pos, price):
    """等额分仓，计算可买股数（100的整数倍，含滑点）"""
    empty = MAX_POSITIONS - n_pos
    if empty <= 0 or price <= 0:
        return 0
    actual = price * (1 + SLIPPAGE)
    alloc  = cash / empty
    qty    = math.floor(alloc / actual / 100) * 100
    return qty if qty >= 100 else 0


# ─── 数据加载 ──────────────────────────────────────────────────────────────────
def _valid_code(code: str) -> bool:
    """只保留主板/创业板/科创板，排除北交所"""
    if code.startswith('8') or code.startswith('4'):
        return False
    return (code.startswith('60') or code.startswith('00') or
            code.startswith('30') or code.startswith('688'))


def _load_daily_from_dir(base_dir: str) -> dict:
    """从指定目录加载日线数据，内部辅助函数"""
    result = {}
    for sub in ('SH', 'SZ'):
        d = os.path.join(base_dir, sub)
        if not os.path.exists(d):
            continue
        for fpath in glob.glob(os.path.join(d, 'price_*.csv')):
            code = os.path.basename(fpath)[len('price_'):-len('.csv')]
            if not _valid_code(code):
                continue
            try:
                df = pd.read_csv(fpath)
                if df.empty or len(df) < 2:
                    continue
                df = df.rename(columns={'timetag': 'date', 'volumn': 'volume'})
                df['date'] = pd.to_datetime(df['date'].astype(str), format='%Y%m%d', errors='coerce')
                df = df.dropna(subset=['date'])
                keep = [c for c in ('date', 'open', 'high', 'low', 'close', 'volume', 'amount')
                        if c in df.columns]
                df = df[keep].sort_values('date').reset_index(drop=True)
                if not df.empty:
                    result[code] = df
            except Exception:
                pass
    return result


def load_daily_data(extra_daily_dir: str = None) -> dict:
    """加载本地日线数据。extra_daily_dir 若指定则合并其数据（用于补充历史年份）。"""
    print('[数据] 加载日线数据...')
    daily = _load_daily_from_dir(DAILY_DIR)

    if extra_daily_dir and os.path.exists(extra_daily_dir):
        extra = _load_daily_from_dir(extra_daily_dir)
        print(f'[数据] 合并额外日线数据（{extra_daily_dir}）: {len(extra)} 只')
        for code, df_extra in extra.items():
            if code in daily:
                # 拼接并去重排序
                combined = pd.concat([df_extra, daily[code]], ignore_index=True)
                combined = combined.drop_duplicates(subset=['date']).sort_values('date').reset_index(drop=True)
                daily[code] = combined
            else:
                daily[code] = df_extra

    print(f'[数据] 日线数据: {len(daily)} 只')
    return daily


def load_fivemin_data(start_date_str: str, end_date_str: str,
                      fivemin_dir: str = None) -> dict:
    """
    加载5分钟K线。返回嵌套字典：
      bars_idx[code][day_str][(hour, minute)] = {open, high, low, close, volume}
    """
    print('[数据] 加载5分钟K线...')
    start_dt = pd.to_datetime(start_date_str)
    end_dt   = pd.to_datetime(end_date_str)

    # bars_idx[code][day_str][(h,m)] = bar_dict
    bars_idx: dict[str, dict[str, dict[tuple, dict]]] = {}
    n_files = 0

    base_dir = fivemin_dir or FIVEMIN_DIR
    for sub in ('SH', 'SZ'):
        d = os.path.join(base_dir, sub)
        if not os.path.exists(d):
            continue
        for fpath in glob.glob(os.path.join(d, '*.csv')):
            code = os.path.basename(fpath)[:-len('.csv')]
            if not _valid_code(code):
                continue
            try:
                df = pd.read_csv(fpath, dtype={'time': str})
                if df.empty:
                    continue
                df['date'] = pd.to_datetime(df['date'], errors='coerce')
                df = df.dropna(subset=['date'])
                df = df[(df['date'] >= start_dt) & (df['date'] <= end_dt)]
                if df.empty:
                    continue

                # 解析时间：time 格式 '20250102093500000' → hour=9, min=35
                t_str = df['time'].astype(str).str.zfill(17)
                df['bar_h'] = t_str.str[8:10].astype(int)
                df['bar_m'] = t_str.str[10:12].astype(int)

                for col in ('open', 'high', 'low', 'close', 'volume'):
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)

                code_idx: dict[str, dict] = {}
                for row in df.itertuples(index=False):
                    day_str = row.date.strftime('%Y-%m-%d')
                    hm_key  = (int(row.bar_h), int(row.bar_m))
                    if day_str not in code_idx:
                        code_idx[day_str] = {}
                    code_idx[day_str][hm_key] = {
                        'open':   float(row.open),
                        'high':   float(row.high),
                        'low':    float(row.low),
                        'close':  float(row.close),
                        'volume': float(row.volume),
                    }
                bars_idx[code] = code_idx
                n_files += 1
            except Exception:
                pass

    print(f'[数据] 5分钟K线: {len(bars_idx)} 只 / {n_files} 个文件')
    return bars_idx


def build_trading_calendar(daily_data: dict, start_str: str, end_str: str) -> list:
    """从日线数据提取交易日历，返回排序后的日期字符串列表"""
    dates_set = set()
    for df in daily_data.values():
        for d in df['date']:
            ds = d.strftime('%Y-%m-%d')
            if start_str <= ds <= end_str:
                dates_set.add(ds)
    return sorted(dates_set)


# ─── B+A 每日选股池（对齐 init_rebalance_pool.py 逻辑） ───────────────────────
def build_ba_pool(daily_data: dict, ref_date_str: str, all_trading_dates: list) -> list:
    """
    以 ref_date_str（含）为截止日，计算B+A选股池。
    实际调用时应传入"前一个交易日"作为 ref_date，避免未来信息。

    返回: Top-N 股票代码列表
    """
    from bisect import bisect_right
    all_td_set = set(all_trading_dates)       # O(1) 查找
    if ref_date_str not in all_td_set:
        prev = [d for d in all_trading_dates if d <= ref_date_str]
        if not prev:
            return []
        ref_date_str = prev[-1]

    # 用 bisect 做 O(log n) 的索引定位
    cur_idx        = bisect_right(all_trading_dates, ref_date_str) - 1
    lookback_start = max(0, cur_idx - BA_LOOKBACK)
    ma20_start     = max(0, cur_idx - BA_MA20_DAYS)
    ref_dt         = pd.to_datetime(ref_date_str)

    # 预先把日期集合转为 Timestamp set（只做一次，不在股票循环内重复）
    lb_ts_set   = set(pd.to_datetime(all_trading_dates[lookback_start: cur_idx + 1]))
    ma20_ts_set = set(pd.to_datetime(all_trading_dates[ma20_start:     cur_idx + 1]))

    results = []
    for code, df in daily_data.items():
        # 用 boolean sum 代替 len(filter)
        if int((df['date'] <= ref_dt).sum()) < 60:
            continue

        period_df = df[df['date'].isin(lb_ts_set)]
        if period_df.empty or len(period_df) < 5:
            continue

        # MA20 过滤
        ma20_df = df[df['date'].isin(ma20_ts_set)]
        if ma20_df.empty:
            continue
        last_close = float(period_df['close'].iloc[-1])
        ma20 = float(ma20_df['close'].mean())
        if last_close <= ma20:
            continue

        # 信号质量评分（涨幅1%~7% 且收阳线 的历史天数）
        period_sorted = period_df.sort_values('date')
        prev_closes   = period_sorted['close'].shift(1)
        chg           = (period_sorted['close'] - prev_closes) / prev_closes
        mask = ((chg > MIN_CHANGE_PCT) &
                (chg < MAX_CHANGE_PCT) &
                (period_sorted['close'] > period_sorted['open']))
        results.append({'code': code, 'score': int(mask.sum())})

    if not results:
        return []
    df_res = pd.DataFrame(results).sort_values('score', ascending=False, kind='stable')
    return df_res.head(TOP_N)['code'].tolist()


# ─── 主模拟引擎 ────────────────────────────────────────────────────────────────
def run_simulation(start_date: str, end_date: str, initial_capital: float = 300_000.0,
                   fivemin_dir: str = None, extra_daily_dir: str = None,
                   buy_price_mode: str = 'close', prev_bar_up: bool = PREV_BAR_UP,
                   no_open_30: bool = False, slippage: float = 0.0005):  # 默认0.05%滑点，对齐实盘路由加权均值成本
    """
    主入口：逐日逐根5分钟K线模拟完整交易逻辑。
    start_date / end_date 格式: 'YYYYMMDD'
    fivemin_dir:      可选，覆盖默认5分钟数据目录
    extra_daily_dir:  可选，额外日线数据目录（用于补充历史年份，如2021年）
    buy_price_mode:   'close'（默认，5分钟K线收盘价）或 'high'（K线最高价，对齐实盘）
    prev_bar_up:      True 时要求上一根5分钟K线非阴线（close >= open）才允许买入
    no_open_30:       True 时跳过开盘前30分钟（9:35-9:55）的买入信号
    slippage:         双向滑点（单边），买入×(1+slip)，卖出×(1-slip)
    """
    global SLIPPAGE
    SLIPPAGE = slippage
    start_str = f'{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}'
    end_str   = f'{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}'

    # ── 1. 数据加载 ──────────────────────────────────────────────────────────
    daily_data = load_daily_data(extra_daily_dir=extra_daily_dir)
    bars_idx   = load_fivemin_data(start_str, end_str, fivemin_dir=fivemin_dir)

    if not bars_idx:
        print('[错误] 5分钟数据为空，退出')
        return

    # ── 2. 交易日历 ───────────────────────────────────────────────────────────
    # 全历史日历（用于B+A的lookback，需要从2020开始）
    all_trading_dates = build_trading_calendar(daily_data, '2020-01-01', end_str)
    # 模拟区间的交易日
    sim_dates = sorted(
        d for d in set(
            day_str
            for code_days in bars_idx.values()
            for day_str in code_days
        )
        if start_str <= d <= end_str
    )

    if not sim_dates:
        print('[错误] 模拟区间无5分钟数据')
        return

    print(f'\n[模拟] 区间: {start_str} ~ {end_str}，共 {len(sim_dates)} 个交易日')
    print(f'[模拟] 初始资金: {initial_capital:,.0f} 元')

    print(f'[模拟] 买入价模式: {buy_price_mode}')
    print(f'[模拟] 前K线非阴线过滤: {"开启" if prev_bar_up else "关闭"}')
    print(f'[模拟] 开盘30分钟回避: {"开启(仅10:00后买入)" if no_open_30 else "关闭"}')
    print(f'[模拟] 滑点(单边): {slippage:.4f} ({slippage*100:.2f}%，往返{slippage*200:.2f}%)')

    # ── 3. 预计算每日B+A选股池（支持磁盘缓存） ────────────────────────────────
    import pickle, hashlib
    _cache_key = hashlib.md5(f'{start_str}|{end_str}|{fivemin_dir}|{extra_daily_dir}'.encode()).hexdigest()[:8]
    _cache_path = os.path.join(REPORTS_DIR, f'pool_cache_{start_date}_{end_date}_{_cache_key}.pkl')
    os.makedirs(REPORTS_DIR, exist_ok=True)

    if os.path.exists(_cache_path):
        print(f'[选股] 读取缓存选股池: {_cache_path}')
        with open(_cache_path, 'rb') as f:
            pool_by_date = pickle.load(f)
    else:
        print('[选股] 预计算每日B+A选股池（基于前一日数据）...')
        pool_by_date: dict[str, list] = {}
        for i, day_str in enumerate(sim_dates):
            prev_days = [d for d in all_trading_dates if d < day_str]
            ref_date  = prev_days[-1] if prev_days else day_str
            pool_by_date[day_str] = build_ba_pool(daily_data, ref_date, all_trading_dates)
            if (i + 1) % 50 == 0 or (i + 1) == len(sim_dates):
                print(f'  已处理 {i+1}/{len(sim_dates)} 天，当前池大小={len(pool_by_date[day_str])}')
        with open(_cache_path, 'wb') as f:
            pickle.dump(pool_by_date, f)
        print(f'[选股] 选股池已缓存: {_cache_path}')

    # ── 4. 预构建 prev_close 和 day_open 缓存 ─────────────────────────────────
    print('[数据] 构建 prev_close / day_open 缓存...')
    prev_close_cache: dict[str, dict[str, float]] = defaultdict(dict)  # [code][day] = float
    day_open_cache:   dict[str, dict[str, float]] = defaultdict(dict)  # [code][day] = float

    for code, df in daily_data.items():
        if len(df) < 2:
            continue
        date_arr  = df['date'].dt.strftime('%Y-%m-%d').values
        close_arr = df['close'].values
        for idx in range(1, len(df)):
            prev_close_cache[code][date_arr[idx]] = float(close_arr[idx - 1])

    for code, code_days in bars_idx.items():
        for day_str, hm_bars in code_days.items():
            if not hm_bars:
                continue
            first_hm  = min(hm_bars.keys())          # 最早的 (h, m)
            day_open_cache[code][day_str] = hm_bars[first_hm]['open']

    # ── 5. 主循环状态初始化 ───────────────────────────────────────────────────
    cash          = float(initial_capital)
    positions: dict[str, dict] = {}         # {code: position_dict}
    pending_sells: list[dict]  = []         # [{code, quantity, sell_type}]
    trades:     list[dict] = []
    nav_series: list[dict] = []
    sell_type_stats: dict[str, dict] = defaultdict(lambda: {'count': 0, 'pnl': 0.0})

    # ── 6. 逐日模拟 ───────────────────────────────────────────────────────────
    for day_idx, day_str in enumerate(sim_dates):
        pool = pool_by_date.get(day_str, [])

        # 当日参与的所有时间点（持仓股 + 候选池股的并集）
        active_codes = set(positions.keys()) | set(
            c for c in pool if day_str in bars_idx.get(c, {})
        )
        time_points_set: set[tuple] = set()
        for code in active_codes:
            time_points_set.update(bars_idx.get(code, {}).get(day_str, {}).keys())
        time_points = sorted(time_points_set)

        # ── [开盘前] 执行 pending 卖出（T+1，次日第一根K线 open 成交） ─────────
        executed_pending: set[str] = set()
        if pending_sells:
            for ps in pending_sells:
                code      = ps['code']
                qty       = ps['quantity']
                sell_type = ps['sell_type']
                if code not in positions:
                    executed_pending.add(code)
                    continue

                # 用当日第一根K线的 open，若无数据用 prev_close
                first_bar = None
                code_bars = bars_idx.get(code, {}).get(day_str, {})
                if code_bars:
                    first_hm  = min(code_bars.keys())
                    first_bar = code_bars[first_hm]
                open_price = (first_bar['open'] if first_bar
                              else prev_close_cache[code].get(day_str, 0))
                if open_price <= 0:
                    executed_pending.add(code)
                    continue

                net_income, commission, stamp_tax = _sell_net(open_price, qty)
                cash += net_income
                pos      = positions[code]
                buy_cost = pos['buy_price'] * qty
                pnl      = net_income - buy_cost
                pnl_pct  = pnl / buy_cost if buy_cost > 0 else 0.0
                trades.append({
                    'date': day_str, 'code': code, 'direction': 'sell',
                    'price': round(open_price, 3), 'quantity': qty,
                    'sell_type': sell_type,
                    'pnl': round(pnl, 2), 'pnl_pct': round(pnl_pct, 4),
                    'days_held': pos.get('days_held', 0),
                    'commission': round(commission, 2), 'stamp_tax': round(stamp_tax, 2),
                })
                sell_type_stats[sell_type]['count'] += 1
                sell_type_stats[sell_type]['pnl']   += pnl
                del positions[code]
                executed_pending.add(code)
        pending_sells = [ps for ps in pending_sells if ps['code'] not in executed_pending]

        # ── days_held 递增（买入当天的持仓不递增） ────────────────────────────
        for pos in positions.values():
            if pos.get('buy_date') != day_str:
                pos['days_held'] = pos.get('days_held', 0) + 1

        bought_today: set[str] = set()   # 当日已买入，避免重复

        # ── 逐根K线 ───────────────────────────────────────────────────────────
        for i, hm in enumerate(time_points):
            h, m = hm

            # ── 持仓监控：硬止损 / 移动止盈 ──────────────────────────────────
            codes_to_remove: list[str] = []
            for code, pos in list(positions.items()):
                if pos.get('days_held', 0) == 0:   # T+1：买入当天不触发
                    bar = bars_idx.get(code, {}).get(day_str, {}).get(hm)
                    if bar:
                        pos['highest_price'] = max(pos.get('highest_price', pos['buy_price']),
                                                   bar['high'])
                    continue

                bar = bars_idx.get(code, {}).get(day_str, {}).get(hm)
                if bar is None:
                    continue

                buy_price     = pos['buy_price']
                highest_price = max(pos.get('highest_price', buy_price), bar['high'])
                pos['highest_price'] = highest_price

                # 硬止损
                hard_price = buy_price * (1 - _hard_sl(code))
                if bar['low'] <= hard_price:
                    sell_price = max(hard_price, bar['open'])
                    _execute_sell(code, pos, sell_price, 'hard_stop',
                                  day_str, trades, sell_type_stats)
                    cash += _sell_net(sell_price, pos['quantity'])[0]
                    codes_to_remove.append(code)
                    continue

                # 移动止盈
                if highest_price >= buy_price * (1 + _trail_act(code)):
                    trigger = highest_price * (1 - _trail_stop(code))
                    if bar['low'] <= trigger:
                        # 5分钟K线粒度：触发移动止盈时，用该根K线的 close 作为卖出价
                        # 理由：
                        #   1. bar['close'] 是5分钟K线已知的最后成交价，语义明确
                        #   2. close 可以低于 trigger（价格继续下行），允许真实亏损
                        #   3. 用 trigger 作为卖出价等于保证最低为 buy×(1+act)×(1-ts)
                        #      数学上永远高于买入价，等同于原Bug（移动止盈永不亏）
                        #   4. 用 bar['open'] 同样高于trigger（开盘尚未触发），也偏乐观
                        sell_price = bar['close']
                        _execute_sell(code, pos, sell_price, 'trailing_stop',
                                      day_str, trades, sell_type_stats)
                        cash += _sell_net(sell_price, pos['quantity'])[0]
                        codes_to_remove.append(code)

            for code in codes_to_remove:
                positions.pop(code, None)

            # ── 买入扫描（全天，仓位不满时） ──────────────────────────────────
            if no_open_30 and h < 10:
                pass  # 开盘30分钟内（9:35-9:55）跳过买入
            elif len(positions) < MAX_POSITIONS:
                for code in pool:
                    if len(positions) >= MAX_POSITIONS:
                        break
                    if code in positions or code in bought_today:
                        continue

                    bar = bars_idx.get(code, {}).get(day_str, {}).get(hm)
                    if bar is None or bar['volume'] <= 0:
                        continue

                    # ── 第一根K线（9:30-9:35）仅做卖出/信息收集，不触发买入 ──
                    # 对齐实盘：实盘9:35首次扫描无前置K线，从9:40第二根起才判断买入
                    if i == 0:
                        continue

                    # ── 前K线非阴线过滤（close >= open，允许十字星） ──────
                    if prev_bar_up:
                        prev_hm = time_points[i - 1]
                        pb = bars_idx.get(code, {}).get(day_str, {}).get(prev_hm)
                        if pb is None or pb['close'] < pb['open']:
                            continue  # 上一根K线为阴线，跳过

                    # 根据模式选择买入价格
                    if buy_price_mode == 'close':
                        buy_px = bar['close']
                    elif buy_price_mode == 'mid':
                        buy_px = (bar['high'] + bar['close']) / 2.0
                    else:  # 'high'
                        buy_px = bar['high']
                    if buy_px <= 0:
                        continue

                    prev_close = prev_close_cache[code].get(day_str)
                    if not prev_close or prev_close <= 0:
                        continue

                    # 买入条件：收阳线判断（bar收盘价 > 当日9:30开盘价，对齐实盘tick.open）
                    day_open_price = day_open_cache[code].get(day_str)
                    if not day_open_price or day_open_price <= 0:
                        continue
                    chg = (bar['close'] - prev_close) / prev_close
                    if (chg > _min_chg(code)
                            and chg < _max_chg(code)
                            and buy_px > day_open_price   # 收阳：bar收盘价 > 当日开盘价
                            and chg < _limit_up(code)):   # 未涨停

                        qty = _buy_qty(cash, len(positions), buy_px)
                        if qty <= 0:
                            continue
                        buy_px_actual = buy_px * (1 + SLIPPAGE)
                        commission = _buy_commission(buy_px, qty)
                        total_cost = buy_px_actual * qty + commission
                        if total_cost > cash:
                            continue

                        cash -= total_cost
                        positions[code] = {
                            'code':          code,
                            'buy_price':     buy_px_actual,
                            'buy_date':      day_str,
                            'quantity':      qty,
                            'days_held':     0,
                            'highest_price': buy_px_actual,
                        }
                        bought_today.add(code)
                        trades.append({
                            'date': day_str, 'code': code, 'direction': 'buy',
                            'price': round(buy_px_actual, 3), 'quantity': qty,
                            'sell_type': None, 'pnl': None, 'pnl_pct': None,
                            'days_held': None,
                            'commission': round(commission, 2), 'stamp_tax': 0,
                        })

            # ── 14:55：阴跌止损 / 移动止盈 / 时间止损（pending，次日开盘执行） ─────
            if h == 14 and m == 55:
                pending_codes = {ps['code'] for ps in pending_sells}
                for code, pos in list(positions.items()):
                    if code in pending_codes:
                        continue
                    if pos.get('days_held', 0) == 0:
                        continue
                    bar = bars_idx.get(code, {}).get(day_str, {}).get((14, 55))
                    if bar is None:
                        continue
                    buy_price      = pos['buy_price']
                    day_open_price = day_open_cache[code].get(day_str, buy_price)

                    # 1. 阴跌止损
                    soft_price = buy_price * (1 - _soft_sl(code))
                    if bar['close'] < soft_price and bar['close'] < day_open_price:
                        pending_sells.append({
                            'code': code, 'quantity': pos['quantity'],
                            'sell_type': 'soft_stop',
                        })
                        continue

                    # 2. 移动止盈：最高价激活后收盘价触达回撤线（对齐实盘 _check_close_signals）
                    highest_price = pos.get('highest_price', buy_price)
                    if highest_price >= buy_price * (1 + _trail_act(code)):
                        trail_trigger = highest_price * (1 - _trail_stop(code))
                        if bar['close'] <= trail_trigger:
                            pending_sells.append({
                                'code': code, 'quantity': pos['quantity'],
                                'sell_type': 'trailing_stop',
                            })
                            continue

                    # 3. 时间止损
                    if pos['days_held'] >= _time_stop_d(code) and bar['close'] <= buy_price:
                        pending_sells.append({
                            'code': code, 'quantity': pos['quantity'],
                            'sell_type': 'time_stop',
                        })

        # ── 日终净值 ──────────────────────────────────────────────────────────
        mkt_val = 0.0
        for code, pos in positions.items():
            code_bars = bars_idx.get(code, {}).get(day_str, {})
            if code_bars:
                last_hm = max(code_bars.keys())
                price   = code_bars[last_hm]['close']
            else:
                price   = pos['buy_price']
            mkt_val += pos['quantity'] * price

        total_nav = cash + mkt_val
        nav_series.append({
            'date':      day_str,
            'nav':       round(total_nav, 2),
            'cash':      round(cash, 2),
            'mkt_val':   round(mkt_val, 2),
            'positions': len(positions),
        })

        if (day_idx + 1) % 20 == 0 or (day_idx + 1) == len(sim_dates):
            ret = (total_nav - initial_capital) / initial_capital
            print(f'  [{day_str}] 持仓={len(positions)} '
                  f'现金={cash:>10,.0f}  市值={mkt_val:>10,.0f}  '
                  f'总资产={total_nav:>10,.0f}  累计收益={ret:>+.2%}')

    # ── 7. 强平所有持仓（按末日收盘价） ─────────────────────────────────────
    last_day = sim_dates[-1]
    for code, pos in list(positions.items()):
        code_bars = bars_idx.get(code, {}).get(last_day, {})
        sell_price = (code_bars[max(code_bars.keys())]['close']
                      if code_bars else pos['buy_price'])
        _execute_sell(code, pos, sell_price, 'end_of_sim',
                      last_day, trades, sell_type_stats)
        cash += _sell_net(sell_price, pos['quantity'])[0]
        positions.pop(code, None)

    final_nav  = cash
    return_pct = (final_nav - initial_capital) / initial_capital

    # ── 8. 输出统计 & 保存报告 ────────────────────────────────────────────────
    _print_stats(initial_capital, final_nav, return_pct,
                 nav_series, trades, sell_type_stats, start_str, end_str)
    _save_reports(nav_series, trades, start_date, end_date, buy_price_mode=buy_price_mode,
                  prev_bar_up=prev_bar_up, no_open_30=no_open_30, slippage=slippage)


# ─── 卖出记录辅助 ──────────────────────────────────────────────────────────────
def _execute_sell(code, pos, sell_price, sell_type, day_str, trades, sell_type_stats):
    qty      = pos['quantity']
    net, commission, stamp_tax = _sell_net(sell_price, qty)
    buy_cost = pos['buy_price'] * qty
    pnl      = net - buy_cost
    pnl_pct  = pnl / buy_cost if buy_cost > 0 else 0.0
    trades.append({
        'date': day_str, 'code': code, 'direction': 'sell',
        'price': round(sell_price, 3), 'quantity': qty,
        'sell_type': sell_type,
        'pnl': round(pnl, 2), 'pnl_pct': round(pnl_pct, 4),
        'days_held': pos.get('days_held', 0),
        'commission': round(commission, 2), 'stamp_tax': round(stamp_tax, 2),
    })
    sell_type_stats[sell_type]['count'] += 1
    sell_type_stats[sell_type]['pnl']   += pnl


# ─── 统计输出 ──────────────────────────────────────────────────────────────────
def _print_stats(initial_capital, final_nav, return_pct,
                 nav_series, trades, sell_type_stats, start_str, end_str):
    nav_arr = np.array([x['nav'] for x in nav_series])
    n_days  = len(nav_arr)
    years   = n_days / 250

    # 年化收益
    annual = (1 + return_pct) ** (1 / years) - 1 if years > 0 else 0

    # 最大回撤
    peak   = nav_arr[0]
    max_dd = 0.0
    for v in nav_arr:
        peak  = max(peak, v)
        max_dd = max(max_dd, (peak - v) / peak)

    # 夏普比率
    if n_days > 1:
        daily_ret = np.diff(nav_arr) / nav_arr[:-1]
        sharpe = (daily_ret.mean() / daily_ret.std() * np.sqrt(250)
                  if daily_ret.std() > 0 else 0.0)
    else:
        sharpe = 0.0

    # 胜率 & 盈亏比
    sell_trades = [t for t in trades
                   if t['direction'] == 'sell' and t['pnl'] is not None
                   and t.get('sell_type') != 'end_of_sim']
    n_t   = len(sell_trades)
    wins  = [t['pnl'] for t in sell_trades if t['pnl'] > 0]
    loses = [t['pnl'] for t in sell_trades if t['pnl'] <= 0]
    win_rate  = len(wins) / n_t if n_t else 0.0
    avg_win   = sum(wins)  / len(wins)  if wins  else 0.0
    avg_lose  = sum(loses) / len(loses) if loses else 0.0
    pf        = abs(avg_win / avg_lose) if avg_lose else 999.0

    print('\n' + '=' * 65)
    print('  5分钟线实盘模拟回测结果（B+A策略，每日更新选股池）')
    print('=' * 65)
    print(f'  回测区间  : {start_str} ~ {end_str}  ({n_days} 个交易日)')
    print(f'  初始资金  : {initial_capital:>13,.0f} 元')
    print(f'  最终净值  : {final_nav:>13,.0f} 元')
    print(f'  总收益率  : {return_pct:>+.2%}')
    print(f'  年化收益率: {annual:>+.2%}')
    print(f'  最大回撤  : {max_dd:.2%}')
    print(f'  夏普比率  : {sharpe:.3f}')
    print(f'  交易次数  : {n_t} 次（不含末日强平）')
    print(f'  胜率      : {win_rate:.2%}')
    print(f'  盈亏比    : {pf:.2f}')
    print(f'  平均盈利  : {avg_win:>+.2f} 元')
    print(f'  平均亏损  : {avg_lose:>+.2f} 元')
    print()
    print('  卖出类型统计:')
    order = ['hard_stop', 'trailing_stop', 'soft_stop', 'time_stop', 'end_of_sim']
    for st in order:
        if st not in sell_type_stats:
            continue
        cnt = sell_type_stats[st]['count']
        avg = sell_type_stats[st]['pnl'] / cnt if cnt else 0
        print(f'    {st:<16}: {cnt:>4} 次  平均盈亏 {avg:>+8.2f} 元')
    print('=' * 65)


# ─── 报告保存 ──────────────────────────────────────────────────────────────────
def _save_reports(nav_series, trades, start_date, end_date, buy_price_mode='high',
                  prev_bar_up=False, no_open_30=False, slippage=0.0):
    os.makedirs(REPORTS_DIR, exist_ok=True)
    today = datetime.date.today().strftime('%Y%m%d')
    slip_tag = f'_slip{int(slippage*1000)}' if slippage > 0 else ''
    suffix = f'_{buy_price_mode}' + ('_pbu' if prev_bar_up else '') + ('_no30' if no_open_30 else '') + slip_tag
    tag   = f'{start_date}_{end_date}_{today}{suffix}'

    nav_path   = os.path.join(REPORTS_DIR, f'5min_live_sim_{tag}_nav.csv')
    trade_path = os.path.join(REPORTS_DIR, f'5min_live_sim_{tag}_trades.csv')

    pd.DataFrame(nav_series).to_csv(nav_path,   index=False, encoding='utf-8-sig')
    pd.DataFrame(trades)    .to_csv(trade_path,  index=False, encoding='utf-8-sig')

    print(f'\n  净値曲线 → {nav_path}')
    print(f'  交易明细 → {trade_path}')


# ─── CLI 入口 ──────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description='5分钟线实盘模拟回测（B+A策略，每日选股池）'
    )
    parser.add_argument('--start',   default='20250101',
                        help='回测开始日期 YYYYMMDD（默认 20250101）')
    parser.add_argument('--end',     default='20260430',
                        help='回测结束日期 YYYYMMDD（默认 20260430）')
    parser.add_argument('--capital', type=float, default=None,
                        help='初始资金（默认读取 config.V3_INITIAL_CAPITAL）')
    parser.add_argument('--fivemin-dir', default=None,
                        help='5分钟数据根目录（默认 D:/5min_data）')
    parser.add_argument('--extra-daily-dir', default=None,
                        help='额外日线数据目录，用于补充历史数据（如 D:/daily_data_2021）')
    parser.add_argument('--buy-price', default='high', choices=['high', 'close', 'mid'],
                        help="买入价格模式: high（默认，对齐实盘）/ close（K线收盘价）/ mid（high+close平均价）")
    parser.add_argument('--slippage', type=float, default=0.0005,
                        help='双向滑点单边比例，如0.001表示买入价×1.001、卖出价×0.999（默认0）')
    parser.add_argument('--prev-bar-up', action='store_true',
                        help='买入前过滤：要求上一根5分钟K线非阴线（close >= open）')
    parser.add_argument('--no-open-30', action='store_true',
                        help='开盘30分钟回避：跳过9:35-9:55的买入信号，仃10:00开始执行买入')
    args = parser.parse_args()

    capital = args.capital if args.capital else config.V3_INITIAL_CAPITAL
    run_simulation(args.start, args.end, capital,
                   fivemin_dir=args.fivemin_dir,
                   extra_daily_dir=args.extra_daily_dir,
                   buy_price_mode=args.buy_price,
                   prev_bar_up=args.prev_bar_up,
                   no_open_30=args.no_open_30,
                   slippage=args.slippage)


if __name__ == '__main__':
    main()
