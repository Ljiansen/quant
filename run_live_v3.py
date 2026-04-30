"""V3策略真实实盘 - 3W资金限制

连接 miniQMT 进行实盘交易，但只使用 3W 资金（账号有 50W）。
运行完整的主循环：启动恢复 -> 竞价卖出 -> 盘中监控 -> 扫描买入 -> 收盘持久化。

用法:
    python run_live_v3.py          # 运行完整实盘（主循环）
    python run_live_v3.py --status # 查看当前实盘持仓状态
"""
import argparse
import sys

sys.path.insert(0, 'd:/miniqmt_quant')


def main():
    parser = argparse.ArgumentParser(description='V3策略真实实盘（3W资金限制）')
    parser.add_argument(
        '--status', action='store_true',
        help='查看当前实盘持仓状态（不运行主循环，不连接 miniQMT）'
    )
    args = parser.parse_args()

    try:
        from engine.live_engine_v3 import LiveEngineV3
    except Exception as e:
        print(f"[错误] 导入引擎失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    try:
        engine = LiveEngineV3(mode='live', capital_limit=30000)

        if args.status:
            # 只查看状态报告（不连接 miniQMT）
            engine.get_status_report()
        else:
            # 运行完整主循环
            print("=" * 60)
            print("V3策略真实实盘 - 资金限制 3W")
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
