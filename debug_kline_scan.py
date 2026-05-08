# -*- coding: utf-8 -*-
"""
测试新版 5分钟K线买入逻辑 + API延迟/数据可用性诊断

关键：get_market_data 返回 pandas DataFrame
  - index   = 股票代码  ['600331.SH', ...]
  - columns = 时间戳    ['20260506132500', '20260506133000']
  - 取 arr[-2] = 最新已完成Bar，arr[-1] = 当前成型Bar

Phase 1 只读，Phase 2 加 --order 参数启用挂单/撤单测试
"""
import sys, os, json, time, datetime
sys.path.insert(0, os.path.dirname(__file__))

RUN_ORDER_TEST   = '--order' in sys.argv
TEST_PRICE_RATIO = 0.50
TEST_VOLUME      = 100
TEST_MAX_STOCKS  = 2

import config

def _format_symbol(code):
    s = str(code).strip().split('.')[0]
    return f"{s}.SH" if (s.startswith('6') or s.startswith('5')) else f"{s}.SZ"

def _is_star(code):
    s = str(code).split('.')[0]
    return s.startswith('688') or s.startswith('30')

def _check_buy_signal(code, bar, pre_close):
    if pre_close <= 0:
        return False, 'pre_close<=0'
    close      = bar.get('close', 0)
    open_price = bar.get('open',  0)
    volume     = bar.get('volume',0)
    if volume == 0 or close <= 0:
        return False, 'volume=0 or close<=0'
    change_pct = (close - pre_close) / pre_close
    is_star    = _is_star(code)
    min_change = config.V3_STAR_MIN_CHANGE_PCT if is_star else config.V3_MIN_CHANGE_PCT
    max_change = getattr(config, 'V3_STAR_MAX_CHANGE_PCT' if is_star else 'V3_MAX_CHANGE_PCT',
                         0.08 if is_star else 0.05)
    limit_up   = getattr(config, 'V3_STAR_LIMIT_UP', 0.198) if is_star else 0.098
    if change_pct <= min_change:
        return False, f'涨幅不足 {change_pct:.2%}<={min_change:.2%}'
    if change_pct >= max_change:
        return False, f'追高 {change_pct:.2%}>={max_change:.2%}'
    if close <= open_price:
        return False, f'收阴 close={close:.2f}<=open={open_price:.2f}'
    if change_pct >= limit_up:
        return False, f'涨停 {change_pct:.2%}>={limit_up:.2%}'
    return True, 'OK'

def _fmt_xt(t):
    try:
        s = str(int(t))
        if len(s) >= 12:
            return f"{s[0:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}"
        return s
    except Exception:
        return str(t)

def _df_to_sym_dict(kd, field):
    """DataFrame(index=股票, columns=时间戳) → {symbol: numpy_array}"""
    df = kd.get(field)
    if df is None or not hasattr(df, 'loc'):
        return {}
    return {sym: df.loc[sym].values for sym in df.index}

# ── 加载调仓池 ─────────────────────────────────────────────────────
pool_file = os.path.join(os.path.dirname(__file__), 'state_v3_rebalance.json')
with open(pool_file, 'r', encoding='utf-8') as f:
    pool = json.load(f).get('pool', [])
print(f"调仓池: {len(pool)} 只   前5: {pool[:5]}")
symbols = [_format_symbol(c) for c in pool]

print("\n" + "="*65)
print("Phase 1 【只读】：5分钟K线延迟/可用性诊断 + 信号检查")
print("="*65)

try:
    from xtquant import xtdata
    print("xtdata 导入成功")
except Exception as e:
    print(f"xtdata 导入失败: {e}"); sys.exit(1)

# ── 1-A：subscribe_quote + 批量拉K线 ─────────────────────────────
print(f"\n[1-A] subscribe_quote 订阅5分钟K线 → 批量 get_market_data")
print(f"      当前时刻: {datetime.datetime.now().strftime('%H:%M:%S.%f')[:12]}")

t_sub = time.perf_counter()
for sym in symbols:
    xtdata.subscribe_quote(sym, period='5m', count=3)
sub_ms = (time.perf_counter() - t_sub) * 1000
print(f"  subscribe_quote 耗时: {sub_ms:.0f}ms  等待2s 让数据就绪...")
time.sleep(2)

