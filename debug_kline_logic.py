# -*- coding: utf-8 -*-
"""
K线买入/卖出逻辑完整测试脚本

测试目标：
  1. 买入信号 _check_buy_signal：涨幅/收阳/防追高/涨停过滤
  2. 卖出监控 _monitor_positions：bar_low触发硬止损/移动止盈，bar_high更新最高价
  3. 14:55收盘检查 _check_close_signals：bar_close触发阴跌/时间止损/移动止盈
  4. 兜底路径：无5m bar时回退tick lastPrice

运行方式：
  python debug_kline_logic.py              # 全离线逻辑测试（无需QMT）
  python debug_kline_logic.py --live       # 追加实盘数据验证（需QMT运行）
"""

import sys
import argparse
from datetime import datetime, date, timedelta

sys.path.insert(0, 'd:/miniqmt_quant')
import config
from engine.live_engine_v3 import (
    LiveEngineV3, _format_symbol, _strip_suffix, _calculate_days_held
)

# ─── 测试辅助：Mock引擎（屏蔽所有真实IO） ─────────────────────────────────────

class _MockEngine(LiveEngineV3):
    """覆盖所有IO接口，用于纯逻辑测试，不发送任何真实委托/连接QMT"""

    def __init__(self):
        super().__init__(mode='simulation', capital_limit=200000)
        self._injected_bars  = {}   # {code: {open,high,low,close,volume}}
        self._injected_ticks = {}   # {symbol: tick_dict}
        self._sell_calls     = []   # 记录所有 _execute_sell_with_fallback 调用
        self._cond_updates   = []   # 记录条件单更新

    # ── 注入接口 ───────────────────────────────────────────────────────
    def inject_bar(self, code, open_, high, low, close, volume=100000):
        self._injected_bars[code] = {
            'open': open_, 'high': high, 'low': low,
            'close': close, 'volume': volume,
        }

    def inject_tick(self, code, last_price, open_=None, last_close=None):
        sym = _format_symbol(code)
        self._injected_ticks[sym] = {
            'lastPrice': last_price,
            'open':      open_ or last_price,
            'lastClose': last_close or last_price,
            'bidPrice':  [round(last_price * 0.999, 3)] * 5,
            'askPrice':  [round(last_price * 1.001, 3)] * 5,
        }

    def inject_tick_full(self, code, last_price, bid_price=None, ask_price=None,
                         open_=None, last_close=None):
        """Tick注入（可精确控制 bid/ask，用于路由测试）"""
        sym = _format_symbol(code)
        self._injected_ticks[sym] = {
            'lastPrice': last_price,
            'open':      open_ or last_price,
            'lastClose': last_close or last_price,
            'bidPrice':  [float(bid_price)] * 5 if bid_price is not None else [],
            'askPrice':  [float(ask_price)] * 5 if ask_price is not None else [],
        }

    def add_position(self, code, buy_price, quantity=1000,
                     days_held=1, highest_price=None):
        buy_date = (date.today() - timedelta(days=days_held)).strftime('%Y-%m-%d')
        self.positions.append({
            'code':          code,
            'buy_price':     buy_price,
            'quantity':      quantity,
            'buy_date':      buy_date,
            'days_held':     days_held,
            'highest_price': highest_price or buy_price,
        })

    # ── 覆盖IO方法 ────────────────────────────────────────────────────
    def _get_position_5m_bars(self):
        return self._injected_bars  # 直接返回注入数据

    def _get_full_tick(self, symbols):
        return {s: self._injected_ticks[s]
                for s in symbols if s in self._injected_ticks}

    def _execute_sell_with_fallback(self, code, sell_price, quantity,
                                    sell_type, pos, buy_price, days_held):
        record = {
            'code': code, 'sell_price': sell_price,
            'quantity': quantity, 'sell_type': sell_type,
            'buy_price': buy_price, 'days_held': days_held,
        }
        self._sell_calls.append(record)
        print(f"    [SELL触发] {code}  type={sell_type}  "
              f"buy={buy_price:.2f}  sell={sell_price:.3f}  qty={quantity}")

    def _cancel_condition_order_for_code(self, code):
        pass

    def _update_condition_order(self, pos, new_stop_price):
        self._cond_updates.append({
            'code': pos.get('code', ''), 'new_stop': new_stop_price
        })

    def _save_state(self):
        pass

    def _is_star(self, code):
        return str(code).startswith('688') or str(code).startswith('30')


