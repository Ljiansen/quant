# -*- coding: utf-8 -*-
"""
V4 BA池盘后预计算脚本（每日收盘后运行）

功能：
  1. 以今日（或指定日期）为 ref_date，计算 BA 池并保存到
     d:/miniqmt_quant/ba_pool_v4_{YYYY-MM-DD}.json
  2. 回测时 offline_sim_engine_v4 会优先加载缓存；实盘时
     live_engine_v4 也会优先读缓存，找不到才实时计算兜底

运行时机：每天 15:30 收盘后，日线数据更新完成之后再运行本脚本

用法：
  python compute_ba_pool_v4.py              # 以今天为 ref_date
  python compute_ba_pool_v4.py 2026-04-30   # 指定 ref_date
  python compute_ba_pool_v4.py 2026-01-02 2026-04-30  # 批量补算一段区间
"""
import sys
import os
from datetime import datetime, timedelta

sys.path.insert(0, 'd:/miniqmt_quant')

import pandas as pd
from engine.live_engine_v4 import (
    _load_daily_csv, _now_str, precompute_ba_pool_save, BASE_DIR
)

DAILY_DIR = 'D:/daily_data'


# ── 加载日线数据 ──────────────────────────────────────────
def load_all_daily_data() -> dict:
    print(f'[{_now_str()}] 加载日线数据...')
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
    print(f'[{_now_str()}] 日线数据：{len(daily_data)} 只')
    return daily_data


# ── 构造交易日历 ───────────────────────────────────────────
def build_trading_dates(daily_data: dict):
    ref = daily_data.get('000001')
    if ref is None:
        ref = next((v for v in daily_data.values() if v is not None), None)
    if ref is None:
        raise RuntimeError('日线数据为空，请检查 D:/daily_data 目录')
    return sorted(ref['date'].dt.strftime('%Y-%m-%d').tolist())


# ── 主逻辑 ────────────────────────────────────────────────
def main():
    args = sys.argv[1:]

    daily_data = load_all_daily_data()
    all_dates  = build_trading_dates(daily_data)

    if len(args) == 0:
        # 默认：今天
        today_str = datetime.today().strftime('%Y-%m-%d')
        dates_to_compute = [today_str]
    elif len(args) == 1:
        # 指定单日
        dates_to_compute = [args[0]]
    else:
        # 批量区间
        start, end = args[0], args[1]
        dates_to_compute = [d for d in all_dates if start <= d <= end]
        if not dates_to_compute:
            print(f'[{_now_str()}] 警告：{start}~{end} 无交易日，退出')
            return

    print(f'[{_now_str()}] 待计算 {len(dates_to_compute)} 天，'
          f'区间 {dates_to_compute[0]} ~ {dates_to_compute[-1]}\n')

    ok, skip = 0, 0
    for ref_date in dates_to_compute:
        cache_path = os.path.join(BASE_DIR, f'ba_pool_v4_{ref_date}.json')
        if os.path.exists(cache_path):
            print(f'[{_now_str()}] {ref_date} 缓存已存在，跳过 ({cache_path})')
            skip += 1
            continue
        # 检查 ref_date 是否有日线数据（节假日跳过）
        ref_has_data = any(
            not df[df['date'] == pd.to_datetime(ref_date)].empty
            for df in list(daily_data.values())[:5]
        )
        if not ref_has_data:
            # 容忍：compute_ba_pool 内部会自动找最近交易日
            pass
        try:
            precompute_ba_pool_save(ref_date, daily_data, all_dates, top_n=50)
            ok += 1
        except Exception as e:
            print(f'[{_now_str()}] {ref_date} 计算失败: {e}')

    print(f'\n[{_now_str()}] 完成：新计算={ok} 已跳过={skip}')
    print(f'[{_now_str()}] 缓存目录：{BASE_DIR}')


if __name__ == '__main__':
    main()
