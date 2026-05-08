#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_full_study.py
=================
无人值守总调度：
  1. 多年基准回测（当前最优参数，2022/2023/2024/2025-2026）
  2. 网格搜索 Phase 1（trailing/hard 参数，2025年）
  3. 网格搜索 Phase 2（time/soft 精调，四年验证 2022-2025）

预计总用时：3~5 小时
结果汇总写入: full_study_result.txt

用法:
    python run_full_study.py
"""

import sys, datetime, subprocess, os

LOG_FILE = 'd:/miniqmt_quant/full_study_result.txt'
_log_fh = open(LOG_FILE, 'w', encoding='utf-8')


def _log(msg):
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    _log_fh.write(line + '\n')
    _log_fh.flush()
    print(line)


def _run_step(label, script_path, args=None):
    """同步运行子进程，将输出追加到日志，返回是否成功"""
    cmd = [sys.executable, script_path] + (args or [])
    _log(f'\n{"=" * 70}')
    _log(f'  开始: {label}')
    _log(f'  命令: {" ".join(cmd)}')
    _log(f'{"=" * 70}')

    t0 = datetime.datetime.now()
    try:
        proc = subprocess.run(
            cmd,
            cwd='d:/miniqmt_quant',
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
        )
        elapsed = (datetime.datetime.now() - t0).seconds // 60
        # 将输出写入日志
        for line in proc.stdout.splitlines():
            _log_fh.write(f'    {line}\n')
        _log_fh.flush()

        if proc.returncode == 0:
            _log(f'  完成: {label}  用时 {elapsed} 分钟  [OK]')
            return True
        else:
            _log(f'  失败: {label}  returncode={proc.returncode}  用时 {elapsed} 分钟  [ERROR]')
            return False
    except Exception as e:
        _log(f'  异常: {label}  {e}')
        return False


if __name__ == '__main__':
    _log('*' * 70)
    _log('  全量研究任务启动（基准回测 + 两阶段网格搜索）')
    _log('*' * 70)

    base_dir = 'd:/miniqmt_quant'
    t_start  = datetime.datetime.now()

    # ── Step 1: 多年基准回测 ────────────────────────────────────────────────
    ok1 = _run_step(
        '多年基准回测（2022/2023/2024/2025-2026）',
        os.path.join(base_dir, 'run_baseline_all_years.py'),
    )

    # ── Step 2: 网格搜索 Phase 1 ────────────────────────────────────────────
    ok2 = _run_step(
        '网格搜索 Phase 1（27组合 × 2025年）',
        os.path.join(base_dir, 'run_grid_search.py'),
        args=['--phase', '1'],
    )

    # ── Step 3: 网格搜索 Phase 2（仅当 Phase1 成功时）──────────────────────
    if ok2:
        ok3 = _run_step(
            '网格搜索 Phase 2（Top3 × 时/软 × 四年验证）',
            os.path.join(base_dir, 'run_grid_search.py'),
            args=['--phase', '2'],
        )
    else:
        _log('[跳过] Phase 1 失败，跳过 Phase 2')
        ok3 = False

    # ── 汇总 ─────────────────────────────────────────────────────────────
    total_min = (datetime.datetime.now() - t_start).seconds // 60
    _log(f'\n{"*" * 70}')
    _log('  全量研究任务完成汇总')
    _log(f'{"*" * 70}')
    _log(f'  Step1 基准回测:      {"成功" if ok1 else "失败"}')
    _log(f'  Step2 网格Phase1:    {"成功" if ok2 else "失败"}')
    _log(f'  Step3 网格Phase2:    {"成功" if ok3 else "失败"}')
    _log(f'  总用时:              {total_min} 分钟')
    _log(f'')
    _log(f'  主要输出文件:')
    _log(f'    基准结果:  d:/miniqmt_quant/baseline_all_years_result.txt')
    _log(f'    Phase1:    d:/miniqmt_quant/grid_results/grid_phase1_2025.csv')
    _log(f'    Phase2:    d:/miniqmt_quant/grid_results/grid_phase2_4years.csv')
    _log(f'    仪表盘:    d:/miniqmt_quant/sim_results/ (刷新 Ctrl+Shift+R)')
    _log(f'    完整日志:  {LOG_FILE}')
    _log(f'{"*" * 70}')

    _log_fh.close()
    sys.exit(0 if (ok1 and ok2 and ok3) else 1)
