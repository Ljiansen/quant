# -*- coding: utf-8 -*-
"""计算A股历史每日上涨/下跌比例"""
import sys, os
sys.path.insert(0, 'd:/miniqmt_quant')

import pandas as pd
from engine.live_engine_v4 import _load_daily_csv

print("加载日线数据（全市场5231只）...")
results = []
for sub in ['SH', 'SZ']:
    d = f'D:/daily_data/{sub}'
    if not os.path.isdir(d):
        continue
    for fname in os.listdir(d):
        if fname.startswith('price_') and fname.endswith('.csv'):
            df = _load_daily_csv(fname[6:-4])
            if df is not None and len(df) > 1:
                df = df.sort_values('date').copy()
                df['chg'] = df['close'].pct_change()
                results.append(df[['date', 'chg']].dropna())

print(f"完成，共 {len(results)} 只股票")

all_chg = pd.concat(results, ignore_index=True)

# 按日期统计
daily = all_chg.groupby('date').agg(
    total=('chg', 'count'),
    up=('chg', lambda x: (x > 0).sum()),
    down=('chg', lambda x: (x < 0).sum()),
    flat=('chg', lambda x: (x == 0).sum()),
).copy()
daily['up_pct'] = daily['up'] / daily['total']
daily['down_pct'] = daily['down'] / daily['total']

# 全时段
print(f"\n=== 全时段 ({len(daily)} 个交易日) ===")
print(f"平均上涨比例: {daily['up_pct'].mean():.1%}")
print(f"平均下跌比例: {daily['down_pct'].mean():.1%}")

# 近250个交易日（约1年）
recent = daily.tail(250)
print(f"\n=== 近250个交易日（约1年）===")
print(f"平均上涨比例: {recent['up_pct'].mean():.1%}")
print(f"平均下跌比例: {recent['down_pct'].mean():.1%}")
best_day = recent['up_pct'].idxmax()
worst_day = recent['up_pct'].idxmin()
print(f"上涨最多的一天: {best_day.strftime('%Y-%m-%d')} = {recent.loc[best_day,'up_pct']:.1%}")
print(f"上涨最少的一天: {worst_day.strftime('%Y-%m-%d')} = {recent.loc[worst_day,'up_pct']:.1%}")

# 本周各日
print("\n=== 本周各交易日上涨比例 ===")
this_week = daily[daily.index >= pd.Timestamp('2026-05-07')]
for dt, row in this_week.iterrows():
    bar = '█' * int(row['up_pct'] * 20)
    print(f"  {dt.strftime('%Y-%m-%d')}  {bar:<20}  上涨{row['up_pct']:.1%}  "
          f"({int(row['up'])}/{int(row['total'])}只上涨，{int(row['down'])}只下跌)")

# V4胜率对比
print("\n=== V4策略 vs 市场基准对比 ===")
v4_stats = {
    '2026-05-07': (2, 3),
    '2026-05-08': (2, 3),
    '2026-05-11': (5, 8),
    '2026-05-12': (5, 10),
    '2026-05-13': (2, 6),
    '2026-05-14': (0, 7),
    '2026-05-15': (5, 9),
}
print(f"  {'日期':<12} {'V4胜率':<12} {'市场基准':<12} {'相对市场'}")
print(f"  {'-'*52}")
for date_str, (up, total) in v4_stats.items():
    v4_wr = up / total if total > 0 else 0
    dt = pd.Timestamp(date_str)
    mkt_wr = daily.loc[dt, 'up_pct'] if dt in daily.index else 0
    diff = v4_wr - mkt_wr
    arrow = '▲' if diff >= 0 else '▼'
    print(f"  {date_str:<12} {v4_wr:.0%}({up}/{total})    {mkt_wr:.1%}        {arrow}{abs(diff):.1%}")

v4_total_up = sum(v[0] for v in v4_stats.values())
v4_total = sum(v[1] for v in v4_stats.values())
avg_mkt = daily.loc[daily.index >= pd.Timestamp('2026-05-07'), 'up_pct'].mean()
print(f"\n  V4整体胜率: {v4_total_up}/{v4_total} = {v4_total_up/v4_total:.1%}")
print(f"  市场本周均值: {avg_mkt:.1%}")
print(f"  相对市场: {'▲' if v4_total_up/v4_total >= avg_mkt else '▼'}{abs(v4_total_up/v4_total - avg_mkt):.1%}")

print("\n[DONE]")
