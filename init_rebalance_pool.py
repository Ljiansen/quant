# -*- coding: utf-8 -*-
"""
初始化 V3 调仓池脚本
先增量更新日线数据，再构建调仓池
输出格式与 state_v3_rebalance.json 完全一致

用法：
  python init_rebalance_pool.py              # 先更新日线，再建池（默认）
  python init_rebalance_pool.py --skip-update # 跳过日线更新，直接建池
  python init_rebalance_pool.py --strategy a  # 指定策略（ba/a/b）
"""

import json
import os
import sys
import subprocess
import glob
import pandas as pd

sys.path.insert(0, 'd:/miniqmt_quant')
import config


def run_daily_data_update():
    """调用 update_daily_data.py --force 更新日线数据，返回是否成功"""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'update_daily_data.py')
    print("\n" + "=" * 60)
    print("[前置步骤] 更新本地日线数据（update_daily_data.py --force）")
    print("=" * 60)
    try:
        ret = subprocess.run(
            [sys.executable, script, '--force'],
            check=False
        )
        if ret.returncode == 0:
            print("[前置步骤] 日线数据更新完成 ✓")
            return True
        else:
            print(f"[前置步骤] 日线数据更新异常（returncode={ret.returncode}），继续用已有数据建池")
            return False
    except Exception as e:
        print(f"[前置步骤] 调用更新脚本失败: {e}，继续用已有数据建池")
        return False


def download_pool_5min_today(pool: list, output_dir: str, date_str: str):
    """下载调仓池今日5分钟K线（前复权），存入 output_dir/{code}_{YYYYMMDD}.csv

    每张文件列：datetime,open,high,low,close,volume,amount
    失败的股票静默跳过，不中断整个建池流程。
    """
    try:
        import baostock as bs
    except ImportError:
        print("[5min预缓存] baostock 未安装，跳过下载")
        return

    os.makedirs(output_dir, exist_ok=True)
    date_fmt = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"  # YYYYMMDD → YYYY-MM-DD

    print(f"\n[建池后置] 下载调仓池今日({date_fmt})5分钟K线，共{len(pool)}只，存入: {output_dir}")
    bs_ret = bs.login()
    if bs_ret.error_code != '0':
        print(f"[建池后置] baostock登录失败({bs_ret.error_msg})，跳过下载")
        return

    ok, fail, skip = 0, 0, 0
    for code in pool:
        bs_code = f"sh.{code}" if code.startswith('6') else f"sz.{code}"
        out_file = os.path.join(output_dir, f"{code}_{date_str}.csv")
        try:
            rs = bs.query_history_k_data_plus(
                code=bs_code,
                fields='time,open,high,low,close,volume,amount',
                start_date=date_fmt,
                end_date=date_fmt,
                frequency='5',
                adjustflag='2'
            )
            rows = []
            while rs.error_code == '0' and rs.next():
                rows.append(rs.get_row_data())

            if not rows:
                skip += 1
                continue

            df = pd.DataFrame(rows, columns=['time', 'open', 'high', 'low', 'close', 'volume', 'amount'])
            # baostock time 格式: '20260506093000000' → datetime
            df['datetime'] = pd.to_datetime(df['time'].str[:14], format='%Y%m%d%H%M%S')
            df = df[['datetime', 'open', 'high', 'low', 'close', 'volume', 'amount']]
            for col in ['open', 'high', 'low', 'close', 'volume', 'amount']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            df.to_csv(out_file, index=False)
            ok += 1
        except Exception as e:
            print(f"[建池后置] {code} 下载失败: {e}")
            fail += 1

    bs.logout()
    print(f"[建池后置] 5分钟预缓存完成: 成功={ok} 无数据={skip} 失败={fail}")
    if ok + skip == len(pool) and fail == 0:
        print(f"[建池后置] 注意: baostock 今日数据可能尚未发布（15:30后才平稳）")



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


# 支持的选股策略
STRATEGIES = {
    'ba': {
        'name': 'B+A（最优组合）',
        'desc': 'MA20趋势过滤 + 信号质量排名（点刃两层屏蔽）',
        'use_ma20':    True,
        'quality_mode': True,
    },
    'a': {
        'name': 'A（信号质量）',
        'desc': '信号质量排名，不做MA20过滤（选历史胜率高的股票）',
        'use_ma20':    False,
        'quality_mode': True,
    },
    'b': {
        'name': 'B（趋势 MA20）',
        'desc': 'MA20趋势过滤 + 信号频率排名（不要求收阳线）',
        'use_ma20':    True,
        'quality_mode': False,
    },
}


