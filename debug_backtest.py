# -*- coding: utf-8 -*-
"""
回测前5个有交易发生的交易日详细调试脚本
逐笔分析资金变化，不修改原有引擎代码
"""
import sys
import math
import copy

sys.path.insert(0, 'd:/miniqmt_quant')

import pandas as pd
import config
from data import DataManager
from strategy.strategy_v3 import StrategyV3
from engine.backtest_engine_v3 import BacktestEngineV3

# ============================================================
# 初始化
# ============================================================
START_DATE = '20260101'
END_DATE   = '20260429'

v3_source = getattr(config, 'V3_DATA_SOURCE', 'local')
dm = DataManager(source=v3_source)
strategy = StrategyV3()
engine = BacktestEngineV3(strategy, dm)

# 数据准备（复用引擎方法）
print("正在准备数据，请稍候...")
engine._prepare_data(START_DATE, END_DATE)

start_fmt = f"{START_DATE[:4]}-{START_DATE[4:6]}-{START_DATE[6:]}"
end_fmt   = f"{END_DATE[:4]}-{END_DATE[4:6]}-{END_DATE[6:]}"

trading_dates = [
    d for d in engine.trading_dates
    if start_fmt <= d <= end_fmt
]
print(f"回测区间交易日共 {len(trading_dates)} 天\n")

# ============================================================
# 回测状态
# ============================================================
cash          = float(engine.initial_capital)
positions     = {}           # {code: pos_dict}
pending_sells = []
rebalance_pool = []
prev_nav      = cash

trade_days_count = 0         # 有交易发生的天数
MAX_DEBUG_DAYS   = 5         # 只调试前5个有交易发生的天