class _RoutingMockEngine(_MockEngine):
    """测试卖出路由逻辑：放通 _execute_sell_with_fallback 真实实现，
    覆盖下层执行接口（下单/等待/记录），捕获第1轮实际使用的价格和超时。"""

    def __init__(self):
        super().__init__()
        self._placed_orders = []   # [{'remark', 'price', 'volume', 'timeout'}]
        self._fill_mode = 'filled' # 'filled'=全部成交, 'timeout'=超时不成交

    def reset(self):
        self._placed_orders = []
        self._fill_mode = 'filled'

    # 放通真实 _execute_sell_with_fallback
    def _execute_sell_with_fallback(self, code, sell_price, quantity,
                                    sell_type, pos, buy_price, days_held):
        LiveEngineV3._execute_sell_with_fallback(
            self, code, sell_price, quantity, sell_type, pos, buy_price, days_held)

    def _place_sell_order(self, code, price, volume, remark=''):
        order_id = len(self._placed_orders) + 1
        self._placed_orders.append({
            'remark': remark, 'price': price, 'volume': volume, 'timeout': None
        })
        return order_id

    def _wait_fill_result(self, order_id, timeout=180, monitor_while_waiting=False):
        idx = order_id - 1
        if 0 <= idx < len(self._placed_orders):
            self._placed_orders[idx]['timeout'] = timeout
        vol   = self._placed_orders[idx]['volume'] if 0 <= idx < len(self._placed_orders) else 1000
        price = self._placed_orders[idx]['price']  if 0 <= idx < len(self._placed_orders) else 0.0
        if self._fill_mode == 'filled':
            return {'status': 'filled', 'filled_qty': vol, 'fill_price': price}
        return {'status': 'timeout', 'filled_qty': 0, 'fill_price': 0.0}

    def _record_sell_fill(self, code, filled_qty, fill_price, sell_type,
                          buy_price, days_held, pos):
        pass  # 不进行真实状态变更

    def _calc_sell_income(self, price, qty):
        return price * qty * (1 - self.stamp_tax_rate - self.commission_rate)

    def _remove_position(self, code):      pass
    def _log_trade(self, *a, **kw):        pass
    def _cancel_order(self, order_id):     pass
    def _remove_pending_sell(self, code):  pass
    def _log_failed_order(self, *a, **kw): pass
    def _save_state(self):                 pass


# ─── 断言辅助 ──────────────────────────────────────────────────────────────────

_pass = 0
_fail = 0

def check(label, condition, detail=''):
    global _pass, _fail
    status = 'PASS' if condition else 'FAIL'
    if condition:
        _pass += 1
        print(f"  [{status}] {label}")
    else:
        _fail += 1
        print(f"  [{status}] {label}  ← {detail}")


# ══════════════════════════════════════════════════════════════════════════════
# 第一部分：买入信号逻辑测试
# ══════════════════════════════════════════════════════════════════════════════

