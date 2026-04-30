# -*- coding: utf-8 -*-
"""
紧急初始化调仓池脚本
使用本地数据（D:/daily_data）构建当前调仓池
输出格式与 state_v3_rebalance.json 完全一致
"""

import json
import os
import sys
import pandas as pd

sys.path.insert(0, 'd:/miniqmt_quant')
import config


def build_trading_calendar(data_dir='D:/daily_data'):
    """从本地数据中提取完整的交易日历，返回 'YYYY-MM-DD' 格式列表"""
    dates_set = set()
    for d in ['SH', 'SZ']:
        dir_path = os.path.join(data_dir, d)
        if not os.path.exists(dir_path):
            continue
        files = [f for f in os.listdir(dir_path)
                 if f.startswith('price_') and f.endswith('.csv')]
        for f in files:
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
    """读取单只股票的本地CSV数据，返回DataFrame
    列: date(datetime), open, high, low, close, volume, amount
    """
    # 确定路径
    if code.startswith('6'):
        filepath = os.path.join(data_dir, 'SH', f'price_{code}.csv')
    else:
        filepath = os.path.join(data_dir, 'SZ', f'price_{code}.csv')

    if not os.path.exists(filepath):
        return pd.DataFrame()

    df = pd.read_csv(filepath)
    if df.empty or len(df) <= 1:
        return pd.DataFrame()

    # 列名映射
    df = df.rename(columns={'timetag': 'date', 'volumn': 'volume'})
    df['date'] = pd.to_datetime(df['date'].astype(str), format='%Y%m%d')

    # 日期过滤
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


def main():
    print("=" * 60)
    print("【紧急】初始化 V3 调仓池")
    print("=" * 60)

    data_dir = config.V3_LOCAL_DATA_DIR
    rebalance_date = '2026-01-05'  # 2026年1月第一个交易日
    rebalance_date_ymd = rebalance_date.replace('-', '')

    # 1. 构建交易日历
    print("\n[1/4] 构建交易日历...")
    trading_dates = build_trading_calendar(data_dir)
    print(f"  交易日历: {len(trading_dates)} 个交易日 ({trading_dates[0]} ~ {trading_dates[-1]})")

    if rebalance_date not in trading_dates:
        print(f"  错误: {rebalance_date} 不在交易日历中")
        return

    cur_idx = trading_dates.index(rebalance_date)
    lookback_start_idx = max(0, cur_idx - config.V3_REBALANCE_LOOKBACK)
    lookback_start_date = trading_dates[lookback_start_idx]
    lookback_start_ymd = lookback_start_date.replace('-', '')

    print(f"  调仓日: {rebalance_date} (索引 {cur_idx})")
    print(f"  回看 {config.V3_REBALANCE_LOOKBACK} 个交易日，起始: {lookback_start_date}")

    # 2. 获取所有股票代码并过滤
    print("\n[2/4] 获取股票列表并过滤...")
    all_codes = get_all_stock_codes(data_dir)
    filtered_codes = []
    for code in all_codes:
        if code.startswith('8') or code.startswith('4'):
            continue
        if code.startswith('60') or code.startswith('00') or code.startswith('30') or code.startswith('688'):
            filtered_codes.append(code)
    print(f"  本地股票总数: {len(all_codes)}")
    print(f"  符合板块规则: {len(filtered_codes)}")

    # 3. 逐只读取数据，计算排名指标
    print("\n[3/4] 读取数据并计算排名指标...")
    lookback_dates_set = set(trading_dates[lookback_start_idx:cur_idx + 1])
    rebalance_dt = pd.to_datetime(rebalance_date)

    results = []
    total = len(filtered_codes)
    for i, code in enumerate(filtered_codes):
        if (i + 1) % 500 == 0 or i == 0:
            print(f"  处理中: {i+1}/{total}...")

        # 读取完整历史到调仓日（用于新股判断）
        df_full = get_stock_data(code, data_dir, '20220101', rebalance_date_ymd)
        if df_full.empty:
            continue

        # 排除新股：到调仓日历史数据不足60行
        total_rows = len(df_full[df_full['date'] <= rebalance_dt])
        if total_rows < 60:
            continue

        # 截取回看区间数据
        period_df = df_full[df_full['date'].isin(
            pd.to_datetime(list(lookback_dates_set))
        )].copy()

        if period_df.empty or len(period_df) < 2:
            continue

        # 计算总成交额
        total_amount = float(period_df['amount'].sum())

        # 计算累计涨跌幅：(最后收盘 - 最早收盘) / 最早收盘
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

    if not results:
        print("  错误: 没有有效股票数据")
        return

    # 4. 综合排名
    print("\n[4/4] 综合排名并保存...")
    result_df = pd.DataFrame(results)

    # 排名：越大越好，所以 ascending=False → rank升序=排名1最好
    result_df['amount_rank'] = result_df['total_amount'].rank(ascending=False, method='min')
    result_df['pct_rank'] = result_df['cum_pct'].rank(ascending=False, method='min')

    # 综合排名越小越好
    result_df['composite_rank'] = (
        result_df['amount_rank'] * 0.5 + result_df['pct_rank'] * 0.5
    )

    # 取综合排名前 top_n
    top_n = config.V3_TOP_N
    result_df = result_df.sort_values('composite_rank').head(top_n)
    pool = result_df['code'].tolist()

    # 5. 输出文件（与 live_engine_v3.py 读取格式一致）
    output = {
        'pool': pool,
        'rebalance_date': rebalance_date,
    }
    output_path = 'd:/miniqmt_quant/state_v3_rebalance.json'
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"  文件已保存: {output_path}")
    print(f"  调仓池大小: {len(pool)}")
    print("\n" + "=" * 60)
    print(f"调仓池生成完成！共 {len(pool)} 只股票")
    print("=" * 60)
    for i, code in enumerate(pool, 1):
        print(f"  {i:2d}. {code}")


if __name__ == '__main__':
    main()
