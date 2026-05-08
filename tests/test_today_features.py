# -*- coding: utf-8 -*-
"""
test_today_features.py

测试今日新增/修改代码（完全离线，无需 xtquant / miniQMT）：

  TestPoolRebuiltToday      - _pool_rebuilt_today：调仓池幂等检查
  TestFiveMinUpdatedToday   - _5min_updated_today：5分钟哨兵文件幂等检查
  TestMarkFiveMinUpdated    - _mark_5min_updated_today：写哨兵文件
  TestWaitUntil19           - _wait_until_19：定时等待逻辑
  TestRebalanceSnapshot     - init_rebalance_pool.main() 快照保存逻辑
"""

import os
import sys
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock, mock_open

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run_live_v3


# ============================================================================
#  run_live_v3._pool_rebuilt_today
# ============================================================================

class TestPoolRebuiltToday(unittest.TestCase):
    """_pool_rebuilt_today — 读 state_v3_rebalance.json 判断今日是否已建池"""

    def _today(self):
        from datetime import date
        return date.today().strftime('%Y-%m-%d')

    def test_returns_true_when_date_matches_today(self):
        data = json.dumps({'rebalance_date': self._today()})
        with patch('builtins.open', mock_open(read_data=data)):
            self.assertTrue(run_live_v3._pool_rebuilt_today())

    def test_returns_false_when_date_is_old(self):
        data = json.dumps({'rebalance_date': '2020-01-01'})
        with patch('builtins.open', mock_open(read_data=data)):
            self.assertFalse(run_live_v3._pool_rebuilt_today())

    def test_returns_false_when_key_missing(self):
        data = json.dumps({'other_key': 'value'})
        with patch('builtins.open', mock_open(read_data=data)):
            self.assertFalse(run_live_v3._pool_rebuilt_today())

    def test_returns_false_on_file_not_found(self):
        with patch('builtins.open', side_effect=FileNotFoundError('no file')):
            self.assertFalse(run_live_v3._pool_rebuilt_today())

    def test_returns_false_on_invalid_json(self):
        with patch('builtins.open', mock_open(read_data='NOT VALID JSON ][')):
            self.assertFalse(run_live_v3._pool_rebuilt_today())

    def test_returns_false_on_empty_file(self):
        with patch('builtins.open', mock_open(read_data='')):
            self.assertFalse(run_live_v3._pool_rebuilt_today())


# ============================================================================
#  run_live_v3._5min_updated_today
# ============================================================================

class TestFiveMinUpdatedToday(unittest.TestCase):
    """_5min_updated_today — os.path.exists 检查哨兵文件"""

    def test_returns_true_when_sentinel_exists(self):
        with patch('run_live_v3.os.path.exists', return_value=True):
            self.assertTrue(run_live_v3._5min_updated_today())

    def test_returns_false_when_no_sentinel(self):
        with patch('run_live_v3.os.path.exists', return_value=False):
            self.assertFalse(run_live_v3._5min_updated_today())

    def test_sentinel_path_contains_today_date(self):
        """哨兵文件路径包含当日 YYYYMMDD"""
        from datetime import date
        today_str = date.today().strftime('%Y%m%d')
        captured = {}

        def fake_exists(path):
            captured['path'] = path
            return False

        with patch('run_live_v3.os.path.exists', side_effect=fake_exists):
            run_live_v3._5min_updated_today()

        self.assertIn(today_str, captured['path'],
                      f"哨兵路径 {captured.get('path')} 中应包含今日 {today_str}")

    def test_sentinel_path_contains_5min_incremental_done(self):
        """哨兵文件名包含 .5min_incremental_done_ 前缀"""
        captured = {}

        def fake_exists(path):
            captured['path'] = path
            return False

        with patch('run_live_v3.os.path.exists', side_effect=fake_exists):
            run_live_v3._5min_updated_today()

        self.assertIn('.5min_incremental_done_', captured.get('path', ''))


# ============================================================================
#  run_live_v3._mark_5min_updated_today
# ============================================================================

