#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
最终修复脚本：从交易记录精确重建 cash 链
=========================================
1. 688448 除权调整 (10转3.5, factor=1.35): price/qty/amount/pnl
2. 从 300,000 逐笔重算所有 cash_after = prev_cash ± (price*qty ± fee)
3. state cash = 最终重算值

用法: python fix_cash_rebuild.py
"""
import json, os

BASE = os.path.dirname(os.path.abspath(__file__))
TRADES = os.path.join(BASE, 'trades_v4.json')
STATE  = os.path.join(BASE, 'state_v4.json')
FACTOR = 1.35

print("=" * 60)
print("Cash 链重建 + 688448 除权修正")
print("=" * 60)

with open(TRADES, 'r', encoding='utf-8') as f:
    trades = json.load(f)
with open(STATE, 'r', encoding='utf-8') as f:
    state = json.load(f)

# ── Step 1: 修正 688448 交易记录 ──
buy_idx = sell_idx = None
for i, t in enumerate(trades):
    if t['code'] == '688448':
        if t['type'] == 'buy':  buy_idx = i
        elif t['type'] == 'sell': sell_idx = i

assert buy_idx is not None and sell_idx is not None

bt = trades[buy_idx]
bt['price']    = round(bt['price'] / FACTOR, 3)       # 71.25 → 52.778
bt['quantity'] = int(round(bt['quantity'] * FACTOR))   # 800 → 1080
bt['amount']   = round(bt['price'] * bt['quantity'], 2)
bt['signal_px'] = round(bt.get('signal_px', bt['price']) / FACTOR, 4)
bt['order_px']  = round(bt['price'], 4)

st = trades[sell_idx]
old_sell_price = st['price']
old_sell_qty   = st['quantity']
st['price']    = 54.13          # 用户确认的实际成交价
st['quantity'] = int(round(old_sell_qty * FACTOR))     # 800 → 1080
st['amount']   = round(st['price'] * st['quantity'], 2)
st['fee']      = round(st.get('fee', 0) * (st['amount'] / max(old_sell_price * old_sell_qty, 1)), 2)
st['buy_price_ref'] = bt['price']

# pnl = (sell_price - buy_price) * qty - fee
pnl_val = round((st['price'] - bt['price']) * st['quantity'] - st['fee'], 2)
cost_val = bt['price'] * st['quantity']
st['pnl']     = pnl_val
st['pnl_pct'] = round(pnl_val / cost_val, 4) if cost_val > 0 else 0

print(f"买入 688448: {bt['price']} × {bt['quantity']} = {bt['amount']}")
print(f"卖出 688448: {st['price']} × {st['quantity']} = {st['amount']}  fee={st['fee']}  pnl={pnl_val:+.2f}")

# ── Step 2: 重建 cash 链 ──
initial = state['initial_capital']
cash = initial
print(f"\n初始资金: {initial:,.2f}")
print(f"{'#':>2} {'时间':19s} {'类型':4s} {'代码':6s} {'价格':>8s} {'数量':>5s} {'变动':>12s} {'新cash':>14s}")
print("-" * 85)

for i, t in enumerate(trades):
    price = t['price']
    qty   = t['quantity']
    fee   = t.get('fee', 0)
    if t['type'] == 'buy':
        delta = -(price * qty + fee)
    else:
        delta = +(price * qty - fee)
    cash += delta
    t['cash_after'] = round(cash, 2)
    print(f"{i:2d} {t['timestamp']} {t['type']:4s} {t['code']:6s} {price:8.3f} {qty:5d} {delta:+12.2f} {cash:14.2f}")

final_cash = round(cash, 2)
print(f"\n最终 cash: {final_cash:,.2f}")

# ── Step 3: 更新 state ──
state['cash'] = final_cash
pos = state.get('positions', {}).get('300319', {})
pos_cost = pos.get('buy_price', 0) * pos.get('quantity', 0)
state['total_value'] = round(final_cash + pos_cost, 2)

with open(TRADES, 'w', encoding='utf-8') as f:
    json.dump(trades, f, ensure_ascii=False, indent=2)
with open(STATE, 'w', encoding='utf-8') as f:
    json.dump(state, f, ensure_ascii=False, indent=2)
print(f"trades_v4.json + state_v4.json 已保存")

# ── 验证 ──
sell_pnl_sum = sum(t.get('pnl', 0) for t in trades if t['type'] == 'sell')
dash_pnl = final_cash + pos_cost - initial
print(f"\n── 验证 ──")
print(f"交易 pnl 之和:   {sell_pnl_sum:+,.2f}")
print(f"Dashboard 盈亏:  {dash_pnl:+,.2f}")
print(f"差异 (≈总手续费): {dash_pnl - sell_pnl_sum:+,.2f}")
print("=" * 60)
print("完成！刷新 Dashboard 查看结果。")
