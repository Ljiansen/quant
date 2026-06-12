# -*- coding: utf-8 -*-
"""
明日买入候选预测

读取 BA 池 JSON，模拟全链路盘前过滤（流动性→趋势→过热→量比→弱市低动量），
推算上证指数 regime，输出通过过滤的候选股票列表。

可作为模块导入:
    from check_monday_candidates import predict_next_day_candidates
    result = predict_next_day_candidates('ba_pool_v4_2026-06-12.json')

也可命令行运行:
    python check_monday_candidates.py [ba_pool.json]
"""
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import numpy as np
from collections import Counter

from engine.live_engine_v4 import (
    classify_trend, DAILY_MIN_AMOUNT, DAILY_AMOUNT_DAYS,
    VOL_RATIO_MIN, VOL_RATIO_MAX, COOL_RET_MAX,
    NEW_STOCK_MIN_DAYS, NEW_STOCK_MAX_DAYS,
)

# ── 日线加载 ──
_DAILY_DATA_DIR = 'D:/daily_data'


def _load_daily(symbol):
    if symbol.startswith('6'):
        path = os.path.join(_DAILY_DATA_DIR, 'SH', f'price_{symbol}.csv')
    else:
        path = os.path.join(_DAILY_DATA_DIR, 'SZ', f'price_{symbol}.csv')
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path)
    if df.empty or len(df) <= 1:
        return None
    df = df.rename(columns={'timetag': 'date', 'volumn': 'volume'})
    df['date'] = pd.to_datetime(df['date'].astype(str), format='%Y%m%d')
    cols = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount']
    for col in cols:
        if col not in df.columns:
            return None
    return df[cols].sort_values('date').reset_index(drop=True)


def _load_sh_index():
    path = os.path.join(_DAILY_DATA_DIR, 'SH', 'price_sh000001.csv')
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path)
    df = df.rename(columns={'timetag': 'date'})
    df['date'] = pd.to_datetime(df['date'].astype(str), format='%Y%m%d')
    df = df.sort_values('date').reset_index(drop=True)
    for col in ['open', 'high', 'low', 'close', 'volume']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
    return df


def _compute_regime(sh_hist):
    """从上证指数历史推算 regime，返回 (regime_str, max_positions, sh_info_dict)"""
    if sh_hist is None or len(sh_hist) < 61:
        return 'unknown', 0, {}

    closes = sh_hist['close'].values
    ma20_v = float(np.mean(closes[-20:]))
    ma60_v = float(np.mean(closes[-60:]))
    last_c = float(closes[-1])
    ret_5d = float(closes[-1] / closes[-6] - 1) if len(closes) > 5 else 0
    ret_30d = float(closes[-1] / closes[-31] - 1) if len(closes) > 30 else 0
    ret_60d = float(closes[-1] / closes[-61] - 1) if len(closes) > 60 else 0
    below = last_c < ma20_v
    close_gt_ma60 = last_c > ma60_v

    streak = 0
    if below:
        for i in range(len(closes) - 1, -1, -1):
            if i < 20:
                break
            m20 = float(np.mean(closes[max(0, i - 19): i + 1]))
            if closes[i] < m20:
                streak += 1
            else:
                break

    log_rets = np.diff(np.log(sh_hist['close'].values[-31:]))
    vol_30d = float(np.std(log_rets)) if len(sh_hist) >= 30 else 0

    sh_info = {
        'close': last_c, 'ma20': ma20_v, 'ma60': ma60_v,
        'below_ma20': below, 'streak': streak, 'close_gt_ma60': close_gt_ma60,
        'ret_5d': ret_5d, 'ret_30d': ret_30d, 'ret_60d': ret_60d,
        'vol_30d': vol_30d,
        'data_date': sh_hist['date'].iloc[-1].strftime('%Y-%m-%d'),
    }

    # regime 判定 (与 _g34_regime_decide 对齐)
    if ret_30d < -0.06:
        regime, mp = 'panic_30d', 0
    elif vol_30d > 0.022:
        regime, mp = 'vol_30d', 0
    elif ret_60d < -0.05:
        regime, mp = 'macro_bear_60d', 0
    elif not below:
        regime, mp = 'bull', 5
    else:
        init_bnd = 3 if close_gt_ma60 else 5
        if streak <= init_bnd:
            regime, mp = 'chop_init', 4
        elif ret_5d < -0.01:
            regime, mp = 'chop_else_ret5', 0
        else:
            regime, mp = 'chop_else', 3

    return regime, mp, sh_info


