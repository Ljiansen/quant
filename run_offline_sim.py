# -*- coding: utf-8 -*-
"""
run_offline_sim.py —— 非交易时段离线策略测试入口

用途：
  - 在夜间/周末不影响实盘数据的情况下验证策略代码逻辑
  - 独立文件：state_v3_offline.json / trades_v3_offline.json

两种模式：
──────────────────────────────────────────────────────
  历史回放模式（默认）：用真实 data_cache/ 数据验证整体效果
    python run_offline_sim.py
    python run_offline_sim.py --start 2025-01-01 --end 2025-03-31
    python run_offline_sim.py --capital 500000 --clear

  分支覆盖测试模式（--test）：用合成数据覆盖所有逻辑分支
    python generate_offline_test_data.py   # 先生成测试数据（只需一次）
    python run_offline_sim.py --test --clear

  分支覆盖测试场景（--test 模式）：
    B01 正常买入（600991 D01）
    B02 满仓不再扫描（D01 后段）
    B03 涨幅不足拒绝（600992 多天）
    B04 收阴线拒绝（600992 D04）
    B05 停牌拒绝（600992 D05 volume=0）
    B06 涨停拒绝（600992 D10 change≥9.8%）
    B07 硬止损（600993 D02）
    B08 移动止盈激活+触发（600994 D02-D03）
    B09 阴跌止损→pending→竞价成交（600995 D04→D05）
    B10 时间止损→pending→竞价成交（600996 D08→D09）
    B11 部分成交（600997 D05，fill=25%）
    B12 部分成交不占有效槽→再买一只（300991 D05）
    B13 创业板规则trailing（300991 D07-D08）
    B14 现金不足直接返回（D01 满仓后）
    B15 pending_sells skip in _monitor（600996 D09 竞价前）
──────────────────────────────────────────────────────
"""

import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    parser = argparse.ArgumentParser(description='V3策略离线逻辑测试')
    parser.add_argument('--test',    action='store_true', help='分支覆盖测试模式（使用合成数据）')
    parser.add_argument('--start',   default='2025-01-01', help='回放开始日期 YYYY-MM-DD')
    parser.add_argument('--end',     default=None,          help='回放结束日期（默认今天）')
    parser.add_argument('--capital', type=float, default=300000.0, help='虚拟资金（默认30W）')
    parser.add_argument('--clear',   action='store_true',   help='清空离线状态文件，从零开始')
    args = parser.parse_args()

    # ── 清空离线状态 ────────────────────────────────────────────────────────
    if args.clear:
        for f in [
            'd:/miniqmt_quant/state_v3_offline.json',
            'd:/miniqmt_quant/trades_v3_offline.json',
        ]:
            if os.path.exists(f):
                os.remove(f)
                print(f'[清空] 已删除 {f}')

    # ── 导入引擎 ────────────────────────────────────────────────────────────
    try:
        from engine.offline_sim_engine_v3 import OfflineSimEngineV3
    except Exception as e:
        print(f'[错误] 导入离线引擎失败: {e}')
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # ── 根据模式配置引擎 ────────────────────────────────────────────────────
    if args.test:
        # 分支覆盖测试模式
        TEST_DIR  = 'd:/miniqmt_quant/test_data'
        TEST_POOL = 'd:/miniqmt_quant/test_data/test_rebalance_pool.json'

        if not os.path.exists(TEST_POOL):
            print('[错误] 测试数据不存在，请先运行：')
            print('       python generate_offline_test_data.py')
            sys.exit(1)

        engine = OfflineSimEngineV3(
            capital=args.capital,
            start_date='2025-03-03',   # 测试数据区间固定
            end_date='2025-03-21',
            data_dir=TEST_DIR,
            rebalance_file=TEST_POOL,
        )

        # ── 部分成交配置（B11/B12分支）──────────────────────────────────────
        # 600997.SH 只成交25%，验证：
        #   1. partial fill 仓位正确记录 intended_qty
        #   2. 成本 < slot_budget*50% 不占有效槽
        #   3. 空出的有效槽让 300991 得以在同一天买入（B12）
        engine._partial_fill_rates = {
            '600997.SH': 0.25,  # B11: 25%成交
            '601002.SH': 0.0,   # B20: 买入完全失败(0%成交)
        }

        # 卖出部分成交序列: B16=r1部分+r2全量, B17=r1零+r2部分+r3 pending
        engine._sell_fill_seq = {
            '600998.SH': [0.3, 1.0],
            '600999.SH': [0.0, 0.5],
        }

        # 竞价失败配置: B18=600999 D12竞价失败→_resubmit_sells_at_930
        engine._auction_fail_codes = {'600999'}

        print('=' * 60)
        print('[TEST] 分支覆盖测试模式 | 合成数据 | 2025-03-03 ~ 2025-03-21')
        print('[TEST] 买入部分成交:', engine._partial_fill_rates)
        print('[TEST] 卖出序列:', engine._sell_fill_seq)
        print('[TEST] 竞价失败:', engine._auction_fail_codes)
        print('=' * 60)

    else:
        # 历史回放模式
        engine = OfflineSimEngineV3(
            capital=args.capital,
            start_date=args.start,
            end_date=args.end,
            data_dir='d:/miniqmt_quant/data_cache',
            rebalance_file='d:/miniqmt_quant/state_v3_rebalance.json',
        )

    # ── 运行 ────────────────────────────────────────────────────────────────
    try:
        engine.run()
    except KeyboardInterrupt:
        print('\n[信息] 收到中断信号，程序退出')
    except Exception as e:
        print(f'\n[错误] 运行异常: {e}')
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    main()