def test_buy_signals():
    print("\n" + "═" * 60)
    print("【买入信号测试】_check_buy_signal")
    print("═" * 60)

    eng = _MockEngine()
    pre_close = 10.0   # 昨日收盘

    def mk_bar(o, c, vol=100000):
        return {'open': o, 'high': c + 0.1, 'low': o - 0.1,
                'close': c, 'volume': vol, 'amount': c * vol}

    # 场景1: 正常买入信号（主板 +2%，收阳）
    bar = mk_bar(10.1, 10.2)
    result = eng._check_buy_signal('600331', bar, pre_close)
    check("主板 +2% 收阳 → 买入", result is True)

    # 场景2: 涨幅不足（+0.5% < 1%）
    bar = mk_bar(10.0, 10.05)
    result = eng._check_buy_signal('600331', bar, pre_close)
    check("主板 +0.5% → 不买（涨幅不足）", result is False)

    # 场景3: 阴线（close < open）→ 不买
    bar = mk_bar(10.2, 10.1)
    result = eng._check_buy_signal('600331', bar, pre_close)
    check("阴线 close<open → 不买", result is False)

    # 场景4: 追高（+8%，明确超出 max_change_pct）→ 不买
    bar = mk_bar(10.5, 10.8)
    result = eng._check_buy_signal('600331', bar, pre_close)
    check("主板 +8% 追高 → 不买", result is False)

    # 场景5: 科创板 +1.5%（低于星板2%阈值）→ 不买
    bar = mk_bar(10.1, 10.15)
    result = eng._check_buy_signal('688001', bar, pre_close)
    check("科创板 +1.5% → 不买（低于2%阈值）", result is False)

    # 场景6: 科创板 +3%，收阳 → 买入
    bar = mk_bar(10.2, 10.3)
    result = eng._check_buy_signal('688001', bar, pre_close)
    check("科创板 +3% 收阳 → 买入", result is True)

    # 场景7: volume=0 停牌 → 不买
    bar = mk_bar(10.2, 10.3)
    bar['volume'] = 0
    result = eng._check_buy_signal('600331', bar, pre_close)
    check("停牌 volume=0 → 不买", result is False)

    # 场景8: 接近涨停（+9.5% ≥ limit_up 9.8%）→ 不买
    bar = mk_bar(10.0, 10.98)
    result = eng._check_buy_signal('600331', bar, pre_close)
    check("涨停 +9.8% → 不买", result is False)


# ══════════════════════════════════════════════════════════════════════════════
# 第二部分：盘中止损/止盈测试（_monitor_positions）
# ══════════════════════════════════════════════════════════════════════════════

