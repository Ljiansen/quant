"""验证实盘引擎初始化流程"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

print("=" * 50)
print("【验证1】模拟引擎初始化")
print("=" * 50)
try:
    from engine.live_engine_v3 import SimulationEngineV3
    sim = SimulationEngineV3(capital=300000)
    print(f"  模拟引擎创建成功")
    print(f"  资金: {sim.capital_limit if hasattr(sim, 'capital_limit') else 'N/A'}")
    print(f"  持仓: {sim.positions if hasattr(sim, 'positions') else 'N/A'}")
    print(f"  Pending: {sim.pending_sells if hasattr(sim, 'pending_sells') else 'N/A'}")
    print("  ✓ 模拟引擎初始化通过")
except Exception as e:
    print(f"  ✗ 模拟引擎初始化失败: {e}")
    import traceback; traceback.print_exc()

print()
print("=" * 50)
print("【验证2】策略加载")
print("=" * 50)
try:
    from strategy.strategy_v3 import StrategyV3
    strategy = StrategyV3()
    print(f"  策略创建成功")
    print(f"  主板止盈: {strategy.take_profit}")
    print(f"  科创/创业板止盈: {strategy.star_take_profit}")
    print(f"  滑点: {strategy.slippage}")
    print(f"  is_star('300033'): {strategy._is_star('300033')}")
    print(f"  is_star('688270'): {strategy._is_star('688270')}")
    print(f"  is_star('600000'): {strategy._is_star('600000')}")
    print("  ✓ 策略加载通过")
except Exception as e:
    print(f"  ✗ 策略加载失败: {e}")
    import traceback; traceback.print_exc()

print()
print("=" * 50)
print("【验证3】调仓池加载")
print("=" * 50)
try:
    # 检查是否能加载调仓池（需要读取本地数据）
    from data.local_data import LocalDailyData
    local = LocalDailyData()
    codes = local.get_stock_list()
    print(f"  本地数据股票数: {len(codes)}")
    print(f"  示例: {codes[:5]}")
    print("  ✓ 本地数据加载通过")
except Exception as e:
    print(f"  ✗ 本地数据加载失败: {e}")
    import traceback; traceback.print_exc()

print()
print("=" * 50)
print("【验证4】xtdata行情接口")
print("=" * 50)
try:
    from xtquant import xtdata
    # 尝试获取快照（收盘后可能返回空或最后数据）
    ticks = xtdata.get_full_tick(['000001.SZ'])
    if ticks and '000001.SZ' in ticks:
        tick = ticks['000001.SZ']
        print(f"  000001.SZ 快照: lastPrice={tick.get('lastPrice', 'N/A')}, preClose={tick.get('preClose', 'N/A')}")
        print("  ✓ xtdata行情接口通过")
    else:
        print(f"  收盘后快照可能为空: {ticks}")
        print("  ⚠ xtdata接口可连接，收盘后数据可能不完整")
except Exception as e:
    print(f"  ✗ xtdata行情接口失败: {e}")
    import traceback; traceback.print_exc()

print()
print("=" * 50)
print("【验证5】miniQMT连接")
print("=" * 50)
try:
    from trade.executor import TradeExecutor
    import config
    executor = TradeExecutor()
    connected = executor.connect()
    if connected:
        print(f"  miniQMT连接成功")
        # 查询资产
        asset = executor.query_asset()
        if asset:
            print(f"  可用资金: {asset.get('cash', 'N/A')}")
            print(f"  总资产: {asset.get('total_asset', 'N/A')}")
        # 查询持仓
        positions = executor.query_positions()
        print(f"  当前持仓数: {len(positions) if positions else 0}")
        print("  ✓ miniQMT连接通过")
    else:
        print("  ✗ miniQMT连接失败（客户端可能未运行）")
except Exception as e:
    print(f"  ✗ miniQMT连接失败: {e}")
    import traceback; traceback.print_exc()

print()
print("=" * 50)
print("【验证6】State文件读写")
print("=" * 50)
try:
    import json
    state_file = os.path.join(os.path.dirname(__file__), 'state_v3_sim.json')
    # 写测试
    test_state = {"positions": [], "pending_sells": [], "cash": 300000, "test": True}
    with open(state_file, 'w', encoding='utf-8') as f:
        json.dump(test_state, f, ensure_ascii=False, indent=2)
    # 读测试
    with open(state_file, 'r', encoding='utf-8') as f:
        loaded = json.load(f)
    assert loaded['cash'] == 300000
    # 清理测试数据
    os.remove(state_file)
    print(f"  State文件读写正常")
    print("  ✓ State文件验证通过")
except Exception as e:
    print(f"  ✗ State文件验证失败: {e}")

print()
print("=" * 50)
print("【验证7】入口脚本语法】")
print("=" * 50)
try:
    import py_compile
    py_compile.compile('run_simulation_v3.py', doraise=True)
    print("  ✓ run_simulation_v3.py 语法通过")
    py_compile.compile('run_live_v3.py', doraise=True)
    print("  ✓ run_live_v3.py 语法通过")
except Exception as e:
    print(f"  ✗ 语法验证失败: {e}")

print()
print("=" * 50)
print("验证完毕")
print("=" * 50)
