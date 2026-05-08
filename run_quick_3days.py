#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
最新最优参数 回测最近3个交易日（20260506~20260508）
ta=2%, ts=1%, hs=3%, ss=2%, td=5
"""
import sys
sys.path.insert(0, 'd:/miniqmt_quant')

import run_backtest_5min_live_sim as sim

# ── 注入新最优参数 ─────────────────────────────────────────────────────────────
sim.TRAILING_ACTIVATE      = 0.02
sim.TRAILING_STOP          = 0.01
sim.STAR_TRAILING_ACTIVATE = sim.STAR_TRAILING_ACTIVATE  # 科创板保持不变
sim.HARD_STOP_LOSS         = 0.03
sim.STAR_HARD_STOP_LOSS    = 0.03
sim.SOFT_STOP_LOSS         = 0.02
sim.STAR_SOFT_STOP_LOSS    = 0.02
sim.TIME_STOP_DAYS         = 5
sim.STAR_TIME_STOP_DAYS    = 5

print("=" * 60)
print("新最优参数 3日回测：20260506 ~ 20260508")
print("ta=2%  ts=1%  hs=3%  ss=2%  td=5")
print("=" * 60)

# ── 拦截 _save_reports 以捕获 nav/trades 并打印详细明细 ────────────────────
_captured_nav    = []
_captured_trades = []

_orig_save = sim._save_reports

def _my_save(nav_series, trades, *args, **kwargs):
    _captured_nav.extend(nav_series)
    _captured_trades.extend(trades)
    _orig_save(nav_series, trades, *args, **kwargs)  # 正常走报告流程

sim._save_reports = _my_save

sim.run_simulation(
    '20260506', '20260508',
    initial_capital=300000,
    buy_price_mode='close',
)

# ── 打印详细交易明细 ──────────────────────────────────────────────────────────
print("\n=== 交易明细（卖出）===")
print(f"{'日期':<12} {'代码':<12} {'买入价':>8} {'卖出价':>8} {'数量':>7} {'盈亏':>10} {'类型':<16}")
print("-" * 75)
sell_trades = [t for t in _captured_trades if t.get('action') != 'buy']
for t in sell_trades:
    pnl   = t.get('pnl', 0)
    pnl_s = f"{pnl:+.2f}" if isinstance(pnl, (int, float)) else ''
    stype = t.get('sell_type', t.get('reason', '')) or ''
    print(f"{t.get('date',''):<12} {t.get('code',''):<12} "
          f"{t.get('buy_price',0):>8.3f} {t.get('price',0):>8.3f} "
          f"{t.get('quantity',0):>7} {pnl_s:>10} {stype:<16}")
