"""V3策略回测入口
用法:
    python run_backtest_v3.py --start 20260101 --end 20260429
    python run_backtest_v3.py  # 使用默认参数（今年1月1日至今天）
"""
import argparse
import sys
sys.path.insert(0, 'd:/miniqmt_quant')


def main():
    parser = argparse.ArgumentParser(description='V3策略回测')
    parser.add_argument('--start', default='20260101', help='回测开始日期 YYYYMMDD')
    parser.add_argument('--end', default='20260429', help='回测结束日期 YYYYMMDD')
    parser.add_argument('--capital', type=float, default=None, help='初始资金（默认从config读取）')
    args = parser.parse_args()

    try:
        # 1. 初始化
        from data import DataManager
        from strategy.strategy_v3 import StrategyV3
        from engine.backtest_engine_v3 import BacktestEngineV3
        from report import ReportGenerator
        import config

        # 使用 config 中配置的数据源
        v3_source = getattr(config, 'V3_DATA_SOURCE', 'akshare')
        dm = DataManager(source=v3_source)
        strategy = StrategyV3()
        engine = BacktestEngineV3(strategy, dm, initial_capital=args.capital)

        # 2. 运行回测
        print(f"开始V3策略回测: {args.start} ~ {args.end}")
        result = engine.run(args.start, args.end)

        # 3. 打印关键指标
        metrics = result.get('metrics', {})
        print("\n" + "=" * 60)
        print("V3策略回测结果")
        print("=" * 60)
        for key, value in metrics.items():
            if key == 'sell_type_stats':
                continue
            if isinstance(value, float):
                if 'return' in key or 'drawdown' in key or 'rate' in key or 'pct' in key:
                    print(f"  {key}: {value:.2%}")
                else:
                    print(f"  {key}: {value:.2f}")
            else:
                print(f"  {key}: {value}")

        # 4. 打印卖出类型统计
        sell_stats = result.get('sell_type_stats', {})
        if sell_stats:
            print("\n卖出类型统计:")
            for stype, stats in sell_stats.items():
                print(
                    f"  {stype}: {stats.get('count', 0)}次, "
                    f"平均盈亏: {stats.get('avg_pnl', 0):.2f}元"
                )

        # 5. 生成HTML报告
        rg = ReportGenerator()
        report_path = rg.generate_v3(result)
        print(f"\n回测报告已生成: {report_path}")

    except Exception as e:
        print(f"\n[错误] V3回测执行异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