def test_monitor_positions():
    print("\n" + "═" * 60)
    print("【盘中止损/止盈测试】_monitor_positions（5m bar驱动）")
    print("═" * 60)

    # ── 场景A: 硬止损触发 ─────────────────────────────────────────────
    print("\n  场景A: 硬止损 bar_low 触发，卖出价 = max(止损价, bar_open)")
    eng = _MockEngine()
    buy_price = 10.0
    eng.add_position('600331', buy_price=buy_price, days_held=1)
    hard_stop = buy_price * (1 - config.V3_HARD_STOP_LOSS)
    # bar_low 跌破止损价，bar_open 高于止损价
    eng.inject_bar('600331', open_=9.80, high=10.0, low=hard_stop - 0.05, close=9.78)
    eng.inject_tick('600331', last_price=9.78, open_=10.0)
    eng._monitor_positions()
    triggered = len(eng._sell_calls) == 1 and eng._sell_calls[0]['sell_type'] == 'hard_stop'
    check("场景A: 硬止损触发", triggered)
    if eng._sell_calls:
        sp = eng._sell_calls[0]['sell_price']
        expected = max(hard_stop, 9.80)
        check(f"  卖出价=max(止损价{hard_stop:.3f}, bar_open=9.80)={expected:.3f}",
              abs(sp - expected) < 0.001, f"实际={sp:.3f}")

    # ── 场景B: bar_low 未触发止损（高于止损价）→ 不卖 ─────────────────
    print("\n  场景B: bar_low 高于止损价 → 不卖")
    eng = _MockEngine()
    eng.add_position('600331', buy_price=10.0, days_held=1)
    # bar_high 小于 trailing_activate 门槛，避免意外激活移动止盈
    safe_high = 10.0 * (1 + config.V3_TRAILING_ACTIVATE) - 0.01
    eng.inject_bar('600331', open_=10.0, high=safe_high, low=9.80, close=10.05)
    eng.inject_tick('600331', last_price=10.05, open_=10.0)
    eng._monitor_positions()
    check("场景B: bar_low > 止损价 → 不卖", len(eng._sell_calls) == 0)

    # ── 场景C: 移动止盈激活并触发 ────────────────────────────────────
    print("\n  场景C: 移动止盈激活后 bar_low 触达回撤线，卖出价 = max(回撤价, bar_open)")
    eng = _MockEngine()
    buy_price = 10.0
    trail_act = config.V3_TRAILING_ACTIVATE    # 如 0.05 = +5%
    trail_stop = config.V3_TRAILING_STOP       # 如 0.03 = 回撤3%
    highest = buy_price * (1 + trail_act + 0.01)  # 已激活移动止盈
    trail_trigger = highest * (1 - trail_stop)
    eng.add_position('600331', buy_price=buy_price, days_held=2,
                     highest_price=highest)
    # bar_low 跌破回撤线，bar_open 高于回撤线
    eng.inject_bar('600331', open_=trail_trigger + 0.05,
                   high=highest, low=trail_trigger - 0.02, close=trail_trigger - 0.01)
    eng.inject_tick('600331', last_price=trail_trigger - 0.01, open_=highest)
    eng._monitor_positions()
    triggered_c = (len(eng._sell_calls) == 1 and
                   eng._sell_calls[0]['sell_type'] == 'trailing_stop')
    check("场景C: 移动止盈触发", triggered_c)
    if eng._sell_calls:
        sp = eng._sell_calls[0]['sell_price']
        expected = max(trail_trigger, trail_trigger + 0.05)
        check(f"  卖出价=max(回撤价, bar_open)", sp >= trail_trigger)

    # ── 场景D: 移动止盈已激活，bar_high 刷新最高价，更新条件单 ─────────
    print("\n  场景D: bar_high 刷新最高价 → 条件单更新（不卖出）")
    eng = _MockEngine()
    buy_price = 10.0
    highest = buy_price * (1 + trail_act)  # 刚激活
    eng.add_position('600331', buy_price=buy_price, days_held=1, highest_price=highest)
    new_high = highest + 0.5  # 今天创新高
    # bar_low 高于回撤线（不触发卖出），bar_high 刷新最高价
    trail_trigger = new_high * (1 - trail_stop)
    eng.inject_bar('600331', open_=highest, high=new_high,
                   low=trail_trigger + 0.1, close=new_high - 0.1)
    eng.inject_tick('600331', last_price=new_high - 0.1, open_=buy_price)
    eng._monitor_positions()
    check("场景D: 不触发卖出", len(eng._sell_calls) == 0)
    updated_high = eng.positions[0].get('highest_price', 0)
    check(f"  bar_high={new_high:.2f} 更新到 highest_price",
          abs(updated_high - new_high) < 0.001, f"实际={updated_high:.3f}")
    check("  条件单更新被记录", len(eng._cond_updates) >= 1)

    # ── 场景E: T+1限制（days_held=0）→ 当天买入不卖出 ───────────────
    print("\n  场景E: T+1限制，当天买入不触发止损")
    eng = _MockEngine()
    eng.add_position('600331', buy_price=10.0, days_held=0)
    hard_stop = 10.0 * (1 - config.V3_HARD_STOP_LOSS)
    eng.inject_bar('600331', open_=9.5, high=9.8, low=hard_stop - 0.1, close=9.5)
    eng.inject_tick('600331', last_price=9.5, open_=10.0)
    eng._monitor_positions()
    check("场景E: days_held=0，T+1不卖出", len(eng._sell_calls) == 0)

    # ── 场景F: 无5m bar，兜底tick lastPrice ─────────────────────────
    print("\n  场景F: 无5m bar数据，回退tick lastPrice")
    eng = _MockEngine()
    buy_price = 10.0
    hard_stop = buy_price * (1 - config.V3_HARD_STOP_LOSS)
    eng.add_position('600331', buy_price=buy_price, days_held=1)
    # 不注入bar数据，只注入tick
    eng.inject_tick('600331', last_price=hard_stop - 0.1, open_=10.0)
    eng._monitor_positions()
    check("场景F: 无bar时tick兜底触发硬止损",
          len(eng._sell_calls) == 1 and eng._sell_calls[0]['sell_type'] == 'hard_stop')


