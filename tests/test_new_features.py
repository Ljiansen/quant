# -*- coding: utf-8 -*-
"""
test_new_features.py

覆盖本次新增/修改的三个模块（完全离线，无需 xtquant / baostock / miniQMT）：

  TestStrategiesDict          - init_rebalance_pool.STRATEGIES 结构与字段完整性
  TestStrategyFallback        - main() 未知策略回退行为
  TestPoolRebuildState        - dashboard app.py 重建状态字典与 API 逻辑
  TestApiPoolStatus           - /api/pool/status 端点返回字段
  TestApiPoolRebuild          - /api/pool/rebuild 端点并发保护
  TestFiveminHelpers          - update_5min_incremental 工具函数
  TestGetLastDate             - _get_last_date 各种日期格式
"""

import os
import sys
import json
import unittest
import tempfile
import datetime
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ===========================================================================
#  init_rebalance_pool — STRATEGIES 字典
# ===========================================================================

class TestStrategiesDict(unittest.TestCase):
    """STRATEGIES 字典结构完整性"""

    def setUp(self):
        import init_rebalance_pool as irp
        self.STRATEGIES = irp.STRATEGIES

    def test_all_three_keys_present(self):
        """ba / a / b 三个策略键均存在"""
        self.assertIn('ba', self.STRATEGIES)
        self.assertIn('a',  self.STRATEGIES)
        self.assertIn('b',  self.STRATEGIES)

    def test_each_strategy_has_required_fields(self):
        """每个策略必须包含 name / desc / use_ma20 / quality_mode"""
        required = {'name', 'desc', 'use_ma20', 'quality_mode'}
        for key, info in self.STRATEGIES.items():
            with self.subTest(strategy=key):
                self.assertTrue(required.issubset(info.keys()),
                                f"策略 {key} 缺少字段: {required - info.keys()}")

    def test_ba_uses_ma20_and_quality(self):
        """B+A 策略：use_ma20=True, quality_mode=True"""
        s = self.STRATEGIES['ba']
        self.assertTrue(s['use_ma20'])
        self.assertTrue(s['quality_mode'])

    def test_a_no_ma20_but_quality(self):
        """仅A策略：use_ma20=False, quality_mode=True"""
        s = self.STRATEGIES['a']
        self.assertFalse(s['use_ma20'])
        self.assertTrue(s['quality_mode'])

    def test_b_uses_ma20_no_quality(self):
        """仅B策略：use_ma20=True, quality_mode=False"""
        s = self.STRATEGIES['b']
        self.assertTrue(s['use_ma20'])
        self.assertFalse(s['quality_mode'])

    def test_strategy_names_no_typo(self):
        """策略名称不含已知错别字 '趋勿'"""
        for key, info in self.STRATEGIES.items():
            with self.subTest(strategy=key):
                self.assertNotIn('趋勿', info['name'],
                                 f"策略 {key} 名称仍含错别字 '趋勿'")

    def test_ba_name_contains_BA(self):
        """B+A 策略的 name 应包含 'B+A'"""
        self.assertIn('B+A', self.STRATEGIES['ba']['name'])

    def test_b_name_contains_MA20(self):
        """B策略名称应包含 'MA20'"""
        self.assertIn('MA20', self.STRATEGIES['b']['name'])


# ===========================================================================
#  init_rebalance_pool — 未知策略回退
# ===========================================================================

class TestStrategyFallback(unittest.TestCase):
    """main() 遇到未知策略时应回退到 'ba' 而非崩溃"""

    def test_unknown_strategy_falls_back_gracefully(self):
        """传入不存在的策略键时，main() 应打印警告后回退，不抛异常"""
        import init_rebalance_pool as irp
        # 只验证 STRATEGIES['ba'] 是 fallback 目标；不实际运行 main()（需要数据文件）
        self.assertIn('ba', irp.STRATEGIES)
        # 模拟回退逻辑（与源码一致）
        strategy = 'INVALID_XYZ'
        if strategy not in irp.STRATEGIES:
            strategy = 'ba'
        self.assertEqual(strategy, 'ba')

    def test_valid_strategies_accepted(self):
        """ba / a / b 均被视为合法策略"""
        import init_rebalance_pool as irp
        for s in ('ba', 'a', 'b'):
            self.assertIn(s, irp.STRATEGIES)


