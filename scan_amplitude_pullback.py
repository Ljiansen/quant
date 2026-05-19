"""
新选股策略扫描脚本：高振幅回调
=====================================
策略定义：
  1. 最近60个交易日内，至少有 N 次单日振幅超过 X%
     振幅 = (当日high - 当日low) / 前日close
  2. 在振幅 >= X% 的交易日里，收阳（close >= open）的天数占比 >= R%
  3. 最新交易日收盘价距60日最高价（max high）跌幅超过 Y%
     即：last_close <= max_high * (1 - Y)

用法：
  python scan_amplitude_pullback.py
  python scan_amplitude_pullback.py --base-date 20260430
  python scan_amplitude_pullback.py --amp-thresh 0.10 --amp-count 23 --amp-up-ratio 0.70 --pullback 0.10
  python scan_amplitude_pullback.py --show-detail
"""

import os
import argparse
import pandas as pd
from datetime import datetime

# ─── 数据目录 ────────────────────────────────────────────────────────────────
DATA_DIR = 'D:/daily_data'
SH_DIR   = os.path.join(DATA_DIR, 'SH')
SZ_DIR   = os.path.join(DATA_DIR, 'SZ')

# ─── 默认参数 ────────────────────────────────────────────────────────────────
DEFAULT_BASE_DATE  = datetime.today().strftime('%Y%m%d')
DEFAULT_LOOKBACK   = 60    # 回看交易日数
DEFAULT_AMP_THRESH = 0.10  # 振幅阈值（10%）
DEFAULT_AMP_COUNT    = 20    # 至少出现次数
DEFAULT_AMP_UP_RATIO = 0.7  # 振幅日中收阳占比阈值
DEFAULT_PULLBACK     = 0.10  # 距最高点跌幅阈值
DEFAULT_MIN_AMOUNT   = 500000000     # 最小日均成交额（元），0=不过滤


def get_all_stocks():
    """获取本地数据中所有股票代码"""
    codes = []
    for d in [SH_DIR, SZ_DIR]:
        if not os.path.exists(d):
            continue
        for f in os.listdir(d):
            if f.startswith('price_') and f.endswith('.csv'):
                code = f.replace('price_', '').replace('.csv', '')
                fpath = os.path.join(d, f)
                if os.path.getsize(fpath) > 200:
                    codes.append(code)
    return sorted(codes)


def load_bars(code, base_date_dt, lookback=60):
    """
    读取截止到 base_date_dt 的最近 (lookback+1) 条日线数据
    多1条用于计算第1日的 prev_close
    返回 DataFrame 或 None
    """
    if code.startswith('6'):
        fpath = os.path.join(SH_DIR, f'price_{code}.csv')
    else:
        fpath = os.path.join(SZ_DIR, f'price_{code}.csv')

    if not os.path.exists(fpath):
        return None

    try:
        df = pd.read_csv(fpath, dtype={'timetag': str})
        if df.empty:
            return None

        df = df.rename(columns={'timetag': 'date', 'volumn': 'volume'})
        if 'date' not in df.columns:
            return None

        df['date'] = pd.to_datetime(df['date'].astype(str).str[:8], format='%Y%m%d')
        df = df.sort_values('date').reset_index(drop=True)
        df = df[df['date'] <= base_date_dt]

        need = lookback + 1  # 多1条用于算 prev_close
        if len(df) < need:
            return None

        return df.tail(need).reset_index(drop=True)

    except Exception:
        return None


def check_strategy(code, base_date_dt,
                   lookback=60, amp_thresh=0.10, amp_count=23,
                   amp_up_ratio=0.70, pullback=0.10, min_amount=0):
    """
    检查单只股票是否满足高振幅回调策略
    返回 (qualified, amp_hit_count, up_in_amp_ratio, max_high, last_close, drop_pct)
    """
    df = load_bars(code, base_date_dt, lookback)
    if df is None:
        return False, 0, 0.0, 0, 0, 0.0

    # df[0] 是前置行（仅提供 prev_close），df[1:] 是60个交易日
    df['prev_close'] = df['close'].shift(1)
    df60 = df.iloc[1:].reset_index(drop=True)

    if len(df60) < lookback:
        return False, 0, 0.0, 0, 0, 0.0

    # ── 条件1：振幅 = (high - low) / prev_close ──────────────────────────────
    df60['amplitude'] = (df60['high'] - df60['low']) / df60['prev_close']
    amp_mask  = df60['amplitude'] >= amp_thresh
    hit_count = int(amp_mask.sum())

    # ── 条件2：高振幅日中收阳（close >= open）的占比 ─────────────────────────
    if hit_count == 0:
        up_ratio = 0.0
    else:
        amp_days = df60[amp_mask]
        up_in_amp = int((amp_days['close'] >= amp_days['open']).sum())
        up_ratio  = up_in_amp / hit_count

    # ── 条件3：距60日最高价的跌幅 ────────────────────────────────────────────
    max_high   = float(df60['high'].max())
    last_close = float(df60.iloc[-1]['close'])
    drop_pct   = (max_high - last_close) / max_high  # 正值=跌了多少

    # ── 可选：成交额过滤 ──────────────────────────────────────────────────────
    if min_amount > 0:
        avg_amt = df60['amount'].mean() if 'amount' in df60.columns else 0
        if avg_amt < min_amount:
            return False, hit_count, up_ratio, max_high, last_close, drop_pct

    qualified = (
        hit_count >= amp_count and
        up_ratio  >= amp_up_ratio and
        drop_pct  >= pullback
    )
    return qualified, hit_count, up_ratio, max_high, last_close, drop_pct


