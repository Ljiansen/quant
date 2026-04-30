# -*- coding: utf-8 -*-
"""
V3策略科创板/创业板参数调查脚本
调查1：回测结果交易记录分析
调查2：调仓池构成分析
调查3：check_sell_signals 参数验证
"""

import sys
sys.path.insert(0, 'd:/miniqmt_quant')

import pickle

# ============================================================================
# 调查1：检查回测结果中科创板/创业板股票的参与度
# ============================================================================
print("=" * 70)
print("调查1：回测结果交易记录分析")
print("=" * 70)

with open('backtest_result.pkl', 'rb') as f:
    result = pickle.load(f)

trades = result.get('trades', [])
sell_trades = [t for t in trades if t['direction'] == 'sell']
buy_trades = [t for t in trades if t['direction'] == 'buy']

print(f"总交易笔数: {len(trades)} (买入{len(buy_trades)} + 卖出{len(sell_trades)})")
print()

# 按板块分类卖出交易
star_sells = []   # 688
cyb_sells = []    # 30
main_sells = []   # 60/00

for t in sell_trades:
    code = str(t['code'])
    if code.startswith('688'):
        star_sells.append(t)
    elif code.startswith('30'):
        cyb_sells.append(t)
    else:
        main_sells.append(t)

print("卖出交易按板块分布:")
print(f"  科创板(688): {len(star_sells)} 笔")
print(f"  创业板(30):  {len(cyb_sells)} 笔")
print(f"  主板(60/00): {len(main_sells)} 笔")
print()

# 止盈触发统计
star_tp = [t for t in star_sells if t.get('sell_type') == 'take_profit']
cyb_tp = [t for t in cyb_sells if t.get('sell_type') == 'take_profit']
main_tp = [t for t in main_sells if t.get('sell_type') == 'take_profit']

print("止盈触发统计 (sell_type == 'take_profit'):")
print(f"  科创板(688): {len(star_tp)} 次")
print(f"  创业板(30):  {len(cyb_tp)} 次")
print(f"  主板(60/00): {len(main_tp)} 次")
print()

# 各板块止盈时的平均盈亏百分比
if star_tp:
    avg_pnl_pct = sum(t.get('pnl_pct', 0) or 0 for t in star_tp) / len(star_tp)
    print(f"  科创板止盈平均盈亏: {avg_pnl_pct:.2%}")
if cyb_tp:
    avg_pnl_pct = sum(t.get('pnl_pct', 0) or 0 for t in cyb_tp) / len(cyb_tp)
    print(f"  创业板止盈平均盈亏: {avg_pnl_pct:.2%}")
if main_tp:
    avg_pnl_pct = sum(t.get('pnl_pct', 0) or 0 for t in main_tp) / len(main_tp)
    print(f"  主板止盈平均盈亏: {avg_pnl_pct:.2%}")
print()

# 完整卖出类型统计（按板块）
from collections import Counter

print("各板块完整卖出类型分布:")
print("  科创板(688):")
for st, cnt in Counter(t.get('sell_type') for t in star_sells).items():
    print(f"    {st}: {cnt}")
print("  创业板(30):")
for st, cnt in Counter(t.get('sell_type') for t in cyb_sells).items():
    print(f"    {st}: {cnt}")
print("  主板(60/00):")
for st, cnt in Counter(t.get('sell_type') for t in main_sells).items():
    print(f"    {st}: {cnt}")
print()

# 买入交易按板块分布
star_buys = [t for t in buy_trades if str(t['code']).startswith('688')]
cyb_buys = [t for t in buy_trades if str(t['code']).startswith('30')]
main_buys = [t for t in buy_trades if not str(t['code']).startswith(('688', '30'))]

print("买入交易按板块分布:")
print(f"  科创板(688): {len(star_buys)} 笔")
print(f"  创业板(30):  {len(cyb_buys)} 笔")
print(f"  主板(60/00): {len(main_buys)} 笔")
print()

# ============================================================================
# 调查2：检查调仓池中科创板/创业板的占比
# ============================================================================
print("=" * 70)
print("调查2：调仓池构成分析")
print("=" * 70)

from data import DataManager
from strategy.strategy_v3 import StrategyV3
from engine.backtest_engine_v3 import BacktestEngineV3
import config

