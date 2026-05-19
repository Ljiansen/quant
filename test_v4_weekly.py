# -*- coding: utf-8 -*-
"""V4 本周每日选股回测验证脚本
检验过滤链选出的候选股在当天的实际涨跌表现
"""
import sys, os
sys.path.insert(0, 'd:/miniqmt_quant')

import pandas as pd
from engine.live_engine_v4 import (
    LiveEngineV4, compute_ba_pool, _load_daily_csv
)

# ── 1. 加载日线数据 ──────────────────────────────────────
print("加载日线数据...")
daily_data = {}
for sub in ['SH', 'SZ']:
    d = os.path.join('D:/daily_data', sub)
    if not os.path.isdir(d):
        continue
    for fname in os.listdir(d):
        if fname.startswith('price_') and fname.endswith('.csv'):
            code = fname[6:-4]
            df = _load_daily_csv(code)
            if df is not None:
                daily_data[code] = df

print(f"加载完毕: {len(daily_data)}只")

# ── 2. 构建交易日历 ──────────────────────────────────────
ref_df = daily_data.get('000001')
if ref_df is None:
    ref_df = next(iter(daily_data.values()))
all_dates = sorted(ref_df['date'].dt.strftime('%Y-%m-%d').tolist())

# 取本周交易日（最近5个交易日，不含今天如果今天无数据）
recent_dates = all_dates[-7:]   # 取最近7个交易日
print(f"\n最近交易日: {recent_dates}")

# ── 3. 对每个交易日跑过滤链 ─────────────────────────────
print("\n" + "=" * 70)
print(f"{'日期':<12} {'候选股':<60} {'结果'}")
print("=" * 70)

for day_str in recent_dates:
    day_dt = pd.to_datetime(day_str)
    day_idx = all_dates.index(day_str)
    if day_idx < 2:
        continue
    prev_day = all_dates[day_idx - 1]

    # BA pool（用前一日作ref_date）
    pool = compute_ba_pool(daily_data, prev_day, all_dates, top_n=50)
    if not pool:
        print(f"{day_str:<12} BA pool为空，跳过")
        continue

    # 过滤链
    engine = LiveEngineV4()
    engine.daily_data = daily_data
    engine.all_trading_dates = all_dates
    engine.today_pool = pool
    engine._build_filter_chain(day_str)

    candidates = engine.buy_candidates

    if not candidates:
        print(f"{day_str:<12} 无候选股（过滤链全部过滤）")
        continue

    # 检查当日涨跌
    print(f"\n{'─'*70}")
    print(f"  {day_str}  候选股 {len(candidates)} 只:")
    print(f"  {'代码':<10} {'当日涨跌':<10} {'开盘':<10} {'收盘':<10} {'昨收':<10} {'趋势':<12} {'ret_20d'}")
    print(f"  {'─'*80}")

    for code in candidates:
        df = daily_data.get(code)
        if df is None:
            continue
        # 当日数据
        row_today = df[df['date'] == day_dt]
        # 昨日收盘
        hist_prev = df[df['date'] < day_dt]

        if row_today.empty or hist_prev.empty:
            print(f"  {code:<10} 无当日数据")
            continue

        today_open  = float(row_today['open'].iloc[0])
        today_close = float(row_today['close'].iloc[0])
        prev_close  = float(hist_prev['close'].iloc[-1])
        chg = (today_close - prev_close) / prev_close if prev_close > 0 else 0
        chg_open = (today_open - prev_close) / prev_close if prev_close > 0 else 0

        m = engine.stock_meta.get(code, {})
        trend  = m.get('type', '?')
        ret20  = m.get('ret_20d', 0)

        arrow = '▲' if chg >= 0 else '▼'
        print(f"  {code:<10} {arrow}{chg:+.2%}      "
              f"{today_open:<10.3f} {today_close:<10.3f} {prev_close:<10.3f} "
              f"{trend:<12} {ret20:.1%}")

print("\n" + "=" * 70)
print("[DONE]")
