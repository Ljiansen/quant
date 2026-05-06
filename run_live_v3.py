"""V3策略真实实盘 - 20W资金限制

连接 miniQMT 进行实盘交易，使用 20W 资金（3支股票，每支约6.7W）。
运行完整的主循环：启动恢复 -> 竞价卖出 -> 盘中监控 -> 扫描买入 -> 收盘持久化。

用法:
    python run_live_v3.py          # 运行完整实盘（主循环）
    python run_live_v3.py --status # 查看当前实盘持仓状态
"""
import argparse
import json as _json
import os
import sys
import time
from datetime import datetime, date

sys.path.insert(0, 'd:/miniqmt_quant')


# ─── 收盘后自动化辅助函数 ──────────────────────────────────────────────────────

def _wait_until_19():
    """阻塞等待直到当日 19:00（baostock 日线数据约 18:00 后才完整发布）"""
    while True:
        now = datetime.now()
        if now.hour >= 19:
            return
        remaining_mins = 19 * 60 - (now.hour * 60 + now.minute)
        print(f"[收盘后] 当前 {now.strftime('%H:%M')}，等待至 19:00 再更新数据"
              f"（还剩约 {remaining_mins} 分钟）...")
        time.sleep(min(remaining_mins * 60, 5 * 60))  # 最多5分钟打印一次


def _pool_rebuilt_today() -> bool:
    """幂等检查：今日调仓池是否已重建（对比 state_v3_rebalance.json 的 rebalance_date）"""
    today = date.today().strftime('%Y-%m-%d')
    try:
        with open('d:/miniqmt_quant/state_v3_rebalance.json', 'r', encoding='utf-8') as _f:
            data = _json.load(_f)
        return data.get('rebalance_date', '') == today
    except Exception:
        return False


def _5min_updated_today() -> bool:
    """幂等检查：今日5分钟增量更新是否已完成（读取哨兵文件）"""
    today = date.today().strftime('%Y%m%d')
    sentinel = f'd:/miniqmt_quant/.5min_incremental_done_{today}'
    return os.path.exists(sentinel)


def _mark_5min_updated_today():
    """标记今日5分钟增量更新已完成（写哨兵文件）"""
    today = date.today().strftime('%Y%m%d')
    sentinel = f'd:/miniqmt_quant/.5min_incremental_done_{today}'
    try:
        with open(sentinel, 'w', encoding='utf-8') as _sf:
            _sf.write(datetime.now().isoformat())
    except Exception as _e:
        print(f"[收盘后] 写哨兵文件失败（不影响更新结果）: {_e}")


# ──────────────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description='V3策略真实实盘（20W资金限制）')
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
        engine = LiveEngineV3(mode='live', capital_limit=200000)

        if args.status:
            # 只查看状态报告（不连接 miniQMT）
            engine.get_status_report()
        else:
            # 运行完整主循环
            print("=" * 60)
            print("V3策略真实实盘 - 资金限制 20W")
            print("=" * 60)
            engine.run()

            # ── 收盘后追踪今日调仓池表现（须在刷新池子前执行） ──────────────
            print("\n" + "=" * 60)
            print("[收盘后] 追踪今日调仓池涨跌表现...")
            print("=" * 60)
            try:
                import track_pool_performance
                track_pool_performance.track()
            except Exception as _te:
                import traceback
                print(f"[收盘后] 调仓池追踪失败（不影响其他流程）: {_te}")
                traceback.print_exc()

            # ── 等待至 19:00（baostock 日线数据约 18:00 后才完整发布）────────
            _wait_until_19()

            # ── 收盘后自动刷新调仓池（幂等：今日已建则跳过） ────────────────
            print("\n" + "=" * 60)
            print("[收盘后] 开始刷新调仓池（每日自动更新）...")
            print("=" * 60)
            if _pool_rebuilt_today():
                print("[收盘后] 调仓池今日已重建（rebalance_date=今日），跳过（幂等保护）")
            else:
                try:
                    import init_rebalance_pool
                    # 先增量更新日线数据
                    init_rebalance_pool.run_daily_data_update()
                    # 读取当前策略（保持与用户上次手动选择一致）
                    _pool_path = 'd:/miniqmt_quant/state_v3_rebalance.json'
                    _current_strategy = 'ba'  # 默认
                    try:
                        with open(_pool_path, 'r', encoding='utf-8') as _f:
                            _current_strategy = _json.load(_f).get('strategy_key', 'ba') or 'ba'
                    except Exception:
                        pass
                    print(f"[收盘后] 使用当前策略: {_current_strategy}")
                    init_rebalance_pool.main(strategy=_current_strategy)
                    print("[收盘后] 调仓池已刷新，引擎下次启动将自动读取新池子")
                except Exception as _pe:
                    import traceback
                    print(f"[收盘后] 调仓池刷新失败（不影响今日已完成交易）: {_pe}")
                    traceback.print_exc()

            # ── 收盘后自动增量更新 5分钟线（幂等：今日已更新则跳过）────────
            print("\n" + "=" * 60)
            print("[收盘后] 开始增量更新5分钟线数据...")
            print("=" * 60)
            if _5min_updated_today():
                print("[收盘后] 5分钟线今日已更新，跳过（幂等保护）")
            else:
                try:
                    import update_5min_incremental
                    update_5min_incremental.run_incremental(force_full=False)
                    _mark_5min_updated_today()
                    print("[收盘后] 5分钟线增量更新完成")
                except Exception as _ue:
                    import traceback
                    print(f"[收盘后] 5分钟线更新失败（不影响实盘）: {_ue}")
                    traceback.print_exc()

    except KeyboardInterrupt:
        print("\n[信息] 收到中断信号，程序退出")
    except Exception as e:
        print(f"\n[错误] 运行异常: {e}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    main()
