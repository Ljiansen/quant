import pickle, sys
sys.path.insert(0, 'd:/miniqmt_quant')
with open('backtest_result.pkl', 'rb') as f:
    result = pickle.load(f)
trades = result['trades']
cyb_tp = [t for t in trades if t['direction']=='sell' and str(t['code']).startswith('30') and t.get('sell_type')=='take_profit']
print('创业板止盈交易明细 (按pnl_pct排序):')
for t in sorted(cyb_tp, key=lambda x: x['pnl_pct']):
    print(f"  {t['date']} {t['code']} 买入价:{t.get('price',0):.2f} 持仓{t['days_held']}天 盈亏:{t['pnl_pct']:.2%}")
print()
star_tp = [t for t in trades if t['direction']=='sell' and str(t['code']).startswith('688') and t.get('sell_type')=='take_profit']
print('科创板止盈交易明细 (按pnl_pct排序):')
for t in sorted(star_tp, key=lambda x: x['pnl_pct']):
    print(f"  {t['date']} {t['code']} 买入价:{t.get('price',0):.2f} 持仓{t['days_held']}天 盈亏:{t['pnl_pct']:.2%}")
