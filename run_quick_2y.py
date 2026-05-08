#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""快速跑 2022 + 2025 baseline，验证 prev_bar_up=True + i==0跳过买入 的新逻辑"""
import sys, datetime
sys.path.insert(0, 'd:/miniqmt_quant')
import run_baseline_all_years as bl

t0 = datetime.datetime.now()
for yr in ['2022', '2025']:
    bl._log(f'\n>>> {yr}')
    r = bl.run_year(yr)
    if r:
        bl._log(f'  profit={r["profit_pct"]:+.2f}%  '
                f'max_dd={r["max_drawdown"]:.2f}%  '
                f'win_rate={r["win_rate"]:.1f}%  '
                f'sharpe={r["sharpe"]:.3f}  '
                f'trades={r["sell_trades"]}笔')
        bl._export_to_sim(yr, r['_nav_path'], r['_trade_path'])

elapsed = (datetime.datetime.now() - t0).seconds // 60
bl._log(f'\n完成，用时 {elapsed} 分钟')
bl._log_fh.close()
