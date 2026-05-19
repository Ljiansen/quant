# -*- coding: utf-8 -*-
"""V4 过滤链离线测试脚本（方案2）"""
import sys, json, os
sys.path.insert(0, 'd:/miniqmt_quant')

from engine.live_engine_v4 import LiveEngineV4
from datetime import date

engine = LiveEngineV4()
today_str = date.today().strftime('%Y-%m-%d')
print(f"测试日期: {today_str}")

# 1. 加载BA pool缓存（跳过重算）
cache_path = f'd:/miniqmt_quant/ba_pool_v4_{today_str}.json'
if not os.path.exists(cache_path):
    print(f"[ERROR] 缓存文件不存在: {cache_path}")
    print("请先运行方案3生成BA pool缓存")
    sys.exit(1)

with open(cache_path, encoding='utf-8') as f:
    cached = json.load(f)
engine.today_pool = [tuple(x) for x in cached['pool']]
print(f"[OK] BA pool加载: {len(engine.today_pool)}只  (ref_date={cached.get('ref_date')})")

# 2. 加载日线数据
engine._load_all_daily_data(today_str)
print(f"[OK] 日线数据: {len(engine.daily_data)}只")

# 3. 运行过滤链
engine._build_filter_chain(today_str)
print(f"[OK] 过滤链完成")
print(f"\nbuy_candidates ({len(engine.buy_candidates)}只): {engine.buy_candidates}")

# 4. 打印每只详情
if engine.buy_candidates:
    print("\n--- 候选股详情 ---")
    for c in engine.buy_candidates:
        m = engine.stock_meta.get(c, {})
        trend  = m.get('type', '?')
        ret20  = m.get('ret_20d', 0)
        slope  = m.get('slope', 0)
        is_new = m.get('is_new', False)
        print(f"  {c}  趋势={trend}  ret_20d={ret20:.1%}  slope={slope:.3f}  新股={is_new}")
else:
    print("\n[警告] buy_candidates为空！逐步检查：")
    raw = [c for c, _, _ in engine.today_pool]
    print(f"  BA pool raw: {len(raw)}只")

    # 检查每步过滤后剩余数量
    import pandas as pd
    today_dt = pd.to_datetime(today_str)
    from engine.live_engine_v4 import (DAILY_AMOUNT_DAYS, DAILY_MIN_AMOUNT,
                                        classify_trend, VOL_RATIO_MIN, VOL_RATIO_MAX,
                                        COOL_RET_MAX)
    step1 = []
    step2 = []
    for code in raw:
        df = engine.daily_data.get(code)
        if df is None: continue
        hist = df[df['date'] < today_dt]
        if len(hist) < DAILY_AMOUNT_DAYS: continue
        avg_amt = float(hist['amount'].iloc[-DAILY_AMOUNT_DAYS:].mean())
        if avg_amt < DAILY_MIN_AMOUNT: continue
        step1.append(code)
        stype, *_ = classify_trend(hist)
        if stype != 'FALLING':
            step2.append(code)

    print(f"  ①daily_filter后: {len(step1)}只")
    print(f"  ②排除FALLING后: {len(step2)}只")

    step5 = []
    for code in step2:
        df = engine.daily_data.get(code)
        hist = df[df['date'] < today_dt]
        if len(hist) < 21: continue
        yv = float(hist['volume'].iloc[-1])
        mv = float(hist['volume'].iloc[-21:-1].mean())
        if mv <= 0: continue
        vr = yv / mv
        if VOL_RATIO_MIN <= vr <= VOL_RATIO_MAX:
            step5.append(code)
    print(f"  ⑤vol_ratio过滤后: {len(step5)}只")

print("\n[DONE] 测试完成")
