# -*- coding: utf-8 -*-
"""
半月调仓脚本 - 每月1日和15日更新股票池

算法与 init_rebalance_pool.py 一致（本地数据源）：
  - 120个交易日回看窗口
  - 成交额排名×0.5 + 涨跌幅排名×0.5
  - 排除上市不足60个交易日的新股
  - 取综合排名前 TOP_N

用法：
  python run_rebalance.py           # 检查是否是调仓日（1号或15号），是则执行
  python run_rebalance.py --force   # 强制执行（不检查日期）
  python run_rebalance.py --date 20260501  # 指定调仓日期（YYYYMMDD）
"""
import sys
import os
import json
import argparse
import shutil
from datetime import datetime, timedelta

sys.path.insert(0, 'd:/miniqmt_quant')
import config

import pandas as pd


# ──────────────────────────────────────────────────────────────
# 复用 init_rebalance_pool.py 的数据读取函数
# ──────────────────────────────────────────────────────────────

def build_trading_calendar(data_dir='D:/daily_data'):
    """从本地数据中提取完整的交易日历，返回 'YYYY-MM-DD' 格式列表"""
    dates_set = set()
    for d in ['SH', 'SZ']:
        dir_path = os.path.join(data_dir, d)
        if not os.path.exists(dir_path):
            continue
        files = [f for f in os.listdir(dir_path)
                 if f.startswith('price_') and f.endswith('.csv')]
        for f in files[:5]:   # 只需从少量文件提取日历
            filepath = os.path.join(dir_path, f)
            if os.path.getsize(filepath) <= 100:
                continue
            try:
                df = pd.read_csv(filepath, usecols=['timetag'])
                for dt in df['timetag'].astype(str).tolist():
                    dt_str = str(dt)
                    if len(dt_str) == 8:
                        dt_formatted = f"{dt_str[:4]}-{dt_str[4:6]}-{dt_str[6:]}"
                    else:
                        dt_formatted = dt_str
                    dates_set.add(dt_formatted)
            except Exception:
                continue
    return sorted(dates_set)


def get_stock_data(code, data_dir, start_date, end_date):
    """读取单只股票的本地CSV数据，返回DataFrame"""
    if code.startswith('6'):
        filepath = os.path.join(data_dir, 'SH', f'price_{code}.csv')
    else:
        filepath = os.path.join(data_dir, 'SZ', f'price_{code}.csv')

    if not os.path.exists(filepath):
        return pd.DataFrame()

    df = pd.read_csv(filepath)
    if df.empty or len(df) <= 1:
        return pd.DataFrame()

    df = df.rename(columns={'timetag': 'date', 'volumn': 'volume'})
    df['date'] = pd.to_datetime(df['date'].astype(str), format='%Y%m%d')

    start_dt = pd.to_datetime(start_date, format='%Y%m%d')
    end_dt = pd.to_datetime(end_date, format='%Y%m%d')
    df = df[(df['date'] >= start_dt) & (df['date'] <= end_dt)]

    cols = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount']
    for col in cols:
        if col not in df.columns:
            return pd.DataFrame()
    df = df[cols].sort_values('date').reset_index(drop=True)
    return df


def get_all_stock_codes(data_dir):
    """获取本地数据中所有股票代码列表"""
    codes = []
    for d in ['SH', 'SZ']:
        dir_path = os.path.join(data_dir, d)
        if not os.path.exists(dir_path):
            continue
        for f in os.listdir(dir_path):
            if f.startswith('price_') and f.endswith('.csv'):
                code = f.replace('price_', '').replace('.csv', '')
                filepath = os.path.join(dir_path, f)
                if os.path.getsize(filepath) > 200:
                    codes.append(code)
    return sorted(codes)


# ──────────────────────────────────────────────────────────────
# 主调仓逻辑
# ──────────────────────────────────────────────────────────────