class TestMarkFiveMinUpdated(unittest.TestCase):
    """_mark_5min_updated_today — 写哨兵文件"""

    def test_creates_file_with_open(self):
        """open 被调用一次（写哨兵文件）"""
        m = mock_open()
        with patch('builtins.open', m):
            run_live_v3._mark_5min_updated_today()
        self.assertEqual(m.call_count, 1)

    def test_file_opened_for_writing(self):
        """文件以写模式（'w'）打开"""
        captured = {}

        def fake_open(path, mode='r', **kw):
            captured['path'] = path
            captured['mode'] = mode
            return mock_open()()

        with patch('builtins.open', side_effect=fake_open):
            run_live_v3._mark_5min_updated_today()

        self.assertEqual(captured.get('mode'), 'w',
                         '哨兵文件应以写模式打开')

    def test_silently_handles_permission_error(self):
        """写文件失败（PermissionError）不向外抛出异常"""
        with patch('builtins.open', side_effect=PermissionError('denied')):
            try:
                run_live_v3._mark_5min_updated_today()
            except Exception as e:
                self.fail(f'_mark_5min_updated_today 不应抛出异常，但抛出了: {e}')

    def test_silently_handles_oserror(self):
        """写文件失败（OSError）不向外抛出异常"""
        with patch('builtins.open', side_effect=OSError('disk full')):
            run_live_v3._mark_5min_updated_today()  # 不抛出


# ============================================================================
#  run_live_v3._wait_until_19
# ============================================================================

class TestWaitUntil19(unittest.TestCase):
    """_wait_until_19 — 当前时间 >= 19:00 时立即返回，否则等待"""

    def _make_mock_dt(self, hour, minute=0):
        now = MagicMock()
        now.hour = hour
        now.minute = minute
        now.strftime.return_value = f'{hour:02d}:{minute:02d}'
        return now

    def test_returns_immediately_at_19(self):
        """hour = 19 → 立即返回，不调用 sleep"""
        mock_dt = MagicMock()
        mock_dt.now.return_value = self._make_mock_dt(19)
        with patch('run_live_v3.datetime', mock_dt), \
             patch('run_live_v3.time') as mock_time:
            run_live_v3._wait_until_19()
        mock_time.sleep.assert_not_called()

    def test_returns_immediately_after_19(self):
        """hour = 21 → 立即返回"""
        mock_dt = MagicMock()
        mock_dt.now.return_value = self._make_mock_dt(21)
        with patch('run_live_v3.datetime', mock_dt), \
             patch('run_live_v3.time') as mock_time:
            run_live_v3._wait_until_19()
        mock_time.sleep.assert_not_called()

    def test_sleeps_once_then_returns_at_19(self):
        """18:30 → sleep 一次 → 19:00 → 返回"""
        mock_dt = MagicMock()
        mock_dt.now.side_effect = [
            self._make_mock_dt(18, 30),   # 第一次：还需等待
            self._make_mock_dt(19, 0),    # 第二次：到点返回
        ]
        with patch('run_live_v3.datetime', mock_dt), \
             patch('run_live_v3.time') as mock_time:
            run_live_v3._wait_until_19()
        mock_time.sleep.assert_called_once()

    def test_sleep_duration_at_most_300s(self):
        """任何情况下单次 sleep 不超过 300 秒（5 分钟）"""
        mock_dt = MagicMock()
        mock_dt.now.side_effect = [
            self._make_mock_dt(9, 0),     # 距离 19:00 还有 10 小时
            self._make_mock_dt(19, 0),    # 第二次到点
        ]
        with patch('run_live_v3.datetime', mock_dt), \
             patch('run_live_v3.time') as mock_time:
            run_live_v3._wait_until_19()
        sleep_secs = mock_time.sleep.call_args[0][0]
        self.assertLessEqual(sleep_secs, 300,
                             f'sleep 时长 {sleep_secs}s 超过 300s 上限')


# ============================================================================
#  init_rebalance_pool  快照保存逻辑
# ============================================================================