kd = None
for attempt in range(3):
    t0 = time.perf_counter()
    kd = xtdata.get_market_data(
        field_list=['open', 'high', 'low', 'close', 'volume', 'time'],
        stock_list=symbols,
        period='5m',
        count=2
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    n = len(kd.get('close', {}) if isinstance(kd.get('close'), dict) else
           kd.get('close').index if kd.get('close') is not None else [])
    print(f"  第{attempt+1}次  耗时: {elapsed_ms:>7.1f} ms  返回股票数: {n}")
    if attempt < 2:
        time.sleep(1)

# 解析 DataFrame → {symbol: numpy_array}
kd_opens  = _df_to_sym_dict(kd, 'open')
kd_highs  = _df_to_sym_dict(kd, 'high')
kd_lows   = _df_to_sym_dict(kd, 'low')
kd_closes = _df_to_sym_dict(kd, 'close')
kd_vols   = _df_to_sym_dict(kd, 'volume')
kd_times  = _df_to_sym_dict(kd, 'time')
print(f"  解析后有效股票数: {len(kd_closes)}")

# ── 1-A2：数据可用性统计 ──────────────────────────────────────────
print("\n[1-A2] 数据可用性与时间戳核验")
today_yyyymmdd = int(datetime.date.today().strftime('%Y%m%d'))
target_min     = today_yyyymmdd * 1000000 + 93000

stats = {'no_data': [], 'stale': [], 'invalid_vol': [], 'ok': []}
for code in pool:
    symbol = _format_symbol(code)
    closes = kd_closes.get(symbol, [])
    vols   = kd_vols.get(symbol,   [])
    times  = kd_times.get(symbol,  [])
    if len(closes) < 2:
        stats['no_data'].append(code); continue
    bar_v2 = float(vols[-2]) if len(vols) >= 2 else 0
    t2 = None
    if len(times) >= 2:
        try: t2 = int(times[-2])
        except Exception: pass
    is_today = (t2 is not None and t2 >= target_min)
    if not is_today:
        stats['stale'].append({'code': code, 'time': t2})
    elif bar_v2 <= 0:
        stats['invalid_vol'].append(code)
    else:
        stats['ok'].append(code)

cover = len(stats['ok']) / len(pool) * 100 if pool else 0
print(f"  调仓池共 {len(pool)} 只")
print(f"  ✅ 正常（今日+volume>0）: {len(stats['ok'])} 只  覆盖率 {cover:.1f}%")
print(f"  ❌ 数据不足(bars<2)：    {len(stats['no_data'])} 只  {stats['no_data'][:5]}")
print(f"  ⚠  旧数据（非今日）：    {len(stats['stale'])} 只  {[s['code'] for s in stats['stale'][:5]]}")
print(f"  ⚠  volume=0：           {len(stats['invalid_vol'])} 只")

# ── 1-A3：5次精细延迟 ──────────────────────────────────────────────
print("\n[1-A3] 5次精细延迟测量（订阅后）")
latencies = []
for _ in range(5):
    t0 = time.perf_counter()
    xtdata.get_market_data(field_list=['close','volume'], stock_list=symbols, period='5m', count=2)
    latencies.append((time.perf_counter() - t0) * 1000)
    time.sleep(0.2)
print(f"  min={min(latencies):.0f}ms  max={max(latencies):.0f}ms  avg={sum(latencies)/len(latencies):.0f}ms")
if max(latencies) > 200:
    print("  ⚠  延迟偏高，建议在引擎主循环中保持订阅状态")
else:
    print("  ✅ 延迟正常（本地缓存）")

# ── 1-A4：[-2] vs [-1] vol 对比 ──────────────────────────────────
print("\n[1-A4] [-2]已完成Bar vs [-1]成型Bar（前5只）")
print(f"  {'代码':<10} {'[-2]时间':<16} {'[-2]vol':>9} {'[-1]时间':<16} {'[-1]vol':>9}  判断")
print("  " + "-"*72)
for code in pool[:5]:
    sym   = _format_symbol(code)
    times = kd_times.get(sym, [])
    vols  = kd_vols.get(sym,  [])
    closes= kd_closes.get(sym,[])
    if len(closes) >= 2 and len(times) >= 2:
        v2, v1 = float(vols[-2]), float(vols[-1])
        judge = "✅ [-2]已完成" if v2 > v1 * 2 else "⚠ vol异常"
        print(f"  {code:<10} {_fmt_xt(times[-2]):<16} {v2:>9.0f} {_fmt_xt(times[-1]):<16} {v1:>9.0f}  {judge}")
    else:
        print(f"  {code:<10} 数据不足(bars={len(closes)})")

# ── 1-B：tick ──────────────────────────────────────────────────────
print("\n[1-B] 批量 tick（pre_close）...")
t0 = time.perf_counter()
try:
    ticks = xtdata.get_full_tick(symbols)
    print(f"  耗时: {(time.perf_counter()-t0)*1000:.0f}ms  返回: {len(ticks) if ticks else 0} 只")
except Exception as e:
    print(f"  get_full_tick 异常: {e}"); ticks = {}

# ── 1-C：信号分析 ──────────────────────────────────────────────────
print("\n[1-C] 逐只信号分析：")
print(f"  {'代码':<10} {'[-2]open':>9} {'[-2]close':>10} {'[-1]close':>10}"
      f" {'涨幅':>8} {'收阳':>5} {'结果':<6} 备注")
print("  " + "-"*82)

qualified, no_data_list, fail_list = [], [], []
for code in pool:
    sym  = _format_symbol(code)
    tick = (ticks or {}).get(sym, {})
    pre_close = (tick.get('lastClose', 0) or tick.get('preClose', 0)) if tick else 0
    closes = kd_closes.get(sym, [])
    opens  = kd_opens.get(sym,  [])
    highs  = kd_highs.get(sym,  [])
    lows   = kd_lows.get(sym,   [])
    vols   = kd_vols.get(sym,   [])

    if len(closes) < 2:
        no_data_list.append(code)
        print(f"  {code:<10} 无K线数据(bars={len(closes)})")
        continue

    bar_o2, bar_c2, bar_v2 = float(opens[-2]), float(closes[-2]), float(vols[-2])
    bar_h2 = float(highs[-2]) if len(highs) >= 2 else bar_c2
    bar_l2 = float(lows[-2])  if len(lows)  >= 2 else bar_c2
    bar_c1 = float(closes[-1])

    bar = {'open': bar_o2, 'high': bar_h2, 'low': bar_l2,
           'close': bar_c2, 'volume': bar_v2, 'amount': bar_c2 * bar_v2}
    ok, reason   = _check_buy_signal(code, bar, pre_close)
    change_pct   = (bar_c2 - pre_close) / pre_close if pre_close > 0 else 0
    up_str       = 'yes' if bar_c2 > bar_o2 else 'no'
    sig_str      = '✅买入' if ok else '✗'
    print(f"  {code:<10} {bar_o2:>9.3f} {bar_c2:>10.3f} {bar_c1:>10.3f}"
          f" {change_pct:>8.2%} {up_str:>5} {sig_str:<6} {reason}")
    if ok:
        qualified.append({'code': code, 'symbol': sym, 'bar_c': bar_c2,
                          'pre_close': pre_close, 'change_pct': change_pct})
    else:
        fail_list.append({'code': code, 'reason': reason})

print(f"\n  汇总 → 无K线: {len(no_data_list)}  不满足: {len(fail_list)}  满足买入: {len(qualified)}")
if qualified:
    print(f"  满足买入: {[q['code'] for q in qualified]}")

if not RUN_ORDER_TEST:
    print("\n" + "="*65)
    print("Phase 1 完成。加 --order 参数测试挂单/撤单:")
    print("  python debug_kline_scan.py --order")
    sys.exit(0)

# ── Phase 2：挂单/撤单测试 ────────────────────────────────────────
print("\n" + "="*65)
print(f"Phase 2 【挂单/撤单测试】：价格 = bar_c × {TEST_PRICE_RATIO}")
print("="*65)

targets = (qualified or [{'code': f['code'],
                           'symbol': _format_symbol(f['code']),
                           'bar_c': float(kd_closes.get(_format_symbol(f['code']), [0,0])[-2] or 0)}
                          for f in fail_list
                          if len(kd_closes.get(_format_symbol(f['code']), [])) >= 2]
           )[:TEST_MAX_STOCKS]

if not targets:
    print("没有可测试的目标，退出"); sys.exit(0)

print("\n[2-A] 连接 TradeExecutor...")
try:
    from trade.executor import TradeExecutor
    executor = TradeExecutor()
    if not executor.connect():
        print("连接失败"); sys.exit(1)
    print("连接成功")
except Exception as e:
    print(f"TradeExecutor 异常: {e}"); sys.exit(1)

for t in targets:
    code, sym, bar_c = t['code'], t['symbol'], t['bar_c']
    test_price = round(bar_c * TEST_PRICE_RATIO, 2)
    print(f"\n  {code}  bar_c={bar_c:.3f}  测试价={test_price:.3f}  数量={TEST_VOLUME}")
    order_id = executor.buy(symbol=sym, price=test_price, volume=TEST_VOLUME,
                            price_type='limit', order_remark=f'TEST_KLINE_{code}')
    print(f"  下单: order_id={order_id}")
    if order_id and order_id != -1:
        time.sleep(3)
        print(f"  撤单: {'成功' if executor.cancel(order_id) else '失败'}")

print("\nPhase 2 完成。请在 miniQMT 确认：委托价极低 + 状态已撤销 + 无成交")
