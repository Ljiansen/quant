import json
MAC_RET = 69.5568
MAC_NAV = 508670.46
MAC_NBUY = 21
MAC_NSELL = 21

with open('d:/miniqmt_quant/backtest_v4_result.json') as f:
    d = json.load(f)
final = d['equity_curve'][-1]
cash = final['cash']
win_ret = (cash - 300000) / 300000 * 100
diff = win_ret - MAC_RET
status = '✅' if abs(diff) < 0.05 else '⚠️' if abs(diff) < 0.5 else '❌'

n_buy  = sum(1 for t in d['trades'] if t['side']=='buy')
n_sell = sum(1 for t in d['trades'] if t['side']=='sell')

print(f"2026Q1: win={win_ret:.4f}% mac={MAC_RET:.4f}% diff={diff:+.4f}pp {status}")
print(f"  final_cash={cash:,.2f}  mac_nav={MAC_NAV:,.2f}  diff={cash - MAC_NAV:+.2f}")
print(f"  buy/sell={n_buy}/{n_sell}  mac={MAC_NBUY}/{MAC_NSELL}  match={n_buy==MAC_NBUY and n_sell==MAC_NSELL}")
print(f"  positions_count={final['positions_count']} (必须=0)")

from collections import Counter
sells = [t for t in d['trades'] if t['side']=='sell']
by_reason = Counter(t['reason'] for t in sells)
print(f"  sell by reason: {dict(by_reason)} (期望 hard_stop=10, trailing_stop=6, end_of_sim=5)")