"""
新选股策略扫描脚本：阴跌不跌
=====================================
策略定义：
  - 最近60个交易日中，≥70% 的交易日该股下跌（当日close < 前日close）
  - 但60日整体股价没有下跌（第60日close >= 第1日open）

用法：
  python scan_low_volatility_up.py
  python scan_low_volatility_up.py --base-date 20260430  # 指定基准日期
  python scan_low_volatility_up.py --down-ratio 0.70 --min-amount 3e8  # 自定义参数
"""

import os
import sys
import argparse
import pandas as pd
from datetime import datetime

# ─── 数据目录 ────────────────────────────────────────────────────────────────
DATA_DIR = 'D:/daily_data'
SH_DIR   = os.path.join(DATA_DIR, 'SH')
SZ_DIR   = os.path.join(DATA_DIR, 'SZ')

# ─── 默认参数 ────────────────────────────────────────────────────────────────
DEFAULT_BASE_DATE  = datetime.today().strftime('%Y%m%d')
DEFAULT_LOOKBACK   = 60          # 回看交易日数
DEFAULT_DOWN_RATIO = 0.61        # 下跌天数占比阈值
DEFAULT_MIN_AMOUNT = 0           # 最小日均成交额（元），0=不过滤


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

        # 列名兼容
        df = df.rename(columns={'timetag': 'date', 'volumn': 'volume'})
        if 'date' not in df.columns:
            return None

        df['date'] = pd.to_datetime(df['date'].astype(str).str[:8], format='%Y%m%d')
        df = df.sort_values('date').reset_index(drop=True)

        # 过滤到基准日期
        df = df[df['date'] <= base_date_dt]

        # 至少需要 lookback+1 条（多1条用于计算第1日的prev_close）
        need = lookback + 1
        if len(df) < need:
            return None

        return df.tail(need).reset_index(drop=True)

    except Exception as e:
        return None


def check_strategy(code, base_date_dt, lookback=60, down_ratio=0.70, min_amount=0):
    """
    检查单只股票是否满足策略条件
    返回 (True/False, down_count, total_days, pct_change)
    """
    df = load_bars(code, base_date_dt, lookback)
    if df is None:
        return False, 0, 0, 0.0

    # 60个交易日 = df[1:]（df[0]只是为了算prev_close）
    df60 = df.copy()
    df60['prev_close'] = df60['close'].shift(1)
    df60 = df60.iloc[1:].reset_index(drop=True)  # 去掉第0行，剩60行

    if len(df60) < lookback:
        return False, 0, 0, 0.0

    # 条件1：下跌日占比
    down_days  = int((df60['close'] < df60['prev_close']).sum())
    total_days = len(df60)
    ratio      = down_days / total_days

    # 条件2：整体未下跌（末日close >= 首日open）
    first_open  = df60.iloc[0]['open']   # 60日第1天的开盘
    last_close  = df60.iloc[-1]['close'] # 60日最后1天的收盘
    no_decline  = last_close >= first_open

    # 可选：成交额过滤（60日均成交额）
    if min_amount > 0:
        avg_amt = df60['amount'].mean() if 'amount' in df60.columns else 0
        if avg_amt < min_amount:
            return False, down_days, total_days, (last_close / first_open - 1)

    pct = last_close / first_open - 1
    qualified = (ratio >= down_ratio) and no_decline
    return qualified, down_days, total_days, pct


def main():
    ap = argparse.ArgumentParser(description='阴跌不跌选股策略扫描')
    ap.add_argument('--base-date',  default=DEFAULT_BASE_DATE,
                    help=f'基准日期 YYYYMMDD，默认={DEFAULT_BASE_DATE}')
    ap.add_argument('--lookback',   type=int,   default=DEFAULT_LOOKBACK,
                    help=f'回看交易日数，默认={DEFAULT_LOOKBACK}')
    ap.add_argument('--down-ratio', type=float, default=DEFAULT_DOWN_RATIO,
                    help=f'下跌日占比阈值（0~1），默认={DEFAULT_DOWN_RATIO}')
    ap.add_argument('--min-amount', type=float, default=DEFAULT_MIN_AMOUNT,
                    help='60日均成交额下限（元），默认=0不过滤，示例：3e8')
    ap.add_argument('--show-detail', action='store_true',
                    help='显示每只满足条件股票的详细指标')
    args = ap.parse_args()

    base_date_dt = pd.to_datetime(args.base_date, format='%Y%m%d')

    print(f"=" * 60)
    print(f"阴跌不跌选股策略扫描")
    print(f"  基准日期    : {args.base_date}")
    print(f"  回看交易日  : {args.lookback} 日")
    print(f"  下跌日占比  : >= {args.down_ratio*100:.0f}%")
    print(f"  整体涨跌定义: 末日收盘 >= 首日开盘")
    if args.min_amount > 0:
        print(f"  均成交额下限: {args.min_amount/1e8:.2f} 亿")
    print(f"=" * 60)

    codes = get_all_stocks()
    print(f"本地股票总数: {len(codes)}")
    print("开始扫描...\n")

    results = []
    skip_cnt = 0

    for i, code in enumerate(codes):
        if (i + 1) % 1000 == 0:
            print(f"  进度: {i+1}/{len(codes)}, 已选: {len(results)}")

        qualified, down_days, total_days, pct = check_strategy(
            code, base_date_dt,
            lookback=args.lookback,
            down_ratio=args.down_ratio,
            min_amount=args.min_amount
        )

        if qualified:
            results.append({
                'code':       code,
                'down_days':  down_days,
                'total_days': total_days,
                'down_ratio': down_days / total_days if total_days else 0,
                'pct_change': pct,
            })
        elif down_days == 0 and total_days == 0:
            skip_cnt += 1

    # 按下跌日占比降序排列
    results.sort(key=lambda x: x['down_ratio'], reverse=True)

    print(f"\n{'=' * 60}")
    print(f"扫描完成！")
    print(f"  满足条件股票数: {len(results)}")
    print(f"  数据不足跳过  : {skip_cnt}")
    print(f"{'=' * 60}")

    if results:
        if args.show_detail:
            print(f"\n{'代码':^8} {'下跌天数':^8} {'总天数':^6} {'下跌占比':^8} {'区间涨跌':^8}")
            print("-" * 46)
            for r in results:
                print(f"{r['code']:^8} {r['down_days']:^8} {r['total_days']:^6} "
                      f"{r['down_ratio']*100:>6.1f}%  {r['pct_change']*100:>+6.2f}%")
        else:
            print("\n满足条件的股票代码：")
            # 每行10个
            chunk = [results[i:i+10] for i in range(0, len(results), 10)]
            for row in chunk:
                print('  ' + '  '.join(r['code'] for r in row))

    print()


if __name__ == '__main__':
    main()
