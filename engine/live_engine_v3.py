# -*- coding: utf-8 -*-
"""
V3策略实盘引擎（实时行情驱动版）

架构说明：
- 程序启动时恢复历史状态（读 state_v3.json + 查真实持仓）
- 9:15 执行 pending 卖出（集合竞价限价单，价格=昨收×0.99）
- 9:25 检查集合竞价成交，未成交则 9:30 按买一价重挂
- 9:30~15:00 主循环（每分钟一轮）
    - 批量 get_full_tick 获取持仓实时快照
    - 检查硬止损/止盈触发 → 限价卖出
    - 持仓 < 3 → 扫描候选池 → 限价买入 → 等待5分钟成交
- 14:55 检查阴跌/时间止损 → 记录 pending 卖出
- 15:00 收盘持久化 state_v3.json

支持两种模式：
- LiveEngineV3: 实盘模式，连接 miniQMT，资金上限 3 万
- SimulationEngineV3: 模拟模式，使用 SimulatedExecutor，30 万虚拟资金
"""

import json
import os
import sys
import time
from datetime import datetime, date, timedelta

sys.path.insert(0, 'd:/miniqmt_quant')
import config


# ---------------------------------------------------------------------------
# 全局工具函数
# ---------------------------------------------------------------------------

def _now_str() -> str:
    """返回当前时间字符串"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _format_symbol(code: str) -> str:
    """将纯数字股票代码格式化为带交易所后缀的代码
    例如: '600000' -> '600000.SH', '000001' -> '000001.SZ'
    """
    code_str = str(code).strip().split('.')[0]  # 去掉已有的后缀
    if code_str.startswith('6') or code_str.startswith('5'):
        return f"{code_str}.SH"
    return f"{code_str}.SZ"


def _strip_suffix(symbol: str) -> str:
    """去除交易所后缀，返回纯数字代码"""
    return str(symbol).strip().split('.')[0]


def _time_in_range(start_h: int, start_m: int, end_h: int, end_m: int) -> bool:
    """判断当前时间是否在指定范围内（含左端，不含右端）"""
    now = datetime.now()
    t = now.hour * 60 + now.minute
    s = start_h * 60 + start_m
    e = end_h * 60 + end_m
    return s <= t < e


def _market_is_open() -> bool:
    """判断当前时间是否在交易时段（9:00~15:01）"""
    now = datetime.now()
    t = now.hour * 60 + now.minute
    # 早盘开始前（9:00）或收盘后（15:01）退出
    return 9 * 60 <= t <= 15 * 60 + 1


def _calculate_days_held(pos: dict) -> int:
    """计算持仓天数（交易日）

    优先使用持仓记录中的 days_held 字段（交易日计数）。
    如果没有（旧数据兼容），用自然日差兜底。
    """
    days_held = pos.get('days_held')
    if days_held is not None:
        return max(0, int(days_held))

    # 向后兼容：旧 state 没有 days_held，用自然日差
    try:
        buy_date_str = pos.get('buy_date', '')
        buy_date = datetime.strptime(buy_date_str, '%Y-%m-%d').date()
        delta = (date.today() - buy_date).days
        return max(0, delta)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# 订单状态常量（xtquant）
# ---------------------------------------------------------------------------
ORDER_STATUS_FILLED = 56      # 已成交
ORDER_STATUS_CANCELLED = 54   # 已撤销
ORDER_STATUS_REJECTED = 57    # 废单
ORDER_STATUS_PARTIAL = 53     # 部分成交


# ===========================================================================
# LiveEngineV3 —— 实时行情驱动的实盘引擎
# ===========================================================================
class LiveEngineV3:
    """V3策略实时行情驱动实盘引擎

    核心流程（每日）：
    - 启动恢复：读 state_v3.json + 查真实持仓 → 恢复条件单监控
    - 9:15-9:25  执行 pending 卖出（集合竞价限价单，昨收×0.99）
    - 9:25-9:30  检查竞价成交；未成交 → 9:30 按买一价重挂
    - 9:30-15:00 主循环（每分钟一轮）
        - get_full_tick 批量获取持仓快照
        - 检查硬止损/止盈 → 限价卖出
        - 持仓 < 3 → 扫描候选池 → 限价买入
    - 14:55      检查阴跌/时间止损 → pending
    - 15:00      收盘持久化
    """

    # 状态文件路径（子类可覆盖）
    STATE_FILE = 'd:/miniqmt_quant/state_v3.json'
    # 调仓池文件路径
    REBALANCE_FILE = 'd:/miniqmt_quant/state_v3_rebalance.json'
    # 引擎名称（日志用）
    ENGINE_NAME = 'LiveEngineV3'
    TRADES_LOG_FILE = 'trades_v3.json'

    def __init__(self, mode: str = 'live', capital_limit: float = 30000.0):
        """初始化引擎

        参数：
            mode: 'live'（实盘）或 'simulation'（模拟）
            capital_limit: 资金上限
                - 实盘模式: 3W（账号有50W，但只用3W）
                - 模拟模式: 30W 虚拟资金
        """
        self.mode = mode
        self.capital_limit = capital_limit

        # 策略参数（从 config 读取）
        self.max_positions = config.V3_MAX_POSITIONS  # 最大持仓3只
        self.hard_stop_loss = config.V3_HARD_STOP_LOSS
        self.soft_stop_loss = config.V3_SOFT_STOP_LOSS
        self.take_profit = config.V3_TAKE_PROFIT
        self.time_stop_days = config.V3_TIME_STOP_DAYS
        self.star_hard_stop_loss = config.V3_STAR_HARD_STOP_LOSS
        self.star_soft_stop_loss = config.V3_STAR_SOFT_STOP_LOSS
        self.star_take_profit = config.V3_STAR_TAKE_PROFIT
        self.star_time_stop_days = config.V3_STAR_TIME_STOP_DAYS
        # 移动止盈参数
        self.trailing_activate = config.V3_TRAILING_ACTIVATE
        self.trailing_stop = config.V3_TRAILING_STOP
        self.star_trailing_activate = config.V3_STAR_TRAILING_ACTIVATE
        self.star_trailing_stop = config.V3_STAR_TRAILING_STOP
        self.commission_rate = config.V3_COMMISSION_RATE
        self.min_commission = config.V3_MIN_COMMISSION
        self.stamp_tax_rate = config.V3_STAMP_TAX_RATE

        # 运行时状态
        self.positions = []         # 当前持仓列表，每个元素为 dict
        self.pending_sells = []     # 待卖出列表（次日开盘执行）
        self.cash = capital_limit   # 可用现金
        self.rebalance_pool = []    # 调仓池（排名靠前的候选股）

        # 待处理的竞价卖出订单: {order_id: pos_dict}
        self._auction_sell_orders = {}
        # 待处理的买入订单: {order_id: {code, price, volume, placed_at}}
        self._pending_buy_orders = {}

        # 主循环控制标志
        self._auction_sells_executed = False   # 竞价卖出是否已挂单
        self._auction_check_done = False       # 竞价成交检查是否完成
        self._close_check_done = False         # 收盘信号检查是否完成

        # 执行器（子类设置）
        self.executor = None

        # 交易日计数：上次递增 days_held 的日期
        self._last_increment_date = None

        # 日均成交额过滤缓存
        self._daily_filter_cache = []
        self._daily_filter_date = None

        # 今日买入失败记录：{code: date_str}，当日不重复挂单
        self._failed_buys_today = {}

        # 买入扫描间隔控制：每10分钟扫描一次，避免频繁重复挂单
        self._last_buy_scan_time = None

        # 初始化执行器（实盘模式）
        if mode == 'live':
            self._init_live_executor()

    # ------------------------------------------------------------------
    # 执行器初始化（实盘模式）
    # ------------------------------------------------------------------
    def _init_live_executor(self):
        """初始化实盘执行器（连接 miniQMT）"""
        try:
            from trade.executor import TradeExecutor
            self.executor = TradeExecutor()
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 实盘执行器初始化完成（未连接）")
        except Exception as e:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 执行器初始化失败: {e}")
            raise

    def _connect_executor(self):
        """连接执行器"""
        if self.executor is None:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 执行器未初始化")
            return False
        try:
            if hasattr(self.executor, 'is_connected') and self.executor.is_connected:
                return True
            return self.executor.connect()
        except Exception as e:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 连接执行器失败: {e}")
            return False

    # ------------------------------------------------------------------
    # 主运行入口
    # ------------------------------------------------------------------
    def run(self):
        """主运行入口

        完整交易日流程：
        1. 连接执行器
        2. 恢复启动（读 state + 查真实持仓）
        3. 加载调仓池
        4. 进入主循环（每分钟）
        5. 收盘后保存状态
        """
        print(f"[{_now_str()}] [{self.ENGINE_NAME}] ====== 启动 V3 实盘引擎（模式={self.mode}，资金上限={self.capital_limit:.0f}）======")

        # 1. 连接执行器
        if not self._connect_executor():
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 执行器连接失败，退出")
            return

        # 2. 恢复历史状态
        self._recover()

        # 3. 加载调仓池
        self._load_rebalance_pool()

        # 重置当日标志位
        self._auction_sells_executed = False
        self._auction_check_done = False
        self._close_check_done = False

        print(f"[{_now_str()}] [{self.ENGINE_NAME}] 进入主循环，等待交易时段...")

        # 4. 主循环
        _heartbeat_counter = 0   # 心跳计数器，每5分钟刷新一次 last_update
        try:
            while _market_is_open():
                now = datetime.now()
                h, m = now.hour, now.minute

                # 跨日检查：每天第一次进入主循环时递增 days_held
                today_str = date.today().strftime('%Y-%m-%d')
                if self._last_increment_date != today_str:
                    for pos in self.positions:
                        if pos.get('buy_date') != today_str:
                            pos['days_held'] = pos.get('days_held', 0) + 1
                    self._last_increment_date = today_str
                    self._save_state()

                try:
                    # 9:15~9:25 集合竞价阶段：挂 pending 卖出
                    if h == 9 and 15 <= m < 25:
                        if not self._auction_sells_executed:
                            self._execute_pending_sells_auction()
                            self._auction_sells_executed = True

                    # 9:25~9:30 检查竞价成交
                    elif h == 9 and 25 <= m < 30:
                        if not self._auction_check_done:
                            self._check_auction_sell_results()
                            self._auction_check_done = True

                    # 9:30~15:00 盘中主循环
                    elif (h == 9 and m >= 30) or (10 <= h <= 14) or (h == 15 and m == 0):
                        # 检查持仓止损/止盈
                        self._monitor_positions()

                        # 持仓不足3只，扫描买入
                        # 持仓不足，扫描买入（每10分钟扫描一次）
                        if self._count_effective_positions() < self.max_positions:
                            _scan_interval_secs = 10 * 60
                            _should_scan = (
                                self._last_buy_scan_time is None or
                                (datetime.now() - self._last_buy_scan_time).total_seconds() >= _scan_interval_secs
                            )
                            if _should_scan:
                                self._last_buy_scan_time = datetime.now()
                                self._scan_and_buy()

                        # 14:55 收盘前检查阴跌/时间止损
                        if h == 14 and m >= 55 and not self._close_check_done:
                            self._check_close_signals()
                            self._close_check_done = True

                except Exception as e:
                    import traceback
                    print(f"[{_now_str()}] [{self.ENGINE_NAME}] 主循环异常: {e}")
                    traceback.print_exc()

                # 心跳：每5分钟刷新一次 last_update，保证仪表盘时间显示准确
                _heartbeat_counter += 1
                if _heartbeat_counter >= 5:
                    self._save_state()
                    _heartbeat_counter = 0

                # 每分钟轮询一次
                time.sleep(60)

        except KeyboardInterrupt:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 收到中断信号，正在退出...")

        # 5. 收盘保存状态
        self._save_state()
        print(f"[{_now_str()}] [{self.ENGINE_NAME}] ====== V3 引擎已停止 ======")

    # ------------------------------------------------------------------
    # 启动恢复
    # ------------------------------------------------------------------
    def _recover(self):
        """启动恢复：读 state 文件 + 查真实持仓 → 恢复监控

        流程：
        1. 读取 state_v3.json 获取本策略记录的持仓/资金
        2. 若为实盘模式，查询 miniQMT 真实持仓做校验
        3. 恢复 pending_sells 列表
        """
        print(f"[{_now_str()}] [{self.ENGINE_NAME}] === 启动恢复检查 ===")
        state = self._load_state()

        # 恢复持仓（列表格式）
        positions_data = state.get('positions', {})
        if isinstance(positions_data, dict):
            # 兼容旧格式（dict）
            self.positions = list(positions_data.values())
        else:
            self.positions = positions_data

        # 恢复资金
        self.cash = state.get('cash', self.capital_limit)

        # 恢复 pending_sells
        self.pending_sells = state.get('pending_sells', [])

        # 恢复 days_held 兼容性：旧 state 没有 days_held 时，用自然日差初始化
        for pos in self.positions:
            if 'days_held' not in pos:
                pos['days_held'] = _calculate_days_held(pos)

        # 恢复交易日计数日期
        self._last_increment_date = state.get('_last_increment_date')

        # 恢复日均成交额过滤缓存
        self._daily_filter_date = state.get('_daily_filter_date')
        self._daily_filter_cache = state.get('_daily_filter_cache', [])

        print(f"[{_now_str()}] [{self.ENGINE_NAME}] 恢复持仓 {len(self.positions)} 只，"
              f"现金 {self.cash:.2f}，pending_sells {len(self.pending_sells)} 条")

        # 实盘模式：与 miniQMT 真实持仓核对
        if self.mode == 'live' and self.executor is not None:
            self._reconcile_with_broker()

    def _reconcile_with_broker(self):
        """与券商真实持仓核对（实盘模式专用）

        逻辑：
        - 查询 miniQMT 真实持仓
        - 对比本策略记录的持仓
        - 打印差异（不自动修正，需人工干预）
        """
        try:
            real_positions = self.executor.query_positions()
            real_codes = {_strip_suffix(p['symbol']) for p in real_positions if p.get('volume', 0) > 0}
            strategy_codes = {_strip_suffix(p.get('code', p.get('symbol', ''))) for p in self.positions}

            extra_in_broker = real_codes - strategy_codes
            missing_in_broker = strategy_codes - real_codes

            if extra_in_broker:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] [警告] 券商持仓中有策略未记录的股票: {extra_in_broker}")
            if missing_in_broker:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] [警告] 策略持仓中有券商未持有的股票: {missing_in_broker}")
            if not extra_in_broker and not missing_in_broker:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] 持仓核对一致，共 {len(real_codes)} 只")

        except Exception as e:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 持仓核对异常: {e}")

    # ------------------------------------------------------------------
    # 加载调仓池
    # ------------------------------------------------------------------
    def _load_rebalance_pool(self):
        """加载调仓池（从 state_v3_rebalance.json）"""
        if os.path.exists(self.REBALANCE_FILE):
            try:
                with open(self.REBALANCE_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.rebalance_pool = data.get('pool', [])
                rebalance_date = data.get('rebalance_date', '未知')
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] 调仓池加载成功: {len(self.rebalance_pool)} 只，"
                      f"调仓日={rebalance_date}")
            except Exception as e:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] 加载调仓池失败: {e}，使用空池")
                self.rebalance_pool = []
        else:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 调仓池文件不存在: {self.REBALANCE_FILE}，使用空池")
            self.rebalance_pool = []

    # ------------------------------------------------------------------
    # 集合竞价卖出（9:15 执行）
    # ------------------------------------------------------------------
    def _execute_pending_sells_auction(self):
        """集合竞价阶段执行 pending 卖出

        对每个 pending_sells 中的股票：
        - 价格 = 昨收 × 0.99（略低于昨收，集合竞价中尽量成交）
        - 挂限价卖单
        - 记录 order_id 到 _auction_sell_orders
        """
        if not self.pending_sells:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 无 pending 卖出任务")
            return

        print(f"[{_now_str()}] [{self.ENGINE_NAME}] 执行集合竞价卖出，共 {len(self.pending_sells)} 只")

        # 获取 pending 股票的实时快照，取昨收价
        codes_to_sell = [_format_symbol(p.get('code', p.get('symbol', ''))) for p in self.pending_sells]
        ticks = self._get_full_tick(codes_to_sell)

        executed = []
        for pos in self.pending_sells:
            code = _strip_suffix(pos.get('code', pos.get('symbol', '')))
            symbol = _format_symbol(code)
            quantity = pos.get('quantity', pos.get('volume', 0))

            if quantity <= 0:
                continue

            # 获取昨收价
            tick = ticks.get(symbol, {})
            pre_close = tick.get('lastClose', 0) or tick.get('preClose', 0) or tick.get('lastPrice', 0)
            if pre_close <= 0:
                # 用买入价兜底
                pre_close = pos.get('buy_price', 0)

            if pre_close <= 0:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] {code} 无法获取昨收价，跳过竞价卖出")
                continue

            # 竞价限价：昨收 × 0.99
            sell_price = round(pre_close * 0.99, 2)

            order_id = self._place_sell_order(
                code=code,
                price=sell_price,
                volume=quantity,
                remark=f"V3_auction_sell_{code}_{pos.get('sell_type', 'pending')}"
            )

            if order_id and order_id != -1:
                self._auction_sell_orders[order_id] = pos
                executed.append(pos)
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] 竞价卖出委托: {code} "
                      f"价格={sell_price:.3f} 数量={quantity} order_id={order_id}")
            else:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] 竞价卖出下单失败: {code}")

    # ------------------------------------------------------------------
    # 检查集合竞价成交（9:25 检查）
    # ------------------------------------------------------------------
    def _check_auction_sell_results(self):
        """检查集合竞价成交情况

        对 _auction_sell_orders 中的每笔委托：
        - 已成交(56) → 更新持仓和资金，从 pending_sells 移除
        - 未成交 → 标记为需要 9:30 重挂
        """
        if not self._auction_sell_orders:
            return

        print(f"[{_now_str()}] [{self.ENGINE_NAME}] 检查集合竞价成交，共 {len(self._auction_sell_orders)} 笔")

        orders = self._query_orders()
        order_status = {o['order_id']: o for o in orders}

        unfilled_pos = []

        for order_id, pos in list(self._auction_sell_orders.items()):
            code = _strip_suffix(pos.get('code', pos.get('symbol', '')))
            o = order_status.get(order_id)

            if o and o.get('status') == ORDER_STATUS_FILLED:
                # 已成交
                quantity = o.get('traded_volume', pos.get('quantity', 0))
                sell_price = o.get('price', pos.get('buy_price', 0))
                net_income = self._calc_sell_income(sell_price, quantity)
                self.cash += net_income
                days_held = _calculate_days_held(pos)
                sell_type = pos.get('sell_type', 'pending')
                commission = max(sell_price * quantity * self.commission_rate, self.min_commission)
                stamp_tax = sell_price * quantity * self.stamp_tax_rate
                self._log_trade('sell', code, sell_price, quantity, sell_type, fee=commission+stamp_tax, days_held=days_held)
                self._remove_position(code)
                self._remove_pending_sell(code)
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] 竞价卖出成交: {code} "
                      f"数量={quantity} 价格={sell_price:.3f} 收入={net_income:.2f}")
                del self._auction_sell_orders[order_id]
            else:
                # 未成交，标记为 9:30 重挂
                unfilled_pos.append(pos)
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] 竞价卖出未成交: {code}，待 9:30 重挂")

        # 9:30 重挂（按买一价）
        if unfilled_pos:
            self._resubmit_sells_at_930(unfilled_pos)

    def _resubmit_sells_at_930(self, positions_to_sell: list):
        """9:30 开盘后按买一价重挂卖单"""
        print(f"[{_now_str()}] [{self.ENGINE_NAME}] 9:30 重挂卖单，共 {len(positions_to_sell)} 只")
        # 等到 9:30（此方法在 9:25~9:30 之间被调用，等待开盘）
        now = datetime.now()
        wait_until = now.replace(hour=9, minute=30, second=5, microsecond=0)
        wait_secs = max(0, (wait_until - now).total_seconds())
        if wait_secs > 0:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 等待 {wait_secs:.0f} 秒至 9:30 开盘...")
            time.sleep(wait_secs)

        # 获取买一价
        codes = [_format_symbol(_strip_suffix(p.get('code', p.get('symbol', '')))) for p in positions_to_sell]
        ticks = self._get_full_tick(codes)

        for pos in positions_to_sell:
            code = _strip_suffix(pos.get('code', pos.get('symbol', '')))
            symbol = _format_symbol(code)
            quantity = pos.get('quantity', pos.get('volume', 0))

            tick = ticks.get(symbol, {})
            # 买一价
            bid_prices = tick.get('bidPrice', [])
            bid_price = bid_prices[0] if bid_prices else tick.get('lastPrice', 0)

            if bid_price <= 0:
                # 兜底使用昨收
                bid_price = tick.get('lastClose', tick.get('preClose', pos.get('buy_price', 0)))

            if bid_price <= 0:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] {code} 无法获取买一价，跳过重挂")
                continue

            order_id = self._place_sell_order(
                code=code,
                price=bid_price,
                volume=quantity,
                remark=f"V3_resubmit_sell_{code}"
            )

            if order_id and order_id != -1:
                # 等待最多5分钟确认成交
                filled = self._wait_fill(order_id, timeout=300)
                if filled:
                    net_income = self._calc_sell_income(bid_price, quantity)
                    self.cash += net_income
                    days_held = _calculate_days_held(pos)
                    sell_type = pos.get('sell_type', 'pending')
                    commission = max(bid_price * quantity * self.commission_rate, self.min_commission)
                    stamp_tax = bid_price * quantity * self.stamp_tax_rate
                    self._log_trade('sell', code, bid_price, quantity, sell_type, fee=commission+stamp_tax, days_held=days_held)
                    self._remove_position(code)
                    self._remove_pending_sell(code)
                    print(f"[{_now_str()}] [{self.ENGINE_NAME}] 重挂卖出成交: {code} "
                          f"价格={bid_price:.3f} 收入={net_income:.2f}")
                else:
                    self._cancel_order(order_id)
                    print(f"[{_now_str()}] [{self.ENGINE_NAME}] 重挂卖出超时未成交，已撤单: {code}")

    # ------------------------------------------------------------------
    # 盘中持仓监控（9:30~15:00）
    # ------------------------------------------------------------------
    def _monitor_positions(self):
        """检查持仓的硬止损/止盈条件

        流程：
        1. get_full_tick 批量获取持仓实时快照
        2. 检查每只持仓的止损/止盈条件
        3. 触发 → 限价卖出 → 成交后移除持仓
        """
        if not self.positions:
            return

        codes = [_format_symbol(_strip_suffix(p.get('code', p.get('symbol', '')))) for p in self.positions]
        ticks = self._get_full_tick(codes)

        # 已在 pending_sells 中的代码，正在等待次日竞价卖出，跳过防止重复下单
        pending_codes = {_strip_suffix(s.get('code', s.get('symbol', ''))) for s in self.pending_sells}

        for pos in list(self.positions):
            code = _strip_suffix(pos.get('code', pos.get('symbol', '')))
            # 已加入 pending_sells，跳过
            if code in pending_codes:
                continue
            symbol = _format_symbol(code)
            tick = ticks.get(symbol)
            if not tick:
                continue

            last_price = tick.get('lastPrice', 0)
            if last_price <= 0:
                continue

            buy_price = pos.get('buy_price', 0)
            if buy_price <= 0:
                continue

            # 判断是否科创板/创业板
            is_star = self._is_star(code)
            hard_sl = self.star_hard_stop_loss if is_star else self.hard_stop_loss
            trail_act = self.star_trailing_activate if is_star else self.trailing_activate
            trail_pct = self.star_trailing_stop    if is_star else self.trailing_stop

            hard_stop_price = buy_price * (1 - hard_sl)

            # 每 tick 更新持仓历史最高价
            highest_price = pos.get('highest_price', buy_price)
            if last_price > highest_price:
                highest_price = last_price
                pos['highest_price'] = highest_price

            # T+1限制：买入当天（days_held==0）不执行卖出
            days_held = _calculate_days_held(pos)
            if days_held == 0:
                continue

            should_sell = False
            sell_price = last_price
            sell_type = None

            # 1. 硬止损（最高优先级）
            if last_price <= hard_stop_price:
                should_sell = True
                sell_price = hard_stop_price
                sell_type = 'hard_stop'
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] 触发硬止损: {code} "
                      f"现价={last_price:.3f} 止损价={hard_stop_price:.3f}")

            # 2. 移动止盈：激活后从最高价回撤触发
            elif highest_price >= buy_price * (1 + trail_act):
                trail_trigger = highest_price * (1 - trail_pct)
                if last_price <= trail_trigger:
                    should_sell = True
                    sell_price = trail_trigger
                    sell_type = 'trailing_stop'
                    print(f"[{_now_str()}] [{self.ENGINE_NAME}] 触发移动止盈: {code} "
                          f"最高价={highest_price:.3f} 回撤价={trail_trigger:.3f} 现价={last_price:.3f}")

            if should_sell:
                quantity = pos.get('quantity', 0)
                if quantity <= 0:
                    continue

                self._execute_sell_with_fallback(
                    code=code, sell_price=sell_price, quantity=quantity,
                    sell_type=sell_type, pos=pos,
                    buy_price=buy_price, days_held=days_held
                )

    # ------------------------------------------------------------------
    # 扫描买入（持仓 < 3 时）
    # ------------------------------------------------------------------
    def _scan_and_buy(self):
        """扫描候选池，按排名顺序买入

        流程：
        1. 获取当日可交易候选池（对调仓池做二次过滤）
        2. get_full_tick 批量获取快照
        3. 按排名顺序检查买入条件
        4. 条件满足 → 限价单买入（卖一价）
        5. 等待5分钟确认成交
        6. 成交 → 记录持仓
        7. 未成交 → 撤单 → 下一只
        """
        print(f"[{_now_str()}] [{self.ENGINE_NAME}] [扫描] 开始扫描买入，"
              f"调仓池={len(self.rebalance_pool)}只，持仓={len(self.positions)}/{self.max_positions}")

        if not self.rebalance_pool:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] [扫描] 调仓池为空，跳过")
            return

        # 获取可用资金
        available_cash = self._get_available_cash()
        slot_budget    = self.capital_limit / self.max_positions   # 单槕预算
        min_viable     = slot_budget * 0.5                         # 可建立有效仓位的最低资金
        if available_cash < min_viable:
            # 当前现金不足以建立一个有效仓位，停止扫描，等待现有仓位卖出后资金回笼
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] [扫描] 可用资金{available_cash:.0f}不足单槕最低资金{min_viable:.0f}元，跳过")
            return

        # 排除已持仓的股票
        held_codes = {_strip_suffix(p.get('code', p.get('symbol', ''))) for p in self.positions}

        # 当日可交易池（二次过滤）
        tradable_pool = self._get_tradable_pool(held_codes)
        if not tradable_pool:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] [扫描] 可交易候选池为空（已过滤），跳过")
            return

        print(f"[{_now_str()}] [{self.ENGINE_NAME}] [扫描] 候选池{len(tradable_pool)}只，可用资金={available_cash:.0f}")

        # 批量获取行情快照
        symbols = [_format_symbol(c) for c in tradable_pool]
        ticks = self._get_full_tick(symbols)

        today_str = date.today().strftime('%Y-%m-%d')

        # 今日已失败的买入代码（下单超时/未成交），当天不再重试
        failed_today = {c for c, d in self._failed_buys_today.items() if d == today_str}
        if failed_today:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] [扫描] 今日已失败代码跳过: {failed_today}")

        for code in tradable_pool:
            # 持仓已满（按有效仓位计算），停止
            if self._count_effective_positions() >= self.max_positions:
                break

            # 今日已尝试失败，跳过
            if code in failed_today:
                continue

            symbol = _format_symbol(code)
            tick = ticks.get(symbol)
            if not tick:
                continue

            last_price = tick.get('lastPrice', 0)
            pre_close = tick.get('lastClose', 0) or tick.get('preClose', 0)
            open_price = tick.get('open', 0)
            volume = tick.get('volume', 0)
            high_price = tick.get('high', 0)

            # 跳过停牌/无效数据
            if last_price <= 0 or pre_close <= 0 or volume == 0:
                continue

            # 排除ST股票
            try:
                from xtquant import xtdata
                detail = xtdata.get_instrument_detail(symbol)
                if detail:
                    name = detail.get('InstrumentName', '')
                    if 'ST' in name:
                        continue
            except Exception:
                pass

            # 检查买入信号
            bar = {
                'open': open_price,
                'high': high_price,
                'low': tick.get('low', 0),
                'close': last_price,
                'volume': volume,
                'amount': tick.get('amount', 0),
            }
            change_pct = (last_price - pre_close) / pre_close if pre_close > 0 else 0
            is_positive = last_price > open_price if open_price > 0 else False
            if not self._check_buy_signal(code, bar, pre_close):
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] [扫描] {code} 不满足买入条件: "
                      f"涨幅={change_pct:.2%}, 收阳={is_positive}, last={last_price:.2f}")
                continue

            # 计算买入数量（卖一价）
            ask_prices = tick.get('askPrice', [])
            ask_price = ask_prices[0] if ask_prices else last_price
            if ask_price <= 0:
                ask_price = last_price

            # 可用资金重新计算（前面可能已经买入了）
            available_cash = self._get_available_cash()
            volume_to_buy = self._calculate_buy_volume(available_cash, ask_price)

            if volume_to_buy <= 0:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] {code} 资金不足或股数为0，跳过")
                continue

            # 检查资金
            total_cost = ask_price * volume_to_buy * (1 + self.commission_rate)
            if total_cost > available_cash:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] {code} 资金不足: 需{total_cost:.0f} 可用{available_cash:.0f}")
                continue

            # 下单
            order_id = self._place_buy_order(
                code=code,
                price=ask_price,
                volume=volume_to_buy,
                remark=f"V3_buy_{code}"
            )

            if not order_id or order_id == -1:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] {code} 买入下单失败，尝试下一只")
                continue

            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 买入委托: {code} "
                  f"价格={ask_price:.3f} 数量={volume_to_buy} order_id={order_id}")

            # 等待并获取实际成交明细（支持部分成交）
            buy_result = self._wait_fill_result(order_id, timeout=300)
            actual_qty = buy_result['filled_qty']

            if actual_qty > 0:
                # 按实际成交量记录持仓和扣除资金
                buy_cost   = ask_price * actual_qty
                commission = max(buy_cost * self.commission_rate, self.min_commission)
                total_paid = buy_cost + commission
                self.cash -= total_paid

                pos = {
                    'code':         code,
                    'symbol':       symbol,
                    'buy_price':    ask_price,
                    'buy_date':     today_str,
                    'quantity':     actual_qty,
                    'days_held':    0,
                    'sell_type':    None,
                    'highest_price': ask_price,
                }
                # 部分成交时记录计划数量，供仓表盘标记显示
                if actual_qty < volume_to_buy:
                    pos['intended_qty'] = volume_to_buy
                self.positions.append(pos)

                if buy_result['status'] == 'filled':
                    print(f"[{_now_str()}] [{self.ENGINE_NAME}] 买入全量成交: {code} "
                          f"价格={ask_price:.3f} 数量={actual_qty} "
                          f"总成本={total_paid:.2f} 佣金={commission:.2f} "
                          f"剩余现金={self.cash:.2f}")
                else:
                    print(f"[{_now_str()}] [{self.ENGINE_NAME}] 买入部分成交: {code} "
                          f"实际={actual_qty}/计划={volume_to_buy} 价格={ask_price:.3f} "
                          f"总成本={total_paid:.2f} 剩余现金={self.cash:.2f}")

                self._log_trade('buy', code, ask_price, actual_qty, 'buy_signal', fee=commission)
                self._save_state()

            else:
                # 完全未成交，_wait_fill_result 超时时已自动撤单
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] {code} 买入超时未成交，已撤单，当日不再重试")
                self._failed_buys_today[code] = today_str
                failed_today.add(code)
                continue

    # ------------------------------------------------------------------
    # 收盘前检查（14:55）
    # ------------------------------------------------------------------
    def _check_close_signals(self):
        """收盘前检查阴跌/时间止损，生成 pending_sells

        规则：
        1. 阴跌止损：收盘价 < open（收阴线）且跌幅 > soft_stop_loss → pending
        2. 时间止损：持仓 >= time_stop_days 天且收盘价 <= 买入价 → pending
        3. 止盈信号（当日 high >= 止盈价）→ pending

        pending_sells 中的股票将在次日 9:15 集合竞价中挂单卖出
        """
        if not self.positions:
            return

        print(f"[{_now_str()}] [{self.ENGINE_NAME}] 执行收盘前信号检查...")

        codes = [_format_symbol(_strip_suffix(p.get('code', p.get('symbol', '')))) for p in self.positions]
        ticks = self._get_full_tick(codes)
        today_str = date.today().strftime('%Y-%m-%d')

        for pos in list(self.positions):
            code = _strip_suffix(pos.get('code', pos.get('symbol', '')))
            symbol = _format_symbol(code)
            tick = ticks.get(symbol)
            if not tick:
                continue

            last_price = tick.get('lastPrice', 0)
            open_price = tick.get('open', 0)
            high_price = tick.get('high', 0)
            pre_close = tick.get('lastClose', 0) or tick.get('preClose', 0)

            buy_price = pos.get('buy_price', 0)
            if buy_price <= 0:
                continue

            days_held = _calculate_days_held(pos)

            # T+1 限制
            if days_held == 0:
                continue

            is_star = self._is_star(code)
            soft_sl = self.star_soft_stop_loss if is_star else self.soft_stop_loss
            tp = self.star_take_profit if is_star else self.take_profit  # 已废弃
            time_stop = self.star_time_stop_days if is_star else self.time_stop_days
            trail_act = self.star_trailing_activate if is_star else self.trailing_activate
            trail_pct = self.star_trailing_stop    if is_star else self.trailing_stop

            sell_type = None

            # 1. 阴跌止损
            soft_stop_price = buy_price * (1 - soft_sl)
            if last_price < soft_stop_price and last_price < open_price:
                sell_type = 'soft_stop'

            # 2. 移动止盈：导盘最高价激活后，收盘价跳止进回撤线则 pending
            if sell_type is None:
                highest_price = pos.get('highest_price', buy_price)
                if highest_price >= buy_price * (1 + trail_act):
                    trail_trigger = highest_price * (1 - trail_pct)
                    if last_price <= trail_trigger:
                        sell_type = 'trailing_stop'

            # 3. 时间止损
            if sell_type is None and days_held >= time_stop and last_price <= buy_price:
                sell_type = 'time_stop'

            if sell_type:
                # 检查是否已在 pending_sells 中
                already_pending = any(
                    _strip_suffix(ps.get('code', ps.get('symbol', ''))) == code
                    for ps in self.pending_sells
                )
                if not already_pending:
                    pending = dict(pos)
                    pending['sell_type'] = sell_type
                    self.pending_sells.append(pending)
                    print(f"[{_now_str()}] [{self.ENGINE_NAME}] 加入 pending_sells: {code} "
                          f"类型={sell_type} 持仓天数={days_held} 现价={last_price:.3f}")

        # 保存状态
        self._save_state()

    # ------------------------------------------------------------------
    # 实时行情获取（get_full_tick）
    # ------------------------------------------------------------------
    def _get_full_tick(self, symbols: list) -> dict:
        """通过 xtdata.get_full_tick 批量获取实时快照

        参数：
            symbols: 带后缀的股票代码列表，如 ['600000.SH', '000001.SZ']

        返回：
            dict: {symbol: tick_dict}，tick_dict 含 lastPrice/bidPrice/askPrice/lastClose(即preClose) 等
        """
        if not symbols:
            return {}
        try:
            from xtquant import xtdata
            ticks = xtdata.get_full_tick(symbols)
            if ticks is None:
                return {}
            return ticks
        except Exception as e:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] get_full_tick 异常: {e}")
            return {}

    # ------------------------------------------------------------------
    # 下单辅助
    # ------------------------------------------------------------------
    def _place_buy_order(self, code: str, price: float, volume: int, remark: str = '') -> int:
        """挂限价买单，返回 order_id"""
        if not self.executor:
            return -1
        symbol = _format_symbol(code)
        try:
            order_id = self.executor.buy(
                symbol=symbol,
                price=price,
                volume=volume,
                price_type='limit',
                order_remark=remark,
            )
            return order_id
        except Exception as e:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 买入下单异常 {code}: {e}")
            return -1

    def _place_sell_order(self, code: str, price: float, volume: int, remark: str = '') -> int:
        """挂限价卖单，返回 order_id"""
        if not self.executor:
            return -1
        symbol = _format_symbol(code)
        try:
            order_id = self.executor.sell(
                symbol=symbol,
                price=price,
                volume=volume,
                price_type='limit',
                order_remark=remark,
            )
            return order_id
        except Exception as e:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 卖出下单异常 {code}: {e}")
            return -1

    def _cancel_order(self, order_id: int):
        """撤单"""
        if not self.executor or order_id == -1:
            return
        try:
            self.executor.cancel(order_id)
        except Exception as e:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 撤单异常 order_id={order_id}: {e}")

    def _wait_fill(self, order_id: int, timeout: int = 300) -> bool:
        """等待委托成交

        参数：
            order_id: 委托编号
            timeout: 超时秒数（默认300秒/5分钟）

        返回：
            True=已成交，False=超时未成交
        """
        start = time.time()
        check_interval = 10  # 每10秒检查一次

        while time.time() - start < timeout:
            time.sleep(check_interval)
            orders = self._query_orders()
            for o in orders:
                if o.get('order_id') == order_id:
                    status = o.get('status', -1)
                    if status == ORDER_STATUS_FILLED:
                        return True
                    if status in (ORDER_STATUS_CANCELLED, ORDER_STATUS_REJECTED):
                        return False
            # 继续等待
        return False

    def _wait_fill_result(self, order_id: int, timeout: int = 180) -> dict:
        """等待委托成交，返回实际成交明细（支持部分成交）

        返回 dict:
            status:     'filled' | 'partial' | 'cancelled' | 'timeout'
            filled_qty: 已成交股数
            fill_price: 委托价格（目前 QMT API 没有均价字段，用委托价待修）
        """
        start = time.time()
        check_interval = 10
        last_traded, last_price = 0, 0

        while time.time() - start < timeout:
            time.sleep(check_interval)
            orders = self._query_orders()
            for o in orders:
                if o.get('order_id') == order_id:
                    status    = o.get('status', -1)
                    traded    = o.get('traded_volume', 0) or 0
                    price     = o.get('price', 0) or 0
                    last_traded, last_price = traded, price
                    if status == ORDER_STATUS_FILLED:
                        return {'status': 'filled', 'filled_qty': traded, 'fill_price': price}
                    if status in (ORDER_STATUS_CANCELLED, ORDER_STATUS_REJECTED):
                        return {'status': 'cancelled', 'filled_qty': traded, 'fill_price': price}
                    # ORDER_STATUS_PARTIAL (53) → 继续等待

        # 超时：主动撤单后取最终成交量
        self._cancel_order(order_id)
        time.sleep(3)
        orders = self._query_orders()
        for o in orders:
            if o.get('order_id') == order_id:
                traded = o.get('traded_volume', 0) or 0
                price  = o.get('price', 0) or 0
                return {'status': 'timeout', 'filled_qty': traded, 'fill_price': price}
        return {'status': 'timeout', 'filled_qty': last_traded, 'fill_price': last_price}

    def _record_sell_fill(self, code: str, filled_qty: int, fill_price: float,
                          sell_type: str, buy_price: float, days_held: int, pos: dict):
        """记录卖出成交（支持全量/部分），更新持仓与资金"""
        net_income  = self._calc_sell_income(fill_price, filled_qty)
        cost        = buy_price * filled_qty
        profit      = net_income - cost
        profit_pct  = (profit / cost * 100) if cost > 0 else 0
        commission  = max(fill_price * filled_qty * self.commission_rate, self.min_commission)
        stamp_tax   = fill_price * filled_qty * self.stamp_tax_rate

        self.cash += net_income
        self._log_trade('sell', code, fill_price, filled_qty, sell_type,
                        fee=commission + stamp_tax, days_held=days_held)

        orig_qty  = pos.get('quantity', 0)
        remaining = orig_qty - filled_qty

        if remaining <= 0:
            self._remove_position(code)
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 卖出全量成交 [{sell_type}]: {code} "
                  f"数量={filled_qty} 价格={fill_price:.3f} 收入={net_income:.2f} "
                  f"盈亏={profit:+.2f}({profit_pct:+.2f}%)")
        else:
            # 部分成交：更新持仓数量
            code_bare = _strip_suffix(code)
            for p in self.positions:
                if _strip_suffix(p.get('code', p.get('symbol', ''))) == code_bare:
                    p['quantity'] = remaining
                    break
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 卖出部分成交 [{sell_type}]: {code} "
                  f"成交={filled_qty} 剩余={remaining} 价格={fill_price:.3f} "
                  f"收入={net_income:.2f} 盈亏={profit:+.2f}({profit_pct:+.2f}%)")

    def _execute_sell_with_fallback(self, code: str, sell_price: float, quantity: int,
                                    sell_type: str, pos: dict,
                                    buy_price: float, days_held: int):
        """三段式卖出：限价单 → 对手价 → pending_sells

        第1轮（3分钟）：按止损价/止盈价挂限价单
        第2轮（2分钟）：按买一价（对手价）重挂
        第3轮（底探）：加入 pending_sells，次日竞价执行
        每轮实时结算部分成交，剩余量进入下一轮。
        """
        remaining = quantity

        # ── 第1轮：限价单（止损价/止盈价） 3分钟 ─────────────────────────
        order_id = self._place_sell_order(
            code=code, price=sell_price, volume=remaining,
            remark=f"V3_{sell_type}_{code}_r1"
        )
        if order_id and order_id != -1:
            r1 = self._wait_fill_result(order_id, timeout=180)
            if r1['filled_qty'] > 0:
                self._record_sell_fill(code, r1['filled_qty'], r1['fill_price'],
                                       sell_type, buy_price, days_held, pos)
                remaining -= r1['filled_qty']
                pos = dict(pos)
                pos['quantity'] = remaining
            # 若委托未被自动撤单，手动撤单放行下一轮
            if r1['status'] not in ('cancelled',):
                self._cancel_order(order_id)
        if remaining <= 0:
            return

        # ── 第2轮：对手价（买一价） 2分钟 ──────────────────────────
        symbol = _format_symbol(code)
        tick = self._get_full_tick([symbol]).get(symbol, {})
        bid_prices = tick.get('bidPrice', [])
        bid_price  = bid_prices[0] if bid_prices else 0
        if bid_price <= 0:
            bid_price = tick.get('lastPrice', sell_price)
        if bid_price <= 0:
            bid_price = sell_price

        print(f"[{_now_str()}] [{self.ENGINE_NAME}] {code} 第1轮未全部成交 "
              f"剩余 {remaining} 股，改用对手价 {bid_price:.3f} 重挂...")
        order_id2 = self._place_sell_order(
            code=code, price=bid_price, volume=remaining,
            remark=f"V3_{sell_type}_{code}_r2"
        )
        if order_id2 and order_id2 != -1:
            r2 = self._wait_fill_result(order_id2, timeout=120)
            if r2['filled_qty'] > 0:
                self._record_sell_fill(code, r2['filled_qty'], r2['fill_price'],
                                       sell_type, buy_price, days_held, pos)
                remaining -= r2['filled_qty']
                pos = dict(pos)
                pos['quantity'] = remaining
            if r2['status'] not in ('cancelled',):
                self._cancel_order(order_id2)
        if remaining <= 0:
            return

        # ── 第3轮：加入 pending_sells，次日竞价执行 ──────────────────
        print(f"[{_now_str()}] [{self.ENGINE_NAME}] ⚠️ {code} 两轮均未全部成交，"
              f"剩余 {remaining} 股加入 pending_sells，次日竞价执行")
        # 更新持仓剩余量
        code_bare = _strip_suffix(code)
        for p in self.positions:
            if _strip_suffix(p.get('code', p.get('symbol', ''))) == code_bare:
                p['quantity'] = remaining
                break
        # 避免重复添加
        already = any(
            _strip_suffix(ps.get('code', ps.get('symbol', ''))) == code_bare
            for ps in self.pending_sells
        )
        if not already:
            pending = dict(pos)
            pending['quantity']  = remaining
            pending['sell_type'] = sell_type
            self.pending_sells.append(pending)
        self._save_state()

    def _query_orders(self) -> list:
        """查询当日订单列表"""
        if not self.executor:
            return []
        try:
            return self.executor.query_orders()
        except Exception as e:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 查询订单异常: {e}")
            return []

    # ------------------------------------------------------------------
    # 资金管理
    # ------------------------------------------------------------------
    def _get_available_cash(self) -> float:
        """获取策略可用资金（受 capital_limit 限制）

        实盘模式：
            - 查询券商真实可用资金
            - 计算策略已用资金（持仓市值）
            - 可用 = min(真实可用, 资金上限 - 已用)

        模拟模式：
            - 直接使用 self.cash
        """
        # 策略已用资金（按买入价计算）
        used = sum(
            p.get('buy_price', 0) * p.get('quantity', 0)
            for p in self.positions
        )
        remaining_limit = self.capital_limit - used

        if self.mode == 'live' and self.executor is not None:
            try:
                asset = self.executor.query_asset()
                real_cash = asset.get('cash', 0)
                return max(0.0, min(real_cash, remaining_limit))
            except Exception as e:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] 查询资产异常: {e}，使用本地cash")

        # 模拟模式或查询失败，使用本地记录
        return max(0.0, min(self.cash, remaining_limit))

    def _count_effective_positions(self) -> int:
        """计算有效持仓数，用于买入仓位判断。

        逻辑：每个仓位槽预算 = capital_limit / max_positions。
        若某持仓的实际成本 < 槽预算 的 50%（即部分成交导致资金投入过少），
        不计为一个完整槽位，剩余资金可用于购买其他股票。
        """
        slot_budget   = self.capital_limit / self.max_positions
        min_cost      = slot_budget * 0.5
        pending_codes = {_strip_suffix(s.get('code', s.get('symbol', ''))) for s in self.pending_sells}
        count = 0
        for p in self.positions:
            code = _strip_suffix(p.get('code', p.get('symbol', '')))
            cost = p.get('quantity', 0) * p.get('buy_price', 0)
            if cost >= min_cost or code in pending_codes:
                count += 1
        return count

    def _calculate_buy_volume(self, available_cash: float, price: float) -> int:
        """计算买入股数（100股整数倍）

        逻辑：
        - 空仓位数 = max_positions - 当前持仓数
        - 单只分配金额 = available_cash / 空仓位数
        - 买入股数 = floor(单只分配金额 / 价格 / 100) * 100
        """
        import math
        if price <= 0 or available_cash <= 0:
            return 0

        empty_slots = self.max_positions - self._count_effective_positions()
        if empty_slots <= 0:
            return 0

        alloc = available_cash / empty_slots
        volume = math.floor(alloc / price / 100) * 100
        return volume if volume >= 100 else 0

    def _calc_sell_income(self, price: float, volume: int) -> float:
        """计算卖出净收入（扣除佣金和印花税）"""
        amount = price * volume
        commission = max(amount * self.commission_rate, self.min_commission)
        stamp_tax = amount * self.stamp_tax_rate
        return amount - commission - stamp_tax

    # ------------------------------------------------------------------
    # 持仓管理辅助
    # ------------------------------------------------------------------
    def _remove_position(self, code: str):
        """从持仓列表中移除指定股票"""
        code = _strip_suffix(code)
        self.positions = [
            p for p in self.positions
            if _strip_suffix(p.get('code', p.get('symbol', ''))) != code
        ]

    def _remove_pending_sell(self, code: str):
        """从 pending_sells 中移除指定股票"""
        code = _strip_suffix(code)
        self.pending_sells = [
            p for p in self.pending_sells
            if _strip_suffix(p.get('code', p.get('symbol', ''))) != code
        ]

    def _get_tradable_pool(self, held_codes: set) -> list:
        """对调仓池做盘中二次过滤，获取当日可交易候选

        过滤：
        1. 排除已持仓股票
        2. 排除日均成交额 < 5亿 的股票（每天缓存一次）
        """
        if not self.rebalance_pool:
            return []

        # 基础过滤：排除已持仓
        candidates = [c for c in self.rebalance_pool if _strip_suffix(c) not in held_codes]
        if not candidates:
            return []

        # 日均成交额过滤（每天缓存一次）
        today_str = date.today().strftime('%Y-%m-%d')
        if self._daily_filter_date != today_str or not self._daily_filter_cache:
            self._daily_filter_cache = self._filter_by_avg_amount(candidates)
            self._daily_filter_date = today_str
            self._save_state()

        return [c for c in candidates if c in self._daily_filter_cache]

    def _filter_by_avg_amount(self, candidates: list) -> list:
        """过滤日均成交额 >= 5亿 的股票，返回合格代码列表

        使用 xtdata.get_market_data 获取近10个交易日数据。
        异常或本地无数据时全部放行，避免阻塞交易。
        """
        if not candidates:
            return []

        qualified = []
        fallback_all = False  # 是否全部放行
        try:
            from xtquant import xtdata
            symbols = [_format_symbol(c) for c in candidates]

            # 批量获取近10天日线数据
            data = xtdata.get_market_data(
                field_list=['amount'],
                stock_list=symbols,
                period='1d',
                count=10,
            )

            # 兼容多种返回格式
            amount_data = None
            if isinstance(data, dict):
                amount_data = data.get('amount', None)

            # 判断是否有有效数据（DataFrame 或 dict）
            has_data = False
            if amount_data is not None:
                try:
                    import pandas as pd
                    if isinstance(amount_data, pd.DataFrame):
                        # DataFrame：行=股票代码，列=时间戳
                        has_data = (amount_data.shape[1] > 0)  # 有列说明有历史数据
                    elif isinstance(amount_data, dict):
                        has_data = len(amount_data) > 0
                except Exception:
                    has_data = bool(amount_data)

            if not has_data:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] [警告] 本地无日线数据，"
                      f"跳过日均成交额过滤，全部 {len(candidates)} 只候选股放行")
                return candidates

            for code in candidates:
                symbol = _format_symbol(code)
                amounts = []

                try:
                    import pandas as pd
                    if isinstance(amount_data, pd.DataFrame):
                        # DataFrame 行索引是股票代码，列是时间戳
                        if symbol in amount_data.index:
                            row = amount_data.loc[symbol]
                            amounts = [float(v) for v in row.values if v is not None and str(v) != 'nan']
                        else:
                            # 没有该股票的数据，放行
                            qualified.append(code)
                            continue
                    elif isinstance(amount_data, dict):
                        symbol_data = amount_data.get(symbol, {})
                        if not symbol_data:
                            qualified.append(code)
                            continue
                        if hasattr(symbol_data, 'values'):
                            amounts = [float(v) for v in symbol_data.values() if v is not None]
                        elif isinstance(symbol_data, (list, tuple)):
                            amounts = [float(v) for v in symbol_data if v is not None]
                        else:
                            amounts = [float(v) for v in list(symbol_data) if v is not None]
                    else:
                        # 未知格式，放行
                        qualified.append(code)
                        continue
                except Exception:
                    # 解析异常，放行该股票
                    qualified.append(code)
                    continue

                if not amounts:
                    # 无有效数据，放行
                    qualified.append(code)
                    continue

                avg_amount = sum(amounts) / len(amounts)
                if avg_amount >= 500_000_000:  # 5亿
                    qualified.append(code)
                else:
                    print(f"[{_now_str()}] [{self.ENGINE_NAME}] 过滤低流动性: {code} "
                          f"日均成交额={avg_amount/1e8:.2f}亿 < 5亿")

        except Exception as e:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 日均成交额过滤异常: {e}，全部放行")
            return candidates

        print(f"[{_now_str()}] [{self.ENGINE_NAME}] 日均成交额过滤结果: "
              f"{len(candidates)}只 → {len(qualified)}只通过")
        return qualified

    # ------------------------------------------------------------------
    # 买入信号检查
    # ------------------------------------------------------------------
    def _check_buy_signal(self, code: str, bar: dict, pre_close: float) -> bool:
        """检查买入条件

        条件（全部满足）：
        1. 涨幅 > 阈值（科创板/创业板>2%，主板>1%）
        2. 收阳线：close > open
        3. 未涨停（科创板/创业板<19.8%，主板<9.8%）
        """
        if pre_close <= 0:
            return False

        close = bar.get('close', 0)
        open_price = bar.get('open', 0)
        volume = bar.get('volume', 0)

        # 停牌
        if volume == 0 or close <= 0:
            return False

        change_pct = (close - pre_close) / pre_close
        is_star = self._is_star(code)

        # 1. 涨幅阈值
        min_change = config.V3_STAR_MIN_CHANGE_PCT if is_star else config.V3_MIN_CHANGE_PCT
        if change_pct <= min_change:
            return False

        # 2. 收阳线
        if close <= open_price:
            return False

        # 3. 未涨停
        limit_up = config.V3_STAR_LIMIT_UP if is_star else 0.098
        if change_pct >= limit_up:
            return False

        return True

    def _is_star(self, code: str) -> bool:
        """判断是否科创板(688开头)或创业板(30开头)"""
        code_str = str(code).split('.')[0]
        return code_str.startswith('688') or code_str.startswith('30')

    # ------------------------------------------------------------------
    # 交易日志记录
    # ------------------------------------------------------------------
    def _log_trade(self, trade_type, code, price, quantity, reason, fee=0, days_held=0):
        """追加一条交易记录到日志文件"""
        try:
            trade = {
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "type": trade_type,
                "code": code,
                "price": round(price, 3),
                "quantity": quantity,
                "amount": round(price * quantity, 2),
                "fee": round(fee, 2),
                "reason": reason,
                "cash_after": round(self.cash, 2),
                "total_value": round(self.cash + sum(p['buy_price'] * p['quantity'] for p in self.positions), 2),
                "days_held": days_held
            }
            log_path = os.path.join(os.path.dirname(__file__), '..', self.TRADES_LOG_FILE)
            trades = []
            if os.path.exists(log_path):
                try:
                    with open(log_path, 'r', encoding='utf-8') as f:
                        trades = json.load(f)
                except Exception:
                    trades = []
            trades.append(trade)
            with open(log_path, 'w', encoding='utf-8') as f:
                json.dump(trades, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 交易日志记录失败: {e}")

    # ------------------------------------------------------------------
    # 状态文件 I/O
    # ------------------------------------------------------------------
    def _load_state(self) -> dict:
        """加载 state_v3.json，不存在时返回初始状态"""
        if os.path.exists(self.STATE_FILE):
            try:
                with open(self.STATE_FILE, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] 状态文件加载成功: {self.STATE_FILE}")
                return state
            except Exception as e:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] 加载状态文件失败: {e}，使用初始状态")

        default_state = {
            'initial_capital': self.capital_limit,
            'cash': self.capital_limit,
            'positions': [],
            'pending_sells': [],
            'total_value': self.capital_limit,
            'last_update': '',
        }
        print(f"[{_now_str()}] [{self.ENGINE_NAME}] 状态文件不存在，初始化默认状态")
        return default_state

    def _save_state(self):
        """将当前运行时状态序列化保存到 state_v3.json"""
        # 计算总资产（简化：cash + 持仓按买入价估值）
        total_value = self.cash + sum(
            p.get('buy_price', 0) * p.get('quantity', 0)
            for p in self.positions
        )

        state = {
            'initial_capital': self.capital_limit,
            'cash': round(self.cash, 2),
            'positions': self.positions,
            'pending_sells': self.pending_sells,
            'total_value': round(total_value, 2),
            'last_update': _now_str(),
            '_last_increment_date': self._last_increment_date,
            '_daily_filter_date': self._daily_filter_date,
            '_daily_filter_cache': self._daily_filter_cache,
        }

        try:
            with open(self.STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 状态已保存: {self.STATE_FILE}")
        except Exception as e:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 保存状态失败: {e}")

    # ------------------------------------------------------------------
    # 状态报告
    # ------------------------------------------------------------------
    def get_status_report(self) -> str:
        """生成当前持仓状态报告（文本格式）"""
        state = self._load_state()
        positions = state.get('positions', [])
        # 兼容旧版 dict 格式
        if isinstance(positions, dict):
            positions = list(positions.values())

        cash = state.get('cash', 0.0)
        total_value = state.get('total_value', cash)
        initial_capital = state.get('initial_capital', self.capital_limit)

        lines = []
        lines.append("=" * 60)
        lines.append(f"【V3 实时引擎状态报告】 生成时间: {_now_str()}")
        lines.append(f"运行模式: {self.mode}  资金上限: {self.capital_limit:,.0f}")
        lines.append("=" * 60)
        lines.append(f"初始资金: {initial_capital:,.2f}")
        lines.append(f"可用现金: {cash:,.2f}")
        lines.append(f"总资产:   {total_value:,.2f}")
        total_profit = total_value - initial_capital
        total_profit_pct = (total_profit / initial_capital * 100) if initial_capital > 0 else 0
        lines.append(f"累计盈亏: {total_profit:+.2f} ({total_profit_pct:+.2f}%)")
        lines.append(f"当前持仓: {len(positions)} / {self.max_positions}")
        lines.append(f"pending卖出: {len(state.get('pending_sells', []))} 只")
        lines.append("-" * 60)

        if not positions:
            lines.append("当前无持仓")
        else:
            lines.append(f"{'代码':<10}{'成本价':>10}{'数量':>8}{'买入日':>12}{'天数':>6}{'类型':>10}")
            lines.append("-" * 60)
            for pos in positions:
                code = _strip_suffix(pos.get('code', pos.get('symbol', '')))
                buy_price = pos.get('buy_price', 0.0)
                quantity = pos.get('quantity', 0)
                buy_date = pos.get('buy_date', '-')
                days_held = _calculate_days_held(pos)
                sell_type = pos.get('sell_type', '-') or '-'
                lines.append(
                    f"{code:<10}{buy_price:>10.3f}{quantity:>8}{buy_date:>12}{days_held:>6}{sell_type:>10}"
                )

        lines.append("=" * 60)
        report = "\n".join(lines)
        print(report)
        return report


# ===========================================================================
# SimulationEngineV3 —— 模拟引擎（继承实盘引擎）
# ===========================================================================
class SimulationEngineV3(LiveEngineV3):
    """V3策略模拟引擎

    继承 LiveEngineV3，差异：
    - 不连接 miniQMT，使用 SimulatedExecutor 模拟下单
    - 30W 虚拟资金
    - state 文件隔离为 state_v3_sim.json
    - 行情数据仍通过 xtdata.get_full_tick 获取真实行情（只模拟交易，不模拟行情）
    """

    STATE_FILE = 'd:/miniqmt_quant/state_v3_sim.json'
    ENGINE_NAME = 'SimulationEngineV3'
    TRADES_LOG_FILE = 'trades_v3_sim.json'

    def __init__(self, capital: float = 300000.0):
        """初始化模拟引擎

        参数：
            capital: 虚拟资金总量，默认30W
        """
        # 调用父类初始化（mode='simulation'，不会初始化实盘执行器）
        super().__init__(mode='simulation', capital_limit=capital)
        self.cash = capital

        # 初始化模拟执行器
        self._init_simulation_executor()

    def _init_live_executor(self):
        """模拟模式不初始化实盘执行器（覆盖父类方法）"""
        pass  # 模拟模式不连接 miniQMT

    def _init_simulation_executor(self):
        """初始化模拟执行器"""
        try:
            from trade.executor import SimulatedExecutor
            self.executor = SimulatedExecutor()
            self.executor._virtual_cash = self.capital_limit
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 模拟执行器初始化完成，虚拟资金={self.capital_limit:.0f}")
        except Exception as e:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 模拟执行器初始化失败: {e}")
            raise

    def _connect_executor(self):
        """模拟连接（总是成功）"""
        if self.executor is not None:
            return self.executor.connect()
        return False

    def _reconcile_with_broker(self):
        """模拟模式不做持仓核对"""
        print(f"[{_now_str()}] [{self.ENGINE_NAME}] [模拟] 跳过券商持仓核对")

    def _get_available_cash(self) -> float:
        """模拟模式返回本地 cash（不查询券商）"""
        used = sum(
            p.get('buy_price', 0) * p.get('quantity', 0)
            for p in self.positions
        )
        remaining_limit = self.capital_limit - used
        return max(0.0, min(self.cash, remaining_limit))

    def _wait_fill(self, order_id: int, timeout: int = 300) -> bool:
        """模拟模式：SimulatedExecutor 立即成交，直接返回 True"""
        return True