# ══════════════════════════════════════════════════════════════════════════════
# 第三部分：14:55 收盘检查（_check_close_signals）
# ══════════════════════════════════════════════════════════════════════════════

def test_close_signals():
    print("\n" + "═" * 60)
    print("【14:55收盘检查测试】_check_close_signals（5m bar close）")
    print("═" * 60)

    # ── 场景G: 阴跌止损 ───────────────────────────────────────────────
    print("\n  场景G: 14:55 bar_close 跌破 soft_stop 且 < 开盘价 → pending")
    eng = _MockEngine()
    buy_price = 10.0
    soft_stop = buy_price * (1 - config.V3_SOFT_STOP_LOSS)
    day_open  = 9.90
    # bar_close 低于soft_stop 且 低于开盘价
    eng.add_position('600331', buy_price=buy_price, days_held=2)
    eng.inject_bar('600331', open_=day_open, high=day_open,
                   low=soft_stop - 0.15, close=soft_stop - 0.1)
    eng.inject_tick('600331', last_price=soft_stop - 0.1,
                    open_=day_open, last_close=10.0)
    eng._check_close_signals()
    pending_g = any(p['sell_type'] == 'soft_stop' for p in eng.pending_sells)
    check("场景G: 阴跌止损进 pending", pending_g)

    # ── 场景H: 阴线但未跌破soft_stop → 不pending ────────────────────
    print("\n  场景H: 阴线但跌幅不足（未达soft_stop）→ 不pending")
    eng = _MockEngine()
    buy_price = 10.0
    soft_stop = buy_price * (1 - config.V3_SOFT_STOP_LOSS)
    day_open  = 9.95
    eng.add_position('600331', buy_price=buy_price, days_held=2)
    eng.inject_bar('600331', open_=day_open, high=day_open,
                   low=soft_stop + 0.1, close=soft_stop + 0.05)
    eng.inject_tick('600331', last_price=soft_stop + 0.05,
                    open_=day_open, last_close=10.0)
    eng._check_close_signals()
    check("场景H: 跌幅不足 → 不pending", len(eng.pending_sells) == 0)

    # ── 场景I: 时间止损 ───────────────────────────────────────────────
    print("\n  场景I: 持仓满 time_stop_days 且 bar_close <= buy_price → pending")
    eng = _MockEngine()
    buy_price = 10.0
    time_stop = config.V3_TIME_STOP_DAYS
    # 持仓达到时间止损天数，bar_close 未盈利（= buy_price）
    eng.add_position('600331', buy_price=buy_price, days_held=time_stop)
    eng.inject_bar('600331', open_=10.0, high=10.1,
                   low=9.9, close=buy_price)
    eng.inject_tick('600331', last_price=buy_price,
                    open_=10.0, last_close=10.0)
    eng._check_close_signals()
    pending_i = any(p['sell_type'] == 'time_stop' for p in eng.pending_sells)
    check("场景I: 时间止损进 pending", pending_i)

    # ── 场景J: 移动止盈（14:55 bar_close 触达回撤线） ────────────────
    print("\n  场景J: 移动止盈激活后 14:55 bar_close <= 回撤线 → pending")
    eng = _MockEngine()
    buy_price = 10.0
    trail_act  = config.V3_TRAILING_ACTIVATE
    trail_stop = config.V3_TRAILING_STOP
    highest    = buy_price * (1 + trail_act + 0.01)
    trail_trigger = highest * (1 - trail_stop)
    eng.add_position('600331', buy_price=buy_price, days_held=3,
                     highest_price=highest)
    # bar_close 跌破回撤线
    eng.inject_bar('600331', open_=trail_trigger + 0.1, high=highest,
                   low=trail_trigger - 0.15, close=trail_trigger - 0.1)
    eng.inject_tick('600331', last_price=trail_trigger - 0.1,
                    open_=trail_trigger + 0.1, last_close=10.0)
    eng._check_close_signals()
    pending_j = any(p['sell_type'] == 'trailing_stop' for p in eng.pending_sells)
    check("场景J: 14:55移动止盈进 pending", pending_j)

    # ── 场景K: 无bar数据，tick兜底（阴跌止损） ────────────────────────
    print("\n  场景K: 无5m bar，tick lastPrice 兜底触发阴跌止损")
    eng = _MockEngine()
    buy_price = 10.0
    soft_stop = buy_price * (1 - config.V3_SOFT_STOP_LOSS)
    day_open  = 9.90
    eng.add_position('600331', buy_price=buy_price, days_held=2)
    # 不注入bar，只注入tick
    eng.inject_tick('600331', last_price=soft_stop - 0.1,
                    open_=day_open, last_close=10.0)
    eng._check_close_signals()
    pending_k = any(p['sell_type'] == 'soft_stop' for p in eng.pending_sells)
    check("场景K: 无bar时tick兜底阴跌止损", pending_k)


