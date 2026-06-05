#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一次性修复脚本 v2：修正 688448 除权导致的交易数据错误
=======================================================
背景：688448 于 2026-06-02 除权（10转3.5），复权因子 1.35
  系统记录了除权前的价格/数量，导致 P&L 显示 -15,032（实际仅约 -300）

v2 修正：只改 price/qty/amount/pnl 等显示字段
         不修改 cash_after 和 state cash（cash 是真实 broker 资金流）

用法：
  python fix_exrights_688448.py
"""
import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRADES_FILE = os.path.join(BASE_DIR, 'trades_v4.json')
STATE_FILE  = os.path.join(BASE_DIR, 'state_v4.json')

FACTOR = 1.35  # 10转3.5

print("=" * 60)
print("688448 除权修复 v2 (10转3.5, factor=1.35)")
print("仅修正显示字段，不修改 cash")
print("=" * 60)

# ── 1. 修正 trades_v4.json ──
with open(TRADES_FILE, 'r', encoding='utf-8') as f:
    trades = json.load(f)

buy_idx = sell_idx = None
for i, t in enumerate(trades):
    if t['code'] == '688448':
        if t['type'] == 'buy':
            buy_idx = i
        elif t['type'] == 'sell':
            sell_idx = i

assert buy_idx is not None and sell_idx is not None

# ── 修正买入记录 ──
bt = trades[buy_idx]
old_price, old_qty = bt['price'], bt['quantity']
bt['price']    = round(old_price / FACTOR, 3)    # 71.25 → 52.778
bt['quantity'] = int(round(old_qty * FACTOR))     # 800 → 1080
bt['amount']   = round(bt['price'] * bt['quantity'], 2)
bt['signal_px'] = round(bt['signal_px'] / FACTOR, 4)
bt['order_px']  = round(bt['price'], 4)
# cash_after 保持不变
print(f"买入: {old_price}×{old_qty} → {bt['price']}×{bt['quantity']}  "
      f"amt={bt['amount']}  cash_after 不变({bt['cash_after']})")

# ── 修正卖出记录 ──
st = trades[sell_idx]
old_price, old_qty = st['price'], st['quantity']
new_sell_price = st.get('order_px', old_price)  # 实际成交价 = order_px
st['price']    = round(new_sell_price, 3)       # 用实际成交价
st['quantity'] = int(round(old_qty * FACTOR))    # 800 → 1080
st['amount']   = round(st['price'] * st['quantity'], 2)
st['fee']      = round(st['fee'] * (st['amount'] / (old_price * old_qty)), 2)
st['buy_price_ref'] = bt['price']

# 重算 pnl（与 dashboard backfill 公式一致）
# pnl = (sell_price - buy_price) * qty - fee
pnl = round((st['price'] - bt['price']) * st['quantity'] - st['fee'], 2)
st['pnl']     = pnl
cost = bt['price'] * st['quantity']
st['pnl_pct'] = round(pnl / cost, 4) if cost > 0 else 0
# cash_after 保持不变

print(f"卖出: {old_price}×{old_qty} → {st['price']}×{st['quantity']}  "
      f"amt={st['amount']}  fee={st['fee']}  pnl={st['pnl']:+.2f}  "
      f"cash_after 不变({st['cash_after']})")

with open(TRADES_FILE, 'w', encoding='utf-8') as f:
    json.dump(trades, f, ensure_ascii=False, indent=2)
print(f"\ntrades_v4.json 已保存")

# ── 2. state_v4.json: 不修改 ──
print("state_v4.json: 不修改（cash 是真实 broker 资金流）")

# ── 验证 ──
print("\n── 验证 ──")
sell_pnl_sum = sum(t.get('pnl', 0) for t in trades if t['type'] == 'sell')
with open(STATE_FILE, 'r', encoding='utf-8') as f:
    state = json.load(f)
pos = state['positions'].get('300319', {})
pos_cost = pos.get('buy_price', 0) * pos.get('quantity', 0)
dashboard_pnl = state['cash'] + pos_cost - state['initial_capital']
print(f"交易 pnl 之和:  {sell_pnl_sum:+,.2f}")
print(f"Dashboard 盈亏:   {dashboard_pnl:+,.2f}")
print(f"差异 (应≈手续费):  {dashboard_pnl - sell_pnl_sum:+,.2f}")

print("\n" + "=" * 60)
print("修复完成！")
print(f"  688448 P&L: -15,032.30 → {st['pnl']:+,.2f} ({st['pnl_pct']:+.2%})")
print("=" * 60)
