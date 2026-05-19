# -*- coding: utf-8 -*-
"""
market_open_check.py
====================
开盘前一键诊断脚本，检查持仓/调仓池/5min预缓存状态。

用法：
    python market_open_check.py
"""

import json, os, glob
from datetime import datetime, date

print('=' * 55)
print('开盘前诊断报告', datetime.now().strftime('%Y-%m-%d %H:%M'))
print('=' * 55)

# 1. 持仓与资金
with open('state_v3.json') as f:
    s = json.load(f)
print(f'[持仓] {len(s["positions"])} 只 | 现金 {s["cash"]:,.2f}')
print(f'[状态更新] {s["last_update"]}')
ps = s.get('pending_sells', [])
if ps:
    print(f'[待卖] {len(ps)} 只 -> {[p["code"] for p in ps]}  ← 今日9:15自动挂竞价卖单')
else:
    print('[待卖] 无 pending_sells')

# 2. 调仓池
with open('state_v3_rebalance.json') as f:
    pool = json.load(f)
print(f'[调仓池] {len(pool["pool"])} 只 | rebalance_date={pool.get("rebalance_date")}')

# 3. 5min 预缓存
files = glob.glob('5min_next_pool/*.csv')
if files:
    latest = max(files, key=os.path.getmtime)
    mtime = datetime.fromtimestamp(os.path.getmtime(latest)).strftime('%Y-%m-%d %H:%M')
    print(f'[5min池] {len(files)} 只 | 最新={os.path.basename(latest)} ({mtime})')
else:
    print('[5min池] !! 无文件，需手动补：python init_rebalance_pool.py !!')

print('=' * 55)