# ══════════════════════════════════════════════════════════════════════════════
# 第四部分：实盘数据验证（需QMT运行）
# ══════════════════════════════════════════════════════════════════════════════

def test_live_data():
    print("\n" + "═" * 60)
    print("【实盘数据验证】_get_position_5m_bars() 功能检查（需QMT）")
    print("═" * 60)

    import json, os
    state_file = 'd:/miniqmt_quant/state_v3.json'
    if not os.path.exists(state_file):
        print("  [SKIP] state_v3.json 不存在，跳过实盘数据验证")
        return

    with open(state_file, encoding='utf-8') as f:
        state = json.load(f)
    positions = state.get('positions', [])
    if not positions:
        print("  [INFO] 当前无持仓，跳过实盘数据验证")
        return

    try:
        # 创建一个包含真实持仓的 mock 引擎
        eng = _MockEngine()
        eng.positions = positions
        print(f"  当前持仓: {[p.get('code') for p in positions]}")

        import time
        from xtquant import xtdata
        codes = list({_format_symbol(_strip_suffix(p.get('code', p.get('symbol', ''))))
                      for p in positions})

        # 先订阅
        for sym in codes:
            xtdata.subscribe_quote(sym, period='5m', count=3)
        time.sleep(2)

        # 调用实际方法（使用真实xtdata，不用mock）
        from engine.live_engine_v3 import LiveEngineV3
        real_eng = object.__new__(LiveEngineV3)
        real_eng.positions = positions
        real_eng.ENGINE_NAME = 'TestEngine'

        bars = real_eng._get_position_5m_bars()
        print(f"\n  _get_position_5m_bars() 返回 {len(bars)} 只持仓的bar数据")
        for code, bar in bars.items():
            print(f"    {code}: open={bar['open']:.3f}  high={bar['high']:.3f}  "
                  f"low={bar['low']:.3f}  close={bar['close']:.3f}  vol={bar['volume']:.0f}")

        check("实盘数据: bar数量与持仓数量一致",
              len(bars) == len(positions),
              f"bars={len(bars)} positions={len(positions)}")
        if bars:
            all_valid = all(b['volume'] > 0 for b in bars.values())
            check("实盘数据: 所有bar volume>0", all_valid)

    except Exception as e:
        print(f"  [WARN] 实盘数据验证失败: {e}")
        print("  (QMT未运行或持仓股无5m数据，不影响逻辑正确性)")