dm = DataManager(source='local')
strategy = StrategyV3()
engine = BacktestEngineV3(strategy, dm)

print("正在加载数据 (20250101 ~ 20260429)...")
engine._prepare_data('20250101', '20260429')
print(f"数据加载完成: {len(engine.all_data)} 只股票, {len(engine.trading_dates)} 个交易日")
print()

rebalance_dates = ['2025-01-02', '2025-07-01', '2026-01-05']

for date in rebalance_dates:
    if date not in engine.trading_dates:
        print(f"{date}: 非交易日，跳过")
        continue

    pool = strategy.build_rebalance_pool(engine.all_data, date, engine.trading_dates)
    star_count = sum(1 for c in pool if str(c).startswith('688'))
    cyb_count = sum(1 for c in pool if str(c).startswith('30'))
    main_count = len(pool) - star_count - cyb_count
    print(f"{date}: 调仓池共 {len(pool)} 只 -> 主板{main_count}, 创业板{cyb_count}, 科创板{star_count}")
print()

# ============================================================================
# 调查3：直接验证参数区分
# ============================================================================
print("=" * 70)
print("调查3：check_sell_signals 参数验证")
print("=" * 70)

strategy = StrategyV3()

# 打印实际使用的参数
print(f"主板止盈参数:   take_profit = {strategy.take_profit} ({strategy.take_profit:.0%})")
print(f"科创板止盈参数: star_take_profit = {strategy.star_take_profit} ({strategy.star_take_profit:.0%})")
print()

# 测试场景：买入价100，当前high=106（+6%）
# 主板5%止盈应该触发，科创板/创业板15%止盈不应触发

print("测试场景: buy_price=100.0, high=106.0 (+6%)")
print()

pos688 = {'code': '688270', 'buy_price': 100.0, 'quantity': 100, 'days_held': 3}
bar688 = {'open': 105.0, 'high': 106.0, 'low': 104.0, 'close': 105.5}
r688 = strategy.check_sell_signals(pos688, bar688)
print(f"  688270 (科创板) at +6%: should_sell={r688[0]}, sell_type={r688[1]}, mode={r688[2]}")

pos30 = {'code': '300033', 'buy_price': 100.0, 'quantity': 100, 'days_held': 3}
bar30 = {'open': 105.0, 'high': 106.0, 'low': 104.0, 'close': 105.5}
r30 = strategy.check_sell_signals(pos30, bar30)
print(f"  300033 (创业板) at +6%: should_sell={r30[0]}, sell_type={r30[1]}, mode={r30[2]}")

pos60 = {'code': '600000', 'buy_price': 100.0, 'quantity': 100, 'days_held': 3}
bar60 = {'open': 105.0, 'high': 106.0, 'low': 104.0, 'close': 105.5}
r60 = strategy.check_sell_signals(pos60, bar60)
print(f"  600000 (主板)   at +6%: should_sell={r60[0]}, sell_type={r60[1]}, mode={r60[2]}")
print()

# 额外测试：+16% 时科创板/创业板应该触发，主板也应该触发（因为5% < 16%）
print("测试场景: buy_price=100.0, high=116.0 (+16%)")
bar_high = {'open': 110.0, 'high': 116.0, 'low': 109.0, 'close': 115.0}

r688_h = strategy.check_sell_signals({'code': '688270', 'buy_price': 100.0, 'quantity': 100, 'days_held': 3}, bar_high)
r30_h = strategy.check_sell_signals({'code': '300033', 'buy_price': 100.0, 'quantity': 100, 'days_held': 3}, bar_high)
r60_h = strategy.check_sell_signals({'code': '600000', 'buy_price': 100.0, 'quantity': 100, 'days_held': 3}, bar_high)

print(f"  688270 (科创板) at +16%: should_sell={r688_h[0]}, sell_type={r688_h[1]}, mode={r688_h[2]}")
print(f"  300033 (创业板) at +16%: should_sell={r30_h[0]}, sell_type={r30_h[1]}, mode={r30_h[2]}")
print(f"  600000 (主板)   at +16%: should_sell={r60_h[0]}, sell_type={r60_h[1]}, mode={r60_h[2]}")
print()

print("=" * 70)
print("调查完成")
print("=" * 70)