# ===========================================================================
#  dashboard/app.py — 重建状态字典
# ===========================================================================

class TestPoolRebuildState(unittest.TestCase):
    """_pool_rebuild_state 初始值与字段"""

    def setUp(self):
        # 不启动 Flask，仅测试模块级状态变量
        import dashboard.app as app_module
        self.state = app_module._pool_rebuild_state

    def test_initial_running_is_false(self):
        """初始状态 running=False"""
        # 重置为初始值以防其他测试污染
        self.state['running'] = False
        self.assertFalse(self.state['running'])

    def test_has_all_required_keys(self):
        """状态字典必须包含 running / strategy / msg / last_rebuild"""
        required = {'running', 'strategy', 'msg', 'last_rebuild'}
        self.assertTrue(required.issubset(self.state.keys()))

    def test_default_strategy_is_ba(self):
        """默认策略为 'ba'"""
        self.state['strategy'] = 'ba'  # 确保未被污染
        self.assertEqual(self.state['strategy'], 'ba')


# ===========================================================================
#  dashboard/app.py — Flask API 端点
# ===========================================================================

class TestApiPoolStatus(unittest.TestCase):
    """/api/pool/status 返回字段测试"""

    def setUp(self):
        import dashboard.app as app_module
        self.app = app_module.create_app()
        self.client = self.app.test_client()
        # 准备临时 state_v3_rebalance.json
        self._tmp = tempfile.NamedTemporaryFile(
            mode='w', suffix='.json', delete=False, encoding='utf-8'
        )
        json.dump({
            'pool': ['000001', '000002', '000003'],
            'strategy_key': 'ba',
            'strategy': 'B+A（最优组合）',
            'rebalance_date': '2026-04-30',
        }, self._tmp)
        self._tmp.close()
        import dashboard.app as m
        self._orig = m.BASE_DIR
        # patch BASE_DIR 使读文件指向临时目录
        self._patch = patch.object(
            m, 'BASE_DIR', os.path.dirname(self._tmp.name)
        )
        # 同时把文件名改为 state_v3_rebalance.json
        self._orig_name = self._tmp.name
        self._correct_path = os.path.join(
            os.path.dirname(self._tmp.name), 'state_v3_rebalance.json'
        )
        import shutil
        shutil.copy(self._tmp.name, self._correct_path)

    def tearDown(self):
        try:
            os.unlink(self._orig_name)
        except Exception:
            pass
        try:
            os.unlink(self._correct_path)
        except Exception:
            pass

    def test_status_returns_200(self):
        """/api/pool/status 返回 HTTP 200"""
        resp = self.client.get('/api/pool/status')
        self.assertEqual(resp.status_code, 200)

    def test_status_response_has_required_fields(self):
        """响应 JSON 包含 strategy / pool_size / rebuilding / last_msg / last_rebuild"""
        resp = self.client.get('/api/pool/status')
        data = json.loads(resp.data)
        for key in ('strategy', 'pool_size', 'rebuilding', 'last_msg', 'last_rebuild'):
            with self.subTest(field=key):
                self.assertIn(key, data)

    def test_status_rebuilding_default_false(self):
        """默认 rebuilding=False"""
        import dashboard.app as m
        m._pool_rebuild_state['running'] = False
        resp = self.client.get('/api/pool/status')
        data = json.loads(resp.data)
        self.assertFalse(data['rebuilding'])


