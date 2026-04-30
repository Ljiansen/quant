"""诊断模拟盘为什么没有买入信号"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 1. 检查调仓池
print("=== 调仓池 ===")
pool_path = 'd:/miniqmt_quant/state_v3_rebalance.json'
with open(pool_path, 'r') as f:
    pool_data = json.load(f)
pool = pool_data['pool']
print(f"调仓池股票数: {len(pool)}")
print(f"前10只: {pool[:10]}")

# 2. 检查state
print("\n=== 当前状态 ===")
state_path = 'd:/miniqmt_quant/state_v3_sim.json'
with open(state_path, 'r') as f:
    state = json.load(f)
print(f"持仓数: {len(state.get('positions', []))}")
print(f"现金: {state.get('cash')}")
print(f"_daily_filter_date: {state.get('_daily_filter_date')}")
print(f"_daily_filter_cache 长度: {len(state.get('_daily_filter_cache', []))}")
print(f"_daily_filter_cache 内容: {state.get('_daily_filter_cache', [])}")

# 3. 获取实时行情测试
print("\n=== 实时行情测试 ===")
from xtquant import xtdata

def _format_symbol(code):
    code_str = str(code).strip().split('.')[0]
    if code_str.startswith('6') or code_str.startswith('5'):
        return f"{code_str}.SH"
    return f"{code_str}.SZ"

# 构建symbol列表
symbols = [_format_symbol(c) for c in pool[:10]]
print(f"获取行情: {symbols}")
ticks = xtdata.get_full_tick(symbols)
print(f"返回tick数量: {len(ticks) if ticks else 0}")

for sym in symbols:
    if ticks and sym in ticks:
        t = ticks[sym]
        last = t.get('lastPrice', 0)
        pre_close = t.get('lastClose', 0) or t.get('preClose', 0)
        open_p = t.get('open', 0)
        high = t.get('high', 0)
        vol = t.get('volume', 0)
        if pre_close > 0:
            change_pct = (last - pre_close) / pre_close
        else:
            change_pct = 0
        is_positive = last > open_p if open_p > 0 else False
        print(f"  {sym}: last={last}, preClose={pre_close}, open={open_p}, change={change_pct:.4f}, 收阳={is_positive}, vol={vol}")
        # 检查买入条件
        code = sym.split('.')[0]
        is_star = code.startswith('688') or code.startswith('30')
        threshold = 0.02 if is_star else 0.01
        limit_up = 0.198 if is_star else 0.098
        buy_ok = change_pct > threshold and is_positive and change_pct < limit_up
        print(f"    is_star={is_star}, threshold={threshold}, 满足买入={buy_ok}")
    else:
        print(f"  {sym}: 无数据!")

# 4. 诊断日均成交额过滤（根本原因）
print("\n=== 诊断日均成交额过滤（根本原因） ===")
print("直接调用 xtdata.get_market_data 测试...")
test_symbols = [_format_symbol(c) for c in pool[:5]]
try:
    data = xtdata.get_market_data(
        field_list=['amount'],
        stock_list=test_symbols,
        period='1d',
        count=10,
    )
    print(f"get_market_data 返回类型: {type(data)}")
    if isinstance(data, dict):
        print(f"返回keys: {list(data.keys())}")
        amount_data = data.get('amount', {})
        print(f"amount_data 类型: {type(amount_data)}")
        print(f"amount_data keys: {list(amount_data.keys()) if hasattr(amount_data, 'keys') else 'no keys'}")
        for sym in test_symbols:
            sym_data = amount_data.get(sym, None) if hasattr(amount_data, 'get') else None
            print(f"  {sym}: data={sym_data}")
    else:
        print(f"返回值: {data}")
except Exception as e:
    import traceback
    print(f"get_market_data 异常: {e}")
    traceback.print_exc()

# 5. 关键：测试 _filter_by_avg_amount 的返回结果
print("\n=== _filter_by_avg_amount 模拟执行 ===")
candidates = pool[:10]
qualified = []
try:
    symbols_all = [_format_symbol(c) for c in candidates]
    data2 = xtdata.get_market_data(
        field_list=['amount'],
        stock_list=symbols_all,
        period='1d',
        count=10,
    )
    amount_data2 = {}
    if isinstance(data2, dict):
        amount_data2 = data2.get('amount', {})

    for code in candidates:
        symbol = _format_symbol(code)
        symbol_data = amount_data2.get(symbol, {}) if hasattr(amount_data2, 'get') else {}
        if not symbol_data:
            print(f"  {symbol}: symbol_data为空，被过滤！")
            continue

        amounts = []
        if hasattr(symbol_data, 'values'):
            amounts = list(symbol_data.values())
        elif isinstance(symbol_data, (list, tuple)):
            amounts = list(symbol_data)
        else:
            try:
                amounts = list(symbol_data)
            except Exception:
                print(f"  {symbol}: 无法转换数据，被过滤！")
                continue

        if not amounts:
            print(f"  {symbol}: amounts为空，被过滤！")
            continue

        valid_amounts = [float(a) for a in amounts if a is not None]
        if not valid_amounts:
            print(f"  {symbol}: valid_amounts为空，被过滤！")
            continue

        avg_amount = sum(valid_amounts) / len(valid_amounts)
        passed = avg_amount >= 500_000_000
        print(f"  {symbol}: avg_amount={avg_amount/1e8:.2f}亿, 通过={passed}")
        if passed:
            qualified.append(code)

except Exception as e:
    import traceback
    print(f"过滤异常: {e}")
    traceback.print_exc()

print(f"\n过滤后通过的股票 ({len(qualified)}只): {qualified}")
if not qualified:
    print(">>> 根因确认：_filter_by_avg_amount 返回空列表，所有候选股被过滤掉！")
    print(">>> 需要检查 get_market_data 数据格式或降低过滤阈值")
