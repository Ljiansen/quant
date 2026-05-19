# -*- coding: utf-8 -*-
"""
逐日导出 BA 池原始得分与排名到 CSV。
每行对应一只股票在某天的 BA 池排名，不做过滤链。

输出文件：ba_pool_scores.csv
  date     - 交易日（该 BA 池生效当天）
  rank     - BA 池排名（1=最高分）
  code     - 股票代码
  score    - BA 得分（近 lookback 天内满足条件的天数）
  ref_date - 计算所用参考日（= 上一交易日）

用法：
  python export_ba_pool_scores.py 2026-01-02 2026-04-30
"""
import sys
import os
import csv

sys.path.insert(0, 'd:/miniqmt_quant')
import pandas as pd

from engine.live_engine_v4 import (
    compute_ba_pool, _load_daily_csv, BASE_DIR,
)

# ── 参数 ──────────────────────────────────────────────
START_DATE = sys.argv[1] if len(sys.argv) > 1 else '2026-01-02'
END_DATE   = sys.argv[2] if len(sys.argv) > 2 else '2026-04-30'
OUT_CSV    = os.path.join(BASE_DIR, 'ba_pool_scores.csv')
DAILY_DIR  = 'D:/daily_data'

# ── 加载日线 ───────────────────────────────────────────
print('[export] 加载日线数据...')
daily_data = {}
for sub in ('SH', 'SZ'):
    sub_dir = os.path.join(DAILY_DIR, sub)
    if not os.path.isdir(sub_dir):
        continue
    for fname in os.listdir(sub_dir):
        if fname.startswith('price_') and fname.endswith('.csv'):
            code = fname[len('price_'):-len('.csv')]
            df = _load_daily_csv(code)
            if df is not None:
                daily_data[code] = df
print(f'[export] 日线数据：{len(daily_data)} 只')

# ── 构造交易日历 ───────────────────────────────────────
ref_df = daily_data.get('000001')
if ref_df is None:
    ref_df = next((v for v in daily_data.values() if v is not None), None)
if ref_df is None:
    raise RuntimeError('日线数据为空，请检查 D:/daily_data 目录')

all_dates    = sorted(ref_df['date'].dt.strftime('%Y-%m-%d').tolist())
trading_days = [d for d in all_dates if START_DATE <= d <= END_DATE]

if not trading_days:
    print(f'[export] 警告：{START_DATE}~{END_DATE} 无交易日，退出')
    sys.exit(0)

print(f'[export] 区间 {START_DATE} ~ {END_DATE}，共 {len(trading_days)} 个交易日\n')

# ── 逐日计算 BA 池 ─────────────────────────────────────
rows = []
for day_idx, day_str in enumerate(trading_days):
    # 确定参考日（上一交易日）
    if day_idx > 0:
        prev_day = trading_days[day_idx - 1]
    else:
        pre = [d for d in all_dates if d < day_str]
        prev_day = pre[-1] if pre else None

    if prev_day is None:
        print(f'[{day_str}] 无前一交易日，跳过')
        continue

    pool = compute_ba_pool(daily_data, prev_day, all_dates, top_n=50)
    print(f'[{day_str}] ref={prev_day}  BA池={len(pool)}只  '
          f'top3: {" ".join(f"{c}({s})" for c,_,s in pool[:3])}')

    for code, rank, score in pool:
        rows.append({
            'date':     day_str,
            'rank':     rank,
            'code':     code,
            'score':    score,
            'ref_date': prev_day,
        })

# ── 写 CSV ─────────────────────────────────────────────
fieldnames = ['date', 'rank', 'code', 'score', 'ref_date']
with open(OUT_CSV, 'w', newline='', encoding='utf-8-sig') as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)

total_days = len(set(r['date'] for r in rows))
print(f'\n[export] 完成！写入 {len(rows)} 行，共 {total_days} 个交易日')
print(f'[export] 输出文件：{OUT_CSV}')
