# -*- coding: utf-8 -*-
"""
实盘模拟入口脚本（不真实下单）
用法: python run_simulation.py [--symbols 516630.SH,600000.SH]
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from data.data_manager import DataManager
from engine.simulation_engine import SimulationEngine
from strategy.example_strategy import DualMAStrategy
from utils.logger import get_logger


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description="实盘模拟入口脚本（不真实下单）")
    parser.add_argument(
        "--symbols",
        type=str,
        default="516630.SH",
        help="股票代码列表，逗号分隔，默认 516630.SH",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    logger = get_logger("simulation")
    logger.info(f"模拟运行参数: symbols={symbols}")

    # 1. 初始化 DataManager
    dm = DataManager(
        source=config.DATA_SOURCE,
        cache_dir=config.DATA_CACHE_DIR,
    )

    # 2. 初始化策略（默认双均线策略）
    strategy = DualMAStrategy()

    # 3. 初始化 SimulationEngine（内部使用 SimulatedExecutor）
    engine = SimulationEngine(
        strategy=strategy,
        data_manager=dm,
    )

    # 4. 执行一次信号检查和下单
    logger.info("执行模拟信号检查与下单...")
    engine.run_once(symbols)

    # 5. 打印交易日志
    trade_log = engine.get_trade_log()
    if trade_log:
        logger.info(f"本次模拟交易记录共 {len(trade_log)} 条")
        print("\n" + "=" * 60)
        print("模拟交易日志")
        print("=" * 60)
        for log in trade_log:
            print(
                f"[{log['time']}] {log['symbol']} {log['direction']} "
                f"价格={log['price']} 数量={log['volume']} order_id={log['order_id']}"
            )
        print("=" * 60)
    else:
        logger.info("本次无交易信号")
        print("\n本次无交易信号")

    logger.info("模拟运行完成")


if __name__ == "__main__":
    main()
