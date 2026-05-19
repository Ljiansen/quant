# -*- coding: utf-8 -*-
"""
快速版 V4 调仓池诊断脚本。
只计算 BA pool + 过滤链，不跑5分钟K线，速度快。
冷却队列（wait_queue）正确跨日维护状态，与回测引擎一致。

输出：
  - 控制台汇总表（每日一行）
  - daily_pool_v4.csv（每行一只股，含过滤阶段标记）

用法：
  python show_daily_pool_v4.py 2026-01-02 2026-04-30
"""
import sys
import os
import json
import csv

sys.path.insert(0, 'd:/miniqmt_quant')

import pandas as pd
from collections import defaultdict

from engine.live_engine_v4 import (
    compute_ba_pool, _load_daily_csv, BASE_DIR,
    classify_trend,
    DAILY_AMOUNT_DAYS, DAILY_MIN_AMOUNT,
    NEW_STOCK_MIN_DAYS, NEW_STOCK_MAX_DAYS,
    COOL_RET_MAX, COOL_DAYS_MAX,
    VOL_RATIO_MIN, VOL_RATIO_MAX,
)

# ── 参数 ──────────────────────────────────────────
START_DATE  = sys.argv[1] if len(sys.argv) > 1 else '2026-01-02'
END_DATE    = sys.argv[2] if len(sys.argv) > 2 else '2026-04-30'
DEBUG_CODE  = sys.argv[3] if len(sys.argv) > 3 else ''   # 如 600084，打印详细过程
OUT_CSV     = os.path.join(BASE_DIR, 'daily_pool_v4.csv')
DAILY_DIR   = 'D:/daily_data'

def dbg(msg):
    if DEBUG_CODE:
        print(f'  [DEBUG {DEBUG_CODE}] {msg}')


# ── 加载日线 ──────────────────────────────────────
print('[DiagV4] 加载日线数据...')
daily_data = {}
for sub in ('SH', 'SZ'):
    sub_dir = os.path.join(DAILY_DIR, sub)
    if not os.path.isdir(sub_dir):
        continue
    for fname in os.listdir(sub_dir):
        if not (fname.startswith('price_') and fname.endswith('.csv')):
            continue
        code = fname[len('price_'):-len('.csv')]
        df = _load_daily_csv(code)
        if df is not None:
            daily_data[code] = df
print(f'[DiagV4] 日线数据：{len(daily_data)} 只')

# ── DEBUG：打印指定股票全部日线数据 ──────────────────
if DEBUG_CODE:
    dbg_df = daily_data.get(DEBUG_CODE)
    if dbg_df is None:
        print(f'  [DEBUG {DEBUG_CODE}] ❌ 未找到该股票日线数据！')
    else:
        print(f'\n{"="*70}')
        print(f'  [DEBUG {DEBUG_CODE}] 全部日线数据（共 {len(dbg_df)} 行）')
        first_date = dbg_df['date'].iloc[0].strftime('%Y-%m-%d')
        last_date  = dbg_df['date'].iloc[-1].strftime('%Y-%m-%d')
        print(f'  [DEBUG {DEBUG_CODE}] 数据区间: {first_date} ~ {last_date}  共{len(dbg_df)}个交易日')
        print(f'{"="*70}')
        pd.set_option('display.max_rows', None)
        pd.set_option('display.float_format', '{:.4f}'.format)
        print(dbg_df[['date','open','high','low','close','volume','amount']].to_string(index=False))
        print(f'{"="*70}\n')

# ── 构造交易日历 ───────────────────────────────────
ref_df = daily_data.get('000001')
if ref_df is None:
    ref_df = next((v for v in daily_data.values() if v is not None), None)
if ref_df is None:
    raise RuntimeError('日线数据为空，请检查 D:/daily_data 目录')

all_dates    = sorted(ref_df['date'].dt.strftime('%Y-%m-%d').tolist())
trading_days = [d for d in all_dates if START_DATE <= d <= END_DATE]