def main(strategy: str = 'ba'):
    if strategy not in STRATEGIES:
        print(f'[!] 未知策略 "{strategy}"，已回退到 ba')
        strategy = 'ba'
    sinfo = STRATEGIES[strategy]
    print("=" * 60)
    print(f"》{sinfo['name']}「初始化 V3 调仓池")
    print(f"选股公式: {sinfo['desc']}")
    print("=" * 60)

    data_dir = config.V3_LOCAL_DATA_DIR

    # 自动使用今天日期（如果不在交易日内则和就取最近交易日）
    from datetime import date as _date
    today_str = _date.today().strftime('%Y-%m-%d')

    # 1. 构建交易日历
    print("\n[1/4] 构建交易日历...")
    trading_dates = build_trading_calendar(data_dir)
    print(f"  交易日历: {len(trading_dates)} 个交易日 ({trading_dates[0]} ~ {trading_dates[-1]})")

    # 取最近交易日
    if today_str in trading_dates:
        rebalance_date = today_str
    else:
        rebalance_date = max(d for d in trading_dates if d <= today_str)
    rebalance_date_ymd = rebalance_date.replace('-', '')

    cur_idx = trading_dates.index(rebalance_date)
    lookback_start_idx = max(0, cur_idx - config.V3_REBALANCE_LOOKBACK)
    lookback_start_date = trading_dates[lookback_start_idx]
    lookback_start_ymd  = lookback_start_date.replace('-', '')

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

    # ST 过滤：通过 xtquant 获取股票名称，排除名称含 ST 的股票
    try:
        from xtquant import xtdata as _xtd
        st_excluded = []
        non_st_codes = []
        for code in filtered_codes:
            suffix = '.SH' if code.startswith('6') else '.SZ'
            detail = _xtd.get_instrument_detail(code + suffix)
            if detail:
                name = detail.get('InstrumentName', '')
                if 'ST' in name.upper():
                    st_excluded.append(code)
                    continue
            non_st_codes.append(code)
        print(f"  ST过滤排除: {len(st_excluded)} 只（{', '.join(st_excluded[:10])}{'...' if len(st_excluded)>10 else ''}）")
        filtered_codes = non_st_codes
    except Exception as _e:
        print(f"  [警告] ST过滤失败({_e})，跳过ST过滤")
    print(f"  过滤后可用: {len(filtered_codes)} 只")

    # 3. 逐只读取数据，计算 B+A 指标
    print("\n[3/4] 读取数据并计算 B+A 指标...")
    lookback_dates_set = set(trading_dates[lookback_start_idx:cur_idx + 1])
    ma20_dates_set     = set(trading_dates[max(0, cur_idx - 20):cur_idx + 1])
    rebalance_dt       = pd.to_datetime(rebalance_date)

    # B+A 选股参数（与实盘一致）
    min_chg = config.V3_MIN_CHANGE_PCT    # 1%
    max_chg = config.V3_MAX_CHANGE_PCT    # 7%

    results = []
    total = len(filtered_codes)
    for i, code in enumerate(filtered_codes):
        if (i + 1) % 500 == 0 or i == 0:
            print(f"  处理中: {i+1}/{total}...")

        df_full = get_stock_data(code, data_dir, '20220101', rebalance_date_ymd)
        if df_full.empty:
            continue

        # 排除新股：到调仓日历史数据不足60行
        total_rows = len(df_full[df_full['date'] <= rebalance_dt])
        if total_rows < 60:
            continue

        # 截取回看区间
        period_df = df_full[df_full['date'].isin(
            pd.to_datetime(list(lookback_dates_set))
        )].copy().sort_values('date')

        if period_df.empty or len(period_df) < 5:
            continue

        # ── B: MA20 趋势过滤（ba 和 b 策略使用）────────────────────────────────────
        ma20_df = df_full[df_full['date'].isin(pd.to_datetime(list(ma20_dates_set)))]
        last_close = float(period_df.iloc[-1]['close'])
        if sinfo['use_ma20']:
            if ma20_df.empty:
                continue
            ma20 = float(ma20_df['close'].mean())
            if last_close <= ma20:
                continue   # 跌破MA20，直接排除
        else:
            ma20 = float(ma20_df['close'].mean()) if not ma20_df.empty else 0.0

        # ── 信号得分────────────────────────────────────────────────
        period_df = period_df.copy()
        period_df['prev_close'] = period_df['close'].shift(1)
        period_df = period_df.dropna(subset=['prev_close'])
        period_df = period_df[period_df['prev_close'] > 0]
        period_df['daily_chg'] = (period_df['close'] - period_df['prev_close']) / period_df['prev_close']

        if sinfo['quality_mode']:
            # 信号质量: 涨幅在区间 AND 收阳线
            score_mask = (
                (period_df['daily_chg'] > min_chg) &
                (period_df['daily_chg'] < max_chg) &
                (period_df['close'] > period_df['open'])
            )
        else:
            # 信号频率: 涨幅在区间即可（不要求收阳）
            score_mask = (
                (period_df['daily_chg'] > min_chg) &
                (period_df['daily_chg'] < max_chg)
            )
        quality_score = int(score_mask.sum())

        results.append({
            'code':          code,
            'quality_score': quality_score,
            'last_close':    last_close,
            'ma20':          round(ma20, 4),
        })

    print(f"  MA20过滤后有效股票数: {len(results)}")

    if not results:
        print("  错误: 没有有效股票数据")
        return

    # 4. 按 quality_score 降序取 Top N
    print("\n[4/4] 排名并保存...")
    result_df = pd.DataFrame(results)
    result_df = result_df.sort_values('quality_score', ascending=False)

    top_n = config.V3_TOP_N
    result_df = result_df.head(top_n)
    pool = result_df['code'].tolist()

    # 5. 输出文件
    output = {
        'pool':            pool,
        'rebalance_date':  rebalance_date,
        'strategy_key':    strategy,
        'strategy':        sinfo['name'],
        'min_chg':         min_chg,
        'max_chg':         max_chg,
    }
    output_path = 'd:/miniqmt_quant/state_v3_rebalance.json'
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # 建池完成后立即存历史快照（以 rebalance_date 命名，可跨期追溯）
    import os as _os
    _snap_dir = 'd:/miniqmt_quant/pool_snapshots'
    _os.makedirs(_snap_dir, exist_ok=True)
    _snap_path = _os.path.join(_snap_dir, f'{rebalance_date_ymd}_pool.json')
    with open(_snap_path, 'w', encoding='utf-8') as _sf:
        json.dump(output, _sf, ensure_ascii=False, indent=2)
    print(f"  快照已保存: {_snap_path}")

    print(f"  文件已保存: {output_path}")
    print(f"  调仓池大小: {len(pool)}")
    print("\n" + "=" * 60)
    print(f"\u9009股完成！共 {len(pool)} 只股票")
    print("  调仓日:", rebalance_date)
    print("  策略:", sinfo['name'])
    print("  应用参数: min_chg={:.0%}  max_chg={:.0%}  MA20趋势过滤={}".format(
        min_chg, max_chg, '开' if sinfo['use_ma20'] else '关'))
    print("=" * 60)
    for i, code in enumerate(pool, 1):
        row = result_df[result_df['code'] == code].iloc[0]
        print(f"  {i:2d}. {code}  信号质量={int(row['quality_score'])}天  "
              f"收盘={row['last_close']:.2f}  MA20={row['ma20']:.2f}")

    # 建池后置：下载今日5分钟K线到预缓存目录
    next_pool_dir = getattr(config, 'V3_NEXT_POOL_5MIN_DIR', 'd:/miniqmt_quant/5min_next_pool')

    # ── 步骤A：保存旧调仓池今日5min数据（回测存档）──────────────────────────────
    # 读取即将被覆盖的旧池子，下载它们今天的5分钟K线
    # 与新池子重叠的股票会被覆盖写入，内容相同，无影响
    _old_pool = []
    try:
        if os.path.exists(output_path):
            with open(output_path, 'r', encoding='utf-8') as _f:
                _old_pool = json.load(_f).get('pool', [])
    except Exception as _e:
        print(f"[建池后置] 读取旧调仓池失败({_e})，跳过旧池5min存档")
    if _old_pool:
        _old_only = [c for c in _old_pool if c not in pool]
        print(f"[建池后置] 旧调仓池{len(_old_pool)}只，其中{len(_old_only)}只不在新池（退出股票），存档其今日5min数据")
        download_pool_5min_today(_old_pool, next_pool_dir, rebalance_date_ymd)

    # ── 步骤B：下载新调仓池今日5min数据（供次日实盘引擎兜底）────────────────────
    # 供次日实盘引擎对新入池股票（miniQMT无历史bar）的兜底使用
    download_pool_5min_today(pool, next_pool_dir, rebalance_date_ymd)


if __name__ == '__main__':
    import argparse
    _ap = argparse.ArgumentParser()
    _ap.add_argument('--strategy',    default='ba', help='选股策略: ba/a/b')
    _ap.add_argument('--skip-update', action='store_true', help='跳过日线数据更新步骤')
    _args = _ap.parse_args()

    if not _args.skip_update:
        run_daily_data_update()

    main(strategy=_args.strategy)
