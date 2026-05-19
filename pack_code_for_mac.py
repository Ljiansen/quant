# -*- coding: utf-8 -*-
"""
打包回测相关核心代码，一键发给 Mac 分析。

用法：
  python pack_code_for_mac.py
"""

import os
import zipfile
import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── 要打包的文件（相对于项目根目录）────────────────────────────────────────────
FILES = [
    # 核心引擎
    'engine/live_engine_v4.py',
    'engine/offline_sim_engine_v4.py',
    # 回测入口
    'run_backtest_v4_weekly.py',
]

# ── 打包 ──────────────────────────────────────────────────────────────────────
ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
zip_name = os.path.join(BASE_DIR, f'code_for_mac_{ts}.zip')

found, missing = [], []
for rel in FILES:
    full = os.path.join(BASE_DIR, rel.replace('/', os.sep))
    if os.path.exists(full):
        found.append((full, rel))
    else:
        missing.append(rel)

if missing:
    print("[pack] 以下文件不存在，跳过：")
    for m in missing:
        print(f"  - {m}")

if not found:
    print("[pack] 没有找到任何文件，退出。")
    raise SystemExit(1)

with zipfile.ZipFile(zip_name, 'w', zipfile.ZIP_DEFLATED) as zf:
    for full, arc in found:
        zf.write(full, arc)
        size_kb = os.path.getsize(full) // 1024
        print(f"[pack]   + {arc}  ({size_kb} KB)")

total_kb = os.path.getsize(zip_name) // 1024
print(f"\n[pack] 完成：{zip_name}")
print(f"[pack] 共 {len(found)} 个文件，zip 大小：{total_kb} KB")