def run_rebalance(rebalance_date_str=None, force=False):
    """
    执行调仓。

    Args:
        rebalance_date_str: 调仓日期字符串 'YYYYMMDD'，None则自动取今天
        force: True表示跳过日期检查（不要求必须是1号/15号）

    Returns:
        True表示成功，False表示跳过或失败
    """
    today = datetime.today()

    if rebalance_date_str:
        rebalance_dt = datetime.strptime(rebalance_date_str, '%Y%m%d')
    else:
        rebalance_dt = today

    day = rebalance_dt.day

    if not force and day not in (1, 15):
        print(f"今天是 {rebalance_dt.strftime('%Y-%m-%d')}（{day}号），不是调仓日（每月1号或15号）")
        print("如需强制执行，使用 --force 参数")
        return False

    rebalance_date = rebalance_dt.strftime('%Y-%m-%d')
    rebalance_date_ymd = rebalance_dt.strftime('%Y%m%d')

    print("=" * 60)
    print(f"【半月调仓】 {rebalance_date}")
    print("=" * 60)

    data_dir = config.V3_LOCAL_DATA_DIR

    # 1. 构建交易日历
    print("\n[1/4] 构建交易日历...")
    trading_dates = build_trading_calendar(data_dir)
    if not trading_dates:
        print("  错误: 无法构建交易日历，请检查本地数据目录:", data_dir)
        return False
    print(f"  交易日历: {len(trading_dates)} 个 ({trading_dates[0]} ~ {trading_dates[-1]})")

    # 如果指定日期不在交易日历，找最近一个交易日
    if rebalance_date not in trading_dates:
        # 找不超过调仓日的最近一个交易日
        candidates = [d for d in trading_dates if d <= rebalance_date]
        if not candidates:
            print(f"  错误: 找不到 {rebalance_date} 之前的交易日")
            return False
        rebalance_date = candidates[-1]
        rebalance_date_ymd = rebalance_date.replace('-', '')
        print(f"  注意: {rebalance_dt.strftime('%Y-%m-%d')} 非交易日，使用最近交易日: {rebalance_date}")

    cur_idx = trading_dates.index(rebalance_date)
    lookback_start_idx = max(0, cur_idx - config.V3_REBALANCE_LOOKBACK)
    lookback_start_date = trading_dates[lookback_start_idx]
    lookback_start_ymd = lookback_start_date.replace('-', '')

    print(f"  调仓日: {rebalance_date} (索引 {cur_idx})")
    print(f"  回看 {config.V3_REBALANCE_LOOKBACK} 个交易日，起始: {lookback_start_date}")

    # 2. 获取并过滤股票
    print("\n[2/4] 获取股票列表...")
    all_codes = get_all_stock_codes(data_dir)
    filtered_codes = [
        c for c in all_codes
        if not (c.startswith('8') or c.startswith('4'))
        and (c.startswith('60') or c.startswith('00')
             or c.startswith('30') or c.startswith('688'))
    ]
    print(f"  本地股票总数: {len(all_codes)}")
    print(f"  符合板块规则: {len(filtered_codes)}")

    # 3. 计算排名指标
    print("\n[3/4] 读取数据并计算排名指标...")
    rebalance_dt_pd = pd.to_datetime(rebalance_date)
    lookback_dates_set = set(trading_dates[lookback_start_idx:cur_idx + 1])

    results = []
    total = len(filtered_codes)
    for i, code in enumerate(filtered_codes):
        if (i + 1) % 500 == 0 or i == 0:
            print(f"  处理中: {i+1}/{total}...")

        df_full = get_stock_data(code, data_dir, '20220101', rebalance_date_ymd)
        if df_full.empty:
            continue

        # 排除新股：历史数据不足60行
        total_rows = len(df_full[df_full['date'] <= rebalance_dt_pd])
        if total_rows < 60:
            continue

        period_df = df_full[df_full['date'].isin(
            pd.to_datetime(list(lookback_dates_set))
        )].copy()

        if period_df.empty or len(period_df) < 2:
            continue

        total_amount = float(period_df['amount'].sum())

        period_df = period_df.sort_values('date')
        first_close = float(period_df.iloc[0]['close'])
        last_close = float(period_df.iloc[-1]['close'])
        if first_close <= 0:
            continue
        cum_pct = (last_close - first_close) / first_close

        results.append({
            'code': code,
            'total_amount': total_amount,
            'cum_pct': cum_pct,
        })

    print(f"  有效股票数: {len(results)}")

    if len(results) < 10:
        print(f"  警告: 有效股票数过少（{len(results)}），调仓中止")
        return False

    # 4. 综合排名
    print(f"\n[4/4] 综合排名，取前 {config.V3_TOP_N} 只...")
    result_df = pd.DataFrame(results)
    result_df['amount_rank'] = result_df['total_amount'].rank(ascending=False, method='min')
    result_df['pct_rank'] = result_df['cum_pct'].rank(ascending=False, method='min')
    result_df['composite_rank'] = result_df['amount_rank'] * 0.5 + result_df['pct_rank'] * 0.5
    result_df = result_df.sort_values('composite_rank').head(config.V3_TOP_N)
    pool = result_df['code'].tolist()

    # 5. 备份旧池并保存新池
    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'state_v3_rebalance.json')

    if os.path.exists(output_path):
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_path = output_path.replace('.json', f'_backup_{ts}.json')
        try:
            shutil.copy2(output_path, backup_path)
            print(f"  旧股票池已备份: {os.path.basename(backup_path)}")
        except Exception as e:
            print(f"  备份失败（继续执行）: {e}")

    output = {
        'pool': pool,
        'rebalance_date': rebalance_date,
        'rebalance_method': 'local_data',
        'lookback_days': config.V3_REBALANCE_LOOKBACK,
        'total_candidates': len(results),
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}")
    print(f"✅ 调仓完成！股票池已更新")
    print(f"   调仓日: {rebalance_date}")
    print(f"   股票数: {len(pool)} 只")
    print(f"   文件  : {output_path}")
    print(f"{'='*60}")
    print("前10只：")
    for i, code in enumerate(pool[:10], 1):
        print(f"  {i:2d}. {code}")

    # 6. 钉钉通知（同步发送，避免进程退出时守护线程被杀）
    try:
        from utils.notifier import _do_send
        pool_preview = '、'.join(pool[:5]) + (f' 等{len(pool)}只' if len(pool) > 5 else '')
        import datetime as _dt
        msg = (
            f'【量化 股票池已更新（{rebalance_date}）】\n'
            f'半月调仓完成\n'
            f'新股池大小：{len(pool)} 只\n'
            f'前5只：{pool_preview}\n'
            f'算法：动量+流动性综合排名（回看{config.V3_REBALANCE_LOOKBACK}交易日）\n'
            f'时间：{_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'
        )
        _do_send(msg)   # 同步调用，确保发出去再退出
        print('\n✅ 钉钉通知已发送')
    except Exception as e:
        print(f'\n注意: 钉钉通知发送失败: {e}')

    return True


def main():
    parser = argparse.ArgumentParser(description='半月调仓脚本（每月1日/15日更新股票池）')
    parser.add_argument('--force', action='store_true',
                        help='强制执行，不检查今天是否为1号/15号')
    parser.add_argument('--date', type=str, default=None,
                        help='指定调仓日期，格式 YYYYMMDD（默认今天）')
    args = parser.parse_args()

    success = run_rebalance(rebalance_date_str=args.date, force=args.force)
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
