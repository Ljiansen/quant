import json, os

BASE = os.path.dirname(os.path.abspath(__file__))
trades = json.load(open(os.path.join(BASE, 'trades_v4.json'), 'r', encoding='utf-8'))
state  = json.load(open(os.path.join(BASE, 'state_v4.json'), 'r', encoding='utf-8'))

SLIPPAGE = 0.00015; CR = 0.0000854; MC = 5.0; STR = 0.0005
def sell_net(price, qty):
    a = price * (1 - SLIPPAGE)
    c = max(a * qty * CR, MC)
    s = a * qty * STR
    return a * qty - c - s, c, s

print("=" * 90)
print(f"{'#':>2} {'type':4s} {'code':6s} {'price':>8} {'qty':>5} {'signal':>8} {'order':>8} {'reason':15s}")
print("-" * 90)
for i, t in enumerate(trades):
    sp = t.get('signal_px', '-')
    op = t.get('order_px', '-')
    print(f"{i:2d} {t['type']:4s} {t['code']:6s} {t['price']:8.3f} {t['quantity']:5d} {str(sp):>8s} {str(op):>8s} {t.get('reason','-'):15s}")
    if t['type'] == 'sell' and sp != '-' and op != '-':
        if abs(float(sp) - float(op)) > 0.01:
            print(f"   ⚠️  signal({sp}) ≠ order({op}), 当前price={t['price']}")

print("\n=== Cash 链验证 ===")
cash = 300000.0
print(f"{'#':>2} {'type':4s} {'code':6s} {'delta':>12} {'cash_after':>14} {'recorded':>14} {'match':>6}")
print("-" * 70)
for i, t in enumerate(trades):
    price, qty, fee = t['price'], t['quantity'], t['fee']
    if t['type'] == 'buy':
        delta = -(price * qty + fee)
    else:
        net, _, _ = sell_net(price, qty)
        delta = net
    cash += delta
    cash = round(cash, 2)
    rec = t['cash_after']
    ok = "✅" if abs(cash - rec) < 0.02 else f"❌{cash-rec:+.2f}"
    print(f"{i:2d} {t['type']:4s} {t['code']:6s} {delta:+12.2f} {cash:14.2f} {rec:14.2f} {ok:>6}")

print(f"\n最终 cash: 计算={cash:.2f}  state={state['cash']:.2f}  {'✅' if abs(cash-state['cash'])<0.02 else '❌'}")

print("\n=== 当前持仓 600378 止盈止损分析 ===")
pos = state['positions']['600378']
bp = pos['buy_price']
hp = pos['highest_price']
qty = pos['quantity']
hs = pos['snapshot_hs']
ta = pos['snapshot_ta']
ts = pos['snapshot_ts']
regime = pos['snapshot_regime']

hard_stop = round(bp * (1 - hs), 3)
trail_act_px = round(bp * (1 + ta), 3)
trail_active = hp >= trail_act_px
trail_px = round(hp * (1 - ts), 3) if trail_active else None

print(f"买入价:        {bp}")
print(f"最高价:        {hp}")
print(f"数量:          {qty}")
print(f"Regime:        {regime}")
print(f"硬止损 (hs):   {hs} → {hard_stop}")
print(f"激活阈值 (ta): {ta} → {trail_act_px}  ({'已激活' if trail_active else '未激活'})")
print(f"回撤止损 (ts): {ts} → {trail_px if trail_px else 'N/A'}")
if not trail_active:
    gap = trail_act_px - hp
    print(f"  距激活差:    {gap:.2f} ({gap/bp*100:.1f}%)")
print(f"浮盈:          {(hp-bp)/bp*100:+.1f}% (基于最高价)")