# 未来日期处理：目标日期超出日历末尾（如查询下周一的调仓池）
if not trading_days and START_DATE > all_dates[-1]:
    print(f'[DiagV4] {START_DATE} 为未来日期，将直接计算盘后调仓池（使用最近交易日数据）')
    trading_days = [START_DATE]   # 手动加入，过滤链用 < day_dt 仍能正确取历史数据

print(f'[DiagV4] 回测区间 {START_DATE} ~ {END_DATE}，共 {len(trading_days)} 个交易日\n')


# ── 跨日冷却队列（与引擎保持一致） ─────────────────
wait_queue = {}   # code → {'score': 0, 'since_days': N}

# ── 逐日计算 ──────────────────────────────────────
rows = []

for day_idx, day_str in enumerate(trading_days):
    day_dt   = pd.to_datetime(day_str)
    if day_idx > 0:
        prev_day = trading_days[day_idx - 1]
    else:
        # 第一天：找 START_DATE 之前最近的交易日（用全量 all_dates）
        pre = [d for d in all_dates if d < day_str]
        prev_day = pre[-1] if pre else None

    # ── BA pool ──────────────────────────────────
    if prev_day:
        cache_path = os.path.join(BASE_DIR, f'ba_pool_v4_{prev_day}.json')
        if os.path.exists(cache_path):
            with open(cache_path, encoding='utf-8') as f:
                cached = json.load(f)
            today_pool = [tuple(x) for x in cached['pool']]
            pool_src = 'cache'
        else:
            today_pool = compute_ba_pool(daily_data, prev_day, all_dates, top_n=50)
            pool_src = 'computed'
    else:
        today_pool = []
        pool_src = 'empty'

    raw_pool = [c for c, _, _ in today_pool]
    rank_map  = {c: i for i, (c, _, _) in enumerate(today_pool)}

    # DEBUG: BA pool命中检查
    if DEBUG_CODE in rank_map:
        score_val = next((s for c,r,s in today_pool if c == DEBUG_CODE), 0)
        dbg(f'★ 进入BA池  rank={rank_map[DEBUG_CODE]}  score={score_val}  (来源:{pool_src})')
    else:
        dbg(f'✗ 未进入BA池 (来源:{pool_src}, BA池共{len(raw_pool)}只)')

    # ── ① daily_filter + ② 趋势分类 ─────────────
    pool       = []
    stock_meta = {}
    for code in raw_pool:
        df = daily_data.get(code)
        if df is None:
            if code == DEBUG_CODE: dbg('✗ ①流动性: daily_data中无数据')
            continue
        hist = df[df['date'] < day_dt]
        if len(hist) < DAILY_AMOUNT_DAYS:
            if code == DEBUG_CODE: dbg(f'✗ ①流动性: 历史行数{len(hist)} < 最低要求{DAILY_AMOUNT_DAYS}')
            continue
        avg_amount = float(hist['amount'].iloc[-DAILY_AMOUNT_DAYS:].mean())
        if avg_amount < DAILY_MIN_AMOUNT:
            if code == DEBUG_CODE: dbg(f'✗ ①流动性: 近{DAILY_AMOUNT_DAYS}日均额={avg_amount/1e8:.2f}亿 < 门槛{DAILY_MIN_AMOUNT/1e8:.0f}亿')
            continue
        stype, ma20, slope, cp, low20, vol = classify_trend(hist)
        if stype == 'FALLING':
            if code == DEBUG_CODE: dbg(f'✗ ②趋势: FALLING  close={cp:.3f}  MA20={ma20:.3f}  slope={slope:.4f}')
            continue
        n_rows = int((df['date'] <= day_dt).sum())
        is_new = NEW_STOCK_MIN_DAYS <= n_rows < NEW_STOCK_MAX_DAYS
        pool.append(code)
        stock_meta[code] = {
            'type': stype, 'ma20': ma20, 'slope': slope,
            'price': cp, 'low20': low20, 'vol': vol,
            'rsi': 50.0, 'vol_ratio': 1.0, 'is_new': is_new,
            'ret_20d': 0.0,
        }
        if code == DEBUG_CODE:
            dbg(f'✓ ①②通过: 趋势={stype}  close={cp:.3f}  MA20={ma20:.3f}  '
                f'slope={slope:.4f}  近{DAILY_AMOUNT_DAYS}日均额={avg_amount/1e8:.2f}亿')

    # ── ③ prioritize_rank ────────────────────────
    pool.sort(key=lambda c: rank_map.get(c, 9999))

    # ── ④ 冷却队列（跨日维护 wait_queue） ──────────
    for code in pool:
        df   = daily_data.get(code)
        hist = df[df['date'] < day_dt] if df is not None else pd.DataFrame()
        if len(hist) >= 20:
            c_now = float(hist['close'].iloc[-1])
            c20   = float(hist['close'].iloc[-20])
            stock_meta[code]['ret_20d'] = (c_now / c20 - 1) if c20 > 0 else 0.0
            if code == DEBUG_CODE:
                dbg(f'④ ret_20d计算: 今收={c_now:.3f}  20日前收={c20:.3f}  '
                    f'ret_20d={(c_now/c20-1)*100:.2f}%  门槛={COOL_RET_MAX*100:.0f}%')
        else:
            stock_meta[code]['ret_20d'] = 0.0
            if code == DEBUG_CODE:
                dbg(f'④ ret_20d=0（历史不足20行，len={len(hist)}）')

    # 冷却队列过期清理
    wait_queue = {c: v for c, v in wait_queue.items()
                  if v.get('since_days', 0) < COOL_DAYS_MAX}
    cooled_list = []
    for code in pool:
        r = stock_meta[code]['ret_20d']
        if r > COOL_RET_MAX:
            if code not in wait_queue:
                wait_queue[code] = {'score': 0, 'since_days': 0}
            if code == DEBUG_CODE:
                dbg(f'✗ ④过热: ret_20d={r*100:.2f}% > {COOL_RET_MAX*100:.0f}%  → 进入冷却队列')
        else:
            if code in wait_queue:
                cooled_list.append(code)
                del wait_queue[code]
                if code == DEBUG_CODE:
                    dbg(f'✓ ④退热: 从冷却队列释放，进入cooled_list优先候选')
            else:
                if code == DEBUG_CODE:
                    dbg(f'✓ ④正常: ret_20d={r*100:.2f}% ≤ {COOL_RET_MAX*100:.0f}%  不过热')
    for v in wait_queue.values():
        v['since_days'] = v.get('since_days', 0) + 1

    not_hot = [c for c in pool if stock_meta[c]['ret_20d'] <= COOL_RET_MAX]
    seen = set()
    buy_candidates = []
    for c in cooled_list + not_hot:
        if c not in seen:
            seen.add(c)
            buy_candidates.append(c)

    hot_cnt = len(pool) - len(not_hot)

    # ── ⑤ vol_ratio ──────────────────────────────
    vol_ok = []
    for code in buy_candidates:
        df = daily_data.get(code)
        if df is None:
            if code == DEBUG_CODE: dbg('✗ ⑤量比: daily_data中无数据')
            continue
        hist = df[df['date'] < day_dt]
        if len(hist) < 21:
            if code == DEBUG_CODE: dbg(f'✗ ⑤量比: 历史行数{len(hist)} < 21')
            continue
        yest_vol = float(hist['volume'].iloc[-1])
        ma20_vol = float(hist['volume'].iloc[-21:-1].mean())
        if ma20_vol <= 0:
            if code == DEBUG_CODE: dbg('✗ ⑤量比: MA20成交量=0')
            continue
        vr = yest_vol / ma20_vol
        stock_meta[code]['vol_ratio'] = round(vr, 2)
        if code == DEBUG_CODE:
            status = '✓' if VOL_RATIO_MIN <= vr <= VOL_RATIO_MAX else '✗'
            dbg(f'{status} ⑤量比: 昨量={yest_vol:.0f}  MA20量={ma20_vol:.0f}  '
                f'vol_ratio={vr:.2f}  要求[{VOL_RATIO_MIN},{VOL_RATIO_MAX}]  → {"通过" if VOL_RATIO_MIN <= vr <= VOL_RATIO_MAX else "过滤"}')
        if VOL_RATIO_MIN <= vr <= VOL_RATIO_MAX:
            vol_ok.append(code)

    print(f'[{day_str}] BA={len(raw_pool)}({pool_src}) '
          f'daily_ok={len(pool)} hot={hot_cnt} '
          f'vol_ok={len(vol_ok)} '
          f'→ [{", ".join(vol_ok)}]')

    # ── 打印完整50只BA池 ──────────────────────────
    if raw_pool:
        print(f'  {"排名":<4} {"代码":<8} {"趋势":<8} {"收盘":>7} {"MA20":>7} '
              f'{"20日涨幅":>8} {"量比":>6} {"daily_ok":>8} {"最终":>4}')
        print(f'  {"-"*72}')
        for rank_i, (code, _, score) in enumerate(today_pool):
            meta    = stock_meta.get(code, {})
            is_dok  = code in stock_meta
            is_vol  = code in vol_ok
            trend   = meta.get('type', '—')
            price   = meta.get('price', 0)
            ma20    = meta.get('ma20', 0)
            ret20   = meta.get('ret_20d', 0)
            vr      = meta.get('vol_ratio', 0)
            final   = '★' if is_vol else ('过热' if meta.get('ret_20d',0) > COOL_RET_MAX else '—')
            print(f'  {rank_i:<4} {code:<8} {trend:<8} {price:>7.3f} {ma20:>7.3f} '
                  f'{ret20*100:>7.1f}% {vr:>6.2f} {"✓" if is_dok else "✗":>8} {final:>4}')
    print()

    # ── 记录到CSV ─────────────────────────────────
    for code in raw_pool:
        meta = stock_meta.get(code, {})
        rows.append({
            'date':         day_str,
            'code':         code,
            'rank':         rank_map.get(code, 999),
            'in_daily_ok':  int(code in stock_meta),
            'hot_filtered': int(code in stock_meta and meta.get('ret_20d', 0) > COOL_RET_MAX),
            'in_cooled':    int(code in cooled_list),
            'vol_ok':       int(code in vol_ok),
            'trend':        meta.get('type', ''),
            'ret_20d_pct':  round(meta.get('ret_20d', 0) * 100, 2),
            'vol_ratio':    meta.get('vol_ratio', ''),
            'price':        round(meta.get('price', 0), 3),
        })