class TestRebalanceSnapshot(unittest.TestCase):
    """init_rebalance_pool.main() 快照保存逻辑（以等价代码块单元测试）"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.snap_dir = os.path.join(self.tmpdir, 'pool_snapshots')

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run_snapshot_logic(self, output, rebalance_date_ymd, snap_dir=None):
        """执行与 init_rebalance_pool.main() 快照块完全等价的逻辑"""
        _snap_dir = snap_dir or self.snap_dir
        os.makedirs(_snap_dir, exist_ok=True)
        _snap_path = os.path.join(_snap_dir, f'{rebalance_date_ymd}_pool.json')
        with open(_snap_path, 'w', encoding='utf-8') as _sf:
            json.dump(output, _sf, ensure_ascii=False, indent=2)
        return _snap_path

    def _make_output(self, date_ymd='20260506', pool=None):
        date_fmt = f'{date_ymd[:4]}-{date_ymd[4:6]}-{date_ymd[6:]}'
        return {
            'pool': pool or ['000001', '000002'],
            'rebalance_date': date_fmt,
            'strategy_key': 'ba',
            'strategy': 'B+A',
            'min_chg': 0.02,
            'max_chg': 0.07,
        }

    # ── 文件存在性 ────────────────────────────────────────────────────────────

    def test_snapshot_file_is_created(self):
        """调用快照逻辑后，文件应存在"""
        snap_path = self._run_snapshot_logic(self._make_output(), '20260506')
        self.assertTrue(os.path.exists(snap_path))

    def test_snapshot_filename_matches_rebalance_date(self):
        """文件名应为 {rebalance_date_ymd}_pool.json"""
        snap_path = self._run_snapshot_logic(self._make_output(), '20260507')
        self.assertTrue(snap_path.endswith('20260507_pool.json'))

    def test_snapshot_dir_auto_created_if_absent(self):
        """pool_snapshots 目录不存在时应自动创建"""
        new_dir = os.path.join(self.tmpdir, 'new_snap_dir_xyz')
        self.assertFalse(os.path.exists(new_dir))
        self._run_snapshot_logic(self._make_output(), '20260506', snap_dir=new_dir)
        self.assertTrue(os.path.exists(new_dir))

    # ── 内容一致性 ────────────────────────────────────────────────────────────

    def test_snapshot_content_equals_output_dict(self):
        """快照内容与 output dict 完全一致"""
        output = self._make_output()
        snap_path = self._run_snapshot_logic(output, '20260506')
        with open(snap_path, encoding='utf-8') as rf:
            loaded = json.load(rf)
        self.assertEqual(loaded, output)

    def test_snapshot_pool_list_order_preserved(self):
        """池中股票列表顺序不变"""
        codes = ['688001', '000001', '600999', '300001']
        output = self._make_output(pool=codes)
        snap_path = self._run_snapshot_logic(output, '20260506')
        with open(snap_path, encoding='utf-8') as rf:
            loaded = json.load(rf)
        self.assertEqual(loaded['pool'], codes)

    def test_snapshot_rebalance_date_matches(self):
        """快照中 rebalance_date 与 output 一致"""
        output = self._make_output('20260507')
        snap_path = self._run_snapshot_logic(output, '20260507')
        with open(snap_path, encoding='utf-8') as rf:
            loaded = json.load(rf)
        self.assertEqual(loaded['rebalance_date'], '2026-05-07')

    def test_snapshot_overwrites_same_day_old_file(self):
        """同一天再次建池应覆盖旧快照"""
        output_v1 = self._make_output(pool=['000001'])
        output_v2 = self._make_output(pool=['000002', '000003'])
        self._run_snapshot_logic(output_v1, '20260506')
        self._run_snapshot_logic(output_v2, '20260506')
        snap_path = os.path.join(self.snap_dir, '20260506_pool.json')
        with open(snap_path, encoding='utf-8') as rf:
            loaded = json.load(rf)
        self.assertEqual(loaded['pool'], ['000002', '000003'])

    def test_snapshot_valid_json(self):
        """快照文件应是合法 JSON"""
        snap_path = self._run_snapshot_logic(self._make_output(), '20260506')
        try:
            with open(snap_path, encoding='utf-8') as rf:
                json.load(rf)
        except json.JSONDecodeError as e:
            self.fail(f'快照文件不是合法 JSON: {e}')

    # ── 源码完整性验证（集成检查） ────────────────────────────────────────────

    def test_init_rebalance_pool_source_contains_snapshot_logic(self):
        """确认 init_rebalance_pool.main() 源码中包含快照保存关键字"""
        import inspect
        import init_rebalance_pool
        src = inspect.getsource(init_rebalance_pool.main)
        self.assertIn('pool_snapshots', src,
                      'main() 应包含 pool_snapshots 快照保存')
        self.assertIn('json.dump', src,
                      'main() 应包含 json.dump 写快照')
        self.assertIn('rebalance_date_ymd', src,
                      'main() 快照文件名应基于 rebalance_date_ymd')

    def test_init_rebalance_pool_snapshot_called_after_state_save(self):
        """快照保存行在 state_v3_rebalance.json 保存行之后（顺序校验）"""
        import inspect
        import init_rebalance_pool
        src = inspect.getsource(init_rebalance_pool.main)
        idx_state = src.find('state_v3_rebalance.json')
        idx_snap  = src.find('pool_snapshots')
        self.assertGreater(idx_snap, idx_state,
                           '快照保存应在 state_v3_rebalance.json 写入之后')


if __name__ == '__main__':
    unittest.main(verbosity=2)
