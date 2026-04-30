"""V3策略实盘模拟 - 30W虚拟资金

不实际连接 miniQMT，使用 SimulatedExecutor 模拟交易。
行情数据仍通过 xtdata.get_full_tick 获取真实实时行情。

用法:
    python run_simulation_v3.py          # 运行完整模拟盘（主循环）
    python run_simulation_v3.py --status # 查看当前模拟持仓状态
"""
import argparse
import sys

sys.path.insert(0, 'd:/miniqmt_quant')


def main():
    parser = argparse.ArgumentParser(description='V3策略实盘模拟（30W虚拟资金）')
    parser.add_argument(
        '--status', action='store_true',
        help='查看当前模拟持仓状态（不运行主循环）'
    )
    args = parser.parse_args()

    try:
        from engine.live_engine_v3 import SimulationEngineV3
    except Exception as e:
        print(f"[错误] 导入引擎失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    try:
        engine = SimulationEngineV3(capital=300000)

        if args.status:
            # 只查看状态报告
            engine.get_status_report()
        else:
            # 运行完整主循环
            print("=" * 60)
            print("V3策略实盘模拟 - 30W虚拟资金")
            print("行情为真实行情，交易为模拟")
            print("=" * 60)
            engine.run()

    except KeyboardInterrupt:
        print("\n[信息] 收到中断信号，程序退出")
    except Exception as e:
        print(f"\n[错误] 运行异常: {e}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    main()
