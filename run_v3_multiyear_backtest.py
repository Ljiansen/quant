#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_v3_multiyear_backtest.py
============================
V3 策略多年回测汇总（单槽 · 最优参数）

年份划分：
  2022       : 20220101 ~ 20221231  5min: D:/5min_data_2022
  2023       : 20230101 ~ 20231231  5min: D:/5min_data_2023
  2024       : 20240101 ~ 20241231  5min: D:/5min_data_2024
  2025~2026  : 20250101 ~ 20260430  5min: D:/5min_data（默认）

日线数据均来自 D:/daily_data（config.V3_LOCAL_DATA_DIR）。
2022 年因需要 2021 年的 lookback 日线，额外传入 D:/daily_data_2021_all（若存在）。

用法：
    python run_v3_multiyear_backtest.py
    python run_v3_multiyear_backtest.py --buy-price close
    python run_v3_multiyear_backtest.py --capital 300000
"""

import os
import sys
import io
import re
import argparse
import datetime

sys.path.insert(0, 'd:/miniqmt_quant')
import config
import run_backtest_5min_live_sim as sim

# ── 年份配置 ─────────────────────────────────────────────────────────────────
# (标签, start_YYYYMMDD, end_YYYYMMDD, fivemin_dir, extra_daily_dir)
YEAR_PERIODS = [
    ('2022',     '20220101', '20221231',
     'D:/5min_data_2022',
     'D:/daily_data_2021_all'),   # 为 2022 年初的 B+A lookback 提供 2021 日线
    ('2023',     '20230101', '20231231',
     'D:/5min_data_2023',
     None),
    ('2024',     '20240101', '20241231',
     'D:/5min_data_2024',
     None),
    ('2025~2026','20250101', '20260430',
     None,                         # 默认 D:/5min_data
     None),
]

# ── 结果解析正则 ──────────────────────────────────────────────────────────────
_RE = {
    'total_return':  re.compile(r'总收益率\s*:\s*([+\-\d.]+)%'),
    'annual_return': re.compile(r'年化收益率\s*:\s*([+\-\d.]+)%'),
    'max_drawdown':  re.compile(r'最大回撤\s*:\s*([\d.]+)%'),
    'sharpe':        re.compile(r'夏普比率\s*:\s*([+\-\d.]+)'),
    'n_trades':      re.compile(r'交易次数\s*:\s*(\d+)\s*次'),
    'win_rate':      re.compile(r'胜率\s*:\s*([\d.]+)%'),
    'profit_factor': re.compile(r'盈亏比\s*:\s*([\d.]+)'),
}

def _parse(text: str) -> dict:
    out = {}
    for key, pat in _RE.items():
        m = pat.search(text)
        out[key] = m.group(1) if m else 'N/A'
    return out


# ── 单年回测 ──────────────────────────────────────────────────────────────────
def run_one_year(label, start, end, fivemin_dir, extra_daily_dir,
                 buy_price_mode, capital, prev_bar_up, no_open_30, slippage):
    print(f'\n{"="*65}')
    print(f'  [{label}] 开始回测：{start} ~ {end}')
    if fivemin_dir:
        print(f'  5分钟数据目录: {fivemin_dir}')
    if extra_daily_dir and os.path.exists(extra_daily_dir):
        print(f'  额外日线目录: {extra_daily_dir}')
    elif extra_daily_dir:
        print(f'  [注意] extra_daily_dir 不存在，忽略: {extra_daily_dir}')
        extra_daily_dir = None
    print(f'{"="*65}')

    # 检查5分钟数据目录
    base_5min = fivemin_dir or sim.FIVEMIN_DIR
    if not os.path.exists(base_5min):
        print(f'  [跳过] 5分钟数据目录不存在: {base_5min}')
        return {'label': label, 'start': start, 'end': end, 'skipped': True}

    # 捕获输出（run_simulation 只打印不返回）
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        sim.run_simulation(
            start, end, capital,
            fivemin_dir=fivemin_dir,
            extra_daily_dir=extra_daily_dir,
            buy_price_mode=buy_price_mode,
            prev_bar_up=prev_bar_up,
            no_open_30=no_open_30,
            slippage=slippage,
        )
    except Exception as e:
        sys.stdout = old_stdout
        print(f'  [错误] {label} 回测异常: {e}')
        import traceback; traceback.print_exc()
        return {'label': label, 'start': start, 'end': end, 'error': str(e)}
    finally:
        sys.stdout = old_stdout

    output = buf.getvalue()
    # 打印关键摘要（过滤掉逐日/逐K线的详细输出）
    for line in output.splitlines():
        if any(kw in line for kw in ['回测区间', '初始资金', '最终净值', '总收益率',
                                      '年化收益率', '最大回撤', '夏普', '交易次数',
                                      '胜率', '盈亏比', '净値曲线', '交易明细',
                                      'ERROR', '错误', '===', '卖出类型']):
            print(' ', line)

    result = _parse(output)
    result['label'] = label
    result['start'] = start
    result['end']   = end
    return result


# ── 汇总表格 ──────────────────────────────────────────────────────────────────
def _print_summary(results):
    print('\n\n' + '=' * 75)
    print('  【V3策略多年回测汇总】  单槽 · 最优参数')
    print('  ' + f"hard_stop={config.V3_HARD_STOP_LOSS:.0%}  "
          f"soft_stop={config.V3_SOFT_STOP_LOSS:.0%}  "
          f"trail_act={config.V3_TRAILING_ACTIVATE:.0%}  "
          f"trail_stop={config.V3_TRAILING_STOP:.0%}  "
          f"time_stop={config.V3_TIME_STOP_DAYS}天")
    print('=' * 75)
    hdr = f"  {'年份':<10}  {'总收益':>8}  {'年化':>8}  {'最大回撤':>8}  {'夏普':>6}  {'胜率':>7}  {'盈亏比':>6}  {'笔数':>5}"
    print(hdr)
    print('  ' + '-' * 71)
    for r in results:
        if r.get('skipped'):
            print(f"  {r['label']:<10}  [数据缺失，已跳过]")
            continue
        if r.get('error'):
            print(f"  {r['label']:<10}  [回测出错: {r['error'][:30]}]")
            continue
        tr  = r.get('total_return',  'N/A')
        ar  = r.get('annual_return', 'N/A')
        dd  = r.get('max_drawdown',  'N/A')
        sh  = r.get('sharpe',        'N/A')
        wr  = r.get('win_rate',      'N/A')
        pf  = r.get('profit_factor', 'N/A')
        nt  = r.get('n_trades',      'N/A')
        # 格式化
        tr_s  = f"{float(tr):>+7.2f}%"  if tr  != 'N/A' else f"{'N/A':>8}"
        ar_s  = f"{float(ar):>+7.2f}%"  if ar  != 'N/A' else f"{'N/A':>8}"
        dd_s  = f"{float(dd):>7.2f}%"   if dd  != 'N/A' else f"{'N/A':>8}"
        sh_s  = f"{float(sh):>6.3f}"    if sh  != 'N/A' else f"{'N/A':>6}"
        wr_s  = f"{float(wr):>6.2f}%"   if wr  != 'N/A' else f"{'N/A':>7}"
        pf_s  = f"{float(pf):>6.2f}"    if pf  != 'N/A' else f"{'N/A':>6}"
        nt_s  = f"{int(nt):>5}"         if nt  != 'N/A' else f"{'N/A':>5}"
        print(f"  {r['label']:<10}  {tr_s}  {ar_s}  {dd_s}  {sh_s}  {wr_s}  {pf_s}  {nt_s}")
    print('=' * 75)


# ── 主入口 ────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='V3 策略多年回测汇总')
    parser.add_argument('--buy-price', default='close',
                        choices=['high', 'close', 'mid'],
                        help='买入价模式（默认 close，5分钟K线收盘价）')
    parser.add_argument('--capital', type=float,
                        default=config.V3_INITIAL_CAPITAL,
                        help=f'初始资金（默认 {config.V3_INITIAL_CAPITAL:,}）')
    parser.add_argument('--slippage', type=float, default=0.0,
                        help='双向滑点单边比例（默认 0）')
    parser.add_argument('--prev-bar-up', action='store_true',
                        help='买入前过滤：要求上一根K线非阴线')
    parser.add_argument('--no-open-30', action='store_true',
                        help='开盘30分钟回避')
    parser.add_argument('--years', default='all',
                        help='指定回测年份，逗号分隔，如 "2022,2023"（默认 all）')
    args = parser.parse_args()

    # 过滤年份
    if args.years == 'all':
        periods = YEAR_PERIODS
    else:
        wanted = set(args.years.split(','))
        periods = [p for p in YEAR_PERIODS if p[0] in wanted]
        if not periods:
            print(f'[错误] 未找到匹配的年份: {args.years}')
            return

    print('=' * 65)
    print('  V3 策略多年回测（单槽 · 最优参数）')
    print(f'  买入价模式: {args.buy_price}  初始资金: {args.capital:,.0f}')
    print(f'  运行时间: {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print('=' * 65)

    results = []
    for label, start, end, fivemin_dir, extra_daily_dir in periods:
        r = run_one_year(
            label, start, end, fivemin_dir, extra_daily_dir,
            buy_price_mode=args.buy_price,
            capital=args.capital,
            prev_bar_up=args.prev_bar_up,
            no_open_30=args.no_open_30,
            slippage=args.slippage,
        )
        results.append(r)

    _print_summary(results)

    # 保存汇总 JSON
    import json
    out_path = 'd:/miniqmt_quant/backtest_multiyear_summary.json'
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump({
            'run_time':   datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'buy_price':  args.buy_price,
            'capital':    args.capital,
            'params': {
                'hard_stop':        config.V3_HARD_STOP_LOSS,
                'soft_stop':        config.V3_SOFT_STOP_LOSS,
                'trailing_activate':config.V3_TRAILING_ACTIVATE,
                'trailing_stop':    config.V3_TRAILING_STOP,
                'time_stop_days':   config.V3_TIME_STOP_DAYS,
            },
            'results': results,
        }, f, ensure_ascii=False, indent=2)
    print(f'\n  汇总结果已保存: {out_path}')


if __name__ == '__main__':
    main()
