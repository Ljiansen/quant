# -*- coding: utf-8 -*-
"""
运行 export_detailed_backtest.py 并将 5 个 CSV 打包为 zip。

用法：
  python pack_debug_export.py                      # 默认区间 2026-01-02 ~ 2026-04-30
  python pack_debug_export.py 2026-01-02 2026-04-30
"""

import sys
import os
import subprocess
import zipfile
import datetime

# ── 参数 ──────────────────────────────────────────────────────────────────────
START = sys.argv[1] if len(sys.argv) > 1 else '2026-01-02'
END   = sys.argv[2] if len(sys.argv) > 2 else '2026-04-30'

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
SNAP_DATE = END.replace('-', '')
BARS_CODE = '002866'
BARS_DATE = '20260330'

# 5 个目标文件（与 export_detailed_backtest.py 保持一致）
TARGET_FILES = [
    os.path.join(BASE_DIR, 'detailed_buys.csv'),
    os.path.join(BASE_DIR, 'detailed_sells.csv'),
    os.path.join(BASE_DIR, f'snapshot_{SNAP_DATE}.csv'),
    os.path.join(BASE_DIR, 'daily_ba_pools.csv'),
    os.path.join(BASE_DIR, f'bars_{BARS_CODE}_{BARS_DATE}.csv'),
]

# ── Step 1: 运行导出脚本 ───────────────────────────────────────────────────────
print(f"[pack] 正在运行 export_detailed_backtest.py {START} {END} ...")
ret = subprocess.run(
    [sys.executable, os.path.join(BASE_DIR, 'export_detailed_backtest.py'), START, END],
    cwd=BASE_DIR,
)
if ret.returncode != 0:
    print(f"[pack] export 脚本异常退出 (code={ret.returncode})，中止打包。")
    sys.exit(1)

# ── Step 2: 打包 zip ──────────────────────────────────────────────────────────
ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
zip_name = os.path.join(BASE_DIR, f'debug_export_{ts}.zip')

missing = [f for f in TARGET_FILES if not os.path.exists(f)]
if missing:
    print("[pack] 警告：以下文件不存在，跳过：")
    for m in missing:
        print(f"  - {m}")

found = [f for f in TARGET_FILES if os.path.exists(f)]
if not found:
    print("[pack] 没有找到任何目标文件，打包失败。")
    sys.exit(1)

with zipfile.ZipFile(zip_name, 'w', zipfile.ZIP_DEFLATED) as zf:
    for fpath in found:
        arcname = os.path.basename(fpath)
        zf.write(fpath, arcname)
        print(f"[pack]   + {arcname}")

print(f"\n[pack] 打包完成：{zip_name}")
print(f"[pack] 共 {len(found)} 个文件，zip 大小：{os.path.getsize(zip_name) // 1024} KB")
