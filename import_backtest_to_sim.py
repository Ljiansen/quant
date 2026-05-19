# -*- coding: utf-8 -*-
"""
把 backtest_v4_result.json 转换成 sim_results/ 格式，让 dashboard 模拟盘 Tab 能直接读取。
用法：python import_backtest_to_sim.py [backtest_json] [label]
  backtest_json: 默认 backtest_v4_result.json
  label:         显示在下拉框的自定义名称，默认用日期范围+收益率
示例：
  python import_backtest_to_sim.py
  python import_backtest_to_sim.py backtest_v4_result.json "V4基准回测"
"""
import os, sys, json, uuid
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SIM_DIR  = os.path.join(BASE_DIR, 'sim_results')
os.makedirs(SIM_DIR, exist_ok=True)

# ── 参数 ──────────────────────────────────────────
src_file = sys.argv[1] if len(sys.argv) > 1 else 'backtest_v4_result.json'
src_path = src_file if os.path.isabs(src_file) else os.path.join(BASE_DIR, src_file)

if not os.path.exists(src_path):
    print(f'[ERROR] 找不到文件: {src_path}')
    sys.exit(1)

with open(src_path, 'r', encoding='utf-8') as f:
    raw = json.load(f)

# ── 字段映射 ──────────────────────────────────────
equity_curve = raw.get('equity_curve', [])
raw_trades   = raw.get('trades', [])
raw_summary  = raw.get('summary', {})

initial = raw_summary.get('initial_capital', 300000)
final   = raw_summary.get('final_value', initial)
profit  = round(final - initial, 2)
profit_pct = raw_summary.get('total_return_pct', round((final - initial) / initial * 100, 3))

buy_trades  = [t for t in raw_trades if t.get('side') == 'buy']
sell_trades = [t for t in raw_trades if t.get('side') == 'sell']

# win_rate：backtest 里 win_rate_pct 是百分比整数（如 23.0），sim viewer 用 s.win_rate.toFixed(1) + '%'
win_rate   = raw_summary.get('win_rate_pct', 0)
max_dd     = raw_summary.get('max_drawdown_pct', 0)

# 起止日期
dates = [p['date'] for p in equity_curve if 'date' in p]
start_date = dates[0]  if dates else ''
end_date   = dates[-1] if dates else ''

# ── 交易记录转换 ──────────────────────────────────
# backtest: {date, side, code, price, qty, reason, fee, pnl, pnl_pct?, cash_after, days_held}
# sim viewer: t.action/direction/type, t.quantity/volume, t.time/timestamp/date, t.pnl, t.pnl_pct

def convert_trade(t):
    side = t.get('side', '')
    qty  = t.get('qty', t.get('quantity', 0))
    price = t.get('price', 0)
    pnl   = t.get('pnl')
    # 补算 pnl_pct（如果没有）
    pnl_pct = t.get('pnl_pct')
    if pnl is not None and pnl_pct is None:
        cost = t.get('buy_price', 0) * qty if t.get('buy_price') else 0
        if cost > 0:
            pnl_pct = round(pnl / cost * 100, 3)
    return {
        'date':      t.get('date', ''),
        'time':      t.get('date', ''),   # sim viewer 优先用 time
        'action':    side,                # buy / sell
        'code':      t.get('code', ''),
        'price':     price,
        'quantity':  qty,
        'amount':    round(price * qty, 2),
        'reason':    t.get('reason', ''),
        'fee':       t.get('fee', 0),
        'pnl':       pnl,
        'pnl_pct':   pnl_pct,
        'days_held': t.get('days_held'),
        'cash_after':t.get('cash_after'),
    }

trades_converted = [convert_trade(t) for t in raw_trades]

# ── run_id & 文件名 ──────────────────────────────
run_id    = f"v4_{start_date.replace('-','')}_{end_date.replace('-','')}"
run_time  = datetime.now().strftime('%Y-%m-%d %H:%M')
sign      = '+' if profit_pct >= 0 else ''

sim_data = {
    'run_id':          run_id,
    'start_date':      start_date,
    'end_date':        end_date,
    'run_time':        run_time,
    'initial_capital': initial,
    'summary': {
        'final_value':   round(final, 2),
        'profit':        profit,
        'profit_pct':    profit_pct,
        'win_rate':      win_rate,
        'max_drawdown':  max_dd,
        'buy_trades':    len(buy_trades),
        'sell_trades':   len(sell_trades),
        'trade_count':   len(raw_trades),
        'trading_days':  len(equity_curve),
    },
    'equity_curve': equity_curve,
    'trades':        trades_converted,
    'params': {
        'main_board': {},
        'star_board': {},
        'general': {
            'initial_capital': initial,
            'top_n': 50,
            'max_positions': 5,
            'buy_mode': 'close',
        }
    },
}

# 尝试从 live_engine_v4 读取实际参数
try:
    sys.path.insert(0, BASE_DIR)
    import engine.live_engine_v4 as ev4
    sim_data['params']['main_board'] = {
        'min_change_pct':    getattr(ev4, 'MIN_CHG_MAIN', 0.01),
        'max_change_pct':    getattr(ev4, 'MAX_CHG_MAIN', 0.035),
        'trailing_activate': getattr(ev4, 'TRAIL_ACT', 0.17),
        'trailing_stop':     getattr(ev4, 'TRAIL_STOP', 0.05),
        'hard_stop_loss':    getattr(ev4, 'HARD_STOP', 0.05),
        'limit_up':          0.098,
    }
    sim_data['params']['star_board'] = {
        'min_change_pct':    getattr(ev4, 'MIN_CHG_STAR', 0.001),
        'max_change_pct':    getattr(ev4, 'MAX_CHG_STAR', 0.055),
        'trailing_activate': getattr(ev4, 'TRAIL_ACT', 0.17),
        'trailing_stop':     getattr(ev4, 'TRAIL_STOP', 0.05),
        'hard_stop_loss':    getattr(ev4, 'HARD_STOP', 0.05),
        'limit_up':          0.198,
    }
    sim_data['params']['general']['max_positions'] = getattr(ev4, 'MAX_POSITIONS', 5)
    sim_data['params']['general']['ba_min_chg']    = getattr(ev4, 'BA_MIN_CHG', 0.01)
    sim_data['params']['general']['ba_max_chg']    = getattr(ev4, 'BA_MAX_CHG', 0.07)
    sim_data['params']['general']['ba_lookback']   = getattr(ev4, 'BA_LOOKBACK', 120)
except Exception as e:
    print(f'[WARN] 读取 live_engine_v4 参数失败，用默认值: {e}')

out_path = os.path.join(SIM_DIR, f'{run_id}.json')
with open(out_path, 'w', encoding='utf-8') as f:
    json.dump(sim_data, f, ensure_ascii=False, indent=2)

print(f'[OK] 已写入: {out_path}')
print(f'     区间: {start_date} ~ {end_date}')
print(f'     收益: {sign}{profit_pct:.2f}%  ({sign}¥{profit:,.0f})')
print(f'     胜率: {win_rate:.1f}%  最大回撤: {max_dd:.2f}%')
print(f'     交易: {len(buy_trades)}买 / {len(sell_trades)}卖  共{len(equity_curve)}个交易日')
print()
print('现在启动 dashboard，切换到「模拟盘」Tab 即可看到净值曲线。')
print('  python run_dashboard.py')
