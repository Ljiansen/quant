# -*- coding: utf-8 -*-
"""
回测入口脚本
用法: python run_backtest.py [--symbol 600000] [--start 20240101] [--end 20241231] [--optimize]
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from data.data_manager import DataManager
from engine.backtest_engine import BacktestEngine
from report.report_generator import ReportGenerator
from strategy.example_strategy import DualMAStrategy
from utils.logger import get_logger


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description="回测入口脚本")
    parser.add_argument(
        "--symbol",
        type=str,
        default="600000",
        help="股票代码，默认 600000",
    )
    parser.add_argument(
        "--start",
        type=str,
        default="20240101",
        help="开始日期，格式 YYYYMMDD，默认 20240101",
    )
    parser.add_argument(
        "--end",
        type=str,
        default="20241231",
        help="结束日期，格式 YYYYMMDD，默认 20241231",
    )
    parser.add_argument(
        "--optimize",
        action="store_true",
        help="开启参数优化（遍历策略参数空间）",
    )
    return parser.parse_args()


def print_metrics(metrics: dict):
    """打印关键绩效指标"""
    print("\n" + "=" * 60)
    print("回测关键指标")
    print("=" * 60)
    print(f"总收益率:    {metrics.get('total_return', 0):.2%}")
    print(f"年化收益率:  {metrics.get('annual_return', 0):.2%}")
    print(f"最大回撤:    {metrics.get('max_drawdown', 0):.2%}")
    print(f"夏普比率:    {metrics.get('sharpe_ratio', 0):.2f}")
    print(f"胜率:        {metrics.get('win_rate', 0):.2%}")
    print(f"盈亏比:      {metrics.get('profit_loss_ratio', 0):.2f}")
    print(f"总交易次数:  {metrics.get('total_trades', 0)}")
    print(f"交易天数:    {metrics.get('trading_days', 0)}")
    print("=" * 60)


def main():
    args = parse_args()
    logger = get_logger("backtest")

    logger.info(
        f"回测参数: symbol={args.symbol}, start={args.start}, "
        f"end={args.end}, optimize={args.optimize}"
    )

    # 1. 初始化 DataManager
    dm = DataManager(
        source=config.DATA_SOURCE,
        cache_dir=config.DATA_CACHE_DIR,
    )

    # 2. 初始化策略（默认双均线策略）
    strategy = DualMAStrategy()

    # 3. 初始化 BacktestEngine
    engine = BacktestEngine(
        strategy=strategy,
        data_manager=dm,
        initial_capital=config.BACKTEST_INITIAL_CAPITAL,
        commission_rate=config.BACKTEST_COMMISSION_RATE,
        slippage=config.BACKTEST_SLIPPAGE,
        stamp_tax_rate=config.BACKTEST_STAMP_TAX_RATE,
    )

    # 4. 运行回测（或参数优化）
    if args.optimize:
        logger.info("开始参数优化...")
        results = engine.run_optimization(
            symbol=args.symbol,
            start_date=args.start,
            end_date=args.end,
        )
        best = results[0]
        logger.info(f"最优参数: {best['params']}")
        print_metrics(best["metrics"])

        # 5. 生成参数优化对比报告
        report = ReportGenerator(output_dir=config.REPORT_OUTPUT_DIR)
        report_path = report.generate_comparison(results)
        logger.info(f"参数优化报告已生成: {report_path}")
        print(f"\n报告路径: {report_path}")
    else:
        logger.info("开始单次回测...")
        result = engine.run(
            symbol=args.symbol,
            start_date=args.start,
            end_date=args.end,
        )
        print_metrics(result["metrics"])

        # 5. 生成 HTML 报告
        report = ReportGenerator(output_dir=config.REPORT_OUTPUT_DIR)
        report_path = report.generate(result)
        logger.info(f"回测报告已生成: {report_path}")
        print(f"\n报告路径: {report_path}")

    logger.info("回测完成")


if __name__ == "__main__":
    main()