# ══════════════════════════════════════════════════════════════
# 核心函数
# ══════════════════════════════════════════════════════════════

WEAK_MKT_MOMENTUM_THRESHOLD = 0.20


def predict_next_day_candidates(ba_pool_path: str) -> dict:
    """
    读取 BA pool JSON，模拟全链路盘前过滤，返回结构化结果。

    Returns:
        {
            'candidates': [{'code', 'type', 'price', 'ret_20d', 'vol_ratio', 'ma20'}, ...],
            'regime': str,
            'max_positions': int,
            'sh_status': dict,
            'filtered': {code: reason, ...},
            'filter_stats': {reason_category: count, ...},
            'pool_count': int,
        }
    """
    with open(ba_pool_path, 'r', encoding='utf-8') as f:
        pool_data = json.load(f)
    raw_codes = [item[0] for item in pool_data['pool']]
    ref_date = pool_data.get('ref_date', '?')

    # 推算 next trading day (ref_date 的下一个工作日占位, 实际用 ref_date+1dt 过滤)
    ref_dt = pd.to_datetime(ref_date)
    today_dt = ref_dt + pd.Timedelta(days=1)  # 用 ref_date+1 做 < today_dt 过滤

    # 加载日线
    daily_data = {}
    for code in raw_codes:
        df = _load_daily(code)
        if df is not None and not df.empty:
            daily_data[code] = df
    sh_df = _load_sh_index()

    # ── ①② 流动性 + 趋势 ──
    pool = []
    meta = {}
    filtered = {}
    cnt = {'no_data': 0, 'low_hist': 0, 'low_amt': 0, 'falling': 0}

    for code in raw_codes:
        df = daily_data.get(code)
        if df is None:
            cnt['no_data'] += 1
            filtered[code] = '无日线数据'
            continue
        hist = df[df['date'] < today_dt]
        if len(hist) < DAILY_AMOUNT_DAYS:
            cnt['low_hist'] += 1
            filtered[code] = '历史数据不足'
            continue
        avg_amount = float(hist['amount'].iloc[-DAILY_AMOUNT_DAYS:].mean())
        if avg_amount < DAILY_MIN_AMOUNT:
            cnt['low_amt'] += 1
            filtered[code] = f'流动性不足({avg_amount / 1e8:.1f}亿)'
            continue
        stype, ma20, slope, cp, low20, vol = classify_trend(hist)
        if stype == 'FALLING':
            cnt['falling'] += 1
            filtered[code] = '下跌趋势'
            continue
        n_rows = int((df['date'] <= today_dt).sum())
        is_new = NEW_STOCK_MIN_DAYS <= n_rows < NEW_STOCK_MAX_DAYS
        pool.append(code)
        meta[code] = {'type': stype, 'ma20': ma20, 'price': cp,
                      'is_new': is_new, 'ret_20d': 0.0, 'vol_ratio': 0.0}

    # ── ④ 过热 ──
    for code in pool:
        df = daily_data.get(code)
        hist = df[df['date'] < today_dt]
        if len(hist) >= 20:
            c_now = float(hist['close'].iloc[-1])
            c20 = float(hist['close'].iloc[-20])
            meta[code]['ret_20d'] = (c_now / c20 - 1) if c20 > 0 else 0.0

    not_hot = [c for c in pool if meta[c]['ret_20d'] <= COOL_RET_MAX]
    for code in pool:
        if code not in set(not_hot):
            filtered[code] = f'过热({meta[code]["ret_20d"] * 100:.0f}%/20d)'

    # ── ⑤ vol_ratio ──
    vr_ok = []
    for code in not_hot:
        df = daily_data.get(code)
        if df is None:
            continue
        hist = df[df['date'] < today_dt]
        if len(hist) < 21:
            vr_ok.append(code)
            continue
        yest_vol = float(hist['volume'].iloc[-1])
        ma20_vol = float(hist['volume'].iloc[-21:-1].mean())
        if ma20_vol <= 0:
            vr_ok.append(code)
            continue
        vr = yest_vol / ma20_vol
        meta[code]['vol_ratio'] = vr
        if VOL_RATIO_MIN <= vr <= VOL_RATIO_MAX:
            vr_ok.append(code)
        else:
            filtered[code] = f'量比异常({vr:.2f}x)'

    # ── ⑥ 弱市低动量 ──
    sh_below = False
    if sh_df is not None:
        sh_hist = sh_df[sh_df['date'] < today_dt]
        if len(sh_hist) >= 20:
            sh_ma20 = float(np.mean(sh_hist['close'].values[-20:]))
            sh_below = float(sh_hist['close'].values[-1]) < sh_ma20

    candidates = []
    if sh_below:
        for code in vr_ok:
            r20 = meta[code].get('ret_20d', 0)
            if r20 < WEAK_MKT_MOMENTUM_THRESHOLD:
                filtered[code] = f'弱市低动量({r20 * 100:.0f}%/20d)'
            else:
                candidates.append(code)
    else:
        candidates = list(vr_ok)

    # ── regime ──
    regime, max_pos, sh_status = _compute_regime(
        sh_df[sh_df['date'] < today_dt] if sh_df is not None else None)

    # 构建结果
    candidate_list = []
    for code in candidates:
        m = meta[code]
        candidate_list.append({
            'code': code,
            'type': m['type'],
            'price': m['price'],
            'ret_20d': m['ret_20d'],
            'vol_ratio': m.get('vol_ratio', 0),
            'ma20': m['ma20'],
        })

    filter_stats = dict(Counter(v.split('(')[0] for v in filtered.values()))

    return {
        'candidates': candidate_list,
        'regime': regime,
        'max_positions': max_pos,
        'sh_status': sh_status,
        'filtered': filtered,
        'filter_stats': filter_stats,
        'pool_count': len(raw_codes),
        'ref_date': ref_date,
    }