# ── 写 CSV ────────────────────────────────────────
fieldnames = ['date','code','rank','in_daily_ok','hot_filtered',
              'in_cooled','vol_ok','trend','ret_20d_pct','vol_ratio','price']
with open(OUT_CSV, 'w', newline='', encoding='utf-8-sig') as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)

print(f'\n[DiagV4] 详细表格已保存：{OUT_CSV}  ({len(rows)} 行)')

# ── 汇总表 ────────────────────────────────────────
daily_sum = defaultdict(lambda: {'ba':0,'dok':0,'hot':0,'vol':0,'codes':[]})
for r in rows:
    d = r['date']
    daily_sum[d]['ba']  += 1
    daily_sum[d]['dok'] += r['in_daily_ok']
    daily_sum[d]['hot'] += r['hot_filtered']
    daily_sum[d]['vol'] += r['vol_ok']
    if r['vol_ok']:
        daily_sum[d]['codes'].append(r['code'])

print('\n' + '='*80)
print(f'{"日期":<12} {"BA池":>4} {"daily_ok":>8} {"过热":>4} {"vol_ok":>6}  最终候选')
print('='*80)
for day_str in trading_days:
    s = daily_sum[day_str]
    codes_str = ' '.join(s['codes']) if s['codes'] else '(无)'
    print(f"{day_str:<12} {s['ba']:>4} {s['dok']:>8} {s['hot']:>4} {s['vol']:>6}  {codes_str}")
