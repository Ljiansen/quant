"""
修复两个问题:
1. 300319 sell 的 snapshot 参数 (fix_cash_rebuild 误写为 chop_else, 应为 chop_init)
2. 300319 sell 的 price/pnl 从信号价 20.30 修正为实际成交价 19.80
3. 重建完整 cash 链 (从 300,000 逐笔重算)
"""
import json, os, copy

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRADES   = os.path.join(BASE_DIR, 'trades_v4.json')
STATE    = os.path.join(BASE_DIR, 'state_v4.json')

# ── 引擎常量 (与 live_engine_v4.py 对齐) ──
SLIPPAGE        = 0.00015
COMMISSION_RATE = 0.0000854
MIN_COMMISSION  = 5.0
STAMP_TAX_RATE  = 0.0005

def sell_net(price, qty):
    actual = price * (1 - SLIPPAGE)
    commission = max(actual * qty * COMMISSION_RATE, MIN_COMMISSION)
    stamp_tax  = actual * qty * STAMP_TAX_RATE
    net = actual * qty - commission - stamp_tax
    return net, commission, stamp_tax

# ── 加载 ──
with open(TRADES, 'r', encoding='utf-8') as f:
    trades = json.load(f)
with open(STATE, 'r', encoding='utf-8') as f:
    state = json.load(f)

# ── 备份 ──
for path in [TRADES, STATE]:
    bak = path + '.bak_sellfix'
    if not os.path.exists(bak):
        with open(path, 'r', encoding='utf-8') as f:
            data = f.read()
        with open(bak, 'w', encoding='utf-8') as f:
            f.write(data)
        print(f"备份: {bak}")

# ══════════════════════════════════════════════
# Step 1: 修正所有 sell trade 的 price → order_px (实际成交价)
# ══════════════════════════════════════════════
print("\n=== Step 1: 修正 sell trade price ===")
for i, t in enumerate(trades):
    if t['type'] != 'sell':
        continue
    actual_sell = t.get('order_px')
    signal_px   = t.get('signal_px')
    if actual_sell is None or signal_px is None:
        continue
    # 跳过已经是正确成交价的 (如 300319 和 688448)
    if abs(t['price'] - actual_sell) < 0.001:
        print(f"  trade[{i}] {t['code']} sell: price={t['price']} 已正确，跳过")
        continue

    old_price = t['price']
    old_amount = t['amount']
    old_pnl = t['pnl']
    qty = t['quantity']

    t['price'] = actual_sell
    t['amount'] = round(actual_sell * qty, 2)

    # 用引擎公式重算 pnl
    net, comm, stamp = sell_net(actual_sell, qty)
    buy_cost = t['buy_price_ref'] * qty
    t['pnl'] = round(net - buy_cost, 2)
    t['pnl_pct'] = round((net - buy_cost) / buy_cost, 4)
    t['fee'] = round(comm + stamp, 2)

    # 修正 slippage
    t['slippage_amt'] = round(signal_px - actual_sell, 4)
    t['slippage_pct'] = round((signal_px - actual_sell) / signal_px, 6) if signal_px else 0

    print(f"  trade[{i}] {t['code']} sell:")
    print(f"    price:  {old_price} → {t['price']}")
    print(f"    amount: {old_amount} → {t['amount']}")
    print(f"    pnl:    {old_pnl} → {t['pnl']}")
    print(f"    fee:    → {t['fee']}")

# 300319 额外修正 snapshot (chop_init 参数)
for i, t in enumerate(trades):
    if t['code'] == '300319' and t['type'] == 'sell':
        old_snap = (t.get('snapshot_hs'), t.get('snapshot_ta'), t.get('snapshot_ts'))
        t['snapshot_hs'] = 0.068
        t['snapshot_ta'] = 0.24
        t['snapshot_ts'] = 0.01
        print(f"\n  300319 snapshot: {old_snap} → ({t['snapshot_hs']}, {t['snapshot_ta']}, {t['snapshot_ts']})")
        break

# ══════════════════════════════════════════════
# Step 2: 从 300,000 重建 cash 链
# ══════════════════════════════════════════════
print("\n=== Step 2: 重建 cash 链 ===")
INITIAL = 300000.0
cash = INITIAL

for i, t in enumerate(trades):
    price = t['price']
    qty   = t['quantity']
    fee   = t['fee']
    
    if t['type'] == 'buy':
        delta = -(price * qty + fee)
    else:
        # 对于 sell: 用引擎 sell_net 公式 (price 已经是 actual_sell)
        net, _, _ = sell_net(price, qty)
        delta = net

    old_cash = cash
    cash += delta
    cash = round(cash, 2)
    old_after = t.get('cash_after', '?')
    t['cash_after'] = cash
    
    print(f"  trade[{i}] {t['type']:4s} {t['code']} "
          f"price={price} qty={qty} delta={delta:+.2f} "
          f"cash: {old_cash:.2f} → {cash:.2f}  (was {old_after})")

print(f"\n  最终 cash = {cash:.2f}")
print(f"  state cash (旧) = {state['cash']:.2f}")
print(f"  差异 = {cash - state['cash']:+.2f}")

# ══════════════════════════════════════════════
# Step 3: 更新 state
# ══════════════════════════════════════════════
print("\n=== Step 3: 更新 state ===")
old_cash = state['cash']
old_tv = state['total_value']
state['cash'] = cash

# 重算 total_value = cash + 持仓市值 (用 highest_price)
pos_value = sum(
    p.get('highest_price', p['buy_price']) * p['quantity']
    for p in state.get('positions', {}).values()
)
state['total_value'] = round(cash + pos_value, 2)

print(f"  cash:        {old_cash:.2f} → {state['cash']:.2f}")
print(f"  total_value: {old_tv:.2f} → {state['total_value']:.2f}")
print(f"  累计盈亏:    {state['total_value'] - INITIAL:+.2f}")

# ══════════════════════════════════════════════
# Step 4: 写入
# ══════════════════════════════════════════════
with open(TRADES, 'w', encoding='utf-8') as f:
    json.dump(trades, f, ensure_ascii=False, indent=2)
with open(STATE, 'w', encoding='utf-8') as f:
    json.dump(state, f, ensure_ascii=False, indent=2)

print("\n✅ 修复完成!")
