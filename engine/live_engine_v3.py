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
from datetime import datetime, date, timedelta, time as dtime

sys.path.insert(0, 'd:/miniqmt_quant')
import config

# 可选：钉钉推送通知
try:
    from utils.notifier import notify_buy as _notify_buy
    from utils.notifier import notify_sell as _notify_sell
    from utils.notifier import notify_pending_sell as _notify_pending_sell
    from utils.notifier import notify_system as _notify_system
    _NOTIFIER_OK = True
except Exception:
    _NOTIFIER_OK = False


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
    FAILED_ORDERS_LOG_FILE = 'failed_orders_v3.json'  # 买卖失败复盘日志

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
        # 买入信号阈值（_check_buy_signal 使用，支持热重载）
        self.min_change_pct = config.V3_MIN_CHANGE_PCT
        self.star_min_change_pct = config.V3_STAR_MIN_CHANGE_PCT
        self.max_change_pct = config.V3_MAX_CHANGE_PCT
        self.star_max_change_pct = config.V3_STAR_MAX_CHANGE_PCT
        self.limit_up = 0.098
        self.star_limit_up = config.V3_STAR_LIMIT_UP
        self.prev_bar_up = getattr(config, 'V3_PREV_BAR_UP', False)  # 前5分钟K线非阴线过滤

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

        # 买入扫描间隔控制：每1分钟扫描一次，快速捕捉信号
        self._last_buy_scan_time = None

        # 调仓池文件修改时间（用于热重载检测）
        self._rebalance_pool_mtime = 0.0

        # 条件单记录：{code: condition_order_id}
        # 保存每只持仓股票在制券商服务器端挂起的止损条件单ID
        # 进程崩溃后条件单仍在制券商服务器有效（当日有效期）
        self._condition_orders = {}
        # 启动时从 params_v3.json 热重载（若文件存在则覆盖 config.py 默认值）
        self._reload_params()

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

    def _maybe_reload_rebalance_pool(self):
        """检查调仓池文件是否有更新，若有则热重载"""
        if not os.path.exists(self.REBALANCE_FILE):
            return
        try:
            current_mtime = os.path.getmtime(self.REBALANCE_FILE)
            if current_mtime > self._rebalance_pool_mtime + 0.5:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] 检测到调仓池文件已更新，执行热重载...")
                self._load_rebalance_pool()
        except Exception:
            pass

    def _reload_params(self):
        """从 params_v3.json 热重载策略参数（文件不存在则静默跳过）"""
        import json
        params_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'params_v3.json')
        try:
            with open(params_file, 'r', encoding='utf-8') as _f:
                _p = json.load(_f)
            _m = _p.get('main_board', {})
            _s = _p.get('star_board', {})
            _g = _p.get('general', {})
            # 通用
            if 'max_positions'    in _g: self.max_positions         = int(_g['max_positions'])
            if 'prev_bar_up'      in _g: self.prev_bar_up           = bool(int(_g['prev_bar_up']))
            # 主板
            if 'min_change_pct'    in _m: self.min_change_pct       = float(_m['min_change_pct'])
            if 'hard_stop_loss'    in _m: self.hard_stop_loss        = float(_m['hard_stop_loss'])
            if 'soft_stop_loss'    in _m: self.soft_stop_loss        = float(_m['soft_stop_loss'])
            if 'trailing_activate' in _m: self.trailing_activate     = float(_m['trailing_activate'])
            if 'trailing_stop'     in _m: self.trailing_stop         = float(_m['trailing_stop'])
            if 'time_stop_days'    in _m: self.time_stop_days        = int(_m['time_stop_days'])
            if 'limit_up'          in _m: self.limit_up              = float(_m['limit_up'])
            if 'max_change_pct'    in _m: self.max_change_pct        = float(_m['max_change_pct'])
            # 科创/创业板
            if 'min_change_pct'    in _s: self.star_min_change_pct   = float(_s['min_change_pct'])
            if 'hard_stop_loss'    in _s: self.star_hard_stop_loss   = float(_s['hard_stop_loss'])
            if 'soft_stop_loss'    in _s: self.star_soft_stop_loss   = float(_s['soft_stop_loss'])
            if 'trailing_activate' in _s: self.star_trailing_activate = float(_s['trailing_activate'])
            if 'trailing_stop'     in _s: self.star_trailing_stop    = float(_s['trailing_stop'])
            if 'time_stop_days'    in _s: self.star_time_stop_days   = int(_s['time_stop_days'])
            if 'limit_up'          in _s: self.star_limit_up         = float(_s['limit_up'])
            if 'max_change_pct'    in _s: self.star_max_change_pct   = float(_s['max_change_pct'])
        except FileNotFoundError:
            pass  # 文件不存在时使用 config.py 默认值
        except Exception as _e:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 参数热重载失败: {_e}")

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

        # 4. 开盘前等待（若早于 09:00 启动，循环休眠直至市场开盘）
        while not _market_is_open():
            now = datetime.now()
            t = now.hour * 60 + now.minute
            # 若已过 15:01（收盘后），无需等待，直接进入盘后流程
            if t > 15 * 60 + 1:
                break
            wait_min = max(0, 9 * 60 - t)
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 开盘前等待，距09:00还有约 {wait_min} 分钟，休眠30秒...")
            time.sleep(30)

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
                    # 条件单当日有效期，每天开盘后重建
                    try:
                        self._setup_all_condition_orders()
                    except Exception as _coe:
                        print(f"[{_now_str()}] [{self.ENGINE_NAME}] 条件单批量重建异常: {_coe}")

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

                        # 持仓不足，扫描买入（9:35起，跳过开盘首根K线，每分钟扫描一次）
                        if self._count_effective_positions() < self.max_positions:
                            # 9:30~9:34 跳过买入扫描：等待第一根5分钟K线（9:30-9:35）收盘
                            _buy_scan_allowed = (h == 9 and m >= 35) or (10 <= h <= 14) or (h == 15 and m == 0)
                            if _buy_scan_allowed:
                                _scan_interval_secs = 1 * 60
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
                # sleep(30) × 10次 = 5分钟
                _heartbeat_counter += 1
                if _heartbeat_counter >= 10:
                    try:
                        self._save_state()
                        self._maybe_reload_rebalance_pool()  # 热重载调仓池
                        self._check_condition_order_fills()  # 检测条件单是否已成交
                    except Exception as _he:
                        print(f"[{_now_str()}] [{self.ENGINE_NAME}] 心跳处理异常: {_he}")
                    _heartbeat_counter = 0

                # 每30秒轮询一次（对齐回测精度，减少信号延迟）
                time.sleep(30)

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

        # 恢复非阻塞 pending 买单（order_id存为字符串，转回int）
        _raw_pending = state.get('_pending_buy_orders', {})
        self._pending_buy_orders = {int(k): v for k, v in _raw_pending.items()}
        if self._pending_buy_orders:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 恢复 pending 买单 {len(self._pending_buy_orders)} 笔")

        print(f"[{_now_str()}] [{self.ENGINE_NAME}] 恢复持仓 {len(self.positions)} 只，"
              f"现金 {self.cash:.2f}，pending_sells {len(self.pending_sells)} 条")

        # 实盘模式：与 miniQMT 真实持仓核对
        if self.mode == 'live' and self.executor is not None:
            self._reconcile_with_broker()

        # 实盘模式：批量重建条件单（上一日条件单已失效，重挂当日止损单）
        self._setup_all_condition_orders()

    def _reconcile_with_broker(self):
        """与券商真实持仓核对（实盘模式专用）

        逻辑：
        - 查询 miniQMT 真实持仓
        - 对比本策略记录的持仓
        - extra_in_broker：券商有策略未记录的股票（手动买入）→ 打印警告，不干预
        - missing_in_broker：策略记录但券商已无持仓的股票
          判断为条件单已成交，自动从策略持仓移除
        """
        try:
            real_positions = self.executor.query_positions()

            # 安全防护：如果制券商返回空持仓但策略有持仓，极可能是 API 异常（query_positions 在连接失败时会静默返回 []）
            # 防止误判全部持仓已卖出而清倉。
            if not real_positions and self.positions:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] [警告] 制券商返回空持仓但策略记录有{len(self.positions)}只，"
                      f"疑似 API 异常，跳过持仓核对（请确认 QMT 客户端连接正常）")
                return

            real_codes = {_strip_suffix(p['symbol']) for p in real_positions if p.get('volume', 0) > 0}
            strategy_codes = {_strip_suffix(p.get('code', p.get('symbol', ''))) for p in self.positions}

            extra_in_broker = real_codes - strategy_codes
            missing_in_broker = strategy_codes - real_codes

            if extra_in_broker:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] [警告] 券商持仓中有策略未记录的股票（可能为手动买入）: {extra_in_broker}")
            if missing_in_broker:
                # 制券商已无该股票持仓，判断为条件单已成交或手动卖出
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] 策略记录中券商已无持仓的股票（判断为条件单已成交或已卖出）: {missing_in_broker}")
                for code in missing_in_broker:
                    pos = next((p for p in self.positions
                                if _strip_suffix(p.get('code', p.get('symbol', ''))) == code), None)
                    if pos:
                        buy_price = pos.get('buy_price', 0)
                        quantity = pos.get('quantity', 0)
                        is_star = self._is_star(code)
                        hard_sl = self.star_hard_stop_loss if is_star else self.hard_stop_loss
                        est_price = round(buy_price * (1 - hard_sl), 3)
                        self._log_trade('sell', code, est_price, quantity, 'condition_order_fill')
                        self._remove_position(code)      # 使用辅助方法移除持仓
                        self._remove_pending_sell(code)  # 同步清理 pending_sells
                        print(f"[{_now_str()}] [{self.ENGINE_NAME}] 自动清理: {code} 已从策略持仓移除（券商无该股票）")
                    # 同时清理条件单内存
                    if code in self._condition_orders:
                        del self._condition_orders[code]
                if missing_in_broker:
                    self.cash = self._get_available_cash()  # 同步现金（反映条件单卖出收入）
                    self._save_state()
            if not extra_in_broker and not missing_in_broker:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] 持仓核对一致，共 {len(real_codes)} 只")

        except Exception as e:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 持仓核对异常: {e}")

    # ------------------------------------------------------------------
    # 条件单管理（服务器端止损兜底）
    # ------------------------------------------------------------------
    def _setup_condition_order(self, pos: dict, override_stop_price: float = None) -> bool:
        """为持仓股票挂防崩盘止损条件单

        触发价设为硬止损价（或 override_stop_price），委托价为触发价再降 0.5％（留让躺间）。
        T+0（买入当天）不挂单，防止 T+1 违规。

        Returns:
            是否成功挂单
        """
        if not self.executor or self.mode != 'live':
            return False

        code = _strip_suffix(pos.get('code', pos.get('symbol', '')))
        symbol = _format_symbol(code)

        # T+1：买入当天不挂条件单
        days_held = _calculate_days_held(pos)
        if days_held == 0:
            return False

        buy_price = pos.get('buy_price', 0)
        if buy_price <= 0:
            return False

        is_star = self._is_star(code)
        hard_sl = self.star_hard_stop_loss if is_star else self.hard_stop_loss
        trigger_price = override_stop_price if override_stop_price is not None else round(buy_price * (1 - hard_sl), 3)
        if trigger_price <= 0:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 条件单触发价异常 {code}: trigger_price={trigger_price}，跳过")
            return False
        # 委托价略低于触发价，尽量成交（避免价跌过快导致限价不成交）
        sell_price = round(trigger_price * 0.995, 3)
        quantity = pos.get('quantity', 0)
        if quantity <= 0:
            return False

        # 如果已有条件单，先撤销旧的
        self._cancel_condition_order_for_code(code)

        try:
            cond_id = self.executor.place_condition_order(
                symbol=symbol,
                trigger_price=trigger_price,
                sell_price=sell_price,
                volume=quantity,
                order_remark=f"V3_cond_sl_{code}",
            )
            if cond_id != -1:
                self._condition_orders[code] = cond_id
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] 条件单已挂: {code} "
                      f"触发={trigger_price:.3f} 委托={sell_price:.3f} 数量={quantity} cond_id={cond_id}")
                return True
            else:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] 条件单挂单失败: {code}")
                return False
        except Exception as e:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 条件单挂单异常 {code}: {e}")
            return False

    def _cancel_condition_order_for_code(self, code: str) -> bool:
        """撤销指定股票的条件单（如果存在）

        在程序主动发起卖出前必须先撤销条件单，防止双重卖出。
        """
        cond_id = self._condition_orders.get(code)
        if cond_id is None:
            return True  # 未挂单，无需撤销
        try:
            ok = self.executor.cancel_condition_order(cond_id)
        except Exception as e:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 撤销条件单异常 {code}: {e}")
            ok = False
        # 不管成功与否，都从内存移除（避免重复撤销）
        del self._condition_orders[code]
        if ok:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 条件单已撤销: {code} cond_id={cond_id}")
        return ok

    def _update_condition_order(self, pos: dict, new_stop_price: float) -> bool:
        """更新条件单触发价（移动止盈触发线上升时调用）

        撤销旧条件单，按新触发价重挂。
        """
        return self._setup_condition_order(pos, override_stop_price=new_stop_price)

    def _setup_all_condition_orders(self):
        """批量为所有持仓股票重建条件单（启动恢复 / 每日开盘前调用）

        最常见场景：
        1. 引擎启动恢复时：上一日的条件单已失效，重挂当日条件单
        2. 每天开盘前：条件单当日有效期，重新挂单
        """
        if self.mode != 'live' or not self.executor:
            return

        print(f"[{_now_str()}] [{self.ENGINE_NAME}] 批量重建条件单，共 {len(self.positions)} 只持仓...")
        ok_count = 0
        # 已在 pending_sells 中的股票即将竞价卖出，无需挂条件单
        pending_codes = {_strip_suffix(p.get('code', p.get('symbol', ''))) for p in self.pending_sells}
        for pos in self.positions:
            code = _strip_suffix(pos.get('code', pos.get('symbol', '')))
            if code in pending_codes:
                continue  # 已在待卖队列，跳过挂单
            # 计算有效止损价：如果移动止盈已激活，优先使用回撤触发线（更紧的保护）
            override_price = None
            buy_price   = pos.get('buy_price', 0)
            highest_p   = pos.get('highest_price', buy_price)
            if buy_price > 0:
                _is_s     = self._is_star(code)
                _t_act    = self.star_trailing_activate if _is_s else self.trailing_activate
                _t_pct    = self.star_trailing_stop     if _is_s else self.trailing_stop
                if highest_p >= buy_price * (1 + _t_act):
                    # 移动止盈已激活：用回撤触发线作为条件单触发价（比硬止损更高，防崩保护更严）
                    override_price = round(highest_p * (1 - _t_pct), 3)
            if self._setup_condition_order(pos, override_stop_price=override_price):
                ok_count += 1
        print(f"[{_now_str()}] [{self.ENGINE_NAME}] 条件单重建完成: 成功 {ok_count}/{len(self.positions)} 只")

    def _check_condition_order_fills(self):
        """检测条件单是否已触发成交（每5分钟心跳调用）

        通过对比制券商真实持仓和策略记录，检测条件单是否已触发：
        - 策略记录中有某套股票，但制券商真实持仓已没有该股票：则判定条件单已成交
        - 清理策略内存中的该持仓记录，并记录交易日志
        """
        if self.mode != 'live' or not self.executor or not self._condition_orders:
            return

        try:
            real_positions = self.executor.query_positions()
            real_codes = {_strip_suffix(p['symbol']) for p in real_positions if p.get('volume', 0) > 0}
        except Exception as e:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 条件单检测查询持仓失败: {e}")
            return

        # 安全防护：若制券商返回空持仓但策略有持仓，极可能是 API 错误（executor.query_positions 遇到连接异常时会静默返回 []）
        # 这种情况下或误清仓，故保守跳过。错过的条件单成交在重启时由 _reconcile_with_broker 补齐。
        if not real_positions and self.positions:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 条件单检测：制券商返回空持仓但策略记录有{len(self.positions)}只，"
                  f"保守跳过（重启时 _reconcile_with_broker 会自动补齐）")
            return

        filled_codes = []
        for code in list(self._condition_orders.keys()):
            if code not in real_codes:
                # 制券商已无该股票持仓，判定条件单已成交
                filled_codes.append(code)

        for code in filled_codes:
            try:
                # 找到对应持仓记录
                pos = next((p for p in self.positions
                            if _strip_suffix(p.get('code', p.get('symbol', ''))) == code), None)
                if pos:
                    buy_price = pos.get('buy_price', 0)
                    quantity = pos.get('quantity', 0)
                    # 止损价估算（硬止损线）
                    is_star = self._is_star(code)
                    hard_sl = self.star_hard_stop_loss if is_star else self.hard_stop_loss
                    est_price = round(buy_price * (1 - hard_sl), 3)
                    self._log_trade('sell', code, est_price, quantity, 'condition_order_fill')
                    self._remove_position(code)        # 使用辅助方法移除持仓
                    self._remove_pending_sell(code)    # 同步清理 pending_sells，防止次日竞价重复卖出
                    print(f"[{_now_str()}] [{self.ENGINE_NAME}] 条件单成交检测: {code} 已从制券商移除，"
                          f"估算成交价约={est_price:.3f}，已从策略持仓移除")
                else:
                    print(f"[{_now_str()}] [{self.ENGINE_NAME}] 条件单成交检测: {code} 制券商已无持仓（策略记录已不存在）")
                # 从条件单内存移除
                del self._condition_orders[code]
            except Exception as _ce:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] 条件单成交清理异常 {code}: {_ce}")
                # 岆管清理失败也移除内存记录，避免下次心跳重复处理
                self._condition_orders.pop(code, None)

        if filled_codes:
            self.cash = self._get_available_cash()  # 对齐资金
            self._save_state()

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
                self._rebalance_pool_mtime = os.path.getmtime(self.REBALANCE_FILE)
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] 调仓池加载成功: {len(self.rebalance_pool)} 只，"
                      f"调仓日={rebalance_date}")

                # ── 预缓存覆盖检查：哪些股票有/无 5min_next_pool 本地文件 ──────────────
                try:
                    import glob as _glob
                    _cache_dir = getattr(config, 'V3_NEXT_POOL_5MIN_DIR', 'd:/miniqmt_quant/5min_next_pool')
                    _with_cache    = [c for c in self.rebalance_pool
                                      if _glob.glob(os.path.join(_cache_dir, f"{c}_*.csv"))]
                    _without_cache = [c for c in self.rebalance_pool
                                      if not _glob.glob(os.path.join(_cache_dir, f"{c}_*.csv"))]
                    print(f"[{_now_str()}] [{self.ENGINE_NAME}] 5分钟预缓存覆盖: "
                          f"有文件={len(_with_cache)} 只，无文件={len(_without_cache)} 只")
                    if _without_cache:
                        print(f"[{_now_str()}] [{self.ENGINE_NAME}] [警告] 以下股票无5分钟预缓存，"
                              f"盘中将依赖 miniQMT 订阅实时填充（新入池股票首日可能出现K线不足）: "
                              f"{_without_cache}")
                    else:
                        print(f"[{_now_str()}] [{self.ENGINE_NAME}] 所有调仓池股票均有5分钟预缓存，兜底路径就绪")
                except Exception as _ce:
                    print(f"[{_now_str()}] [{self.ENGINE_NAME}] 预缓存覆盖检查失败（不影响主流程）: {_ce}")

                # 加载完成后立即订阅5分钟K线，确保 get_market_data 有本地缓存
                self._subscribe_5m_pool()
            except Exception as e:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] 加载调仓池失败: {e}，使用空池")
                self.rebalance_pool = []
        else:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 调仓池文件不存在: {self.REBALANCE_FILE}，使用空池")
            self.rebalance_pool = []

    def _try_local_5min_fallback(self, code: str):
        """从 5min_next_pool 目录读取最新的{code}_*.csv，返回最后2根bar的各字段数组

        返回格式: (opens, highs, lows, closes, vols) 各为长度≥2的 numpy array
        任何失败情况返回 None（调用方读到 None 则跳过该股票）
        """
        import glob
        import numpy as np
        import pandas as pd
        cache_dir = getattr(config, 'V3_NEXT_POOL_5MIN_DIR', 'd:/miniqmt_quant/5min_next_pool')
        try:
            pattern = os.path.join(cache_dir, f"{code}_*.csv")
            files = sorted(glob.glob(pattern))
            if not files:
                return None
            latest = files[-1]  # 按文件名升序，取最新的
            df = pd.read_csv(latest)
            if df.empty or len(df) < 1:
                return None
            tail = df.tail(2)
            opens  = tail['open'].astype(float).values
            highs  = tail['high'].astype(float).values
            lows   = tail['low'].astype(float).values
            closes = tail['close'].astype(float).values
            vols   = tail['volume'].astype(float).values
            # 不足两根时补齐（用第一根复制）
            while len(closes) < 2:
                opens  = np.concatenate([[opens[0]],  opens])
                highs  = np.concatenate([[highs[0]],  highs])
                lows   = np.concatenate([[lows[0]],   lows])
                closes = np.concatenate([[closes[0]], closes])
                vols   = np.concatenate([[vols[0]],   vols])
            return opens, highs, lows, closes, vols
        except Exception as _fe:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] [{code}] 本地5分钟兜底读取失败: {_fe}")
            return None

    def _subscribe_5m_pool(self):
        """订阅调仓池 + 当前持仓股的5分钟K线实时推送

        调用后 get_market_data(period='5m', count=N) 才能正常返回数据。
        流程：先 download_history_data 确保 datadir 有历史数据，
              再 subscribe_quote(count=3) 预载入内存缓存。
        对新入池股票（datadir 无历史数据）尤为关键：
          若跳过 download 直接 subscribe，count=3 预取为空，
          9:35 首次扫描时 get_market_data 返回空导致买入信号失效。
        持仓股也加入订阅，防止非调仓池历史持仓在卖出监控时无5m缓存。
        """
        pool_codes = [_format_symbol(c) for c in self.rebalance_pool]
        pos_codes  = [_format_symbol(_strip_suffix(p.get('code', p.get('symbol', ''))))
                      for p in self.positions]
        symbols = list(dict.fromkeys(pool_codes + pos_codes))  # 去重保序
        if not symbols:
            return

        ok_cnt, fail_cnt = 0, 0
        dl_ok, dl_fail = 0, 0
        try:
            from xtquant import xtdata as _xtd_sub

            # ── 第一步：批量下载历史数据到 datadir（新入池股票首次必须） ──────────
            # download_history_data 对已有数据只做增量补充，幂等安全
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 预下载5分钟历史数据（共{len(symbols)}只）...")
            for sym in symbols:
                try:
                    _xtd_sub.download_history_data(sym, period='5m', start_time='', end_time='')
                    dl_ok += 1
                except Exception as _de:
                    dl_fail += 1
                    print(f"[{_now_str()}] [{self.ENGINE_NAME}] [警告] {sym} 历史下载失败: {_de}")
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 历史数据下载完成: 成功{dl_ok} 失败{dl_fail}")

            # ── 第二步：订阅实时推送（此时 datadir 已有数据，count=3 可正常预取） ──
            for sym in symbols:
                try:
                    _xtd_sub.subscribe_quote(sym, period='5m', count=3)
                    ok_cnt += 1
                except Exception:
                    fail_cnt += 1
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 5分钟K线订阅完成: "
                  f"成功 {ok_cnt} 只，失败 {fail_cnt} 只（调仓池{len(pool_codes)}+持仓{len(pos_codes)}只）")
        except Exception as e:
            _msg = f"订阅5分钟K线异常({e})，K线买入/卖出信号将无法获取数据"
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] {_msg}")
            try:
                _notify_system(title="⚠️ 5分钟K线订阅失败", body=_msg, level='error')
            except Exception:
                pass

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

            # 竞价卖出前先撤销条件单，防止条件单与竞价单同时触发导致双重卖出
            self._cancel_condition_order_for_code(code)

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
                self._log_failed_order('sell', code, sell_price, quantity, 0, 'auction_failed',
                                       {'sell_type': pos.get('sell_type', 'pending'),
                                        'sell_price': sell_price})

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
                actual_fill = self._get_actual_fill_price(order_id, sell_price)
                self._log_trade('sell', code, actual_fill, quantity, sell_type,
                                fee=commission+stamp_tax, days_held=days_held,
                                slip_ref=sell_price)
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
                    self._log_failed_order('sell', code, bid_price, quantity, 0, 'resubmit_timeout',
                                           {'sell_type': pos.get('sell_type', 'pending'),
                                            'bid_price': bid_price})

    # ------------------------------------------------------------------
    # 盘中持仓监控（9:30~15:00）
    # ------------------------------------------------------------------
    def _get_position_5m_bars(self) -> dict:
        """批量获取持仓股最新已完成5分钟bar（count=2 中的 [-2]）

        与回测对齐：止损/止盈触发用 bar['low']，最高价更新用 bar['high']，
        卖出价用 max(止损价, bar['open'])。

        Returns:
            {code_bare: {'open': float, 'high': float, 'low': float,
                         'close': float, 'volume': float}}
        """
        if not self.positions:
            return {}
        symbols = list(dict.fromkeys(
            _format_symbol(_strip_suffix(p.get('code', p.get('symbol', ''))))
            for p in self.positions
        ))
        try:
            from xtquant import xtdata as _xtd_pos
            kd = _xtd_pos.get_market_data(
                field_list=['open', 'high', 'low', 'close', 'volume'],
                stock_list=symbols, period='5m', count=2
            )
            result = {}
            for sym in symbols:
                code = _strip_suffix(sym)
                bar = {}
                for field in ('open', 'high', 'low', 'close', 'volume'):
                    df = kd.get(field)
                    if df is None or not hasattr(df, 'loc') or sym not in df.index:
                        continue
                    vals = df.loc[sym].values
                    if len(vals) < 2:
                        continue
                    bar[field] = float(vals[-2])
                if len(bar) == 5 and bar.get('volume', 0) > 0:
                    result[code] = bar
            return result
        except Exception as _e:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 持仓5分钟bar获取失败({_e})，使用tick兜底")
            return {}

    def _monitor_positions(self):
        """检查持仓的硬止损/止盈条件

        流程（与回测对齐，使用5分钟K线）：
        1. 批量获取持仓股最新已完成5分钟bar
        2. 用 bar['low'] 判断是否触发硬止损/移动止盈
        3. 用 bar['high'] 更新持仓历史最高价
        4. 卖出价 = max(止损价, bar['open'])（无5m bar时回退到tick lastPrice）
        5. 触发 → 限价卖出 → 成交后移除持仓
        """
        if not self.positions:
            return

        codes = [_format_symbol(_strip_suffix(p.get('code', p.get('symbol', '')))) for p in self.positions]
        # 5分钟bar数据（止损/止盈触发主要数据源，与回测对齐）
        pos_bars = self._get_position_5m_bars()
        # tick 仍保留：bar不可用时兜底，以及条件单更新
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

            buy_price = pos.get('buy_price', 0)
            if buy_price <= 0:
                continue

            # 获取5分钟bar；无bar则用tick lastPrice兜底
            bar_data = pos_bars.get(code)
            if bar_data:
                bar_low  = bar_data['low']
                bar_high = bar_data['high']
                bar_open = bar_data['open']
                chk_price = bar_low     # 止损触发用 bar low（与回测对齐）
            else:
                last_price = tick.get('lastPrice', 0) if tick else 0
                if last_price <= 0:
                    continue
                bar_low = bar_high = bar_open = last_price
                chk_price = last_price  # 兜底：tick lastPrice

            # 判断是否科创板/创业板
            is_star = self._is_star(code)
            hard_sl = self.star_hard_stop_loss if is_star else self.hard_stop_loss
            trail_act = self.star_trailing_activate if is_star else self.trailing_activate
            trail_pct = self.star_trailing_stop    if is_star else self.trailing_stop

            hard_stop_price = buy_price * (1 - hard_sl)

            # 用 bar['high'] 更新持仓历史最高价（与回测对齐）
            highest_price = pos.get('highest_price', buy_price)
            if bar_high > highest_price:
                highest_price = bar_high
                pos['highest_price'] = highest_price
                # 最高价上升时，若移动止盈已激活，更新条件单触发价至新的回撤线
                if highest_price >= buy_price * (1 + trail_act):
                    new_trail_trigger = round(highest_price * (1 - trail_pct), 3)
                    self._update_condition_order(pos, new_stop_price=new_trail_trigger)

            # T+1限制：买入当天（days_held==0）不执行卖出
            days_held = _calculate_days_held(pos)
            if days_held == 0:
                continue

            should_sell = False
            sell_price = chk_price
            sell_type = None

            # 1. 硬止损（最高优先级）：用 bar['low'] 判断触发，卖出价=max(止损价, bar_open)
            if chk_price <= hard_stop_price:
                should_sell = True
                sell_price = max(hard_stop_price, bar_open)
                sell_type = 'hard_stop'
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] 触发硬止损: {code} "
                      f"bar_low={bar_low:.3f} 止损价={hard_stop_price:.3f} 卖出价={sell_price:.3f}")

            # 2. 移动止盈：激活后 bar['low'] 触达回撤线触发
            # 卖出参考价：正常情况 bar_open >= trail_trigger，两者等价取 bar_open；
            # 跳空低开时 bar_open < trail_trigger，trail_trigger 价格不可达，强制用 bar_open
            # （路由层无论如何都会用实时买一价下单，sell_price 仅作折价计算的参考基准）
            elif highest_price >= buy_price * (1 + trail_act):
                trail_trigger = highest_price * (1 - trail_pct)
                if chk_price <= trail_trigger:
                    should_sell = True
                    sell_price = bar_open  # 用真实开盘价作参考，跳空低开时语义正确
                    sell_type = 'trailing_stop'
                    print(f"[{_now_str()}] [{self.ENGINE_NAME}] 触发移动止盈: {code} "
                          f"最高价={highest_price:.3f} 回撤价={trail_trigger:.3f} "
                          f"bar_low={bar_low:.3f} bar_open={bar_open:.3f} "
                          f"{'跳空低开' if bar_open < trail_trigger else '正常触发'}")

            if should_sell:
                quantity = pos.get('quantity', 0)
                if quantity <= 0:
                    continue

                # 下单前先撤销条件单，防止主动卖出与条件单同时触发导致双重卖出
                self._cancel_condition_order_for_code(code)

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
        # 开盘首根K线（9:30-9:35）不触发买入，与回测逻辑对齐
        # 最早买入时间：9:40（基于 9:35-9:40 完成K线判断）
        _now_time = datetime.now().time()
        if _now_time < dtime(9, 40):
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] [扫描] 当前时间{_now_time.strftime('%H:%M')} < 09:40，首根K线不触发买入，跳过")
            return
        print(f"[{_now_str()}] [{self.ENGINE_NAME}] [扫描] 开始扫描买入，"
              f"调仓池={len(self.rebalance_pool)}只，持仓={len(self.positions)}/{self.max_positions}")

        # 先检查上一轮挂出的pending买单状态（非阻塞）
        self._check_pending_buy_orders()

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

        # 排除已持仓的股票（策略记录 + 制券商真实持仓）
        held_codes = {_strip_suffix(p.get('code', p.get('symbol', ''))) for p in self.positions}
        # 引入制券商真实持仓，防止对手动买入的股票重复下单
        if self.mode == 'live' and self.executor:
            try:
                _real_pos = self.executor.query_positions()
                for _rp in _real_pos:
                    if _rp.get('volume', 0) > 0:
                        held_codes.add(_strip_suffix(_rp['symbol']))
            except Exception as _qp_err:
                # 查询失败时退化为仅使用策略记录，但打印告警，防止手动买入的股票被重复下单
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] [警告] broker持仓查询失败({_qp_err})，"
                      f"已退化为仅使用策略记录排重，手动持仓可能被重复下单")

        # 当日可交易池（二次过滤）
        tradable_pool = self._get_tradable_pool(held_codes)
        if not tradable_pool:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] [扫描] 可交易候选池为空（已过滤），跳过")
            return

        print(f"[{_now_str()}] [{self.ENGINE_NAME}] [扫描] 候选池{len(tradable_pool)}只，可用资金={available_cash:.0f}")

        # 批量获取行情快照（仅用于 pre_close）
        symbols = [_format_symbol(c) for c in tradable_pool]
        ticks = self._get_full_tick(symbols)

        # 批量获取最近2根5分钟K线（[-2]=最新已完成Bar，[-1]=当前成型Bar）
        # 与回测逻辑对齐：以已完成K线的close作为信号依据和挂单价
        # 注：引擎首次扫描在9:35:30左右，距9:30-9:35收盘≥30s，不存在取不到数据的问题
        # get_market_data 返回的是 pandas DataFrame：index=股票代码, columns=时间戳
        # 需用 df.loc[symbol].values 提取每只股票的时序数组
        try:
            from xtquant import xtdata as _xtd_kd
            _kd = _xtd_kd.get_market_data(
                field_list=['open', 'high', 'low', 'close', 'volume'],
                stock_list=symbols,
                period='5m',
                count=3  # [-1]=成型中, [-2]=最新完成bar, [-3]=前一完成bar(供prev_bar_up使用)
            )

            def _kd_get(field):
                """DataFrame.loc[股票] 提取，不存在则返回空列表"""
                df = _kd.get(field)
                if df is None or not hasattr(df, 'loc'):
                    return {}
                # 转换为 {symbol: numpy_array}
                return {sym: df.loc[sym].values
                        for sym in df.index
                        if sym in df.index}

            _kd_opens  = _kd_get('open')
            _kd_highs  = _kd_get('high')
            _kd_lows   = _kd_get('low')
            _kd_closes = _kd_get('close')
            _kd_vols   = _kd_get('volume')

            n_valid = sum(1 for v in _kd_closes.values() if len(v) >= 2)
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] [扫描] K线数据: "
                  f"共{len(_kd_closes)}只返回，其中{n_valid}只数据充足(>=2根bar)")
        except Exception as _ke:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] [扫描] 5分钟K线批量获取失败({_ke})，本次跳过")
            import traceback; traceback.print_exc()
            return

        today_str = date.today().strftime('%Y-%m-%d')

        # 今日已失败的买入代码（下单超时/未成交），当天不再重试
        failed_today = {c for c, d in self._failed_buys_today.items() if d == today_str}
        if failed_today:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] [扫描] 今日已失败代码跳过: {failed_today}")

        # pending买单数量（非阻塞挂单，已占槽但未确认成交）
        pending_count = len(self._pending_buy_orders)
        if pending_count > 0:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] [扫描] 当前pending买单={pending_count}笔，已占槽{pending_count}个")

        for code in tradable_pool:
            # 持仓已满（含待确认pending买单占位），停止
            if self._count_effective_positions() + pending_count >= self.max_positions:
                break

            # 今日已尝试失败，跳过
            if code in failed_today:
                continue

            symbol = _format_symbol(code)

            # 从 tick 获取昨日收盘价（pre_close）和当日开盘价（day_open），用于涨幅和收阳判断
            tick = ticks.get(symbol)
            pre_close = 0
            day_open  = 0
            if tick:
                pre_close = tick.get('lastClose', 0) or tick.get('preClose', 0)
                day_open  = float(tick.get('open', 0) or 0)
            if pre_close <= 0:
                continue

            # 取最新已完成的 5 分钟 K 线（count=3 中的 [-2]；[-1] 为成型中Bar；[-3] 为前一完成Bar）
            _bar_opens  = _kd_opens.get(symbol,  [])
            _bar_highs  = _kd_highs.get(symbol,  [])
            _bar_lows   = _kd_lows.get(symbol,   [])
            _bar_closes = _kd_closes.get(symbol, [])
            _bar_vols   = _kd_vols.get(symbol,   [])

            if len(_bar_closes) < 2:
                # 尝试本地5分钟兜底（新入池股票 miniQMT 无历史bar时使用）
                _fb = self._try_local_5min_fallback(code)
                if _fb is not None:
                    _bar_opens, _bar_highs, _bar_lows, _bar_closes, _bar_vols = _fb
                    print(f"[{_now_str()}] [{self.ENGINE_NAME}] [扫描] {code} "
                          f"miniQMT无历史bar，已用本地5分钟预缓存兜底")
                else:
                    print(f"[{_now_str()}] [{self.ENGINE_NAME}] [扫描] {code} "
                          f"K线数据不足(bars={len(_bar_closes)})且无本地兜底，跳过")
                    continue

            bar_o = float(_bar_opens[-2])
            bar_h = float(_bar_highs[-2])
            bar_l = float(_bar_lows[-2])
            bar_c = float(_bar_closes[-2])
            bar_v = float(_bar_vols[-2])

            # 无效K线（停牌/无数据）
            if bar_v <= 0 or bar_c <= 0:
                continue

            # prev_bar_up 过滤：上一根完成bar需为非阴线（close >= open）
            if self.prev_bar_up:
                if len(_bar_closes) < 3:
                    print(f"[{_now_str()}] [{self.ENGINE_NAME}] [扫描] {code} "
                          f"prev_bar_up=True 但bar数量不足(<3)，跳过")
                    continue
                _prev_bar_c = float(_bar_closes[-3])
                _prev_bar_o = float(_bar_opens[-3])
                if _prev_bar_c < _prev_bar_o:  # 前K为阴线，跳过
                    continue

            bar = {
                'open':   bar_o,
                'high':   bar_h,
                'low':    bar_l,
                'close':  bar_c,
                'volume': bar_v,
                'amount': bar_c * bar_v,
            }
            change_pct = (bar_c - pre_close) / pre_close if pre_close > 0 else 0

            # 打印 K 线状态
            _bullish_str = 'yes' if (day_open > 0 and bar_c > day_open) else ('no' if day_open > 0 else f'bar:{bar_c>bar_o}')
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] [扫描] {code} "
                  f"5min已完成K线: open={bar_o:.2f} close={bar_c:.2f} "
                  f"涨幅={change_pct:.2%} 收阳(>日开)={_bullish_str} vol={bar_v:.0f}")

            if not self._check_buy_signal(code, bar, pre_close, day_open=day_open):
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] [扫描] {code} 不满足买入条件: "
                      f"涨幅={change_pct:.2%}, 收阳={bar_c > bar_o}, close={bar_c:.2f}")
                continue

            # ── 实时价智能路由：下单前重查卖一价，决定下单价与等待超时 ──────────────
            # 规则：卖一价≤bar_c 或溢价≤阈值 → 用实时价，快速成交（60s）
            #       溢价>阈值 → 挂 bar_c 等价格回落（300s），超时宁可不买
            _fresh_tick = self._get_full_tick([symbol]).get(symbol, {})
            _ask_list = _fresh_tick.get('askPrice', [])
            _ask = float(_ask_list[0]) if _ask_list else 0.0
            if _ask <= 0:
                _ask = float(_fresh_tick.get('lastPrice', bar_c) or bar_c)
            _buy_slip_max = getattr(config, 'V3_LIVE_BUY_SLIP_MAX', 0.003)
            _slip_buy = (_ask - bar_c) / bar_c if bar_c > 0 and _ask > bar_c else 0.0

            if _ask <= bar_c:
                order_price  = _ask
                _buy_timeout = 60
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] [路由] {code} "
                      f"卖一价{_ask:.3f}\u2264close{bar_c:.3f}，直接买入（无溢价）")
            elif _slip_buy <= _buy_slip_max:
                order_price  = _ask
                _buy_timeout = 60
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] [路由] {code} "
                      f"卖一价{_ask:.3f} 溢价{_slip_buy:.2%}\u2264{_buy_slip_max:.2%}，接受实时价")
            else:
                order_price  = bar_c
                _buy_timeout = 300
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] [路由] {code} "
                      f"卖一价{_ask:.3f} 溢价{_slip_buy:.2%}>{_buy_slip_max:.2%}，"
                      f"挂close价{bar_c:.3f}等待回落（超时不买）")
            # ─────────────────────────────────────────────────────────────────

            # 可用资金重新计算（前面可能已经买入了）
            available_cash = self._get_available_cash()
            volume_to_buy = self._calculate_buy_volume(available_cash, order_price)

            if volume_to_buy <= 0:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] {code} 资金不足或股数为0，跳过")
                continue

            # 检查资金
            total_cost = order_price * volume_to_buy * (1 + self.commission_rate)
            if total_cost > available_cash:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] {code} 资金不足: 需{total_cost:.0f} 可用{available_cash:.0f}")
                continue

            # 下单（以路由决定的价格为限价）
            order_id = self._place_buy_order(
                code=code,
                price=order_price,
                volume=volume_to_buy,
                remark=f"V3_buy_{code}"
            )

            if not order_id or order_id == -1:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] {code} 买入下单失败，尝试下一只")
                self._log_failed_order('buy', code, order_price, volume_to_buy, 0, 'order_failed',
                                       {'bar_close': bar_c, 'change_pct': round(change_pct, 4),
                                        'order_price': order_price})
                continue

            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 买入委托已提交: {code} "
                  f"价格={order_price:.3f} 数量={volume_to_buy} order_id={order_id}")

            # ── 非阻塞：记录pending，立即继续扫描下一只 ────────────────────────
            deadline = datetime.now() + timedelta(seconds=_buy_timeout)
            self._pending_buy_orders[order_id] = {
                'code':         code,
                'symbol':       symbol,
                'price':        order_price,
                'intended_qty': volume_to_buy,
                'placed_at':    datetime.now().isoformat(),
                'deadline':     deadline.isoformat(),
                'pre_close':    pre_close,
                'bar_c':        bar_c,
                'change_pct':   change_pct,
            }
            pending_count += 1
            self._save_state()
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] [非阻塞] {code} "
                  f"买单已挂出，截止确认时间={deadline.strftime('%H:%M:%S')}，"
                  f"继续扫描下一只")
            # ─────────────────────────────────────────────────────────────────────────────

    # ------------------------------------------------------------------
    # 收盘前检查（14:55）
    # ------------------------------------------------------------------
    def _check_close_signals(self):
        """收盘前检查阴跌/时间止损，生成 pending_sells

        规则（与回测对齐，使用 14:55 那根 5分钟 bar 的 close）：
        1. 阴跌止损：14:55 bar close < open（收阴线）且跌幅 > soft_stop_loss → pending
        2. 时间止损：持仓 >= time_stop_days 天且 14:55 bar close <= 买入价 → pending
        3. 移动止盈：最高价激活后 close 触达回撤线 → pending

        pending_sells 中的股票将在次日 9:15 集合竞价中挂单卖出
        """
        if not self.positions:
            return

        print(f"[{_now_str()}] [{self.ENGINE_NAME}] 执行收盘前信号检查...")

        codes = [_format_symbol(_strip_suffix(p.get('code', p.get('symbol', '')))) for p in self.positions]
        # 14:55 那根 5分钟bar（主要价格源，与回测对齐）
        pos_bars = self._get_position_5m_bars()
        # tick 付辅：提供当天开盘价（open字段），以及 bar 无数据时兜底
        ticks = self._get_full_tick(codes)
        today_str = date.today().strftime('%Y-%m-%d')

        for pos in list(self.positions):
            code = _strip_suffix(pos.get('code', pos.get('symbol', '')))
            symbol = _format_symbol(code)
            tick = ticks.get(symbol)
            bar_data = pos_bars.get(code)

            # 14:55 收盘价：优先用 bar close（与回测对齐），无bar时用 tick lastPrice 兜底
            if bar_data:
                last_price = bar_data['close']
            elif tick:
                last_price = tick.get('lastPrice', 0)
            else:
                continue
            if last_price <= 0:
                continue

            # 开盘价：用 tick.open（当天全天开盘价，与回测 day_open_price 等价）
            open_price = tick.get('open', 0) if tick else 0
            pre_close = tick.get('lastClose', 0) or tick.get('preClose', 0) if tick else 0

            buy_price = pos.get('buy_price', 0)
            if buy_price <= 0:
                continue

            days_held = _calculate_days_held(pos)

            # T+1 限制
            if days_held == 0:
                continue

            is_star = self._is_star(code)
            soft_sl = self.star_soft_stop_loss if is_star else self.soft_stop_loss
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
                    # 14:55 入队同时撤销条件单，防止 14:55-15:00 间条件单与次日竞价单双重触发
                    self._cancel_condition_order_for_code(code)
                    print(f"[{_now_str()}] [{self.ENGINE_NAME}] 加入 pending_sells: {code} "
                          f"类型={sell_type} 持仓天数={days_held} 现价={last_price:.3f}")
                    # 钉钉通知：待卖出信号
                    if _NOTIFIER_OK:
                        try:
                            _notify_pending_sell(code=code, sell_type=sell_type,
                                                 days_held=days_held, last_price=last_price)
                        except Exception:
                            pass

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

    def _wait_fill_result(self, order_id: int, timeout: int = 180,
                           monitor_while_waiting: bool = False) -> dict:
        """等待委托成交，返回实际成交明细（支持部分成交）

        返回 dict:
            status:     'filled' | 'partial' | 'cancelled' | 'timeout'
            filled_qty: 已成交股数
            fill_price: 实际成交均价（优先从 query_trades 获取；API 不支持时降级为委托价）

        monitor_while_waiting: True 时每轮轮询后额外执行一次 _monitor_positions()，
            用于买入等待期间持续监控持仓的止损/止盈，避免被买入循环阻塞而错过卖出时机。
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
                        # 尝试从成交明细获取实际均价（比委托价更准确）
                        actual_price = self._get_actual_fill_price(order_id, price)
                        return {'status': 'filled', 'filled_qty': traded, 'fill_price': actual_price}
                    if status in (ORDER_STATUS_CANCELLED, ORDER_STATUS_REJECTED):
                        actual_price = self._get_actual_fill_price(order_id, price)
                        return {'status': 'cancelled', 'filled_qty': traded, 'fill_price': actual_price}
                    # ORDER_STATUS_PARTIAL (53) → 继续等待
            # 买入等待期间：持续监控持仓止损/止盈，避免被买入循环阻塞
            if monitor_while_waiting:
                try:
                    self._monitor_positions()
                except Exception as _me:
                    print(f"[{_now_str()}] [{self.ENGINE_NAME}] 买入等待中止损监控异常: {_me}")

        # 超时：主动撤单后取最终成交量
        self._cancel_order(order_id)
        time.sleep(3)
        orders = self._query_orders()
        for o in orders:
            if o.get('order_id') == order_id:
                traded = o.get('traded_volume', 0) or 0
                price  = o.get('price', 0) or 0
                actual_price = self._get_actual_fill_price(order_id, price)
                return {'status': 'timeout', 'filled_qty': traded, 'fill_price': actual_price}
        return {'status': 'timeout', 'filled_qty': last_traded, 'fill_price': last_price}

    def _get_actual_fill_price(self, order_id: int, fallback_price: float) -> float:
        """从成交明细中获取实际成交均价，API 不支持或无数据时降级为 fallback_price"""
        try:
            if self.executor is None or not hasattr(self.executor, 'query_trades'):
                return fallback_price
            trades = self.executor.query_trades()
            matched = [t for t in trades if t.get('order_id') == order_id and t.get('traded_volume', 0) > 0]
            if not matched:
                return fallback_price
            # 加权均价
            total_vol = sum(t['traded_volume'] for t in matched)
            total_amt = sum(t['traded_volume'] * t['traded_price'] for t in matched)
            avg_price = round(total_amt / total_vol, 3) if total_vol > 0 else fallback_price
            return avg_price
        except Exception:
            return fallback_price

    # ------------------------------------------------------------------
    # 非阻塞买入：持仓记录 & pending买单检查
    # ------------------------------------------------------------------
    def _record_buy_fill(self, info: dict, actual_qty: int, fill_price: float, today_str: str):
        """记录买入成交，更新持仓和现金（供 _check_pending_buy_orders 调用）"""
        code         = info['code']
        symbol       = info['symbol']
        order_price  = info['price']
        volume_to_buy = info['intended_qty']
        pre_close    = info.get('pre_close', 0)
        bar_c        = info.get('bar_c', order_price)
        change_pct   = info.get('change_pct', 0)

        buy_cost   = order_price * actual_qty
        commission = max(buy_cost * self.commission_rate, self.min_commission)
        total_paid = buy_cost + commission
        self.cash -= total_paid

        pos = {
            'code':          code,
            'symbol':        symbol,
            'buy_price':     order_price,
            'buy_date':      today_str,
            'quantity':      actual_qty,
            'days_held':     0,
            'sell_type':     None,
            'highest_price': order_price,
        }
        if actual_qty < volume_to_buy:
            pos['intended_qty'] = volume_to_buy
            self._log_failed_order('buy', code, order_price, volume_to_buy, actual_qty, 'partial',
                                   {'bar_close': bar_c, 'change_pct': round(change_pct, 4)})
        self.positions.append(pos)

        if actual_qty >= volume_to_buy:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 买入全量成交(pending确认): {code} "
                  f"价格={order_price:.3f} 数量={actual_qty} "
                  f"总成本={total_paid:.2f} 佣金={commission:.2f} "
                  f"剩余现金={self.cash:.2f}")
        else:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 买入部分成交(pending确认): {code} "
                  f"实际={actual_qty}/计划={volume_to_buy} 价格={order_price:.3f} "
                  f"总成本={total_paid:.2f} 剩余现金={self.cash:.2f}")

        self._log_trade('buy', code, order_price, actual_qty, 'buy_signal', fee=commission, slip_ref=bar_c)
        self._save_state()

        # 买入当天不挂条件单（T+1）；次日启动时统一批量重建
        if _calculate_days_held(pos) > 0:
            self._setup_condition_order(pos)

        # 钉钉通知
        if _NOTIFIER_OK:
            try:
                _chg = (order_price - pre_close) / pre_close * 100 if pre_close > 0 else 0
                _notify_buy(code=code, price=order_price, volume=actual_qty,
                            amount=total_paid, change_pct=_chg)
            except Exception:
                pass

    def _check_pending_buy_orders(self):
        """检查所有非阻塞挂出的买单状态，处理成交/超时/撤单

        每次 _scan_and_buy 开始时调用，确保 pending 槽位及时释放。
        """
        if not self._pending_buy_orders:
            return

        today_str = date.today().strftime('%Y-%m-%d')
        now       = datetime.now()
        to_remove = []

        # 批量查一次订单，减少 API 调用
        try:
            all_orders = self._query_orders()
        except Exception as _qe:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] [pending] 查单异常({_qe})，本次跳过检查")
            return
        orders_by_id = {o.get('order_id'): o for o in all_orders}

        for oid, info in list(self._pending_buy_orders.items()):
            code     = info['code']
            deadline = datetime.fromisoformat(info['deadline'])
            o        = orders_by_id.get(oid)

            if o is None:
                # 查不到订单（可能重启后order_id失效）
                if now >= deadline:
                    print(f"[{_now_str()}] [{self.ENGINE_NAME}] [pending] {code} 查不到订单{oid}，已超时，移除")
                    self._failed_buys_today[code] = today_str
                    to_remove.append(oid)
                continue

            status = o.get('status', -1)
            traded = o.get('traded_volume', 0) or 0
            price  = o.get('price', 0) or 0

            if status == ORDER_STATUS_FILLED:
                # 全成交
                actual_price = self._get_actual_fill_price(oid, price)
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] [pending] {code} 确认全成交 qty={traded}")
                self._record_buy_fill(info, traded, actual_price, today_str)
                to_remove.append(oid)

            elif status in (ORDER_STATUS_CANCELLED, ORDER_STATUS_REJECTED):
                # 已撤/废单：处理部分成交
                actual_price = self._get_actual_fill_price(oid, price)
                if traded > 0:
                    print(f"[{_now_str()}] [{self.ENGINE_NAME}] [pending] {code} 撤单后部分成交 qty={traded}")
                    self._record_buy_fill(info, traded, actual_price, today_str)
                else:
                    print(f"[{_now_str()}] [{self.ENGINE_NAME}] [pending] {code} 已撤/废单，未成交")
                self._failed_buys_today[code] = today_str
                to_remove.append(oid)

            elif now >= deadline:
                # 超时：主动撤单，取最终成交量
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] [pending] {code} 买单超时，主动撤单")
                self._cancel_order(oid)
                import time as _time; _time.sleep(2)
                # 重新查一次获取最终成交量
                try:
                    final_orders = self._query_orders()
                    for fo in final_orders:
                        if fo.get('order_id') == oid:
                            traded = fo.get('traded_volume', 0) or 0
                            price  = fo.get('price', 0) or 0
                            break
                except Exception:
                    pass
                actual_price = self._get_actual_fill_price(oid, price)
                if traded > 0:
                    print(f"[{_now_str()}] [{self.ENGINE_NAME}] [pending] {code} 超时后部分成交 qty={traded}，记录持仓")
                    self._record_buy_fill(info, traded, actual_price, today_str)
                else:
                    print(f"[{_now_str()}] [{self.ENGINE_NAME}] [pending] {code} 超时未成交，当日不再重试")
                    self._log_failed_order('buy', code, info['price'], info['intended_qty'], 0, 'timeout',
                                           {'bar_close': info.get('bar_c', 0),
                                            'change_pct': round(info.get('change_pct', 0), 4)})
                self._failed_buys_today[code] = today_str
                to_remove.append(oid)
            # else: 状态为 partial 或仍在排队，且未超时 → 继续等待

        for oid in to_remove:
            del self._pending_buy_orders[oid]

        if to_remove:
            self._save_state()
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] [pending] 本轮处理 {len(to_remove)} 笔，"
                  f"剩余pending={len(self._pending_buy_orders)}")

    def _record_sell_fill(self, code: str, filled_qty: int, fill_price: float,
                          sell_type: str, buy_price: float, days_held: int, pos: dict,
                          intended_price: float = 0):
        """记录卖出成交（支持全量/部分），更新持仓与资金"""
        net_income  = self._calc_sell_income(fill_price, filled_qty)
        cost        = buy_price * filled_qty
        profit      = net_income - cost
        profit_pct  = (profit / cost * 100) if cost > 0 else 0
        commission  = max(fill_price * filled_qty * self.commission_rate, self.min_commission)
        stamp_tax   = fill_price * filled_qty * self.stamp_tax_rate

        # 钉钉通知：卖出成交
        if _NOTIFIER_OK:
            try:
                _notify_sell(code=_strip_suffix(code), price=fill_price, volume=filled_qty,
                             sell_type=sell_type, buy_price=buy_price,
                             days_held=days_held, profit_pct=profit_pct)
            except Exception:
                pass

        self.cash += net_income
        self._log_trade('sell', code, fill_price, filled_qty, sell_type,
                        fee=commission + stamp_tax, days_held=days_held,
                        slip_ref=intended_price if intended_price > 0 else 0)

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
        """三段式卖出：实时买一价 → 最新买一价 → pending_sells

        第1轮（60s）：实时路由获取买一价，优先保证成交
                  — 买一价≥止损价：更优，直接成交
                  — 折价≤阈值：可接受，以市场价成交
                  — 折价>阈值：警告但仍用买一价（止损优先成交）
        第2轮（2分钟）：按最新买一价重挂（处理第1轮部分成交剩余量）
        第3轮（底探）：加入 pending_sells，次日竞价执行
        每轮实时结算部分成交，剩余量进入下一轮。
        """
        remaining = quantity

        # ── 实时价智能路由：获取买一价，判断折价幅度 ─────────────────────────────────
        _sym      = _format_symbol(code)
        _ft       = self._get_full_tick([_sym]).get(_sym, {})
        _bid_list = _ft.get('bidPrice', [])
        _bid      = float(_bid_list[0]) if _bid_list else 0.0
        if _bid <= 0:
            _bid = float(_ft.get('lastPrice', sell_price) or sell_price)
        if _bid <= 0:
            _bid = sell_price
        _sell_slip_max = getattr(config, 'V3_LIVE_SELL_SLIP_MAX', 0.003)
        _sell_slip = (sell_price - _bid) / sell_price if sell_price > 0 and _bid < sell_price else 0.0

        if _bid >= sell_price:
            r1_price = _bid
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] [路由] {code} "
                  f"买一价{_bid:.3f}≥止损价{sell_price:.3f}，用买一价成交（无折价）")
        elif _sell_slip <= _sell_slip_max:
            r1_price = _bid
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] [路由] {code} "
                  f"买一价{_bid:.3f} 折价{_sell_slip:.2%}≤{_sell_slip_max:.2%}，接受实时价")
        else:
            r1_price = _bid   # 折价>阈值，仍用买一价（止损优先成交）
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] [路由][WARN] {code} "
                  f"买一价{_bid:.3f} 折价{_sell_slip:.2%}>{_sell_slip_max:.2%}，"
                  f"超阈值仍强制用买一价（止损优先成交）")
        # ─────────────────────────────────────────────────────────────────────────

        # ── 第1轮：实时买一价限价，60s ────────────────────────────────────────────
        order_id = self._place_sell_order(
            code=code, price=r1_price, volume=remaining,
            remark=f"V3_{sell_type}_{code}_r1"
        )
        if order_id and order_id != -1:
            r1 = self._wait_fill_result(order_id, timeout=60)
            if r1['filled_qty'] > 0:
                self._record_sell_fill(code, r1['filled_qty'], r1['fill_price'],
                                       sell_type, buy_price, days_held, pos,
                                       intended_price=sell_price)
                remaining -= r1['filled_qty']
                pos = dict(pos)
                pos['quantity'] = remaining
            # 若委托未被自动撤单，手动撤单放行下一轮
            if r1['status'] not in ('cancelled',):
                self._cancel_order(order_id)
        if remaining <= 0:
            return

        # ── 第2轮：最新买一价 2分钟 ────────────────────────────────────────────────
        symbol = _format_symbol(code)
        tick = self._get_full_tick([symbol]).get(symbol, {})
        bid_prices = tick.get('bidPrice', [])
        bid_price  = bid_prices[0] if bid_prices else 0
        if bid_price <= 0:
            bid_price = tick.get('lastPrice', sell_price)
        if bid_price <= 0:
            bid_price = sell_price

        print(f"[{_now_str()}] [{self.ENGINE_NAME}] {code} 第1轮未全部成交 "
              f"剩余 {remaining} 股，第2轮按最新买一价 {bid_price:.3f} 重挂...")
        order_id2 = self._place_sell_order(
            code=code, price=bid_price, volume=remaining,
            remark=f"V3_{sell_type}_{code}_r2"
        )
        if order_id2 and order_id2 != -1:
            r2 = self._wait_fill_result(order_id2, timeout=120)
            if r2['filled_qty'] > 0:
                self._record_sell_fill(code, r2['filled_qty'], r2['fill_price'],
                                       sell_type, buy_price, days_held, pos,
                                       intended_price=sell_price)
                remaining -= r2['filled_qty']
                pos = dict(pos)
                pos['quantity'] = remaining
            if r2['status'] not in ('cancelled',):
                self._cancel_order(order_id2)
        if remaining <= 0:
            return

        # ── 第3轮：加入 pending_sells，次日竞价执行 ──────────────────
        print(f"[{_now_str()}] [{self.ENGINE_NAME}] [WARN] {code} 两轮均未全部成交，"
              f"剩余 {remaining} 股加入 pending_sells，次日竞价执行")
        self._log_failed_order('sell', code, sell_price, quantity, quantity - remaining, 'pending',
                               {'sell_type': sell_type, 'remaining': remaining})
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
                # 同时受 self.cash 约束：防止亏损后 remaining_limit 虚高导致超支
                return max(0.0, min(real_cash, remaining_limit, self.cash))
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
        2. （暂时停用）排除日均成交额 < 5亿 的股票
        """
        if not self.rebalance_pool:
            return []

        # 基础过滤：排除已持仓
        candidates = [c for c in self.rebalance_pool if _strip_suffix(c) not in held_codes]
        if not candidates:
            return []

        # 日均成交额过滤（暂时注释：建池阶段未过滤，盘中二次过滤暂停用）
        # today_str = date.today().strftime('%Y-%m-%d')
        # if self._daily_filter_date != today_str or not self._daily_filter_cache:
        #     self._daily_filter_cache = self._filter_by_avg_amount(candidates)
        #     self._daily_filter_date = today_str
        #     self._save_state()
        # return [c for c in candidates if c in self._daily_filter_cache]

        return candidates

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
    def _check_buy_signal(self, code: str, bar: dict, pre_close: float,
                           day_open: float = 0) -> bool:
        """检查买入条件

        条件（全部满足）：
        1. 涨幅 > 阈值（科创板/创业板>2%，主板>1%）
        2. 涨幅 < 防追高阈值（科创/创业板<8%，主板<5%）
        3. 收阳线：bar收盘价 > 当日9:30开盘价（tick.open），对齐回测逻辑
           若 day_open 无效则降级为 bar['close'] > bar['open']
        4. 未涨停（科创板/创业板<19.8%，主板<9.8%）
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
        min_change = self.star_min_change_pct if is_star else self.min_change_pct
        if change_pct <= min_change:
            return False

        # 2. 防追高：涨幅不超过上限
        max_change = self.star_max_change_pct if is_star else self.max_change_pct
        if change_pct >= max_change:
            return False

        # 3. 收阳线：bar收盘价 > 当日9:30开盘价；day_open无效时降级为bar自身阴阳
        ref_open = day_open if day_open > 0 else open_price
        if close <= ref_open:
            return False

        # 4. 未涨停
        _limit_up = self.star_limit_up if is_star else self.limit_up
        if change_pct >= _limit_up:
            return False

        return True

    def _is_star(self, code: str) -> bool:
        """判断是否科创板(688开头)或创业板(30开头)"""
        code_str = str(code).split('.')[0]
        return code_str.startswith('688') or code_str.startswith('30')

    # ------------------------------------------------------------------
    # 交易日志记录
    # ------------------------------------------------------------------
    def _log_trade(self, trade_type, code, price, quantity, reason, fee=0, days_held=0, slip_ref=0):
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
            # 滑点记录（slip_ref=信号参考价）
            if slip_ref > 0:
                if trade_type == 'buy':
                    # 买入滑点：实际成交价 vs K线收盘参考价，正值=追高
                    slip_pct = round((price - slip_ref) / slip_ref * 100, 3)
                else:
                    # 卖出滑点：预期卖出价 vs 实际成交价，正值=卖差了
                    slip_pct = round((slip_ref - price) / slip_ref * 100, 3)
                trade['slip_ref'] = round(slip_ref, 3)
                trade['slip_pct'] = slip_pct
            # 卖出时补充买入价和盈亏（此时 _remove_position 尚未执行，持仓仍存在）
            if trade_type == 'sell':
                pos = next((p for p in self.positions if p.get('code') == code), None)
                if pos:
                    buy_price = pos.get('buy_price', 0)
                    if buy_price > 0:
                        cost = buy_price * quantity
                        pnl = round((price - buy_price) * quantity - fee, 2)
                        pnl_pct = round(pnl / cost * 100, 3)
                        trade['buy_price'] = round(buy_price, 3)
                        trade['pnl'] = pnl
                        trade['pnl_pct'] = pnl_pct
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

    def _log_failed_order(self, order_type: str, code: str, intended_price: float,
                          intended_qty: int, filled_qty: int, failure_reason: str,
                          context: dict = None):
        """记录买卖未成交事件到 failed_orders_v3.json，供每日复盘

        参数：
            order_type:     'buy' 或 'sell'
            code:           股票代码（纯数字）
            intended_price: 委托价格
            intended_qty:   计划委托数量
            filled_qty:     实际成交数量（0=完全未成交）
            failure_reason: 失败原因
                - 'order_failed'    下单接口返回 -1
                - 'timeout'         超时撤单，完全未成交
                - 'partial'         部分成交（买入未全量）
                - 'pending'         两轮卖出均失败，转入次日竞价
                - 'resubmit_timeout' 9:30 重挂卖出超时
                - 'auction_failed'  集合竞价卖出下单失败
            context:        额外市场信息（用于复盘分析）
        """
        try:
            record = {
                "timestamp":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "date":          date.today().strftime('%Y-%m-%d'),
                "type":          order_type,
                "code":          _strip_suffix(code),
                "intended_price": round(intended_price, 3),
                "intended_qty":  intended_qty,
                "filled_qty":    filled_qty,
                "failure_reason": failure_reason,
            }
            if context:
                record["context"] = context
            log_path = os.path.join(os.path.dirname(__file__), '..', self.FAILED_ORDERS_LOG_FILE)
            records = []
            if os.path.exists(log_path):
                try:
                    with open(log_path, 'r', encoding='utf-8') as f:
                        records = json.load(f)
                except Exception:
                    records = []
            records.append(record)
            with open(log_path, 'w', encoding='utf-8') as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] [复盘记录] {order_type} {_strip_suffix(code)} "
                  f"失败({failure_reason}) 计划={intended_qty} 成交={filled_qty}")
        except Exception as e:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 失败订单记录写入异常: {e}")

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
            '_pending_buy_orders': {str(k): v for k, v in self._pending_buy_orders.items()},
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