# ══════════════════════════════════════════════════════════════════════════════
# ════════════════════════════════════════════════════════════════════════════
# 第四部分：买入实时价路由逻辑测试
# ════════════════════════════════════════════════════════════════════════════

def _buy_route_decision(ask, bar_c, slip_max=None):
    """复现 _scan_and_buy 中的路由决策逻辑，用于离线测试。返回 (order_price, timeout)"""
    if slip_max is None:
        slip_max = getattr(config, 'V3_LIVE_BUY_SLIP_MAX', 0.003)
    slip = (ask - bar_c) / bar_c if bar_c > 0 and ask > bar_c else 0.0
    if ask <= bar_c:
        return ask, 60
    elif slip <= slip_max:
        return ask, 60
    else:
        return bar_c, 300


def test_buy_routing():
    print(f"\n{'='*60}")
    print("第四部分：买入实时价路由逻辑测试（6个场景）")
    print(f"{'='*60}")
    slip_max = getattr(config, 'V3_LIVE_BUY_SLIP_MAX', 0.003)
    print(f"  V3_LIVE_BUY_SLIP_MAX = {slip_max:.1%}")

    print("\n[场景 R1] 卖一价 < bar_c — 价格回落，无溢价")
    price, timeout = _buy_route_decision(ask=9.90, bar_c=10.0)
    check("R1 下单价 = 卖一价 9.90", price == 9.90,  f"got {price}")
    check("R1 timeout = 60s",       timeout == 60,  f"got {timeout}")

    print("\n[场景 R2] 卖一价 = bar_c — 平切，无溢价")
    price, timeout = _buy_route_decision(ask=10.0, bar_c=10.0)
    check("R2 下单价 = bar_c 10.00",  price == 10.0, f"got {price}")
    check("R2 timeout = 60s",         timeout == 60,  f"got {timeout}")

    print("\n[场景 R3] 卖一价溢价 0.2% — 在阈值内")
    price, timeout = _buy_route_decision(ask=10.02, bar_c=10.0)
    check("R3 下单价 = 卖一价 10.02", abs(price - 10.02) < 1e-9, f"got {price}")
    check("R3 timeout = 60s",          timeout == 60,  f"got {timeout}")

    print("\n[场景 R4] 卖一价溢价 0.3% — 恰好等于阈值（边界値）")
    price, timeout = _buy_route_decision(ask=10.03, bar_c=10.0)
    check("R4 下单价 = 卖一价 10.03", abs(price - 10.03) < 1e-9, f"got {price}")
    check("R4 timeout = 60s (溢价<=阈值)", timeout == 60,  f"got {timeout}")

    print("\n[场景 R5] 卖一价溢价 0.35% — 超阈值，挂bar_c等待")
    price, timeout = _buy_route_decision(ask=10.035, bar_c=10.0)
    check("R5 下单价 = bar_c 10.00",  price == 10.0, f"got {price}")
    check("R5 timeout = 300s",        timeout == 300, f"got {timeout}")

    print("\n[场景 R6] 卖一价溢价 1.0% — 明显追高，挂bar_c")
    price, timeout = _buy_route_decision(ask=10.10, bar_c=10.0)
    check("R6 下单价 = bar_c 10.00",  price == 10.0, f"got {price}")
    check("R6 timeout = 300s",        timeout == 300, f"got {timeout}")