# ============================================================
# 逐日循环
# ============================================================
for date in trading_dates:
    if trade_days_count >= MAX_DEBUG_DAYS:
        break

    # ---------- 开盘前状态 ----------
    header_printed = False

    def maybe_print_header():
        global header_printed
        if not header_printed:
            print(f"\n{'='*60}")
            print(f"========== 交易日: {date} ==========")
            print(f"{'='*60}")
            print(f"\n【开盘前状态】")
            print(f"  现金: {cash:.2f} 元")
            if positions:
                pos_list = []
                for c, p in positions.items():
                    pos_list.append(f"{c}: 数量{p['quantity']}, 成本价{p['buy_price']:.2f}, 持仓{p['days_held']}天")
                print(f"  持仓: " + " | ".join(pos_list))
            else:
                print(f"  持仓: 空仓")
            header_printed = True

    # ---- Step 1: 执行 Pending 卖出 ----
    step1_has_action = bool(pending_sells and any(p['code'] in positions for p in pending_sells))
    if step1_has_action:
        maybe_print_header()
        print(f"\n【Step 1: 执行Pending卖出】")

    executed_codes = set()
    for pending in pending_sells:
        code = pending['code']
        if code not in positions:
            continue

        bar_open = engine._get_bar(code, date)
        if bar_open is None:
            continue

        open_price = bar_open['open']
        quantity   = pending['quantity']

        actual_sell_price, net_income, commission, stamp_tax = \
            strategy.calculate_sell_income(open_price, quantity)

        sell_amount = actual_sell_price * quantity
        cash_before = cash
        cash += net_income

        pos = positions[code]
        print(f"  卖出 {code}, 数量{quantity}, 卖出价(开盘价)={open_price:.2f}")
        print(f"    卖出金额(含滑点) = {quantity} × {open_price:.2f} × (1-{strategy.slippage}) = {sell_amount:.2f}")
        print(f"    佣金 = max({sell_amount:.2f} × {strategy.commission_rate}, {strategy.min_commission}) = {commission:.2f}")
        print(f"    印花税 = {sell_amount:.2f} × {strategy.stamp_tax_rate} = {stamp_tax:.2f}")
        print(f"    实际收入 = {sell_amount:.2f} - {commission:.2f} - {stamp_tax:.2f} = {net_income:.2f}")
        print(f"    现金变化: {cash_before:.2f} → {cash:.2f}")

        del positions[code]
        executed_codes.add(code)

    if step1_has_action and not executed_codes:
        print("  无")
    elif not step1_has_action:
        pass  # 不打印 step1（无pending）

    pending_sells = [p for p in pending_sells if p['code'] not in executed_codes]

    # ---- Step 2: 检查卖出条件 ----
    step2_candidates = {c: p for c, p in positions.items() if p.get('days_held', 0) > 0}
    step2_has_action = bool(step2_candidates)

    codes_to_remove = []
    new_pending = []

    if step2_has_action:
        maybe_print_header()
        print(f"\n【Step 2: 检查卖出条件】")

    for code, pos in list(positions.items()):
        if pos.get('days_held', 0) == 0:
            continue

        bar = engine._get_bar(code, date)
        if bar is None:
            continue

        buy_price = pos['buy_price']
        days_held = pos['days_held']
        is_star   = strategy._is_star(code)

        hard_sl   = strategy.star_hard_stop_loss if is_star else strategy.hard_stop_loss
        soft_sl   = strategy.star_soft_stop_loss if is_star else strategy.soft_stop_loss
        tp        = strategy.star_take_profit if is_star else strategy.take_profit
        time_stop = strategy.star_time_stop_days if is_star else strategy.time_stop_days

        hard_line   = buy_price * (1 - hard_sl)
        soft_line   = buy_price * (1 - soft_sl)
        profit_line = buy_price * (1 + tp)

        print(f"  持仓 {code}:")
        print(f"    当日行情: open={bar['open']:.2f}, high={bar['high']:.2f}, "
              f"low={bar['low']:.2f}, close={bar['close']:.2f}")
        print(f"    成本价: {buy_price:.2f}, 持仓天数: {days_held}")

        # 硬止损
        hard_triggered = bar['low'] <= hard_line
        print(f"    硬止损线: cost×{1-hard_sl:.2f} = {hard_line:.2f}, "
              f"当日low = {bar['low']:.2f} → {'触发' if hard_triggered else '未触发'}")

        # 阴跌止损
        soft_triggered = (bar['close'] < soft_line) and (bar['close'] < bar['open'])
        print(f"    阴跌止损线: cost×{1-soft_sl:.2f} = {soft_line:.2f}, "
              f"当日close = {bar['close']:.2f}, close<open? {'是' if bar['close'] < bar['open'] else '否'} "
              f"→ {'触发' if soft_triggered else '未触发'}")

        # 止盈
        profit_triggered = bar['high'] >= profit_line
        print(f"    止盈线: cost×{1+tp:.2f} = {profit_line:.2f}, "
              f"当日high = {bar['high']:.2f} → {'触发' if profit_triggered else '未触发'}")

        # 时间止损
        time_triggered = (days_held >= time_stop) and (bar['close'] <= buy_price)
        print(f"    时间止损: 持仓{days_held}>={time_stop}天? {'是' if days_held >= time_stop else '否'}, "
              f"close{bar['close']:.2f}<=cost{buy_price:.2f}? {'是' if bar['close'] <= buy_price else '否'} "
              f"→ {'触发' if time_triggered else '未触发'}")

        should_sell, sell_type, execution_mode, sell_price_val = \
            strategy.check_sell_signals(pos, bar)

        if not should_sell:
            print(f"    结果: 无卖出")
        elif execution_mode == 'immediate':
            print(f"    结果: 硬止损立即执行 (止损价=max(止损线{hard_line:.2f}, 开盘价{bar['open']:.2f})={sell_price_val:.2f})")
            quantity = pos['quantity']
            actual_sell_price, net_income, commission, stamp_tax = \
                strategy.calculate_sell_income(sell_price_val, quantity)
            sell_amount = actual_sell_price * quantity
            cash_before = cash
            cash += net_income
            print(f"      立即卖出: 数量{quantity}, 卖出价={sell_price_val:.2f}(止损价)")
            print(f"      卖出金额 = {quantity} × {sell_price_val:.2f} = {sell_amount:.2f}")
            print(f"      佣金 = max({sell_amount:.2f} × {strategy.commission_rate}, {strategy.min_commission}) = {commission:.2f}")
            print(f"      印花税 = {sell_amount:.2f} × {strategy.stamp_tax_rate} = {stamp_tax:.2f}")
            print(f"      实际收入 = {sell_amount:.2f} - {commission:.2f} - {stamp_tax:.2f} = {net_income:.2f}")
            print(f"      现金变化: {cash_before:.2f} → {cash:.2f}")
            codes_to_remove.append(code)
        else:
            print(f"    结果: Pending卖出({sell_type})，明日开盘执行")
            new_pending.append({'code': code, 'quantity': pos['quantity'], 'sell_type': sell_type})

    for code in codes_to_remove:
        if code in positions:
            del positions[code]
    pending_sells.extend(new_pending)

    # ---- Step 3: 调仓检查 ----
    is_rebalance_day = engine._is_rebalance_day(date, trading_dates)
    if is_rebalance_day or not rebalance_pool:
        rebalance_pool = strategy.build_rebalance_pool(
            engine.all_data, date, trading_dates
        )

    # ---- Step 4: 二次过滤 ----
    daily_market_df = engine._get_daily_market(date, rebalance_pool)
    tradable_pool = strategy.daily_filter(
        rebalance_pool, engine.all_data, date, daily_market_df
    )

    # ---- Step 5: 检查买入 ----
    bought_today = []
    buy_decisions = []
    candidate_details = []

    if len(positions) < strategy.max_positions:
        empty_slots_before_buy = strategy.max_positions - len(positions)
        shown = 0
        for code in tradable_pool:
            if code in positions:
                continue
            bar = engine._get_bar(code, date)
            if bar is None:
                continue
            pre_close = engine._get_pre_close(code, date)
            if pre_close is None or pre_close == 0:
                continue

            change_pct = (bar['close'] - pre_close) / pre_close
            is_yang = bar['close'] > bar['open']
            limit_up = strategy.get_limit_up_threshold(code)
            is_limit = change_pct >= limit_up
            meets = strategy.check_buy_signal(code, bar, pre_close)

            if shown < 5:
                candidate_details.append({
                    'code': code, 'change_pct': change_pct,
                    'is_yang': is_yang, 'is_limit': is_limit,
                    'meets': meets, 'bar': bar, 'pre_close': pre_close,
                })
                shown += 1

            if not meets:
                continue

            # 计算买入
            volume = strategy.calculate_buy_volume(cash, len(positions), bar['close'])
            if volume <= 0:
                continue

            actual_buy_price, total_cost, commission = \
                strategy.calculate_buy_cost(bar['close'], volume)

            if total_cost > cash:
                continue

            cash_before = cash
            cash -= total_cost

            positions[code] = {
                'code': code,
                'name': engine.stock_names.get(code, ''),
                'buy_price': actual_buy_price,
                'buy_date': date,
                'quantity': volume,
                'days_held': 0,
                'buy_commission': commission,
            }
            bought_today.append(code)

            buy_decisions.append({
                'code': code,
                'close': bar['close'],
                'cash_before': cash_before,
                'cash_after': cash,
                'volume': volume,
                'actual_price': actual_buy_price,
                'total_cost': total_cost,
                'commission': commission,
                'alloc': (cash_before + total_cost) / (strategy.max_positions - (len(positions) - 1)),
            })

            if len(positions) >= strategy.max_positions:
                break

    # ---- Step 6: 更新持仓天数 ----
    for pos in positions.values():
        pos['days_held'] += 1

    # ---- Step 7: 计算净值 ----
    holdings_value = 0.0
    holdings_detail = []
    for c, pos in positions.items():
        bar = engine._get_bar(c, date)
        if bar is not None:
            val = pos['quantity'] * bar['close']
            holdings_value += val
            holdings_detail.append((c, pos['quantity'], bar['close'], val))

    nav = cash + holdings_value
    daily_ret = (nav - prev_nav) / prev_nav * 100 if prev_nav > 0 else 0.0
    prev_nav = nav

    # ---- 判断是否有交易发生 ----
    has_trade = bool(executed_codes) or bool(codes_to_remove) or bool(new_pending) or bool(bought_today)
    if not has_trade:
        continue

    # ---- 打印剩余步骤 ----
    maybe_print_header()

    # Step 3
    print(f"\n【Step 3: 调仓检查】")
    print(f"  是否调仓日: {'是' if (is_rebalance_day or not rebalance_pool) else '否'}")
    print(f"  调仓池: {len(rebalance_pool)} 只股票", end="")
    if rebalance_pool:
        print(f"  (前10只: {rebalance_pool[:10]})")
    else:
        print()

    # Step 4
    print(f"\n【Step 4: 二次过滤】")
    print(f"  过滤前: {len(rebalance_pool)} 只")
    print(f"  过滤后(去ST/低流动性/停牌): {len(tradable_pool)} 只")

    # Step 5
    print(f"\n【Step 5: 检查买入】")
    cur_pos_count = len(positions) - len(bought_today)
    print(f"  当前持仓数: {cur_pos_count}, 最大持仓: {strategy.max_positions}, "
          f"空仓位: {strategy.max_positions - cur_pos_count}")
    if candidate_details:
        print(f"  遍历候选(前{len(candidate_details)}个):")
        for cd in candidate_details:
            is_star_flag = strategy._is_star(cd['code'])
            threshold = strategy.star_min_change_pct if is_star_flag else strategy.min_change_pct
            print(f"    {cd['code']}: 涨幅={cd['change_pct']*100:.2f}%(>{threshold*100:.1f}%?{'是' if cd['change_pct'] > threshold else '否'}), "
                  f"收阳?{'是' if cd['is_yang'] else '否'}, "
                  f"涨停?{'是' if cd['is_limit'] else '否'} → {'满足' if cd['meets'] else '不满足'}")
    else:
        print("  (无候选股票)")

    if buy_decisions:
        print(f"  买入决策:")
        for bd in buy_decisions:
            alloc = bd['cash_before'] / (strategy.max_positions - (len(positions) - len(bought_today) - buy_decisions.index(bd)))
            actual_p = bd['close'] * (1 + strategy.slippage)
            print(f"    买入 {bd['code']}, 价格(close)={bd['close']:.2f}")
            print(f"      单只金额 = 可用资金/空仓位数 = {bd['cash_before'] + bd['total_cost']:.2f}/{strategy.max_positions - cur_pos_count} = {(bd['cash_before'] + bd['total_cost'])/(strategy.max_positions - cur_pos_count):.2f}")
            print(f"      买入价(含滑点) = {bd['close']:.2f} × {1+strategy.slippage} = {actual_p:.4f}")
            print(f"      买入股数 = floor({(bd['cash_before'] + bd['total_cost'])/(strategy.max_positions - cur_pos_count):.2f} / {actual_p:.4f} / 100) × 100 = {bd['volume']}")
            print(f"      买入金额 = {bd['volume']} × {actual_p:.4f} = {bd['volume'] * actual_p:.2f}")
            print(f"      佣金 = max({bd['volume'] * actual_p:.2f} × {strategy.commission_rate}, {strategy.min_commission}) = {bd['commission']:.2f}")
            print(f"      实际支出 = {bd['volume'] * actual_p:.2f} + {bd['commission']:.2f} = {bd['total_cost']:.2f}")
            print(f"      现金变化: {bd['cash_before']:.2f} → {bd['cash_after']:.2f}")
    else:
        print(f"  买入决策: 无买入")

    # Step 6
    print(f"\n【Step 6: 更新持仓天数】")
    if positions:
        for c, pos in positions.items():
            print(f"  {c}: {pos['days_held']-1} → {pos['days_held']} 天")
    else:
        print("  无持仓")

    # 收盘后状态
    print(f"\n【收盘后状态】")
    print(f"  现金: {cash:.2f} 元")
    if holdings_detail:
        print(f"  持仓市值: {holdings_value:.2f} 元")
        for c, qty, cls, val in holdings_detail:
            print(f"    {c}: {qty}股 × {cls:.2f} = {val:.2f}")
    else:
        print(f"  持仓市值: 0.00 元")
    print(f"  总净值: {nav:.2f} 元")
    print(f"  当日收益率: {daily_ret:.2f}%")

    trade_days_count += 1

print(f"\n\n{'='*60}")
print(f"调试完毕，共展示了 {trade_days_count} 个有交易发生的交易日")
print(f"{'='*60}")