def main():
    ap = argparse.ArgumentParser(description='高振幅回调选股策略扫描')
    ap.add_argument('--base-date',     default=DEFAULT_BASE_DATE,
                    help=f'基准日期 YYYYMMDD，默认={DEFAULT_BASE_DATE}')
    ap.add_argument('--lookback',      type=int,   default=DEFAULT_LOOKBACK,
                    help=f'回看交易日数，默认={DEFAULT_LOOKBACK}')
    ap.add_argument('--amp-thresh',    type=float, default=DEFAULT_AMP_THRESH,
                    help=f'单日振幅阈值（0~1），默认={DEFAULT_AMP_THRESH} (10%%)')
    ap.add_argument('--amp-count',     type=int,   default=DEFAULT_AMP_COUNT,
                    help=f'振幅触发最少次数，默认={DEFAULT_AMP_COUNT}')
    ap.add_argument('--amp-up-ratio',  type=float, default=DEFAULT_AMP_UP_RATIO,
                    help=f'振幅日中收阳占比阈值（0~1），默认={DEFAULT_AMP_UP_RATIO} (70%%)')
    ap.add_argument('--pullback',      type=float, default=DEFAULT_PULLBACK,
                    help=f'距最高点跌幅阈值（0~1），默认={DEFAULT_PULLBACK} (10%%)')
    ap.add_argument('--min-amount',    type=float, default=DEFAULT_MIN_AMOUNT,
                    help='60日均成交额下限（元），默认=0不过滤，示例：3e8')
    ap.add_argument('--show-detail',   action='store_true',
                    help='显示每只满足条件股票的详细指标')
    args = ap.parse_args()

    base_date_dt = pd.to_datetime(args.base_date, format='%Y%m%d')

    print('=' * 66)
    print('高振幅回调选股策略扫描')
    print(f'  基准日期          : {args.base_date}')
    print(f'  回看交易日        : {args.lookback} 日')
    print(f'  振幅阈值          : >= {args.amp_thresh*100:.0f}%  (振幅=(high-low)/prev_close)')
    print(f'  振幅触发次数      : >= {args.amp_count} 次')
    print(f'  振幅日收阳占比    : >= {args.amp_up_ratio*100:.0f}%  (close >= open)')
    print(f'  距最高点跌幅      : >= {args.pullback*100:.0f}%  (最高点=60日max high)')
    if args.min_amount > 0:
        print(f'  均成交额下限      : {args.min_amount/1e8:.2f} 亿')
    print('=' * 66)

    codes = get_all_stocks()
    print(f'本地股票总数: {len(codes)}')
    print('开始扫描...\n')

    results  = []
    skip_cnt = 0

    for i, code in enumerate(codes):
        if (i + 1) % 1000 == 0:
            print(f'  进度: {i+1}/{len(codes)}, 已选: {len(results)}')

        qualified, hit_count, up_ratio, max_high, last_close, drop_pct = check_strategy(
            code, base_date_dt,
            lookback=args.lookback,
            amp_thresh=args.amp_thresh,
            amp_count=args.amp_count,
            amp_up_ratio=args.amp_up_ratio,
            pullback=args.pullback,
            min_amount=args.min_amount,
        )

        if hit_count == 0 and max_high == 0:
            skip_cnt += 1
        elif qualified:
            results.append({
                'code':       code,
                'hit_count':  hit_count,
                'up_ratio':   up_ratio,
                'max_high':   max_high,
                'last_close': last_close,
                'drop_pct':   drop_pct,
            })

    # 按距最高点跌幅降序（跌得最多排前面）
    results.sort(key=lambda x: x['drop_pct'], reverse=True)

    print(f'\n{"=" * 66}')
    print(f'扫描完成！')
    print(f'  满足条件股票数: {len(results)}')
    print(f'  数据不足跳过  : {skip_cnt}')
    print(f'{"=" * 66}')

    if results:
        if args.show_detail:
            print(f'\n{"代码":^8} {"振幅次数":^8} {"其中收阳%":^9} {"60日最高":^10} {"最新收盘":^10} {"距高点跌幅":^10}')
            print('-' * 60)
            for r in results:
                print(f'{r["code"]:^8} {r["hit_count"]:^8} '
                      f'{r["up_ratio"]*100:>7.1f}%  '
                      f'{r["max_high"]:>10.3f} {r["last_close"]:>10.3f} '
                      f'{r["drop_pct"]*100:>9.2f}%')
        else:
            print('\n满足条件的股票代码：')
            chunk = [results[i:i+10] for i in range(0, len(results), 10)]
            for row in chunk:
                print('  ' + '  '.join(r['code'] for r in row))

    print()


if __name__ == '__main__':
    main()