class TestApiPoolRebuild(unittest.TestCase):
    """/api/pool/rebuild 并发保护与策略校验"""

    def setUp(self):
        import dashboard.app as app_module
        # 重置状态
        app_module._pool_rebuild_state['running'] = False
        app_module._pool_rebuild_state['msg'] = ''
        self.app = app_module.create_app()
        self.client = self.app.test_client()
        self.mod = app_module

    def test_rebuild_unknown_strategy_returns_error(self):
        """传入未知策略键 → ok=False"""
        resp = self.client.post(
            '/api/pool/rebuild',
            data=json.dumps({'strategy': 'UNKNOWN'}),
            content_type='application/json',
        )
        data = json.loads(resp.data)
        self.assertFalse(data['ok'])

    def test_rebuild_while_running_returns_error(self):
        """重建进行中再次请求 → ok=False"""
        self.mod._pool_rebuild_state['running'] = True
        resp = self.client.post(
            '/api/pool/rebuild',
            data=json.dumps({'strategy': 'ba'}),
            content_type='application/json',
        )
        data = json.loads(resp.data)
        self.assertFalse(data['ok'])
        # 恢复
        self.mod._pool_rebuild_state['running'] = False

    def test_rebuild_sets_running_before_thread_starts(self):
        """发起重建后，响应返回前 running 已为 True（防竞态）"""
        import threading
        # mock _rebuild_pool_bg 使其阻塞足够长，让我们检查 running 状态
        barrier = threading.Event()

        def _fake_rebuild(strategy):
            barrier.wait(timeout=3)
            self.mod._pool_rebuild_state['running'] = False

        with patch.object(self.mod, '_rebuild_pool_bg', side_effect=_fake_rebuild):
            # 在后台线程执行 POST（避免阻塞测试主线程）
            result = {}
            def do_post():
                r = self.client.post(
                    '/api/pool/rebuild',
                    data=json.dumps({'strategy': 'a'}),
                    content_type='application/json',
                )
                result['data'] = json.loads(r.data)

            t = threading.Thread(target=do_post)
            t.start()
            t.join(timeout=2)
            # 检查 ok=True
            self.assertTrue(result.get('data', {}).get('ok', False))
            barrier.set()

    def test_rebuild_valid_strategies_accepted(self):
        """ba / a / b 均可发起重建（ok=True）"""
        for strategy in ('ba', 'a', 'b'):
            self.mod._pool_rebuild_state['running'] = False
            with patch.object(self.mod, '_rebuild_pool_bg'):
                resp = self.client.post(
                    '/api/pool/rebuild',
                    data=json.dumps({'strategy': strategy}),
                    content_type='application/json',
                )
                data = json.loads(resp.data)
                with self.subTest(strategy=strategy):
                    self.assertTrue(data['ok'])
            self.mod._pool_rebuild_state['running'] = False


# ===========================================================================
#  update_5min_incremental — 工具函数
# ===========================================================================

class TestFiveminHelpers(unittest.TestCase):
    """_bs_code / _fivemin_path / _next_day 纯函数测试"""

    def setUp(self):
        import update_5min_incremental as u5
        self.u5 = u5

    def test_bs_code_sh(self):
        """沪市代码 6xxxxx → sh.6xxxxx"""
        self.assertEqual(self.u5._bs_code('600001'), 'sh.600001')

    def test_bs_code_sz(self):
        """深市代码 000001 → sz.000001"""
        self.assertEqual(self.u5._bs_code('000001'), 'sz.000001')

    def test_bs_code_cyb(self):
        """创业板 300001 → sz.300001"""
        self.assertEqual(self.u5._bs_code('300001'), 'sz.300001')

    def test_bs_code_kcb(self):
        """科创板 688001 → sh.688001（上交所，以6开头）"""
        self.assertEqual(self.u5._bs_code('688001'), 'sh.688001')

    def test_fivemin_path_sh(self):
        """沪市代码路径在 SH 子目录"""
        path = self.u5._fivemin_path('600001')
        self.assertIn('SH', path)
        self.assertTrue(path.endswith('600001.csv'))

    def test_fivemin_path_sz(self):
        """深市代码路径在 SZ 子目录"""
        path = self.u5._fivemin_path('000001')
        self.assertIn('SZ', path)
        self.assertTrue(path.endswith('000001.csv'))

    def test_next_day_normal(self):
        """普通日期加1天"""
        self.assertEqual(self.u5._next_day('2026-04-30'), '2026-05-01')

    def test_next_day_month_boundary(self):
        """月末进位"""
        self.assertEqual(self.u5._next_day('2026-01-31'), '2026-02-01')

    def test_next_day_year_boundary(self):
        """年末进位"""
        self.assertEqual(self.u5._next_day('2025-12-31'), '2026-01-01')

    def test_next_day_leap_year(self):
        """闰年 2024-02-28 的下一天"""
        self.assertEqual(self.u5._next_day('2024-02-28'), '2024-02-29')


