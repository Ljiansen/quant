# -*- coding: utf-8 -*-
"""
StrategyV3: 尾盘买入 + 固定止盈止损 全市场轮动策略（含科创板/创业板）

核心逻辑：
- 每半年（1月/7月第一个交易日）构建综合排名调仓池（Top50）
- 每日从调仓池做二次过滤（排除ST/低流动性/停牌）
- 14:30尾盘扫描，从当日可交易池中买入（最多持3只）
- 持仓期间按4种优先级规则检查卖出
- 科创板（688开头）和创业板（30开头）使用相同参数
"""

import pandas as pd
import math
import sys

sys.path.insert(0, 'd:/miniqmt_quant')
import config


class StrategyV3:
    """策略V3：尾盘买入 + 固定止盈止损 全市场轮动策略（含科创板/创业板）

    核心逻辑：
    - 每半年调仓一次，综合排名（成交额+涨跌幅）选Top50作为调仓池
    - 每日从调仓池做二次过滤后形成可交易池
    - 每日14:30，从可交易池中选出满足条件的股票买入（最多持3只）
    - 持仓期间按4种优先级规则检查卖出
    - 科创板（688开头）和创业板（30开头）使用相同参数
    """

    def __init__(self, params=None):
        """初始化策略参数

        params: 可选参数字典，覆盖config中的默认值
        """
        p = params or {}
        self.top_n = p.get('top_n', config.V3_TOP_N)
        self.max_positions = p.get('max_positions', config.V3_MAX_POSITIONS)
        self.min_change_pct = p.get('min_change_pct', config.V3_MIN_CHANGE_PCT)
        self.hard_stop_loss = p.get('hard_stop_loss', config.V3_HARD_STOP_LOSS)
        self.soft_stop_loss = p.get('soft_stop_loss', config.V3_SOFT_STOP_LOSS)
        self.take_profit = p.get('take_profit', config.V3_TAKE_PROFIT)
        self.time_stop_days = p.get('time_stop_days', config.V3_TIME_STOP_DAYS)
        self.commission_rate = p.get('commission_rate', config.V3_COMMISSION_RATE)
        self.min_commission = p.get('min_commission', config.V3_MIN_COMMISSION)
        self.stamp_tax_rate = p.get('stamp_tax_rate', config.V3_STAMP_TAX_RATE)
        self.slippage = p.get('slippage', config.V3_SLIPPAGE)
        self.rebalance_months = p.get('rebalance_months', config.V3_REBALANCE_MONTHS)
        self.rebalance_lookback = p.get('rebalance_lookback', config.V3_REBALANCE_LOOKBACK)
        self.daily_min_amount = p.get('daily_min_amount', config.V3_DAILY_MIN_AMOUNT)
        self.daily_amount_days = p.get('daily_amount_days', config.V3_DAILY_AMOUNT_DAYS)
        # 科创板/创业板独立参数
        self.star_min_change_pct = p.get('star_min_change_pct', config.V3_STAR_MIN_CHANGE_PCT)
        self.star_take_profit = p.get('star_take_profit', config.V3_STAR_TAKE_PROFIT)
        self.star_hard_stop_loss = p.get('star_hard_stop_loss', config.V3_STAR_HARD_STOP_LOSS)
        self.star_soft_stop_loss = p.get('star_soft_stop_loss', config.V3_STAR_SOFT_STOP_LOSS)
        self.star_time_stop_days = p.get('star_time_stop_days', config.V3_STAR_TIME_STOP_DAYS)
        self.star_limit_up = p.get('star_limit_up', config.V3_STAR_LIMIT_UP)
        # 移动止盈参数
        self.trailing_activate = p.get('trailing_activate', config.V3_TRAILING_ACTIVATE)
        self.trailing_stop = p.get('trailing_stop', config.V3_TRAILING_STOP)
        self.star_trailing_activate = p.get('star_trailing_activate', config.V3_STAR_TRAILING_ACTIVATE)
        self.star_trailing_stop = p.get('star_trailing_stop', config.V3_STAR_TRAILING_STOP)
        self.name = 'StrategyV3'

    # ------------------------------------------------------------------
    # 辅助：判断是否科创板或创业板
    # ------------------------------------------------------------------
    def _is_star(self, code: str) -> bool:
        """判断是否为科创板或创业板（使用相同参数）"""
        code_str = str(code).split('.')[0]
        return code_str.startswith('688') or code_str.startswith('30')

    # ------------------------------------------------------------------
    # 半年调仓池构建
    # ------------------------------------------------------------------
    def build_rebalance_pool(self, all_data: dict, current_date: str,
                              trading_dates: list) -> list:
        """构建半年调仓池（在调仓日调用）

        综合排名算法：
            综合排名 = 成交额排名 × 0.5 + 涨跌幅排名 × 0.5
        两个排名均为升序（排名1=最好），综合排名越小越好，取前 top_n 只。

        参数：
            all_data: {code: DataFrame}，每个DataFrame含 date/open/high/low/close/volume/amount 列
            current_date: 当前调仓日 'YYYY-MM-DD'
            trading_dates: 完整交易日历列表 ['YYYY-MM-DD', ...]

        过滤规则（与每日二次过滤无关，仅进行基础过滤）：
            1. 排除北交所（8/4开头）
            2. 只保留主板(60/00)、创业板(30)、科创板(688)
            3. 排除新股（可用历史数据行数 < 60）

        返回：
            list: 股票代码列表，长度 <= top_n
        """
        if not all_data or not trading_dates:
            return []

        # 找到当前日期在交易日历中的索引
        if current_date not in trading_dates:
            return []
        cur_idx = trading_dates.index(current_date)

        # 回看最多 rebalance_lookback 个交易日
        lookback_start = max(0, cur_idx - self.rebalance_lookback)
        lookback_dates_set = set(trading_dates[lookback_start:cur_idx + 1])

        results = []
        for code, df in all_data.items():
            code_str = str(code)

            # 1. 排除北交所（8/4开头）
            if code_str.startswith('8') or code_str.startswith('4'):
                continue

            # 2. 只保留主板(60/00)、创业板(30)、科创板(688)
            if not (code_str.startswith('60') or
                    code_str.startswith('00') or
                    code_str.startswith('30') or
                    code_str.startswith('688')):
                continue

            # 截取回看区间数据
            date_strs = df['date'].dt.strftime('%Y-%m-%d')
            mask = date_strs.isin(lookback_dates_set)
            period_df = df[mask].copy()

            # 3. 排除新股（历史数据不足60行）
            total_rows = len(df[df['date'] <= pd.to_datetime(current_date)])
            if total_rows < 60:
                continue

            if period_df.empty or len(period_df) < 2:
                continue

            # 计算总成交额（过去6个月）
            total_amount = float(period_df['amount'].sum())

            # 计算累计涨跌幅：(最后收盘 - 最早收盘) / 最早收盘
            period_df = period_df.sort_values('date')
            first_close = float(period_df.iloc[0]['close'])
            last_close = float(period_df.iloc[-1]['close'])
            if first_close <= 0:
                continue
            cum_pct = (last_close - first_close) / first_close

            results.append({
                'code': code_str,
                'total_amount': total_amount,
                'cum_pct': cum_pct,
            })

        if not results:
            return []

        result_df = pd.DataFrame(results)

        # 排名：越大越好，所以 ascending=False → rank升序=排名1最好
        result_df['amount_rank'] = result_df['total_amount'].rank(
            ascending=False, method='min'
        )
        result_df['pct_rank'] = result_df['cum_pct'].rank(
            ascending=False, method='min'
        )

        # 综合排名越小越好
        result_df['composite_rank'] = (
            result_df['amount_rank'] * 0.5 + result_df['pct_rank'] * 0.5
        )

        # 取综合排名前 top_n
        result_df = result_df.sort_values('composite_rank').head(self.top_n)

        return result_df['code'].tolist()

    # ------------------------------------------------------------------
    # 每日二次过滤
    # ------------------------------------------------------------------
    def daily_filter(self, pool: list, all_data: dict,
                     current_date: str, market_df: pd.DataFrame) -> list:
        """每日从调仓池做二次过滤，得到当日可交易池

        过滤条件：
            1. 排除ST股票（stock_names或market_df中name含"ST"，不区分大小写）
            2. 排除近 daily_amount_days 天日均成交额 < daily_min_amount 的股票
            3. 排除当日停牌（volume == 0）

        参数：
            pool: 调仓池股票代码列表
            all_data: {code: DataFrame}
            current_date: 当前日期 'YYYY-MM-DD'
            market_df: 当日全市场行情 DataFrame（含 code, name, volume, amount 列）

        返回：
            list: 过滤后的股票代码列表
        """
        if not pool:
            return []

        # 构建当日行情快查字典
        daily_dict = {}
        if market_df is not None and not market_df.empty:
            for _, row in market_df.iterrows():
                c = str(row.get('code', ''))
                daily_dict[c] = row.to_dict()

        date_dt = pd.to_datetime(current_date)
        result = []

        for code in pool:
            code_str = str(code)
            day_info = daily_dict.get(code_str, {})

            # 条件1：排除ST（通过当日市场行情的name字段）
            name = str(day_info.get('name', ''))
            if 'ST' in name.upper():
                continue

            # 条件2：排除近N天日均成交额 < 5亿
            if code_str in all_data:
                df = all_data[code_str]
                hist = df[df['date'] < date_dt].tail(self.daily_amount_days)
                if not hist.empty:
                    avg_amount = float(hist['amount'].mean())
                    if avg_amount < self.daily_min_amount:
                        continue
                # 若历史数据不足，不过滤（可能是回测早期）

            # 条件3：排除当日停牌（volume == 0）
            volume = day_info.get('volume', None)
            if volume is not None and float(volume) == 0:
                continue

            result.append(code_str)

        return result

    # ------------------------------------------------------------------
    # 原有 build_stock_pool（保留兼容性，实盘/非调仓模式使用）
    # ------------------------------------------------------------------
    def build_stock_pool(self, daily_market_df: pd.DataFrame) -> list:
        """构建当日选股池（实盘/非调仓场景兼容用，回测中由引擎调用 build_rebalance_pool+daily_filter）

        过滤规则:
            1. 排除ST股票（name包含"ST"，不区分大小写）
            2. 排除停牌（volume == 0）
            3. 排除北交所（code以'8'或'4'开头）
            4. 保留主板（60/00开头）+ 创业板（30开头）+ 科创板（688开头）
            5. 排除新股（days_listed < 60）
            6. 按成交额降序排序，取前 top_n 只

        返回:
            list: 股票代码列表
        """
        if daily_market_df is None or daily_market_df.empty:
            return []

        df = daily_market_df.copy()
        df['code'] = df['code'].astype(str)

        # 1. 排除ST股票
        if 'name' in df.columns:
            df = df[~df['name'].str.contains('ST', case=False, na=False)]

        # 2. 排除停牌
        if 'volume' in df.columns:
            df = df[df['volume'] != 0]

        # 3. 排除北交所
        df = df[~df['code'].str.startswith('8')]
        df = df[~df['code'].str.startswith('4')]

        # 4. 保留主板(60/00) + 创业板(30) + 科创板(688)
        df = df[
            df['code'].str.startswith('60') |
            df['code'].str.startswith('00') |
            df['code'].str.startswith('30') |
            df['code'].str.startswith('688')
        ]

        # 5. 排除新股（上市不足60个交易日）
        if 'days_listed' in df.columns:
            df = df[df['days_listed'] >= 60]

        # 6. 按成交额降序排序，取前 top_n 只
        if 'amount' in df.columns:
            df = df.sort_values(by='amount', ascending=False)

        df = df.head(self.top_n)
        return df['code'].tolist()

    # ------------------------------------------------------------------
    # 买入信号检查（区分科创板/创业板 和 主板）
    # ------------------------------------------------------------------
    def check_buy_signal(self, code: str, bar: dict, pre_close: float) -> bool:
        """检查单只股票是否满足买入条件（当日尾盘14:30数据）

        参数:
            code: 股票代码（纯数字）
            bar: 当日行情 dict，含 open, high, low, close, volume, amount
            pre_close: 昨日收盘价

        买入条件（全部满足）:
            1. 涨幅 > 阈值（科创板/创业板>2%，主板>1%）
               涨幅 = (close - pre_close) / pre_close（当天数据，近似尾盘扫描）
            2. 收阳线: close > open
            3. 未涨停:
               - 科创板/创业板(688/30开头): 涨幅 < 19.8%
               - 主板(60/00开头): 涨幅 < 9.8%

        返回:
            bool: 是否满足买入条件
        """
        # 边界保护：pre_close 为 0 时无法计算涨幅
        if pre_close is None or pre_close == 0:
            return False

        # 边界保护：volume 为 0 跳过（停牌）
        if bar.get('volume', 0) == 0:
            return False

        close = bar.get('close', 0)
        open_price = bar.get('open', 0)

        # 1. 涨幅判断（当天数据）
        change_pct = (close - pre_close) / pre_close

        # 根据板块使用不同涨幅阈值
        if self._is_star(code):
            # 科创板/创业板：涨幅 > 2%
            if change_pct <= self.star_min_change_pct:
                return False
        else:
            # 主板：涨幅 > 1%
            if change_pct <= self.min_change_pct:
                return False

        # 2. 收阳线: close > open
        if close <= open_price:
            return False

        # 3. 未涨停（不同板块不同阈值）
        limit_up = self.get_limit_up_threshold(code)
        if change_pct >= limit_up:
            return False

        return True

    # ------------------------------------------------------------------
    # 卖出信号检查（区分科创板/创业板 和 主板止盈参数）
    # ------------------------------------------------------------------
    def check_sell_signals(self, position: dict, bar: dict) -> tuple:
        """检查持仓股的卖出条件（按优先级从高到低）

        参数:
            position: 持仓记录 dict
                {
                    'code': str,         # 股票代码
                    'buy_price': float,  # 买入成本价（含滑点）
                    'buy_date': str,     # 买入日期 'YYYY-MM-DD'
                    'quantity': int,     # 持仓数量
                    'days_held': int,    # 已持仓天数（不含买入当天）
                }
            bar: 当日行情 dict
                {
                    'date': str,
                    'open': float,
                    'high': float,
                    'low': float,
                    'close': float,
                }

        卖出规则优先级:
            1. 硬止损: bar['low'] <= buy_price * (1 - hard_stop_loss)
               → 立即卖出，按 bar['low'] 价成交（当天执行）
            2. 阴跌止损: bar['close'] < buy_price * (1 - soft_stop_loss) AND close < open
               → 下一bar开盘卖出（pending模式，次日开盘价执行）
            3. 固定止盈: bar['high'] >= buy_price * (1 + take_profit)
               → 下一bar开盘卖出（pending模式，次日开盘价执行）
               注：科创板/创业板止盈15%，主板止盈5%
            4. 时间止损: days_held >= time_stop_days AND close <= buy_price
               → 下一bar开盘卖出（pending模式，次日开盘价执行）

        注意:
            - T+1限制：days_held == 0 时（买入当天）不检查卖出
            - 多个条件可能同时满足，按优先级只触发第一个

        返回:
            tuple: (should_sell, sell_type, execution_mode, sell_price)
            - should_sell: bool
            - sell_type: 'hard_stop'/'soft_stop'/'take_profit'/'time_stop'/None
            - execution_mode: 'immediate'（当前bar执行）/'pending'（次日开盘执行）/None
            - sell_price: float（仅 immediate 模式有值，pending 模式为 None）
        """
        # T+1限制：买入当天不检查卖出
        if position.get('days_held', 0) == 0:
            return False, None, None, None

        buy_price = position.get('buy_price', 0)
        if buy_price is None or buy_price == 0:
            return False, None, None, None

        code = position.get('code', '')
        low = bar.get('low', 0)
        high = bar.get('high', 0)
        close = bar.get('close', 0)
        open_price = bar.get('open', 0)
        days_held = position.get('days_held', 0)

        # 根据板块选择止损/止盈参数
        if self._is_star(code):
            hard_sl = self.star_hard_stop_loss
            soft_sl = self.star_soft_stop_loss
            tp = self.star_take_profit        # 已废弃，保留兼容
            time_stop = self.star_time_stop_days
            trail_act = self.star_trailing_activate
            trail_pct = self.star_trailing_stop
        else:
            hard_sl = self.hard_stop_loss
            soft_sl = self.soft_stop_loss
            tp = self.take_profit             # 已废弃，保留兼容
            time_stop = self.time_stop_days
            trail_act = self.trailing_activate
            trail_pct = self.trailing_stop

        # 1. 硬止损（最高优先级）：当天立即执行，按止损价成交
        # 原理：盘中监控到价格触及止损线就立即下单，按止损价成交更贴近实际
        # 若开盘已跳空低开（open < stop_price），则只能按开盘价成交
        hard_stop_price = buy_price * (1 - hard_sl)
        if low <= hard_stop_price:
            sell_price = max(hard_stop_price, open_price)  # 跳空低开则按开盘价，否则按止损价
            return True, 'hard_stop', 'immediate', sell_price

        # 2. 阴跌止损：信号当天产生，次日开盘价执行（pending模式）
        soft_stop_price = buy_price * (1 - soft_sl)
        if close < soft_stop_price and close < open_price:
            return True, 'soft_stop', 'pending', None

        # 3. 移动止盈：更新持仓最高价，激活后从最高价回撤 trail_pct 触发
        current_high  = bar.get('high', 0)
        highest_price = max(position.get('highest_price', buy_price), current_high)
        position['highest_price'] = highest_price   # 更新持仓中的最高价记录

        if highest_price >= buy_price * (1 + trail_act):
            trail_trigger = highest_price * (1 - trail_pct)
            if low <= trail_trigger:
                sell_price = max(trail_trigger, open_price)
                print(f"[StrategyV3] 移动止盈触发: {code} "
                      f"最高价={highest_price:.3f} 回撤价={trail_trigger:.3f} 当前低={low:.3f}")
                return True, 'trailing_stop', 'immediate', sell_price

        # 4. 时间止损（最低优先级）：信号当天产生，次日开盘价执行（pending模式）
        if days_held >= time_stop and close <= buy_price:
            return True, 'time_stop', 'pending', None

        # 无卖出信号
        return False, None, None, None

    # ------------------------------------------------------------------
    # 计算买入股数
    # ------------------------------------------------------------------
    def calculate_buy_volume(self, available_cash: float, current_positions: int, price: float) -> int:
        """计算买入股数

        参数:
            available_cash: 当前可用资金
            current_positions: 当前持仓股票数量
            price: 买入价格

        计算逻辑:
            空仓位数 = max_positions - current_positions
            单只分配金额 = available_cash / 空仓位数
            实际买入价 = price * (1 + slippage)  # 含滑点
            买入股数 = floor(单只分配金额 / 实际买入价 / 100) * 100

            如果买入股数 < 100，返回 0（资金不足）

        返回:
            int: 买入股数（100的整数倍），资金不足返回0
        """
        if price is None or price <= 0:
            return 0
        if available_cash is None or available_cash <= 0:
            return 0

        empty_slots = self.max_positions - current_positions
        if empty_slots <= 0:
            return 0

        alloc_per_stock = available_cash / empty_slots
        actual_price = price * (1 + self.slippage)
        volume = math.floor(alloc_per_stock / actual_price / 100) * 100

        if volume < 100:
            return 0
        return volume

    # ------------------------------------------------------------------
    # 计算买入成本
    # ------------------------------------------------------------------
    def calculate_buy_cost(self, price: float, volume: int) -> tuple:
        """计算买入总成本

        返回:
            tuple: (actual_buy_price, total_cost, commission)
            - actual_buy_price: 含滑点的实际买入价 = price * (1 + slippage)
            - commission: max(actual_buy_price * volume * commission_rate, min_commission)
            - total_cost: actual_buy_price * volume + commission
        """
        actual_buy_price = price * (1 + self.slippage)
        amount = actual_buy_price * volume
        commission = max(amount * self.commission_rate, self.min_commission)
        total_cost = amount + commission
        return actual_buy_price, total_cost, commission

    # ------------------------------------------------------------------
    # 计算卖出收入
    # ------------------------------------------------------------------
    def calculate_sell_income(self, price: float, volume: int) -> tuple:
        """计算卖出实际收入

        返回:
            tuple: (actual_sell_price, net_income, commission, stamp_tax)
            - actual_sell_price: 含滑点的实际卖出价 = price * (1 - slippage)
            - commission: max(actual_sell_price * volume * commission_rate, min_commission)
            - stamp_tax: actual_sell_price * volume * stamp_tax_rate
            - net_income: actual_sell_price * volume - commission - stamp_tax
        """
        actual_sell_price = price * (1 - self.slippage)
        amount = actual_sell_price * volume
        commission = max(amount * self.commission_rate, self.min_commission)
        stamp_tax = amount * self.stamp_tax_rate
        net_income = amount - commission - stamp_tax
        return actual_sell_price, net_income, commission, stamp_tax

    # ------------------------------------------------------------------
    # 涨停阈值
    # ------------------------------------------------------------------
    def get_limit_up_threshold(self, code: str) -> float:
        """获取涨停阈值

        科创板/创业板(688/30开头): 19.8%
        主板(60/00开头): 9.8%

        返回: float (如 0.098 或 0.198)
        """
        code_str = str(code)
        if self._is_star(code_str):
            # 科创板/创业板涨停阈值
            return self.star_limit_up
        # 主板(60/00开头)默认返回 9.8%
        return 0.098
