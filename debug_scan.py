# -*- coding: utf-8 -*-
"""
精确模拟引擎的_scan_and_buy流程
同时诊断：tick数据、过滤链、买入信号检查的每个环节
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(__file__))

import config

# ──────────────────────────────────────────────
# 辅助函数（与引擎一致）
# ──────────────────────────────────────────────
def _format_symbol(code):
    code_str = str(code).strip().split('.')[0]
    if code_str.startswith('6') or code_str.startswith('5'):
        return f"{code_str}.SH"
    return f"{code_str}.SZ"

def _is_star(code):
    code_str = str(code).split('.')[0]
    return code_str.startswith('688') or code_str.startswith('30')

# ──────────────────────────────────────────────
# 加载调仓池
# ──────────────────────────────────────────────
rebalance_file = os.path.join(os.path.dirname(__file__), 'state_v3_rebalance.json')
with open(rebalance_file, 'r', encoding='utf-8') as f:
    data = json.load(f)

pool = data.get('pool', [])
print(f"调仓池: {len(pool)} 只")
print(f"  前5只: {pool[:5]}")

# ──────────────────────────────────────────────
# 构建symbol列表
# ──────────────────────────────────────────────
symbols = [_format_symbol(c) for c in pool]
print(f"\n构建symbol列表: {len(symbols)} 只")
print(f"  前5只: {symbols[:5]}")

# ──────────────────────────────────────────────
# 检查 state_v3_sim.json 中的日均成交额缓存
# ──────────────────────────────────────────────
print("\n" + "="*60)
print("【步骤0】检查 _daily_filter_cache 状态")
sim_state_file = os.path.join(os.path.dirname(__file__), 'state_v3_sim.json')
if os.path.exists(sim_state_file):
    with open(sim_state_file, 'r', encoding='utf-8') as f:
        sim_state = json.load(f)
    cache_date = sim_state.get('_daily_filter_date', '无')
    cache_list = sim_state.get('_daily_filter_cache', [])
    print(f"  缓存日期: {cache_date}")
    print(f"  缓存数量: {len(cache_list)} 只")
    print(f"  缓存内容(前10): {cache_list[:10]}")
    # 检查候选股与缓存的交集
    candidates_in_cache = [c for c in pool if c in cache_list]
    print(f"  调仓池中在缓存内的: {len(candidates_in_cache)} 只")
    candidates_not_in_cache = [c for c in pool if c not in cache_list]
    print(f"  调仓池中不在缓存内的: {len(candidates_not_in_cache)} 只 → {candidates_not_in_cache[:10]}")
else:
    print("  state_v3_sim.json 不存在")

# ──────────────────────────────────────────────
# 获取tick数据
# ──────────────────────────────────────────────
print("\n" + "="*60)
print("【步骤1】获取tick数据")
try:
    from xtquant import xtdata
    print(f"  xtdata 导入成功")
    ticks = xtdata.get_full_tick(symbols)
    if ticks is None:
        print("  get_full_tick 返回 None！")
        ticks = {}
    print(f"  返回tick数: {len(ticks)}")
    # 打印有数据 vs 无数据
    has_tick = [s for s in symbols if s in ticks]
    no_tick = [s for s in symbols if s not in ticks]
    print(f"  有tick: {len(has_tick)} 只")
    print(f"  无tick: {len(no_tick)} 只 → {no_tick[:10]}")
except Exception as e:
    import traceback
    print(f"  get_full_tick 异常: {e}")
    traceback.print_exc()
    ticks = {}

# ──────────────────────────────────────────────
# 逐只检查（完全模拟引擎逻辑）
# ──────────────────────────────────────────────
print("\n" + "="*60)
print("【步骤2】逐只检查买入信号（完全复现引擎逻辑）")

# 从config读取参数
V3_MIN_CHANGE_PCT = config.V3_MIN_CHANGE_PCT       # 主板最低涨幅
V3_STAR_MIN_CHANGE_PCT = config.V3_STAR_MIN_CHANGE_PCT  # 科创板最低涨幅
V3_STAR_LIMIT_UP = config.V3_STAR_LIMIT_UP         # 科创板涨停线
MAIN_LIMIT_UP = 0.098                               # 主板涨停线（引擎硬编码）

print(f"  主板涨幅阈值: {V3_MIN_CHANGE_PCT:.2%}（判断用 change_pct <= {V3_MIN_CHANGE_PCT}，注意是<=）")
print(f"  科创板涨幅阈值: {V3_STAR_MIN_CHANGE_PCT:.2%}")
print(f"  主板涨停保护: {MAIN_LIMIT_UP:.1%}")
print(f"  科创板涨停保护: {V3_STAR_LIMIT_UP:.1%}")

results = {
    'no_tick': [],       # 无tick数据
    'invalid_price': [], # 价格无效
    'suspended': [],     # 停牌(volume==0)
    'below_threshold': [],  # 涨幅不足（关键！含==阈值）
    'negative': [],      # 收阴
    'limit_up': [],      # 涨停
    'qualified': [],     # 满足条件
}

for code in pool:
    symbol = _format_symbol(code)
    tick = ticks.get(symbol)

    if not tick:
        results['no_tick'].append(code)
        continue

    last_price = tick.get('lastPrice', 0)
    pre_close = tick.get('preClose', 0)
    open_price = tick.get('open', 0)
    volume = tick.get('volume', 0)
    high_price = tick.get('high', 0)
    ask_prices = tick.get('askPrice', [])
    ask_price = ask_prices[0] if ask_prices else last_price

    # 引擎第一道过滤：价格+volume
    if last_price <= 0 or pre_close <= 0 or volume == 0:
        results['invalid_price'].append({
            'code': code, 'last': last_price,
            'preClose': pre_close, 'volume': volume
        })
        continue

    change_pct = (last_price - pre_close) / pre_close

    # ST检查（引擎会查 xtdata.get_instrument_detail）
    is_st = False
    try:
        detail = xtdata.get_instrument_detail(symbol)
        if detail:
            name = detail.get('InstrumentName', '')
            if 'ST' in name:
                is_st = True
    except Exception:
        pass

    # _check_buy_signal 逻辑（引擎第932行~）
    bar = {
        'open': open_price, 'high': high_price,
        'low': tick.get('low', 0),
        'close': last_price, 'volume': volume,
        'amount': tick.get('amount', 0),
    }
    is_star_stock = _is_star(code)
    min_change = V3_STAR_MIN_CHANGE_PCT if is_star_stock else V3_MIN_CHANGE_PCT
    limit_up_thresh = V3_STAR_LIMIT_UP if is_star_stock else MAIN_LIMIT_UP

    # 逐条件检查（完全还原引擎 _check_buy_signal）
    if volume == 0 or last_price <= 0:
        results['suspended'].append(code)
        continue

    if is_st:
        print(f"  ✗ {symbol}: ST股，已跳过")
        continue

    fail_reason = None
    if change_pct <= min_change:  # 注意：引擎用 <=，正好等于阈值也被过滤！
        fail_reason = f"涨幅不足: {change_pct:.4f} <= {min_change} (阈值)"
    elif last_price <= open_price:
        fail_reason = f"收阴/平: close={last_price} <= open={open_price}"
    elif change_pct >= limit_up_thresh:
        fail_reason = f"涨停: {change_pct:.4f} >= {limit_up_thresh}"

    if fail_reason:
        category = ('below_threshold' if '涨幅' in fail_reason
                    else 'negative' if '收阴' in fail_reason else 'limit_up')
        results[category].append({
            'code': code, 'symbol': symbol,
            'last': last_price, 'preClose': pre_close,
            'open': open_price, 'change': change_pct,
            'ask': ask_price, 'reason': fail_reason
        })
    else:
        results['qualified'].append({
            'code': code, 'symbol': symbol,
            'last': last_price, 'preClose': pre_close,
            'open': open_price, 'change': change_pct,
            'ask': ask_price
        })

# ──────────────────────────────────────────────
# 汇总输出
# ──────────────────────────────────────────────
print("\n" + "="*60)
print("【汇总】各过滤环节统计")
print(f"  无tick数据: {len(results['no_tick'])} 只 → {results['no_tick'][:10]}")
print(f"  价格无效/停牌: {len(results['invalid_price'])+len(results['suspended'])} 只")
print(f"  涨幅不足(<=阈值): {len(results['below_threshold'])} 只")
print(f"  收阴线: {len(results['negative'])} 只")
print(f"  涨停: {len(results['limit_up'])} 只")
print(f"  满足买入条件: {len(results['qualified'])} 只")

if results['qualified']:
    print("\n✅ 满足买入条件的股票：")
    for c in results['qualified']:
        print(f"  {c['code']} change={c['change']:.4f} "
              f"last={c['last']} preClose={c['preClose']} open={c['open']} ask={c['ask']}")

print("\n" + "="*60)
print("【关键诊断】仪表盘显示12只满足，但引擎不买入 → 排查：")

# 检查state_v3_sim.json中的持仓数量
if os.path.exists(sim_state_file):
    positions = sim_state.get('positions', [])
    cash = sim_state.get('cash', 0)
    print(f"\n  当前模拟仓持仓: {len(positions)} 只 / {config.V3_MAX_POSITIONS} 只上限")
    print(f"  当前模拟仓现金: {cash:.2f}")
    if len(positions) >= config.V3_MAX_POSITIONS:
        print("  ⚠️  持仓已满！引擎不会触发买入（需持仓 < 3 才会扫描）")
    elif cash < 1000:
        print("  ⚠️  资金不足1000元！引擎跳过买入")
    else:
        print(f"  ✓ 持仓未满，资金充足")
        print(f"\n  ❓ 关键问题：引擎 _check_buy_signal 使用 <= 判断：")
        print(f"     change_pct <= {V3_MIN_CHANGE_PCT} 会过滤掉正好等于阈值的股票")
        print(f"     仪表盘计算可能用 > 而引擎用 <=，导致判断不一致")

print("\n  🔍 检查日均成交额缓存是否过滤掉了所有候选股：")
if 'cache_list' in dir() and 'pool' in dir():
    tradable = [c for c in pool if c in cache_list]
    print(f"     调仓池50只中，通过日均成交额缓存的: {len(tradable)} 只")
    if len(tradable) == 0:
        print("     ⚠️  全部被日均成交额过滤！这是根本原因！")
        print(f"     缓存内容: {cache_list}")
    else:
        print(f"     通过的: {tradable}")

print("\n完成诊断。")