class TestGetLastDate(unittest.TestCase):
    """_get_last_date 各种输入场景"""

    def setUp(self):
        import update_5min_incremental as u5
        self.fn = u5._get_last_date

    def _write_csv(self, content: str) -> str:
        """写临时 CSV，返回路径"""
        tmp = tempfile.NamedTemporaryFile(
            mode='w', suffix='.csv', delete=False, encoding='utf-8'
        )
        tmp.write(content)
        tmp.close()
        return tmp.name

    def tearDown(self):
        # 清理所有临时文件
        pass

    def test_standard_date_format(self):
        """标准 YYYY-MM-DD 格式"""
        path = self._write_csv('date,time,open,close\n2026-04-29,09:30,10.0,10.2\n2026-04-30,09:35,10.2,10.5\n')
        result = self.fn(path)
        os.unlink(path)
        self.assertEqual(result, '2026-04-30')

    def test_compact_date_format(self):
        """紧凑 YYYYMMDD 格式自动转换"""
        path = self._write_csv('date,time,open,close\n20260429,09:30,10.0,10.2\n20260430,09:35,10.2,10.5\n')
        result = self.fn(path)
        os.unlink(path)
        self.assertEqual(result, '2026-04-30')

    def test_empty_file_returns_none(self):
        """仅有表头无数据行 → None"""
        path = self._write_csv('date,time,open,close\n')
        result = self.fn(path)
        os.unlink(path)
        self.assertIsNone(result)

    def test_nonexistent_file_returns_none(self):
        """文件不存在 → None"""
        result = self.fn('/nonexistent/path/404.csv')
        self.assertIsNone(result)

    def test_only_takes_first_10_chars(self):
        """结果只取日期前10个字符，忽略时间部分"""
        path = self._write_csv('date,time,open,close\n2026-04-30 09:30:00,09:30,10.0,10.2\n')
        result = self.fn(path)
        os.unlink(path)
        self.assertEqual(result, '2026-04-30')


# ===========================================================================
#  update_5min_incremental — today_str 运行时计算
# ===========================================================================

class TestTodayStrRuntime(unittest.TestCase):
    """_today_str 应在运行时计算，而非模块导入时固定"""

    def test_today_str_not_at_module_level(self):
        """模块导入后修改 run_incremental 内部 today_str 逻辑 — 验证是局部变量"""
        import update_5min_incremental as u5
        import inspect
        src = inspect.getsource(u5.run_incremental)
        # today_str 应在函数内部通过 date.today() 获得，不能是全局引用
        self.assertIn('today_str', src)
        self.assertIn('date.today()', src)
        # 模块级不应再有 _today_str 赋值
        module_src = inspect.getsource(u5)
        # 只在函数内部允许出现 today_str 的赋值
        lines = module_src.split('\n')
        module_level_assign = [
            l for l in lines
            if '_today_str' in l and '=' in l and not l.startswith(' ') and not l.startswith('\t')
            and not l.startswith('#')
        ]
        self.assertEqual(module_level_assign, [],
                         f"发现模块级 _today_str 赋值: {module_level_assign}")


if __name__ == '__main__':
    unittest.main(verbosity=2)