# ══════════════════════════════════════════════════════════════
# 命令行入口
# ══════════════════════════════════════════════════════════════

def _print_result(result: dict):
    """格式化打印结果（命令行模式）"""
    print(f"BA池: ref_date={result['ref_date']}, 候选={result['pool_count']}只")

    cands = result['candidates']
    regime = result['regime']
    max_pos = result['max_positions']
    sh = result.get('sh_status', {})

    print(f"\n{'=' * 60}")
    print(f"Regime: {regime} (max_pos={max_pos})")
    if sh:
        print(f"上证: {sh.get('close', 0):.2f}  MA20={sh.get('ma20', 0):.2f}  "
              f"MA60={sh.get('ma60', 0):.2f}")
        print(f"  跌破MA20={sh.get('below_ma20', '?')}  streak={sh.get('streak', '?')}  "
              f"close_gt_ma60={sh.get('close_gt_ma60', '?')}")
        print(f"  ret_5d={sh.get('ret_5d', 0) * 100:.2f}%  "
              f"ret_30d={sh.get('ret_30d', 0) * 100:.2f}%  "
              f"ret_60d={sh.get('ret_60d', 0) * 100:.2f}%  "
              f"vol_30d={sh.get('vol_30d', 0):.4f}")
        print(f"  数据截至: {sh.get('data_date', '?')}")
    print(f"{'=' * 60}")

    print(f"\n通过盘前过滤的候选: {len(cands)} 只")
    if cands:
        print(f"{'代码':>8s} {'趋势':>12s} {'现价':>8s} {'20日涨幅':>8s} {'量比':>6s}")
        print('-' * 50)
        for c in cands:
            print(f"{c['code']:>8s} {c['type']:>12s} {c['price']:>8.2f} "
                  f"{c['ret_20d'] * 100:>7.1f}% {c['vol_ratio']:>5.2f}x")

    if max_pos == 0:
        print(f"\n⚠️  Regime={regime} → 空仓(禁止买入)")

    print(f"\n注: gap_min(高开+0.5%) + 5min K信号 需开盘后引擎实时判断。")

    filt = result['filtered']
    stats = result['filter_stats']
    print(f"\n被过滤: {len(filt)} 只  统计: {stats}")
    for code, reason in sorted(filt.items(), key=lambda x: x[1]):
        print(f"  {code}: {reason}")


if __name__ == '__main__':
    import glob as _glob

    if len(sys.argv) > 1:
        pool_path = sys.argv[1]
    else:
        # 自动找最新的 ba_pool_v4_*.json
        base_dir = os.path.dirname(os.path.abspath(__file__))
        files = sorted(_glob.glob(os.path.join(base_dir, 'ba_pool_v4_*.json')))
        if files:
            pool_path = files[-1]
            print(f"自动选择最新 BA 池: {os.path.basename(pool_path)}")
        else:
            print("未找到 ba_pool_v4_*.json，请指定路径")
            sys.exit(1)

    result = predict_next_day_candidates(pool_path)
    _print_result(result)
