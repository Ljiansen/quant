# -*- coding: utf-8 -*-
"""
test_strategy_v3_buy_signal.py

StrategyV3.check_buy_signal 单元测试（完全离线，无需历史数据）

测试覆盖范围（共 2 个测试类，18 个用例）：
  TestCheckBuySignalMainBoard  - 主板所有分支（含防追高过滤）
  TestCheckBuySignalStarBoard  - 科创板/创业板所有分支（含防追高过滤）
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy.strategy_v3 import StrategyV3


# ===========================================================================
#  测试辅助
# ===========================================================================

def _make_strat(params=None) -> StrategyV3:
    """构造 StrategyV3 实例；可通过 params 覆盖默认参数。"""
    return StrategyV3(params or {})


def _bar(close: float, open_price: float = 9.5, volume: int = 100_000) -> dict:
    """快捷构造 bar dict"""
    return {'close': close, 'open': open_price, 'volume': volume}


# 默认参数（与 config.py 保持同步）
_PRE_CLOSE = 10.0
_MIN_CHANGE   = 0.01   # V3_MIN_CHANGE_PCT
_MAX_CHANGE   = 0.07   # V3_MAX_CHANGE_PCT（config 更新为 7%）
_LIMIT_UP     = 0.098  # 主板涨停

_STAR_MIN_CHANGE = 0.02  # V3_STAR_MIN_CHANGE_PCT
_STAR_MAX_CHANGE = 0.08  # V3_STAR_MAX_CHANGE_PCT
_STAR_LIMIT_UP   = 0.198  # V3_STAR_LIMIT_UP


# ===========================================================================
#  主板测试
# ===========================================================================

class TestCheckBuySignalMainBoard(unittest.TestCase):
    """主板(000/600开头)的买入信号检查"""

    CODE = '000001'

    def setUp(self):
        self.s = _make_strat()

    # ── 边界防护 ──────────────────────────────────────────────────────────

    def test_pre_close_zero_returns_false(self):
        """pre_close=0 → False（无法计算涨幅）"""
        self.assertFalse(self.s.check_buy_signal(
            self.CODE, _bar(10.5), pre_close=0
        ))

    def test_pre_close_none_returns_false(self):
        """pre_close=None → False（无效价格）"""
        self.assertFalse(self.s.check_buy_signal(
            self.CODE, _bar(10.5), pre_close=None
        ))

    def test_volume_zero_returns_false(self):
        """成交量=0（停牌）→ False"""
        self.assertFalse(self.s.check_buy_signal(
            self.CODE, _bar(10.5, volume=0), pre_close=_PRE_CLOSE
        ))

    # ── 涨幅下限过滤 ───────────────────────────────────────────────────────

    def test_change_pct_below_min_returns_false(self):
        """涨幅 0.5% < min_change_pct(1%) → False"""
        close = _PRE_CLOSE * (1 + 0.005)   # +0.5%
        self.assertFalse(self.s.check_buy_signal(
            self.CODE, _bar(close), pre_close=_PRE_CLOSE
        ))

    def test_change_pct_equal_min_returns_false(self):
        """涨幅恰好 = min_change_pct(1%)，判断 <= → False"""
        close = _PRE_CLOSE * (1 + _MIN_CHANGE)
        self.assertFalse(self.s.check_buy_signal(
            self.CODE, _bar(round(close, 4)), pre_close=_PRE_CLOSE
        ))

    # ── 防追高过滤（新增逻辑）──────────────────────────────────────────────

    def test_change_pct_equal_max_returns_false(self):
        """涨幅恰好 = max_change_pct(5%)，判断 >= → False（防追高）"""
        close = _PRE_CLOSE * (1 + _MAX_CHANGE)
        self.assertFalse(self.s.check_buy_signal(
            self.CODE, _bar(round(close, 4)), pre_close=_PRE_CLOSE
        ))

    def test_change_pct_above_max_returns_false(self):
        """涨幅 8% > max_change_pct(5%) → False（防追高）"""
        close = _PRE_CLOSE * (1 + 0.08)
        self.assertFalse(self.s.check_buy_signal(
            self.CODE, _bar(round(close, 4)), pre_close=_PRE_CLOSE
        ))

    def test_custom_max_change_pct_respected(self):
        """通过参数设定 max_change_pct=3%，涨幅4%应被过滤"""
        s = _make_strat({'max_change_pct': 0.03})
        self.assertEqual(s.max_change_pct, 0.03)
        close = _PRE_CLOSE * (1 + 0.04)  # +4% > 3%
        self.assertFalse(s.check_buy_signal(
            self.CODE, _bar(round(close, 4)), pre_close=_PRE_CLOSE
        ))

    # ── 阴线过滤 ───────────────────────────────────────────────────────────

    def test_yin_line_returns_false(self):
        """收阴线(close <= open) → False（涨幅合格但仍需收阳）"""
        # change_pct = 3%（合格），但 close < open
        close = _PRE_CLOSE * (1 + 0.03)   # close=10.3
        self.assertFalse(self.s.check_buy_signal(
            self.CODE, _bar(close, open_price=10.5), pre_close=_PRE_CLOSE
        ))

    # ── 涨停过滤 ───────────────────────────────────────────────────────────

    def test_limit_up_returns_false(self):
        """涨幅 >= limit_up(9.8%) → False（已涨停）

        为使 limit_up 分支可达，需要 max_change_pct > limit_up，
        否则防追高过滤就会先一步截街。
        """
        s = _make_strat({'max_change_pct': 0.15})   # 主板上限调高到 15%
        close = _PRE_CLOSE * (1 + _LIMIT_UP)        # change_pct = 9.8% = limit_up
        # 9.8% < 15% → 通过防追高
        # 9.8% >= 9.8% → 被涨停过滤抓住
        self.assertFalse(s.check_buy_signal(
            self.CODE, _bar(round(close, 4)), pre_close=_PRE_CLOSE
        ))

    # ── 全条件满足 ─────────────────────────────────────────────────────────

    def test_all_conditions_pass_returns_true(self):
        """主板：涨幅3%（1%<3%<5%），收阳线，未涨停 → True"""
        # close=10.3, open=9.5 → yang line; change=3%
        self.assertTrue(self.s.check_buy_signal(
            self.CODE, _bar(10.3, open_price=9.5), pre_close=_PRE_CLOSE
        ))


# ===========================================================================
#  科创板 / 创业板测试
# ===========================================================================

class TestCheckBuySignalStarBoard(unittest.TestCase):
    """科创板(688开头)和创业板(300/301开头)的买入信号检查"""

    CODE_STAR  = '688001'   # 科创板
    CODE_CYB   = '300001'   # 创业板

    def setUp(self):
        self.s = _make_strat()

    # ── 涨幅下限过滤 ───────────────────────────────────────────────────────

    def test_star_change_pct_below_min_returns_false(self):
        """科创板涨幅1% < star_min_change_pct(2%) → False"""
        close = _PRE_CLOSE * (1 + 0.01)
        self.assertFalse(self.s.check_buy_signal(
            self.CODE_STAR, _bar(round(close, 4)), pre_close=_PRE_CLOSE
        ))

    def test_star_change_pct_equal_min_returns_false(self):
        """科创板涨幅 = star_min_change_pct(2%)，<= → False"""
        close = _PRE_CLOSE * (1 + _STAR_MIN_CHANGE)
        self.assertFalse(self.s.check_buy_signal(
            self.CODE_STAR, _bar(round(close, 4)), pre_close=_PRE_CLOSE
        ))

    # ── 防追高过滤（科创板，新增逻辑）───────────────────────────────────────

    def test_star_change_pct_equal_max_returns_false(self):
        """科创板涨幅 = star_max_change_pct(8%) → False（防追高）"""
        close = _PRE_CLOSE * (1 + _STAR_MAX_CHANGE)
        self.assertFalse(self.s.check_buy_signal(
            self.CODE_STAR, _bar(round(close, 4)), pre_close=_PRE_CLOSE
        ))

    def test_star_change_pct_above_max_returns_false(self):
        """科创板涨幅12% > star_max_change_pct(8%) → False（防追高）"""
        close = _PRE_CLOSE * (1 + 0.12)
        self.assertFalse(self.s.check_buy_signal(
            self.CODE_STAR, _bar(round(close, 4)), pre_close=_PRE_CLOSE
        ))

    def test_cyb_anti_chasing_same_threshold(self):
        """创业板(300开头)使用与科创板相同的 star_max_change_pct → 防追高生效"""
        close = _PRE_CLOSE * (1 + _STAR_MAX_CHANGE)  # 恰好8%
        self.assertFalse(self.s.check_buy_signal(
            self.CODE_CYB, _bar(round(close, 4)), pre_close=_PRE_CLOSE
        ))

    # ── 全条件满足 ─────────────────────────────────────────────────────────

    def test_star_all_conditions_pass_returns_true(self):
        """科创板：涨幅5%（2%<5%<8%），收阳线，未涨停 → True"""
        close = _PRE_CLOSE * (1 + 0.05)   # +5%
        self.assertTrue(self.s.check_buy_signal(
            self.CODE_STAR, _bar(round(close, 4), open_price=9.5), pre_close=_PRE_CLOSE
        ))

    def test_cyb_all_conditions_pass_returns_true(self):
        """创业板：涨幅5%，收阳线，未涨停 → True"""
        close = _PRE_CLOSE * (1 + 0.05)
        self.assertTrue(self.s.check_buy_signal(
            self.CODE_CYB, _bar(round(close, 4), open_price=9.5), pre_close=_PRE_CLOSE
        ))


if __name__ == '__main__':
    unittest.main()
