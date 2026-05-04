# -*- coding: utf-8 -*-
"""
条件单功能单元测试（完全离线，无需 xtquant / miniQMT）

测试范围（共 7 个测试类，35 个用例）：
  TestSetupConditionOrder        - 挂单逻辑、T+1 限制、价格防御、科创板参数
  TestCancelConditionOrder       - 撤单逻辑、失败/异常时仍清内存
  TestSetupAllConditionOrders    - 批量重建、pending 跳过、移动止盈感知
  TestCheckConditionOrderFills   - 成交检测、空持仓保守防护、异常后 pop
  TestReconcileWithBroker        - 持仓核对、空持仓防护、现金同步
  TestCheckCloseSignals          - 信号入队后撤条件单
  TestExecutePendingSellsAuction - 竞价前撤条件单
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, call
from datetime import date

# 把项目根目录加入搜索路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# live_engine_v3 不直接 import xtquant，可以安全导入
from engine.live_engine_v3 import LiveEngineV3, _format_symbol, _strip_suffix


# ===========================================================================
#  测试辅助函数
# ===========================================================================

def _make_engine(mode: str = 'live') -> LiveEngineV3:
    """通过 __new__ 绕过 __init__，手动注入所有运行时属性。
    executor 使用 MagicMock，不依赖任何真实网络连接。
    """
    eng = LiveEngineV3.__new__(LiveEngineV3)
    eng.mode            = mode
    eng.capital_limit   = 30000.0
    eng.max_positions   = 3

    # 策略参数（普通板）
    eng.hard_stop_loss      = 0.05
    eng.soft_stop_loss      = 0.03
    eng.take_profit         = 0.20
    eng.time_stop_days      = 20
    # 策略参数（科创/创业板）
    eng.star_hard_stop_loss  = 0.07
    eng.star_soft_stop_loss  = 0.04
    eng.star_take_profit     = 0.20
    eng.star_time_stop_days  = 20
    # 移动止盈参数
    eng.trailing_activate        = 0.10   # 普通板激活阈值
    eng.trailing_stop            = 0.05   # 普通板回撤幅度
    eng.star_trailing_activate   = 0.08
    eng.star_trailing_stop       = 0.04
    # 交易成本
    eng.commission_rate = 0.0003
    eng.min_commission  = 5.0
    eng.stamp_tax_rate  = 0.001
    # 买入信号阈值
    eng.min_change_pct       = 0.01
    eng.star_min_change_pct  = 0.02
    eng.max_change_pct       = 0.05   # 防追高：主板上限5%
    eng.star_max_change_pct  = 0.08   # 防追高：科创/创业板上限8%
    eng.limit_up      = 0.098
    eng.star_limit_up = 0.198

    # 运行时状态
    eng.positions               = []
    eng.pending_sells           = []
    eng.cash                    = 30000.0
    eng.rebalance_pool          = []
    eng._condition_orders       = {}
    eng._auction_sell_orders    = {}
    eng._pending_buy_orders     = {}
    eng._auction_sells_executed = False
    eng._auction_check_done     = False
    eng._close_check_done       = False
    eng._last_increment_date    = None
    eng._daily_filter_cache     = []
    eng._daily_filter_date      = None
    eng._failed_buys_today      = {}
    eng._last_buy_scan_time     = None
    eng._rebalance_pool_mtime   = 0.0

    # 引擎元信息
    eng.ENGINE_NAME     = 'UnitTestEngine'
    eng.STATE_FILE      = os.path.join(os.path.dirname(__file__), '_unit_test_state.json')
    eng.REBALANCE_FILE  = os.path.join(os.path.dirname(__file__), '_unit_test_rebalance.json')
    eng.TRADES_LOG_FILE = '_unit_test_trades.json'

    # Mock 执行器
    eng.executor = MagicMock()
    eng.executor.is_connected = True
    eng.executor.place_condition_order.return_value  = 1001
    eng.executor.cancel_condition_order.return_value = True
    eng.executor.query_positions.return_value = []
    eng.executor.query_asset.return_value = {'cash': 25000.0}

    # Mock I/O 方法（避免磁盘读写）
    eng._save_state         = MagicMock()
    eng._log_trade          = MagicMock()
    eng._get_available_cash = MagicMock(return_value=25000.0)

    return eng


def _make_pos(code: str, buy_price: float = 10.0, quantity: int = 1000,
              days_held: int = 1, highest_price: float = None) -> dict:
    """构造最小化持仓 dict"""
    return {
        'code'          : code,
        'buy_price'     : buy_price,
        'quantity'      : quantity,
        'days_held'     : days_held,
        'buy_date'      : date.today().strftime('%Y-%m-%d'),
        'highest_price' : highest_price if highest_price is not None else buy_price,
    }


def _make_tick(last_price: float, open_price: float = 0.0,
               high_price: float = 0.0, pre_close: float = 0.0) -> dict:
    """构造最小化 tick dict"""
    return {
        'lastPrice' : last_price,
        'open'      : open_price,
        'high'      : high_price,
        'lastClose' : pre_close,
    }


# ===========================================================================
#  TestSetupConditionOrder — 挂单逻辑
# ===========================================================================

class TestSetupConditionOrder(unittest.TestCase):

    def setUp(self):
        self.eng = _make_engine()

    # ------------------------------------------------------------------
    # 跳过场景
    # ------------------------------------------------------------------

    def test_t0_skips_no_api_call(self):
        """T+0（当天买入，days_held=0）不挂条件单，不调用 API"""
        pos = _make_pos('000001', days_held=0)
        self.assertFalse(self.eng._setup_condition_order(pos))
        self.eng.executor.place_condition_order.assert_not_called()

    def test_sim_mode_skips(self):
        """模拟模式不挂条件单"""
        eng = _make_engine(mode='simulation')
        pos = _make_pos('000001', days_held=1)
        self.assertFalse(eng._setup_condition_order(pos))
        eng.executor.place_condition_order.assert_not_called()

    def test_no_executor_skips(self):
        """executor 为 None 不挂条件单"""
        self.eng.executor = None
        pos = _make_pos('000001', days_held=1)
        self.assertFalse(self.eng._setup_condition_order(pos))

    def test_zero_buy_price_skips(self):
        """买入价为 0 不挂条件单"""
        pos = _make_pos('000001', buy_price=0, days_held=1)
        self.assertFalse(self.eng._setup_condition_order(pos))
        self.eng.executor.place_condition_order.assert_not_called()

    def test_zero_quantity_skips(self):
        """持仓量为 0 不挂条件单"""
        pos = _make_pos('000001', quantity=0, days_held=1)
        self.assertFalse(self.eng._setup_condition_order(pos))
        self.eng.executor.place_condition_order.assert_not_called()

    def test_negative_trigger_price_skips(self):
        """override_stop_price <= 0 不挂条件单（防御无效触发价）"""
        pos = _make_pos('000001', days_held=1)
        self.assertFalse(self.eng._setup_condition_order(pos, override_stop_price=-1.0))
        self.eng.executor.place_condition_order.assert_not_called()

    def test_zero_trigger_price_skips(self):
        """override_stop_price = 0 不挂条件单"""
        pos = _make_pos('000001', days_held=1)
        self.assertFalse(self.eng._setup_condition_order(pos, override_stop_price=0.0))
        self.eng.executor.place_condition_order.assert_not_called()

    # ------------------------------------------------------------------
    # 正常挂单场景
    # ------------------------------------------------------------------

    def test_success_stores_cond_id(self):
        """挂单成功，cond_id 写入 _condition_orders"""
        self.eng.executor.place_condition_order.return_value = 2001
        pos = _make_pos('000001', buy_price=10.0, days_held=1)
        self.assertTrue(self.eng._setup_condition_order(pos))
        self.assertEqual(self.eng._condition_orders.get('000001'), 2001)

    def test_failure_not_stored(self):
        """挂单失败（返回 -1），不写入 _condition_orders"""
        self.eng.executor.place_condition_order.return_value = -1
        pos = _make_pos('000001', buy_price=10.0, days_held=1)
        self.assertFalse(self.eng._setup_condition_order(pos))
        self.assertNotIn('000001', self.eng._condition_orders)

    # ------------------------------------------------------------------
    # 价格参数场景
    # ------------------------------------------------------------------

    def test_normal_board_uses_hard_stop_loss(self):
        """普通板使用 hard_stop_loss 计算触发价"""
        self.eng.executor.place_condition_order.return_value = 3001
        pos = _make_pos('000001', buy_price=10.0, days_held=1)
        self.eng._setup_condition_order(pos)
        kw = self.eng.executor.place_condition_order.call_args.kwargs
        expected = round(10.0 * (1 - 0.05), 3)   # hard_stop_loss=0.05
        self.assertAlmostEqual(kw['trigger_price'], expected, places=3)

    def test_star_board_688_uses_star_stop_loss(self):
        """科创板(688xxx) 使用 star_hard_stop_loss 计算触发价"""
        self.eng.executor.place_condition_order.return_value = 4001
        pos = _make_pos('688001', buy_price=20.0, days_held=1)
        self.eng._setup_condition_order(pos)
        kw = self.eng.executor.place_condition_order.call_args.kwargs
        expected = round(20.0 * (1 - 0.07), 3)   # star_hard_stop_loss=0.07
        self.assertAlmostEqual(kw['trigger_price'], expected, places=3)

    def test_star_board_300_uses_star_stop_loss(self):
        """创业板(300xxx) 使用 star_hard_stop_loss 计算触发价"""
        self.eng.executor.place_condition_order.return_value = 4002
        pos = _make_pos('300001', buy_price=15.0, days_held=1)
        self.eng._setup_condition_order(pos)
        kw = self.eng.executor.place_condition_order.call_args.kwargs
        expected = round(15.0 * (1 - 0.07), 3)
        self.assertAlmostEqual(kw['trigger_price'], expected, places=3)

    def test_override_stop_price_used(self):
        """override_stop_price 覆盖默认硬止损价"""
        self.eng.executor.place_condition_order.return_value = 5001
        pos = _make_pos('000001', buy_price=10.0, days_held=1)
        self.eng._setup_condition_order(pos, override_stop_price=9.50)
        kw = self.eng.executor.place_condition_order.call_args.kwargs
        self.assertAlmostEqual(kw['trigger_price'], 9.50, places=2)

    def test_sell_price_is_slightly_below_trigger(self):
        """委托价 = 触发价 × 0.995，略低于触发价确保成交"""
        self.eng.executor.place_condition_order.return_value = 6001
        pos = _make_pos('000001', buy_price=10.0, days_held=1)
        self.eng._setup_condition_order(pos)
        kw = self.eng.executor.place_condition_order.call_args.kwargs
        trigger = kw['trigger_price']
        sell    = kw['sell_price']
        self.assertAlmostEqual(sell, round(trigger * 0.995, 3), places=3)
        self.assertLess(sell, trigger)

    # ------------------------------------------------------------------
    # 防重复挂单场景
    # ------------------------------------------------------------------

    def test_cancels_existing_before_new(self):
        """已有条件单时先撤旧单再挂新单"""
        self.eng._condition_orders['000001'] = 999
        self.eng.executor.cancel_condition_order.return_value = True
        self.eng.executor.place_condition_order.return_value = 7001
        pos = _make_pos('000001', buy_price=10.0, days_held=1)
        self.assertTrue(self.eng._setup_condition_order(pos))
        # 旧单被撤
        self.eng.executor.cancel_condition_order.assert_called_once_with(999)
        # 新 cond_id 写入
        self.assertEqual(self.eng._condition_orders.get('000001'), 7001)

    def test_place_exception_returns_false(self):
        """place_condition_order 抛异常 → 返回 False，cond_id 不存入（覆盖 L571-573）"""
        self.eng.executor.place_condition_order.side_effect = RuntimeError('API crash')
        pos = _make_pos('000001', buy_price=10.0, days_held=1)
        result = self.eng._setup_condition_order(pos)
        self.assertFalse(result)
        self.assertNotIn('000001', self.eng._condition_orders)


# ===========================================================================
#  TestCancelConditionOrder — 撤单逻辑
# ===========================================================================

class TestCancelConditionOrder(unittest.TestCase):

    def setUp(self):
        self.eng = _make_engine()

    def test_no_order_returns_true_no_api_call(self):
        """股票未在 _condition_orders 中，直接返回 True，不调用 API"""
        result = self.eng._cancel_condition_order_for_code('000001')
        self.assertTrue(result)
        self.eng.executor.cancel_condition_order.assert_not_called()

    def test_cancel_success_removes_from_dict(self):
        """撤销成功，从 _condition_orders 中移除"""
        self.eng._condition_orders['000001'] = 1001
        self.eng.executor.cancel_condition_order.return_value = True
        result = self.eng._cancel_condition_order_for_code('000001')
        self.assertTrue(result)
        self.assertNotIn('000001', self.eng._condition_orders)
        self.eng.executor.cancel_condition_order.assert_called_once_with(1001)

    def test_cancel_fail_still_removes_from_dict(self):
        """撤销失败（API 返回 False），仍从 _condition_orders 中移除（防重复撤）"""
        self.eng._condition_orders['000002'] = 1002
        self.eng.executor.cancel_condition_order.return_value = False
        result = self.eng._cancel_condition_order_for_code('000002')
        self.assertFalse(result)
        self.assertNotIn('000002', self.eng._condition_orders)

    def test_cancel_exception_still_removes_from_dict(self):
        """撤销抛异常，仍从 _condition_orders 中移除"""
        self.eng._condition_orders['000003'] = 1003
        self.eng.executor.cancel_condition_order.side_effect = RuntimeError('network error')
        result = self.eng._cancel_condition_order_for_code('000003')
        self.assertFalse(result)
        self.assertNotIn('000003', self.eng._condition_orders)


# ===========================================================================
#  TestSetupAllConditionOrders — 批量重建
# ===========================================================================

class TestSetupAllConditionOrders(unittest.TestCase):

    def setUp(self):
        self.eng = _make_engine()
        self.eng.executor.place_condition_order.return_value = 8001

    def test_sim_mode_skips(self):
        """非实盘模式直接返回，不挂任何单"""
        eng = _make_engine(mode='simulation')
        eng.positions = [_make_pos('000001')]
        eng._setup_all_condition_orders()
        eng.executor.place_condition_order.assert_not_called()

    def test_no_executor_skips(self):
        """executor 为 None 直接返回"""
        self.eng.executor = None
        self.eng.positions = [_make_pos('000001')]
        self.eng._setup_all_condition_orders()  # 不应抛异常

    def test_pending_sells_codes_skipped(self):
        """pending_sells 中的股票跳过挂单（即将竞价卖出，无需服务器止损）"""
        pos = _make_pos('000001', days_held=1)
        pending = dict(pos)
        pending['sell_type'] = 'soft_stop'
        self.eng.positions    = [pos]
        self.eng.pending_sells = [pending]
        self.eng._setup_all_condition_orders()
        self.eng.executor.place_condition_order.assert_not_called()

    def test_trailing_not_activated_uses_default_hard_stop(self):
        """移动止盈未激活 → 使用默认硬止损触发价"""
        # buy=10, highest=10.5, activate_threshold=10*(1+0.10)=11.0 > 10.5 → 未激活
        pos = _make_pos('000001', buy_price=10.0, highest_price=10.5, days_held=1)
        self.eng.positions = [pos]
        self.eng._setup_all_condition_orders()
        kw = self.eng.executor.place_condition_order.call_args.kwargs
        expected = round(10.0 * (1 - 0.05), 3)   # hard_stop_loss=0.05
        self.assertAlmostEqual(kw['trigger_price'], expected, places=3)

    def test_trailing_activated_uses_trailing_trigger(self):
        """移动止盈已激活 → 使用回撤触发线，比硬止损更高（保护更严）"""
        # buy=10, highest=12.0, activate_threshold=10*(1+0.10)=11.0 <= 12 → 已激活
        # override = round(12.0 * (1 - 0.05), 3) = 11.4
        pos = _make_pos('000001', buy_price=10.0, highest_price=12.0, days_held=1)
        self.eng.positions = [pos]
        self.eng._setup_all_condition_orders()
        kw = self.eng.executor.place_condition_order.call_args.kwargs
        expected_override = round(12.0 * (1 - 0.05), 3)   # trailing_stop=0.05
        hard_stop_trigger = round(10.0 * (1 - 0.05), 3)
        self.assertAlmostEqual(kw['trigger_price'], expected_override, places=3)
        # 确认回撤线比硬止损更高（更严格的保护）
        self.assertGreater(expected_override, hard_stop_trigger)

    def test_t0_positions_skipped_in_batch(self):
        """T+0 买入当日的持仓在批量重建中被跳过"""
        self.eng.positions = [
            _make_pos('000001', days_held=1),   # T+1，应挂单
            _make_pos('000002', days_held=0),   # T+0，跳过
        ]
        self.eng._setup_all_condition_orders()
        # 只有 000001 应被挂单
        self.assertEqual(self.eng.executor.place_condition_order.call_count, 1)
        call_kw = self.eng.executor.place_condition_order.call_args.kwargs
        self.assertIn('000001', call_kw.get('symbol', ''))


# ===========================================================================
#  TestCheckConditionOrderFills — 条件单成交检测
# ===========================================================================

class TestCheckConditionOrderFills(unittest.TestCase):

    def setUp(self):
        self.eng = _make_engine()

    def test_sim_mode_skips(self):
        """非实盘模式直接返回，不查询持仓"""
        eng = _make_engine(mode='simulation')
        eng._condition_orders = {'000001': 1001}
        eng.positions         = [_make_pos('000001')]
        eng._check_condition_order_fills()
        eng.executor.query_positions.assert_not_called()

    def test_no_condition_orders_skips(self):
        """_condition_orders 为空直接返回，不查询持仓"""
        self.eng._condition_orders = {}
        self.eng.positions         = [_make_pos('000001')]
        self.eng._check_condition_order_fills()
        self.eng.executor.query_positions.assert_not_called()

    def test_safety_guard_empty_broker_with_positions(self):
        """broker 返回空持仓但策略有持仓 → 保守跳过，防止误清仓"""
        self.eng.positions         = [_make_pos('000001')]
        self.eng._condition_orders = {'000001': 1001}
        self.eng.executor.query_positions.return_value = []
        self.eng._check_condition_order_fills()
        # 持仓和条件单均保持不变
        self.assertEqual(len(self.eng.positions), 1)
        self.assertIn('000001', self.eng._condition_orders)
        self.eng._save_state.assert_not_called()

    def test_fill_detected_cleans_positions_and_pending(self):
        """条件单已成交（broker 无该股）→ 清理 positions、pending_sells、_condition_orders"""
        pos     = _make_pos('000001', buy_price=10.0, quantity=1000, days_held=2)
        pending = dict(pos)
        pending['sell_type'] = 'soft_stop'
        self.eng.positions         = [pos]
        self.eng.pending_sells     = [pending]
        self.eng._condition_orders = {'000001': 1001}
        # broker 返回非空（绕过安全防护），但不含 000001
        self.eng.executor.query_positions.return_value = [
            {'symbol': '000002.SZ', 'volume': 500}
        ]
        self.eng._check_condition_order_fills()
        # 000001 应从 positions 和 pending_sells 中删除
        self.assertEqual(len(self.eng.positions), 0)
        self.assertEqual(len(self.eng.pending_sells), 0)
        self.assertNotIn('000001', self.eng._condition_orders)
        # 应记录交易日志
        self.eng._log_trade.assert_called_once()
        # 应同步现金并保存状态
        self.eng._get_available_cash.assert_called()
        self.eng._save_state.assert_called()

    def test_code_still_in_broker_no_action(self):
        """条件单未成交（broker 仍有该股）→ 不清理任何记录"""
        pos = _make_pos('000001', days_held=2)
        self.eng.positions         = [pos]
        self.eng._condition_orders = {'000001': 1001}
        self.eng.executor.query_positions.return_value = [
            {'symbol': '000001.SZ', 'volume': 1000}
        ]
        self.eng._check_condition_order_fills()
        self.assertEqual(len(self.eng.positions), 1)
        self.assertIn('000001', self.eng._condition_orders)
        self.eng._log_trade.assert_not_called()

    def test_fill_exception_still_pops_condition_order(self):
        """成交清理过程中抛异常 → _condition_orders 仍被 pop，防止下次心跳死循环（覆盖 L688）"""
        pos = _make_pos('000001', buy_price=10.0, quantity=1000, days_held=2)
        self.eng.positions         = [pos]
        self.eng._condition_orders = {'000001': 1001}
        self.eng.executor.query_positions.return_value = [
            {'symbol': '000002.SZ', 'volume': 500}
        ]
        # 让 _remove_position 抛异常模拟清理失败
        self.eng._remove_position = MagicMock(side_effect=RuntimeError('disk full'))
        self.eng._check_condition_order_fills()
        # 即使异常，000001 也应从 _condition_orders 中 pop
        self.assertNotIn('000001', self.eng._condition_orders)

    def test_query_positions_exception_returns_early(self):
        """query_positions() 抛异常 → 返回，所有记录保持不变（覆盖 L647-649）"""
        self.eng._condition_orders = {'000001': 1001}
        self.eng.positions         = [_make_pos('000001')]
        self.eng.executor.query_positions.side_effect = RuntimeError('connection lost')
        self.eng._check_condition_order_fills()
        # 异常后应保持不变
        self.assertEqual(len(self.eng.positions), 1)
        self.assertIn('000001', self.eng._condition_orders)
        self.eng._log_trade.assert_not_called()

    def test_orphaned_condition_order_cleaned(self):
        """条件单有记录但 positions 无对应持仓（孤儿条件单）→ 清理内存，不记录交易（覆盖 L682）"""
        # 场景：_condition_orders 有残留，但策略 positions 已无对应记录
        self.eng._condition_orders = {'000001': 1001}
        self.eng.positions         = []   # 无对应持仓
        # broker 返回非空（绕过安全防护），但不含 000001
        self.eng.executor.query_positions.return_value = [
            {'symbol': '000002.SZ', 'volume': 500}
        ]
        self.eng._check_condition_order_fills()
        # 孤儿条件单应被清理
        self.assertNotIn('000001', self.eng._condition_orders)
        # 无持仓记录可匹配，不应写交易日志
        self.eng._log_trade.assert_not_called()

    def test_multiple_fills_all_cleaned(self):
        """多只条件单同时成交 → 全部清理"""
        pos1 = _make_pos('000001', buy_price=10.0, days_held=2)
        pos2 = _make_pos('000002', buy_price=12.0, days_held=3)
        self.eng.positions         = [pos1, pos2]
        self.eng._condition_orders = {'000001': 1001, '000002': 1002}
        # broker 返回空（但因有持仓会触发安全防护…）
        # 所以让 broker 返回完全不相关的股票以绕过防护
        self.eng.executor.query_positions.return_value = [
            {'symbol': '000003.SZ', 'volume': 500}
        ]
        self.eng._check_condition_order_fills()
        self.assertEqual(len(self.eng.positions), 0)
        self.assertEqual(len(self.eng._condition_orders), 0)
        self.assertEqual(self.eng._log_trade.call_count, 2)


# ===========================================================================
#  TestReconcileWithBroker — 启动时持仓核对
# ===========================================================================

class TestReconcileWithBroker(unittest.TestCase):

    def setUp(self):
        self.eng = _make_engine()

    def test_safety_guard_empty_broker_with_positions(self):
        """broker 返回空持仓但策略有持仓 → 跳过核对，保留持仓"""
        pos = _make_pos('000001')
        self.eng.positions = [pos]
        self.eng.executor.query_positions.return_value = []
        self.eng._reconcile_with_broker()
        self.assertEqual(len(self.eng.positions), 1)
        self.eng._save_state.assert_not_called()

    def test_missing_in_broker_cleans_position(self):
        """策略有但 broker 无 → 清理持仓（判断为条件单已成交）"""
        pos = _make_pos('000001', buy_price=10.0, quantity=1000, days_held=2)
        self.eng.positions = [pos]
        # broker 返回非空（绕过安全防护），但不含 000001
        self.eng.executor.query_positions.return_value = [
            {'symbol': '000002.SZ', 'volume': 500}
        ]
        self.eng._reconcile_with_broker()
        self.assertEqual(len(self.eng.positions), 0)
        self.eng._log_trade.assert_called_once()

    def test_missing_in_broker_cleans_condition_orders(self):
        """missing_in_broker 时同步清理 _condition_orders"""
        pos = _make_pos('000001', buy_price=10.0, quantity=1000, days_held=2)
        self.eng.positions         = [pos]
        self.eng._condition_orders = {'000001': 1001}
        self.eng.executor.query_positions.return_value = [
            {'symbol': '000002.SZ', 'volume': 500}
        ]
        self.eng._reconcile_with_broker()
        self.assertNotIn('000001', self.eng._condition_orders)

    def test_missing_in_broker_also_cleans_pending_sells(self):
        """missing_in_broker 时同步清理 pending_sells（防次日重复竞价卖出）"""
        pos     = _make_pos('000001', buy_price=10.0, quantity=1000, days_held=2)
        pending = dict(pos)
        pending['sell_type'] = 'soft_stop'
        self.eng.positions     = [pos]
        self.eng.pending_sells = [pending]
        self.eng.executor.query_positions.return_value = [
            {'symbol': '000002.SZ', 'volume': 500}
        ]
        self.eng._reconcile_with_broker()
        self.assertEqual(len(self.eng.pending_sells), 0)

    def test_missing_in_broker_syncs_cash(self):
        """missing_in_broker 后调用 _get_available_cash 同步现金"""
        pos = _make_pos('000001', buy_price=10.0, quantity=1000, days_held=2)
        self.eng.positions = [pos]
        self.eng.executor.query_positions.return_value = [
            {'symbol': '000002.SZ', 'volume': 500}
        ]
        self.eng._reconcile_with_broker()
        self.eng._get_available_cash.assert_called()

    def test_extra_in_broker_does_not_add_position(self):
        """broker 有额外股票（手动买入）→ 只打印警告，不干预策略持仓"""
        self.eng.positions = []
        self.eng.executor.query_positions.return_value = [
            {'symbol': '000001.SZ', 'volume': 500}
        ]
        self.eng._reconcile_with_broker()
        self.assertEqual(len(self.eng.positions), 0)
        self.eng._save_state.assert_not_called()

    def test_consistent_positions_no_action(self):
        """持仓核对一致 → 无需任何清理操作"""
        pos = _make_pos('000001', buy_price=10.0, quantity=1000, days_held=2)
        self.eng.positions = [pos]
        self.eng.executor.query_positions.return_value = [
            {'symbol': '000001.SZ', 'volume': 1000}
        ]
        self.eng._reconcile_with_broker()
        self.assertEqual(len(self.eng.positions), 1)
        self.eng._save_state.assert_not_called()
        self.eng._log_trade.assert_not_called()


# ===========================================================================
#  TestCheckCloseSignals — 14:55 信号入队撤条件单
# ===========================================================================

class TestCheckCloseSignals(unittest.TestCase):

    def setUp(self):
        self.eng = _make_engine()

    def test_soft_stop_adds_pending_and_cancels_condition(self):
        """阴跌止损信号 → 加入 pending_sells 并撤销条件单"""
        pos = _make_pos('000001', buy_price=10.0, days_held=1)
        self.eng.positions         = [pos]
        self.eng._condition_orders = {'000001': 1001}
        # last=9.5 < soft_stop=10*(1-0.03)=9.7，且 last < open → 触发 soft_stop
        tick = _make_tick(last_price=9.5, open_price=9.6, high_price=9.8, pre_close=10.0)
        self.eng._get_full_tick = MagicMock(return_value={'000001.SZ': tick})
        self.eng._check_close_signals()
        # 应加入 pending_sells
        self.assertEqual(len(self.eng.pending_sells), 1)
        self.assertEqual(self.eng.pending_sells[0].get('sell_type'), 'soft_stop')
        # 应撤销条件单
        self.assertNotIn('000001', self.eng._condition_orders)
        self.eng.executor.cancel_condition_order.assert_called_once_with(1001)

    def test_trailing_stop_adds_pending_and_cancels_condition(self):
        """移动止盈触发 → 加入 pending_sells 并撤销条件单"""
        # buy=10, highest=12.0, activate=10*(1+0.10)=11 → 已激活
        # trail_trigger=12*(1-0.05)=11.4, last=11.0 <= 11.4 → 触发
        pos = _make_pos('000001', buy_price=10.0, highest_price=12.0, days_held=1)
        self.eng.positions         = [pos]
        self.eng._condition_orders = {'000001': 2001}
        tick = _make_tick(last_price=11.0, open_price=11.5, high_price=12.0, pre_close=11.8)
        self.eng._get_full_tick = MagicMock(return_value={'000001.SZ': tick})
        self.eng._check_close_signals()
        self.assertEqual(len(self.eng.pending_sells), 1)
        self.assertEqual(self.eng.pending_sells[0].get('sell_type'), 'trailing_stop')
        self.assertNotIn('000001', self.eng._condition_orders)

    def test_time_stop_adds_pending_and_cancels_condition(self):
        """时间止损触发 → 加入 pending_sells 并撤销条件单"""
        # days_held=20 >= time_stop_days=20, last_price <= buy_price → 触发
        pos = _make_pos('000001', buy_price=10.0, days_held=20)
        self.eng.positions         = [pos]
        self.eng._condition_orders = {'000001': 3001}
        # last_price <= buy_price，且无阴跌和移动止盈
        tick = _make_tick(last_price=9.8, open_price=9.7, high_price=10.0, pre_close=9.9)
        self.eng._get_full_tick = MagicMock(return_value={'000001.SZ': tick})
        self.eng._check_close_signals()
        self.assertEqual(len(self.eng.pending_sells), 1)
        self.assertEqual(self.eng.pending_sells[0].get('sell_type'), 'time_stop')
        self.assertNotIn('000001', self.eng._condition_orders)

    def test_no_signal_condition_order_preserved(self):
        """无信号 → 条件单保持不变，pending_sells 不增加"""
        pos = _make_pos('000001', buy_price=10.0, days_held=1)
        self.eng.positions         = [pos]
        self.eng._condition_orders = {'000001': 4001}
        # 股价正常上涨，无任何信号
        tick = _make_tick(last_price=10.5, open_price=10.3, high_price=10.8, pre_close=10.2)
        self.eng._get_full_tick = MagicMock(return_value={'000001.SZ': tick})
        self.eng._check_close_signals()
        self.assertEqual(len(self.eng.pending_sells), 0)
        self.assertIn('000001', self.eng._condition_orders)
        self.eng.executor.cancel_condition_order.assert_not_called()

    def test_already_in_pending_no_duplicate(self):
        """已在 pending_sells 中 → 不重复添加，也不重复撤条件单"""
        pos     = _make_pos('000001', buy_price=10.0, days_held=1)
        pending = dict(pos)
        pending['sell_type'] = 'soft_stop'
        self.eng.positions     = [pos]
        self.eng.pending_sells = [pending]
        self.eng._condition_orders = {}   # 条件单已不存在（之前已撤）
        tick = _make_tick(last_price=9.5, open_price=9.6, high_price=9.8, pre_close=10.0)
        self.eng._get_full_tick = MagicMock(return_value={'000001.SZ': tick})
        self.eng._check_close_signals()
        # pending_sells 仍为 1 条（不重复添加）
        self.assertEqual(len(self.eng.pending_sells), 1)

    def test_t0_position_skipped(self):
        """T+0 持仓不触发收盘信号（T+1 限制）"""
        pos = _make_pos('000001', buy_price=10.0, days_held=0)
        self.eng.positions         = [pos]
        self.eng._condition_orders = {'000001': 5001}
        tick = _make_tick(last_price=9.5, open_price=9.6, high_price=9.8, pre_close=10.0)
        self.eng._get_full_tick = MagicMock(return_value={'000001.SZ': tick})
        self.eng._check_close_signals()
        # T+0 跳过，不加入 pending_sells
        self.assertEqual(len(self.eng.pending_sells), 0)
        # 条件单保持不变
        self.assertIn('000001', self.eng._condition_orders)


# ===========================================================================
#  TestExecutePendingSellsAuction — 竞价前撤条件单
# ===========================================================================

class TestExecutePendingSellsAuction(unittest.TestCase):

    def setUp(self):
        self.eng = _make_engine()
        self.eng._place_sell_order = MagicMock(return_value=88001)

    def test_no_pending_sells_no_cancel(self):
        """无 pending 卖出任务 → 不触发任何条件单撤销"""
        self.eng.pending_sells     = []
        self.eng._condition_orders = {'000001': 9001}
        self.eng._execute_pending_sells_auction()
        self.eng.executor.cancel_condition_order.assert_not_called()
        self.assertIn('000001', self.eng._condition_orders)

    def test_cancel_condition_before_place_sell(self):
        """竞价卖出前先撤条件单，再挂卖单"""
        pos = _make_pos('000001', buy_price=10.0, quantity=1000, days_held=1)
        pos['sell_type'] = 'soft_stop'
        self.eng.pending_sells     = [pos]
        self.eng._condition_orders = {'000001': 9001}
        tick = _make_tick(last_price=10.0, open_price=10.0)
        tick['lastClose'] = 10.0
        self.eng._get_full_tick = MagicMock(return_value={'000001.SZ': tick})
        self.eng._execute_pending_sells_auction()
        # 条件单应已撤销
        self.assertNotIn('000001', self.eng._condition_orders)
        self.eng.executor.cancel_condition_order.assert_called_once_with(9001)
        # 卖单应已挂出
        self.eng._place_sell_order.assert_called_once()

    def test_cancel_only_relevant_condition_order(self):
        """只撤销 pending 股票的条件单，不撤其他股票"""
        pos = _make_pos('000001', buy_price=10.0, quantity=1000, days_held=1)
        pos['sell_type'] = 'soft_stop'
        self.eng.pending_sells     = [pos]
        self.eng._condition_orders = {
            '000001': 9001,   # 应被撤
            '000002': 9002,   # 不应被撤
        }
        tick = {'lastClose': 10.0}
        self.eng._get_full_tick = MagicMock(return_value={'000001.SZ': tick})
        self.eng._execute_pending_sells_auction()
        self.assertNotIn('000001', self.eng._condition_orders)
        self.assertIn('000002', self.eng._condition_orders)


# ===========================================================================
#  入口
# ===========================================================================

if __name__ == '__main__':
    unittest.main(verbosity=2)
