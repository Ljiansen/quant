# -*- coding: utf-8 -*-
"""
OfflineSimEngineV3 —— 非交易时段离线测试引擎

用于在夜间/周末验证策略代码逻辑，不影响任何实盘/模拟盘数据。

已隔离的文件：
  - state_v3_offline.json   （独立状态文件）
  - trades_v3_offline.json  （独立交易日志）
  - test_data/test_rebalance_pool.json（独立调仓池，默认值）

已解决的问题：
  1. buy_date 使用虚拟日期（非真实今日），保证 days_held 计算正确
  2. _get_tradable_pool 跳过 xtdata 日均成交额过滤（离线无 QMT）
  3. _wait_fill_result 立即全量成交，无需轮询等待
  4. _resubmit_sells_at_930 跳过 time.sleep 等待开盘
  5. 支持 _partial_fill_rates 模拟部分成交场景
"""

import os
import sys
import glob
from datetime import date, timedelta

sys.path.insert(0, 'd:/miniqmt_quant')

from engine.live_engine_v3 import (
    SimulationEngineV3,
    _format_symbol,
    _strip_suffix,
    _calculate_days_held,
    _now_str,
    ORDER_STATUS_FILLED,
)


class OfflineSimEngineV3(SimulationEngineV3):
    """非交易时段离线测试引擎

    每次 run() 完成后自动将结果（净值曲线 + 参数 + 交易明细 + 摘要）
    存档到 sim_results/ 目录，供仪表盘展示。

    两种用法：
    A) 历史回放（真实数据验证）：
        engine = OfflineSimEngineV3(
            data_dir='d:/miniqmt_quant/data_cache',
            rebalance_file='d:/miniqmt_quant/state_v3_rebalance.json',
            start_date='2025-01-01', end_date='2025-03-31',
        )
    B) 分支覆盖测试（合成数据）：
        engine = OfflineSimEngineV3(
            data_dir='d:/miniqmt_quant/test_data',
            rebalance_file='d:/miniqmt_quant/test_data/test_rebalance_pool.json',
            start_date='2025-03-03', end_date='2025-03-21',
        )
    """

    SIM_RESULTS_DIR = 'd:/miniqmt_quant/sim_results'
    STATE_FILE      = 'd:/miniqmt_quant/state_v3_offline.json'
    ENGINE_NAME     = 'OfflineSimEngineV3'
    TRADES_LOG_FILE = 'trades_v3_offline.json'

    def __init__(self, capital: float = 300000.0,
                 start_date: str = None,
                 end_date: str = None,
                 data_dir: str = 'd:/miniqmt_quant/data_cache',
                 rebalance_file: str = None):
        super().__init__(capital=capital)
        self.data_dir        = data_dir
        self.start_date      = start_date or '2025-01-01'
        self.end_date        = end_date or date.today().strftime('%Y-%m-%d')

        # 独立调仓池（默认指向测试专用，避免污染实盘池）
        self.REBALANCE_FILE  = (rebalance_file or
                                'd:/miniqmt_quant/test_data/test_rebalance_pool.json')

        self._price_snapshot    = {}   # {symbol: tick_dict}
        self._historical_data   = {}   # {code: DataFrame}
        self._virtual_today_str = None # 当前模拟日期字符串

        # 买入部分成交模拟：{symbol_with_suffix: fill_rate(0~1)}
        # 例：{'600997.SH': 0.25} 表示只成交 25%
        self._partial_fill_rates: dict = {}

        # 卖出部分成交序列：{symbol_with_suffix: [rate_r1, rate_r2, ...]}
        # 例：{'600998.SH': [0.3, 1.0]} 第1轮成交30%，第2轮100%
        # 例：{'600999.SH': [0.0, 0.5]} 第1轮0%，第2轮50%→进r3 pending
        self._sell_fill_seq: dict = {}
        # 跟踪每个 symbol 的卖出 _wait_fill_result 调用次数
        self._sell_order_count: dict = {}

        # 需要模拟竞价失败的代码集合（纯数字代码，不含后缀）
        # 用于覆盖 _check_auction_sell_results 未成交→重挂分支
        self._auction_fail_codes: set  = set()

        # 模拟结果存档
        self._equity_curve: list = []   # [{date, total_value, cash, positions_count}, ...]
        self._sim_params:   dict = {}   # 本次模拟使用的参数

    # ------------------------------------------------------------------
    # 历史数据加载
    # ------------------------------------------------------------------
    def _load_historical_data(self):
        """从 data_dir/*.csv 加载历史日线数据"""
        import pandas as pd

        print(f"[{self.ENGINE_NAME}] 加载历史数据: {self.data_dir}")
        loaded = 0
        for filepath in glob.glob(os.path.join(self.data_dir, '*.csv')):
            code = os.path.basename(filepath).split('_')[0]
            try:
                df = pd.read_csv(filepath, parse_dates=['date'])
                df['date'] = pd.to_datetime(df['date'])
                df = df.sort_values('date').reset_index(drop=True)
                if df.empty:
                    continue
                if code in self._historical_data:
                    combined = pd.concat([self._historical_data[code], df])
                    combined = combined.drop_duplicates('date').sort_values('date').reset_index(drop=True)
                    self._historical_data[code] = combined
                else:
                    self._historical_data[code] = df
                loaded += 1
            except Exception:
                pass
        print(f"[{self.ENGINE_NAME}] 历史数据加载完成: "
              f"{len(self._historical_data)} 只股票 / {loaded} 个文件")

    def _build_price_snapshot(self, day_str: str):
        """为指定日期构建价格快照（模拟 get_full_tick 返回格式）"""
        import pandas as pd
        snapshot    = {}
        target_date = pd.to_datetime(day_str)

        for code, df in self._historical_data.items():
            today_rows = df[df['date'] == target_date]
            if today_rows.empty:
                continue
            today = today_rows.iloc[0]

            past_rows  = df[df['date'] < target_date]
            prev_close = (float(past_rows.iloc[-1]['close'])
                          if not past_rows.empty else float(today['open']))

            close = float(today['close'])
            open_ = float(today['open'])
            high  = float(today['high'])
            low   = float(today['low'])
            vol   = float(today['volume'])
            amt   = float(today['amount'])
            bid   = round(close * 0.999, 3)
            ask   = round(close * 1.001, 3)

            symbol = f"{code}.SH" if code.startswith('6') else f"{code}.SZ"
            snapshot[symbol] = {
                'lastPrice': close,
                'bidPrice':  [bid,  bid,  bid,  bid,  bid],
                'askPrice':  [ask,  ask,  ask,  ask,  ask],
                'lastClose': prev_close,
                'open':      open_,
                'high':      high,
                'low':       low,
                'volume':    vol,
                'amount':    amt,
            }
        self._price_snapshot = snapshot

    # ------------------------------------------------------------------
    # 覆盖关键接口
    # ------------------------------------------------------------------
    def _get_full_tick(self, symbols: list) -> dict:
        """用本地历史快照替代 xtdata.get_full_tick"""
        if not symbols:
            return {}
        return {s: self._price_snapshot[s]
                for s in symbols if s in self._price_snapshot}

    def _get_tradable_pool(self, held_codes: set) -> list:
        """离线模式：跳过 xtdata 日均成交额过滤，直接返回候选"""
        if not self.rebalance_pool:
            return []
        candidates = [c for c in self.rebalance_pool
                      if _strip_suffix(c) not in held_codes]
        # 只保留当日有行情数据的股票
        result = []
        for code in candidates:
            sym = _format_symbol(code)
            if sym in self._price_snapshot:
                result.append(code)
        return result

    def _wait_fill_result(self, order_id: int, timeout: int = 180) -> dict:
        """离线模式：立即返回成交结果

        买入订单（order_type=23）：使用 _partial_fill_rates 控制成交率
        卖出订单（order_type=24）：使用 _sell_fill_seq 序列逐次控制成交率
          - 第1次调用取 seq[0]，第2次取 seq[1]，超出则全量成交
        """
        orders = self._query_orders()
        for o in orders:
            if o.get('order_id') == order_id:
                vol        = o.get('volume', 0)
                price      = o.get('price', 0)
                symbol     = o.get('symbol', '')
                order_type = o.get('order_type', 23)  # 23=buy, 24=sell

                if order_type == 24:  # 卖出订单
                    if symbol in self._sell_fill_seq:
                        seq = self._sell_fill_seq[symbol]
                        cnt = self._sell_order_count.get(symbol, 0)
                        rate = seq[cnt] if cnt < len(seq) else 1.0
                        self._sell_order_count[symbol] = cnt + 1
                        print(f"[{self.ENGINE_NAME}] [卖出序列] {symbol} "
                              f"第{cnt+1}轮 rate={rate:.2f} vol={vol}")
                    else:
                        rate = 1.0
                else:  # 买入订单
                    rate = self._partial_fill_rates.get(symbol, 1.0)

                filled = max(0, int(vol * rate // 100) * 100)  # 整手
                status = 'filled' if filled >= vol else ('partial' if filled > 0 else 'timeout')
                return {
                    'status':     status,
                    'filled_qty': filled,
                    'fill_price': price,
                }
        return {'status': 'filled', 'filled_qty': 0, 'fill_price': 0}

    def _check_auction_sell_results(self):
        """离线模式检查集合竞价成交情况

        支持 _auction_fail_codes 模拟竞价失败：
          - codes in _auction_fail_codes → 视为未成交 → 走 _resubmit_sells_at_930
          - 其余 → 正常成交（SimulatedExecutor 始终返回 status=56）
        """
        if not self._auction_sell_orders:
            return

        print(f"[{self.ENGINE_NAME}] [离线] 检查集合竞价成交，共 {len(self._auction_sell_orders)} 笔")

        orders = self._query_orders()
        order_status = {o['order_id']: o for o in orders}
        unfilled_pos = []

        for order_id, pos in list(self._auction_sell_orders.items()):
            code = _strip_suffix(pos.get('code', pos.get('symbol', '')))

            # ── 模拟竞价失败 ──────────────────────────────────────────────
            if code in self._auction_fail_codes:
                unfilled_pos.append(pos)
                print(f"[{self.ENGINE_NAME}] [模拟竞价失败] {code}，待 9:30 重挂")
                # 不删除 _auction_sell_orders 条目（与基类行为一致）
                continue

            # ── 正常成交路径 ──────────────────────────────────────────────
            o = order_status.get(order_id)
            if o and o.get('status') == ORDER_STATUS_FILLED:
                quantity   = o.get('traded_volume', pos.get('quantity', 0))
                sell_price = o.get('price', pos.get('buy_price', 0))
                net_income = self._calc_sell_income(sell_price, quantity)
                self.cash += net_income
                days_held  = _calculate_days_held(pos)
                sell_type  = pos.get('sell_type', 'pending')
                commission = max(sell_price * quantity * self.commission_rate, self.min_commission)
                stamp_tax  = sell_price * quantity * self.stamp_tax_rate
                self._log_trade('sell', code, sell_price, quantity, sell_type,
                                fee=commission + stamp_tax, days_held=days_held)
                self._remove_position(code)
                self._remove_pending_sell(code)
                print(f"[{self.ENGINE_NAME}] 竞价卖出成交: {code} "
                      f"数量={quantity} 价格={sell_price:.3f} 收入={net_income:.2f}")
                del self._auction_sell_orders[order_id]
            else:
                unfilled_pos.append(pos)
                print(f"[{self.ENGINE_NAME}] 竞价卖出未成交: {code}，待 9:30 重挂")

        # 9:30 重挂
        if unfilled_pos:
            self._resubmit_sells_at_930(unfilled_pos)

    def _resubmit_sells_at_930(self, positions_to_sell: list):
        """离线模式：跳过等待开盘，直接按快照价重挂并确认成交"""
        print(f"[{self.ENGINE_NAME}] [离线] 重挂卖单 {len(positions_to_sell)} 笔")
        codes = [_format_symbol(_strip_suffix(
            p.get('code', p.get('symbol', '')))) for p in positions_to_sell]
        ticks = self._get_full_tick(codes)

        for pos in positions_to_sell:
            code     = _strip_suffix(pos.get('code', pos.get('symbol', '')))
            symbol   = _format_symbol(code)
            quantity = pos.get('quantity', pos.get('volume', 0))
            tick     = ticks.get(symbol, {})

            bid_price = (tick.get('bidPrice', [0])[0]
                         or tick.get('lastPrice', 0)
                         or tick.get('lastClose', 0)
                         or pos.get('buy_price', 0))
            if bid_price <= 0:
                continue

            order_id = self._place_sell_order(
                code=code, price=bid_price, volume=quantity,
                remark=f"V3_offline_sell_{code}"
            )
            if order_id and order_id != -1:
                if self._wait_fill(order_id, timeout=300):
                    net_income = self._calc_sell_income(bid_price, quantity)
                    self.cash += net_income
                    days_held  = _calculate_days_held(pos)
                    sell_type  = pos.get('sell_type', 'pending')
                    commission = max(bid_price * quantity * self.commission_rate,
                                     self.min_commission)
                    stamp_tax  = bid_price * quantity * self.stamp_tax_rate
                    self._log_trade('sell', code, bid_price, quantity, sell_type,
                                    fee=commission + stamp_tax, days_held=days_held)
                    self._remove_position(code)
                    self._remove_pending_sell(code)
                    print(f"[{self.ENGINE_NAME}] [离线竞价成交] {code} "
                          f"价格={bid_price:.3f} 收入={net_income:.2f}")

    # ------------------------------------------------------------------
    # 模拟结果存档
    # ------------------------------------------------------------------
    def _load_sim_params(self) -> dict:
        """加载当前策略参数（用于写入结果存档）"""
        import json as _json
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        params_file = os.path.join(base_dir, 'params_v3.json')
        try:
            if os.path.exists(params_file):
                with open(params_file, 'r', encoding='utf-8') as _f:
                    return _json.load(_f)
        except Exception:
            pass
        # 回退到 config.py
        try:
            import config as _cfg
            return {
                'main_board': {
                    'min_change_pct':    getattr(_cfg, 'V3_MIN_CHANGE_PCT', 0.01),
                    'max_change_pct':    getattr(_cfg, 'V3_MAX_CHANGE_PCT', 0.07),
                    'hard_stop_loss':    getattr(_cfg, 'V3_HARD_STOP_LOSS', 0.05),
                    'soft_stop_loss':    getattr(_cfg, 'V3_SOFT_STOP_LOSS', 0.03),
                    'trailing_activate': getattr(_cfg, 'V3_TRAILING_ACTIVATE', 0.03),
                    'trailing_stop':     getattr(_cfg, 'V3_TRAILING_STOP', 0.02),
                    'time_stop_days':    getattr(_cfg, 'V3_TIME_STOP_DAYS', 5),
                    'limit_up':          0.098,
                },
                'star_board': {
                    'min_change_pct':    getattr(_cfg, 'V3_STAR_MIN_CHANGE_PCT', 0.02),
                    'max_change_pct':    getattr(_cfg, 'V3_STAR_MAX_CHANGE_PCT', 0.08),
                    'hard_stop_loss':    getattr(_cfg, 'V3_STAR_HARD_STOP_LOSS', 0.05),
                    'soft_stop_loss':    getattr(_cfg, 'V3_STAR_SOFT_STOP_LOSS', 0.03),
                    'trailing_activate': getattr(_cfg, 'V3_STAR_TRAILING_ACTIVATE', 0.08),
                    'trailing_stop':     getattr(_cfg, 'V3_STAR_TRAILING_STOP', 0.05),
                    'time_stop_days':    getattr(_cfg, 'V3_STAR_TIME_STOP_DAYS', 5),
                    'limit_up':          getattr(_cfg, 'V3_STAR_LIMIT_UP', 0.198),
                },
                'general': {
                    'top_n':           getattr(_cfg, 'V3_TOP_N', 50),
                    'max_positions':   getattr(_cfg, 'V3_MAX_POSITIONS', 3),
                    'prev_bar_up':     int(getattr(_cfg, 'V3_PREV_BAR_UP', False)),
                    'initial_capital': getattr(_cfg, 'V3_INITIAL_CAPITAL', 300000),
                },
            }
        except Exception:
            return {}

    def _save_sim_result(self):
        """将本次模拟结果保存到 sim_results/ 目录"""
        import json as _json
        from datetime import datetime as _dt

        os.makedirs(self.SIM_RESULTS_DIR, exist_ok=True)
        run_time = _dt.now().strftime('%Y-%m-%d %H:%M:%S')
        run_id   = _dt.now().strftime('%Y%m%d_%H%M%S')

        # 读取交易日志
        trades = []
        base_dir    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        trades_file = os.path.join(base_dir, self.TRADES_LOG_FILE)
        try:
            if os.path.exists(trades_file):
                with open(trades_file, 'r', encoding='utf-8') as _f:
                    trades = _json.load(_f)
        except Exception:
            pass

        # 汇总统计
        initial   = self.capital_limit
        final_val = (self._equity_curve[-1]['total_value']
                     if self._equity_curve else initial)
        profit     = final_val - initial
        profit_pct = profit / initial * 100 if initial else 0.0

        sell_trades = [t for t in trades if t.get('action') == 'sell']
        buy_trades  = [t for t in trades if t.get('action') == 'buy']
        win_trades  = [
            t for t in sell_trades
            if (t.get('price', 0) or 0) > (t.get('buy_price', 0) or 0)
        ]
        win_rate = (len(win_trades) / len(sell_trades) * 100
                    if sell_trades else 0.0)

        # 最大回撤
        max_dd = 0.0
        peak   = initial
        for pt in self._equity_curve:
            v = pt['total_value']
            if v > peak:
                peak = v
            if peak > 0:
                dd = (peak - v) / peak * 100
                if dd > max_dd:
                    max_dd = dd

        result = {
            'run_id':          run_id,
            'start_date':      self.start_date,
            'end_date':        self.end_date,
            'run_time':        run_time,
            'initial_capital': initial,
            'params':          self._sim_params,
            'equity_curve':    self._equity_curve,
            'summary': {
                'final_value':  round(final_val, 2),
                'profit':       round(profit, 2),
                'profit_pct':   round(profit_pct, 2),
                'total_trades': len(trades),
                'buy_trades':   len(buy_trades),
                'sell_trades':  len(sell_trades),
                'win_trades':   len(win_trades),
                'loss_trades':  len(sell_trades) - len(win_trades),
                'win_rate':     round(win_rate, 1),
                'max_drawdown': round(-max_dd, 2),
                'trading_days': len(self._equity_curve),
            },
            'trades': trades,
        }

        fname = f"{run_id}_{self.start_date}_{self.end_date}.json"
        fpath = os.path.join(self.SIM_RESULTS_DIR, fname)
        with open(fpath, 'w', encoding='utf-8') as _f:
            _json.dump(result, _f, ensure_ascii=False, indent=2)
        print(f"[{self.ENGINE_NAME}] 模拟结果已存档: {fpath}")
        return fpath

    # ------------------------------------------------------------------
    # 主运行入口：逐交易日步进
    # ------------------------------------------------------------------
    def run(self):
        """离线回放主入口，逐交易日执行完整策略流程"""
        print(f"[{self.ENGINE_NAME}] ====== 离线模拟引擎启动 "
              f"(资金={self.capital_limit:.0f}) ======")
        print(f"[{self.ENGINE_NAME}] 回放区间: {self.start_date} ~ {self.end_date}")
        print(f"[{self.ENGINE_NAME}] 调仓池文件: {self.REBALANCE_FILE}")
        print(f"[{self.ENGINE_NAME}] 数据目录: {self.data_dir}")
        if self._partial_fill_rates:
            print(f"[{self.ENGINE_NAME}] 部分成交配置: {self._partial_fill_rates}")

        if not self._connect_executor():
            return

        self._recover()
        self._sim_params   = self._load_sim_params()
        self._equity_curve = []
        self._load_rebalance_pool()
        self._load_historical_data()

        if not self._historical_data:
            print(f"[{self.ENGINE_NAME}] 无历史数据，退出")
            return

        # 收集区间内所有交易日
        all_dates = set()
        for df in self._historical_data.values():
            all_dates.update(df['date'].dt.strftime('%Y-%m-%d').tolist())
        trading_days = sorted(
            d for d in all_dates
            if self.start_date <= d <= self.end_date
        )

        if not trading_days:
            print(f"[{self.ENGINE_NAME}] 区间内无交易日数据，退出")
            return

        print(f"[{self.ENGINE_NAME}] 共 {len(trading_days)} 个交易日\n")

        # 当日真实日期（用于修正 buy_date）
        real_today = date.today().strftime('%Y-%m-%d')

        for day_str in trading_days:
            self._virtual_today_str = day_str
            print(f"\n{'='*60}")
            print(f"[{self.ENGINE_NAME}] ── 模拟交易日: {day_str} ──")

            # 重置当日标志
            self._auction_sells_executed = False
            self._auction_check_done     = False
            self._close_check_done       = False
            self._failed_buys_today      = {}
            self._last_buy_scan_time     = None

            # 构建当日价格快照
            self._build_price_snapshot(day_str)
            if not self._price_snapshot:
                print(f"  [跳过] 无行情数据")
                continue

            # ── days_held 递增（买入当天不计） ──────────────────────
            if self._last_increment_date != day_str:
                for pos in self.positions:
                    if pos.get('buy_date') != day_str:
                        pos['days_held'] = pos.get('days_held', 0) + 1
                self._last_increment_date = day_str

            # ── [9:15] 集合竞价执行 pending 卖出 ────────────────────
            if self.pending_sells:
                print(f"  [9:15] 执行集合竞价卖出 {len(self.pending_sells)} 笔")
            self._execute_pending_sells_auction()
            self._auction_sells_executed = True

            # ── [9:25] 检查竞价成交 ──────────────────────────────────
            self._check_auction_sell_results()
            self._auction_check_done = True

            # ── [9:30] 持仓监控（硬止损/移动止盈） ──────────────────
            print(f"  [9:30] 持仓监控 & 买入扫描")
            self._monitor_positions()

            # ── [9:30] 买入扫描 ──────────────────────────────────────
            if self._count_effective_positions() < self.max_positions:
                self._scan_and_buy()

            # ── 修正 buy_date：将真实今日改为虚拟交易日 ─────────────
            # _scan_and_buy 内部 buy_date = date.today()（真实日期）
            # 必须改为虚拟日期，保证 days_held 按模拟天数递增
            for pos in self.positions:
                if pos.get('buy_date') == real_today:
                    pos['buy_date'] = day_str

            # ── [14:55] 收盘前止损检查 ───────────────────────────────
            print(f"  [14:55] 收盘前止损检查")
            self._check_close_signals()
            self._close_check_done = True

            # ── 保存当日状态 ─────────────────────────────────────────
            self._save_state()

            # ── 日终统计 ─────────────────────────────────────────────
            mkt_val = 0.0
            for p in self.positions:
                sym   = _format_symbol(_strip_suffix(p.get('code', p.get('symbol', ''))))
                price = self._price_snapshot.get(sym, {}).get('lastPrice', p.get('buy_price', 0))
                mkt_val += p.get('quantity', 0) * price
            total_asset = self.cash + mkt_val
            pending_cnt = len(self.pending_sells)

            print(f"  [15:00] 日终 | 持仓={len(self.positions)} | "
                  f"pending={pending_cnt} | "
                  f"现金={self.cash:.0f} | 市值={mkt_val:.0f} | "
                  f"总资产={total_asset:.0f}")

            # ── 净值曲线采样 ────────────────────────────────────────
            self._equity_curve.append({
                'date':            day_str,
                'total_value':     round(total_asset, 2),
                'cash':            round(self.cash, 2),
                'positions_count': len(self.positions),
            })

        print(f"\n[{self.ENGINE_NAME}] ====== 离线模拟结束 ======")
        self.get_status_report()
        if self._equity_curve:
            self._save_sim_result()
