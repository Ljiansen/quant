# -*- coding: utf-8 -*-
"""
V4 回测运行脚本
用法: python run_backtest_v4_weekly.py [start_date] [end_date]
示例: python run_backtest_v4_weekly.py 2026-05-07 2026-05-15
"""
import sys
import os
sys.path.insert(0, 'd:/miniqmt_quant')

from engine.offline_sim_engine_v4 import OfflineSimEngineV4

# 默认回测本周
start_date = sys.argv[1] if len(sys.argv) > 1 else '2026-05-07'
end_date   = sys.argv[2] if len(sys.argv) > 2 else '2026-05-15'
capital    = float(sys.argv[3]) if len(sys.argv) > 3 else 300_000.0

engine = OfflineSimEngineV4(capital=capital)
engine.run(start_date=start_date, end_date=end_date)