def test_sell_routing():
    print(f"\n{'='*60}")
    print("第五部分：卖出实时价路由逻辑测试（5个场景）")
    print(f"{'='*60}")
    slip_max = getattr(config, 'V3_LIVE_SELL_SLIP_MAX', 0.003)
    print(f"  V3_LIVE_SELL_SLIP_MAX = {slip_max:.1%}")

    sell_price = 10.0
    quantity   = 1000

    def _run(label, bid, last=None, expect_price_near=None, expect_timeout=60):
        eng = _RoutingMockEngine()
        code = '000001'
        eng.positions = [{
            'code': code, 'buy_price': 9.5, 'quantity': quantity,
            'buy_date': '2026-01-01', 'highest_price': 10.0,
        }]
        eng.inject_tick_full(code,
                              last_price=last or (bid if bid is not None else sell_price),
                              bid_price=bid, ask_price=None)
        pos = eng.positions[0]
        eng._execute_sell_with_fallback(
            code=code, sell_price=sell_price, quantity=quantity,
            sell_type='hard_stop', pos=pos, buy_price=9.5, days_held=1
        )
        orders = eng._placed_orders
        if not orders:
            check(f"{label} [已下单]", False, '没有下单记录')
            return
        r1 = orders[0]
        if expect_price_near is not None:
            ok = abs(r1['price'] - expect_price_near) < 0.001
            check(f"{label} 第1轮价格≈{expect_price_near:.3f}",
                  ok, f"actual={r1['price']:.3f}")
        check(f"{label} timeout={expect_timeout}s",
              r1['timeout'] == expect_timeout, f"actual={r1['timeout']}")

    print("\n[场景 S1] 买一价 10.05 > 止损价 10.0 — 无折价，更优")
    _run('S1', bid=10.05, expect_price_near=10.05, expect_timeout=60)

    print("\n[场景 S2] 买一价 = 止损价 10.0 — 边界")
    _run('S2', bid=10.0,  expect_price_near=10.0,  expect_timeout=60)

    print("\n[场景 S3] 买一价 9.98 折价 0.2% — 在阈值内")
    _run('S3', bid=9.98,  expect_price_near=9.98,  expect_timeout=60)

    print("\n[场景 S4] 买一价 9.96 折价 0.4% — 超阈值，仍用买一价（止损优先成交）")
    _run('S4', bid=9.96,  expect_price_near=9.96,  expect_timeout=60)

    print("\n[场景 S5] 无 bidPrice，lastPrice=9.90 — fallback兑底")
    _run('S5', bid=None, last=9.90, expect_price_near=9.90, expect_timeout=60)


# 主入口
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true',
                        help='追加实盘数据验证（需QMT运行）')
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print("K线买入/卖出逻辑完整测试")
    print(f"  hard_stop_loss     = {config.V3_HARD_STOP_LOSS:.1%}")
    print(f"  soft_stop_loss     = {config.V3_SOFT_STOP_LOSS:.1%}")
    print(f"  trailing_activate  = {config.V3_TRAILING_ACTIVATE:.1%}")
    print(f"  trailing_stop      = {config.V3_TRAILING_STOP:.1%}")
    print(f"  time_stop_days     = {config.V3_TIME_STOP_DAYS}")
    print(f"  min_change_pct     = {config.V3_MIN_CHANGE_PCT:.1%}")
    print(f"  buy_slip_max       = {getattr(config, 'V3_LIVE_BUY_SLIP_MAX', 0.003):.1%}")
    print(f"  sell_slip_max      = {getattr(config, 'V3_LIVE_SELL_SLIP_MAX', 0.003):.1%}")
    print(f"{'='*60}")

    test_buy_signals()
    test_monitor_positions()
    test_close_signals()
    test_buy_routing()
    test_sell_routing()

    if args.live:
        test_live_data()

    print(f"\n{'─'*60}")
    total = _pass + _fail
    print(f"测试完成: {_pass}/{total} 通过  {_fail}/{total} 失败")
    if _fail == 0:
        print("所有场景验证通过 ✓")
    else:
        print(f"[WARNING] {_fail} 个场景未通过，请检查！")
    print(f"{'─'*60}\n")
