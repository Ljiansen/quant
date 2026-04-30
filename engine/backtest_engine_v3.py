# -*- coding: utf-8 -*-
"""
V3策略专用回测引擎 - 全市场轮动组合回测

核心流程:
1. 预下载全市场日线数据（带缓存）
2. 逐日循环：卖出检查 → 选股池构建 → 买入检查 → 净值计算
3. 支持4种卖出规则的优先级处理（硬止损/阴跌止损/止盈/时间止损）
"""

import copy
from datetime import datetime, timedelta

import numpy as np
import pandas as pd


class BacktestEngineV3:
    """V3策略专用回测引擎 - 全市场轮动组合回测"""

    def __init__(self, strategy, data_manager, initial_capital=None):
        """
        strategy: StrategyV3 实例
        data_manager: DataManager 实例
        initial_capital: 初始资金，默认从 config.V3_INITIAL_CAPITAL 读取
        """
        self.strategy = strategy
        self.dm = data_manager

        # 延迟导入 config，避免循环依赖
        import config

        self.initial_capital = initial_capital or config.V3_INITIAL_CAPITAL
        self.all_data = {}          # {code: DataFrame, ...}
        self.stock_names = {}       # {code: name, ...}
        self.trading_dates = []     # 交易日历 ['YYYY-MM-DD', ...]

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def run(self, start_date: str, end_date: str) -> dict:
        """运行V3全市场轮动回测

        参数:
            start_date: 回测开始日期 'YYYYMMDD'
            end_date: 回测结束日期 'YYYYMMDD'
        """
        # 日期格式转换
        start_date_fmt = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
        end_date_fmt = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"

        # Phase 1: 数据准备
        self._prepare_data(start_date, end_date)

        if not self.all_data:
            print("[BacktestEngineV3] 数据准备失败，无法回测")
            return self._empty_result()

        # 过滤交易日历到回测区间
        trading_dates = [
            d for d in self.trading_dates
            if start_date_fmt <= d <= end_date_fmt
        ]
        if not trading_dates:
            print("[BacktestEngineV3] 回测区间内无交易日")
            return self._empty_result()

        print(f"[BacktestEngineV3] 回测区间: {start_date_fmt} ~ {end_date_fmt}, "
              f"共 {len(trading_dates)} 个交易日")

        # 初始化状态
        cash = float(self.initial_capital)
        positions = {}              # {code: position_dict}
        trades = []
        daily_nav_list = []
        positions_history = []
        pending_sells = []          # 待下一交易日开盘执行的卖出
        rebalance_pool = []         # 当前调仓池（半年调仓，在调仓日重建）

        sell_type_stats = {
            'hard_stop':     {'count': 0, 'total_pnl': 0.0},
            'soft_stop':     {'count': 0, 'total_pnl': 0.0},
            'trailing_stop': {'count': 0, 'total_pnl': 0.0},
            'time_stop':     {'count': 0, 'total_pnl': 0.0},
        }

        # Phase 2: 逐日回测循环
        for date in trading_dates:
            date_dt = pd.to_datetime(date)

            # ---- step 1: 执行上一日的 pending 卖出信号 ----
            executed_codes = set()
            for pending in pending_sells:
                code = pending['code']
                if code not in positions:
                    continue

                bar_open = self._get_bar(code, date)
                if bar_open is None:
                    continue

                open_price = bar_open['open']
                quantity = pending['quantity']

                # 计算卖出收入
                actual_sell_price, net_income, commission, stamp_tax = \
                    self.strategy.calculate_sell_income(open_price, quantity)

                cash += net_income

                # 计算盈亏
                pos = positions[code]
                buy_cost_total = pos['buy_price'] * quantity + pos.get('buy_commission', 0)
                pnl = net_income - buy_cost_total
                pnl_pct = pnl / buy_cost_total if buy_cost_total > 0 else 0.0

                # 记录交易
                trades.append({
                    'date': date,
                    'code': code,
                    'name': pos.get('name', ''),
                    'direction': 'sell',
                    'price': round(actual_sell_price, 4),
                    'volume': quantity,
                    'amount': round(actual_sell_price * quantity, 4),
                    'commission': round(commission, 4),
                    'stamp_tax': round(stamp_tax, 4),
                    'sell_type': pending['sell_type'],
                    'pnl': round(pnl, 4),
                    'pnl_pct': round(pnl_pct, 6),
                    'days_held': pos['days_held'],
                })

                # 更新统计
                st = pending['sell_type']
                if st in sell_type_stats:
                    sell_type_stats[st]['count'] += 1
                    sell_type_stats[st]['total_pnl'] += pnl

                del positions[code]
                executed_codes.add(code)

            pending_sells = [p for p in pending_sells if p['code'] not in executed_codes]

            # ---- step 2: 检查当前持仓的卖出条件 ----
            codes_to_remove = []
            for code, pos in list(positions.items()):
                # T+1限制：买入当天不检查卖出
                if pos.get('days_held', 0) == 0:
                    continue

                bar = self._get_bar(code, date)
                if bar is None:
                    continue

                should_sell, sell_type, execution_mode, sell_price = \
                    self.strategy.check_sell_signals(pos, bar)

                if not should_sell:
                    continue

                if execution_mode == 'immediate':
                    quantity = pos['quantity']
                    actual_sell_price, net_income, commission, stamp_tax = \
                        self.strategy.calculate_sell_income(sell_price, quantity)

                    cash += net_income

                    buy_cost_total = pos['buy_price'] * quantity + pos.get('buy_commission', 0)
                    pnl = net_income - buy_cost_total
                    pnl_pct = pnl / buy_cost_total if buy_cost_total > 0 else 0.0

                    trades.append({
                        'date': date,
                        'code': code,
                        'name': pos.get('name', ''),
                        'direction': 'sell',
                        'price': round(actual_sell_price, 4),
                        'volume': quantity,
                        'amount': round(actual_sell_price * quantity, 4),
                        'commission': round(commission, 4),
                        'stamp_tax': round(stamp_tax, 4),
                        'sell_type': sell_type,
                        'pnl': round(pnl, 4),
                        'pnl_pct': round(pnl_pct, 6),
                        'days_held': pos['days_held'],
                    })

                    if sell_type in sell_type_stats:
                        sell_type_stats[sell_type]['count'] += 1
                        sell_type_stats[sell_type]['total_pnl'] += pnl

                    codes_to_remove.append(code)

                elif execution_mode == 'pending':
                    pending_sells.append({
                        'code': code,
                        'quantity': pos['quantity'],
                        'sell_type': sell_type,
                    })

            for code in codes_to_remove:
                if code in positions:
                    del positions[code]

            # ---- step 3: 检查是否调仓日，重建调仓池 ----
            # 调仓日：1月和7月的第一个交易日
            is_rebalance_day = self._is_rebalance_day(date, trading_dates)
            if is_rebalance_day or not rebalance_pool:
                # 调仓日或调仓池为空（回测首日）：重建池
                rebalance_pool = self.strategy.build_rebalance_pool(
                    self.all_data, date, self.trading_dates
                )
                print(f"[BacktestEngineV3] {date} 调仓日，重建调仓池 {len(rebalance_pool)} 只")

            # ---- step 4: 每日二次过滤，得到当日可交易池 ----
            daily_market_df = self._get_daily_market(date, rebalance_pool)
            tradable_pool = self.strategy.daily_filter(
                rebalance_pool, self.all_data, date, daily_market_df
            )

            # ---- step 5: 检查买入（尾盘买入，等效于当日 close） ----
            if len(positions) < self.strategy.max_positions:
                for code in tradable_pool:
                    if code in positions:
                        continue

                    bar = self._get_bar(code, date)
                    if bar is None:
                        continue

                    pre_close = self._get_pre_close(code, date)
                    if not self.strategy.check_buy_signal(code, bar, pre_close):
                        continue

                    close_price = bar['close']
                    volume = self.strategy.calculate_buy_volume(
                        cash, len(positions), close_price
                    )
                    if volume <= 0:
                        continue

                    actual_buy_price, total_cost, commission = \
                        self.strategy.calculate_buy_cost(close_price, volume)

                    if total_cost > cash:
                        continue

                    cash -= total_cost

                    name = self.stock_names.get(code, '')
                    positions[code] = {
                        'code': code,
                        'name': name,
                        'buy_price': actual_buy_price,
                        'buy_date': date,
                        'quantity': volume,
                        'days_held': 0,
                        'buy_commission': commission,
                        'highest_price': actual_buy_price,  # 移动止盈跟踪最高价，初始化为买入价
                    }

                    trades.append({
                        'date': date,
                        'code': code,
                        'name': name,
                        'direction': 'buy',
                        'price': round(actual_buy_price, 4),
                        'volume': volume,
                        'amount': round(actual_buy_price * volume, 4),
                        'commission': round(commission, 4),
                        'stamp_tax': 0,
                        'sell_type': None,
                        'pnl': None,
                        'pnl_pct': None,
                        'days_held': None,
                    })

                    if len(positions) >= self.strategy.max_positions:
                        break

            # ---- step 6: 更新持仓天数 ----
            for pos in positions.values():
                pos['days_held'] += 1

            # ---- step 7: 计算当日净値 ----
            holdings_value = 0.0
            for code, pos in positions.items():
                bar = self._get_bar(code, date)
                if bar is not None:
                    close_price = bar['close']
                    holdings_value += pos['quantity'] * close_price

            nav = cash + holdings_value
            daily_nav_list.append({'date': date, 'nav': round(nav, 4)})

            # 记录持仓快照
            positions_history.append({
                'date': date,
                'positions': [
                    {
                        'code': p['code'],
                        'name': p['name'],
                        'quantity': p['quantity'],
                        'buy_price': p['buy_price'],
                        'days_held': p['days_held'],
                    }
                    for p in positions.values()
                ]
            })

        # Phase 3: 计算绩效指标
        daily_nav_df = pd.DataFrame(daily_nav_list)
        if not daily_nav_df.empty:
            daily_nav_df['date'] = pd.to_datetime(daily_nav_df['date'])

        metrics = self._calculate_metrics(daily_nav_df, trades)

        # V3 特有指标：卖出类型统计
        sell_type_avg_pnl = {}
        for st, data in sell_type_stats.items():
            count = data['count']
            avg = data['total_pnl'] / count if count > 0 else 0.0
            sell_type_avg_pnl[st] = {
                'count': count,
                'avg_pnl': round(avg, 2),
            }
        metrics['sell_type_stats'] = sell_type_avg_pnl

        # V3 特有指标：平均持仓天数
        sell_trades = [
            t for t in trades
            if t['direction'] == 'sell' and t.get('days_held') is not None
        ]
        if sell_trades:
            avg_holding_days = sum(t['days_held'] for t in sell_trades) / len(sell_trades)
            metrics['avg_holding_days'] = round(avg_holding_days, 2)
        else:
            metrics['avg_holding_days'] = 0.0

        # V3 特有指标：平均每笔盈利金额
        if sell_trades:
            avg_profit_per_trade = sum(
                (t.get('pnl') or 0) for t in sell_trades
            ) / len(sell_trades)
            metrics['avg_profit_per_trade'] = round(avg_profit_per_trade, 2)
        else:
            metrics['avg_profit_per_trade'] = 0.0

        return {
            'daily_nav': daily_nav_df,
            'trades': trades,
            'metrics': metrics,
            'positions_history': positions_history,
            'sell_type_stats': sell_type_avg_pnl,
        }

    # ------------------------------------------------------------------
    # 数据准备
    # ------------------------------------------------------------------
    def _prepare_data(self, start_date, end_date):
        """预下载回测所需的全市场数据

        步骤:
        1. 获取A股股票列表
        2. 过滤：保留主板(60/00)、创业板(30)、科创板(688)
        3. 排除北交所(8/4开头)
        4. 批量下载日线数据（开始日期比 start_date 早约210个自然日，支持6个月回看）
        5. 构建数据字典和交易日历
        """
        print("[BacktestEngineV3] 开始数据准备...")

        # 本地数据源特殊处理
        if hasattr(self.dm, 'source') and self.dm.source == 'local':
            print("[BacktestEngineV3] 使用本地数据源")
            codes = self.dm._provider.get_stock_list()
            # 过滤：保留主板(60/00)、创业板(30)、科创板(688)
            codes = [c for c in codes if c.startswith(('60', '00', '30', '688'))]
            print(f"[BacktestEngineV3] 候选股票池: {len(codes)} 只")
            self.stock_names = {code: '' for code in codes}

            start_dt = datetime.strptime(start_date, '%Y%m%d')
            extended_start = (start_dt - timedelta(days=210)).strftime('%Y%m%d')

            print(f"[BacktestEngineV3] 批量加载本地数据: {extended_start} ~ {end_date}")
            self.all_data = self.dm.get_multi_stock_daily(
                codes, extended_start, end_date, use_cache=False
            )

            # 构建交易日历
            if self.all_data:
                sample_df = next(iter(self.all_data.values()))
                sample_df = sample_df.sort_values('date').reset_index(drop=True)
                self.trading_dates = sample_df['date'].dt.strftime('%Y-%m-%d').tolist()
                print(f"[BacktestEngineV3] 数据准备完成, "
                      f"成功 {len(self.all_data)}/{len(codes)} 只, "
                      f"交易日历 {len(self.trading_dates)} 天")
            else:
                self.trading_dates = []
                print("[BacktestEngineV3] 数据准备完成，但未获取到任何数据")
            return

        # 1. 获取A股股票列表（使用 data_manager，避免直接依赖 baostock 套接字错误）
        stock_list_df = self.dm.get_all_stock_spot()

        # 备用方案
        if stock_list_df.empty or 'code' not in stock_list_df.columns:
            print("[BacktestEngineV3] get_all_stock_spot 为空，尝试备用方案 get_stock_list")
            try:
                stock_list_df = self.dm._provider.get_stock_list()
                if stock_list_df is not None and not stock_list_df.empty:
                    if '代码' in stock_list_df.columns and '名称' in stock_list_df.columns:
                        stock_list_df = stock_list_df.rename(
                            columns={'代码': 'code', '名称': 'name'}
                        )
            except Exception as e:
                print(f"[BacktestEngineV3] 备用方案获取股票列表也失败: {e}")
                return

        if stock_list_df.empty or 'code' not in stock_list_df.columns:
            print("[BacktestEngineV3] 无法获取股票列表")
            return

        # 确保 code 为字符串
        stock_list_df['code'] = stock_list_df['code'].astype(str).str.strip()

        # 2. 过滤：保留主板(60/00)、创业板(30)、科创板(688)
        stock_list_df = stock_list_df[
            stock_list_df['code'].str.startswith('60') |
            stock_list_df['code'].str.startswith('00') |
            stock_list_df['code'].str.startswith('30') |
            stock_list_df['code'].str.startswith('688')
        ]

        # 3. ST股票在策略层过滤，此处不排除（保留回测所需历史数据）

        codes = stock_list_df['code'].tolist()
        self.stock_names = dict(
            zip(stock_list_df['code'], stock_list_df.get('name', pd.Series(dtype=str)))
        )

        print(f"[BacktestEngineV3] 候选股票池: {len(codes)} 只")

        # 4. 批量下载日线数据（扩展开始日期约210天，支持6个月回看调仓）
        start_dt = datetime.strptime(start_date, '%Y%m%d')
        extended_start = (start_dt - timedelta(days=210)).strftime('%Y%m%d')

        print(f"[BacktestEngineV3] 批量下载日线: {extended_start} ~ {end_date}")
        self.all_data = self.dm.get_multi_stock_daily(
            codes, extended_start, end_date, use_cache=True
        )

        # 5. 构建交易日历
        if self.all_data:
            sample_df = next(iter(self.all_data.values()))
            sample_df = sample_df.sort_values('date').reset_index(drop=True)
            self.trading_dates = sample_df['date'].dt.strftime('%Y-%m-%d').tolist()
            print(f"[BacktestEngineV3] 数据准备完成, "
                  f"成功 {len(self.all_data)}/{len(codes)} 只, "
                  f"交易日历 {len(self.trading_dates)} 天")
        else:
            self.trading_dates = []
            print("[BacktestEngineV3] 数据准备完成，但未获取到任何数据")

    # ------------------------------------------------------------------
    # 调仓日判断
    # ------------------------------------------------------------------
    def _is_rebalance_day(self, date: str, trading_dates: list) -> bool:
        """判断是否是调仓日（每年1月和7月的第一个交易日）

        判断逻辑：
            - 当前日期属于调仓月份（配置中 V3_REBALANCE_MONTHS）
            - 且当前日期是该月第一个交易日

        参数：
            date: 当前日期 'YYYY-MM-DD'
            trading_dates: 回测区间的交易日历列表

        返回：
            bool: 是否是调仓日
        """
        import config as _cfg
        rebalance_months = getattr(_cfg, 'V3_REBALANCE_MONTHS', [1, 7])

        date_dt = pd.to_datetime(date)
        # 判断当前月是否是调仓月份
        if date_dt.month not in rebalance_months:
            return False

        # 判断是否是该月内第一个交易日
        # 该月内第一个交易日 = trading_dates 中所有属于该年该月的最小日期
        same_month_dates = [
            d for d in trading_dates
            if pd.to_datetime(d).year == date_dt.year
            and pd.to_datetime(d).month == date_dt.month
        ]
        if not same_month_dates:
            return False
        first_trading_day = min(same_month_dates)
        return date == first_trading_day

    # ------------------------------------------------------------------
    # 数据查询辅助
    # ------------------------------------------------------------------
    def _get_bar(self, code, date) -> dict:
        """获取某只股票在某交易日的 bar 数据

        返回 dict: {date, open, high, low, close, volume, amount}
        无数据返回 None
        """
        if code not in self.all_data:
            return None

        df = self.all_data[code]
        date_dt = pd.to_datetime(date)
        row = df[df['date'] == date_dt]

        if row.empty:
            return None

        return {
            'date': date,
            'open': float(row.iloc[0]['open']),
            'high': float(row.iloc[0]['high']),
            'low': float(row.iloc[0]['low']),
            'close': float(row.iloc[0]['close']),
            'volume': float(row.iloc[0]['volume']),
            'amount': float(row.iloc[0]['amount']),
        }

    def _get_daily_market(self, date, codes=None) -> pd.DataFrame:
        """获取某一天的全市场行情

        从 self.all_data 中提取指定日期的数据
        返回 DataFrame: code, name, open, high, low, close, volume, amount, days_listed

        days_listed: 指定日期已拥有的交易日行数（含该日）。
            若某股数据小于60行，视为上市不足60个交易日的新股。
        """
        date_dt = pd.to_datetime(date)
        rows = []

        iter_items = ((c, self.all_data[c]) for c in codes if c in self.all_data) if codes is not None else self.all_data.items()
        for code, df in iter_items:
            row = df[df['date'] == date_dt]
            if not row.empty:
                data = row.iloc[0].to_dict()
                data['code'] = code
                data['name'] = self.stock_names.get(code, '')
                # 计算截止当前回测日期该股票的已有交易日行数（用于新股过滤）
                days_listed = int((df['date'] <= date_dt).sum())
                data['days_listed'] = days_listed
                rows.append(data)

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        cols = ['code', 'name', 'open', 'high', 'low', 'close', 'volume', 'amount', 'days_listed']
        for col in cols:
            if col not in df.columns:
                df[col] = None

        return df[cols].copy()

    def _get_pre_close(self, code, date) -> float:
        """获取某只股票在指定日期的前一日收盘价

        从 self.all_data[code] 中查找
        无数据返回 None
        """
        if code not in self.all_data:
            return None

        df = self.all_data[code]
        date_dt = pd.to_datetime(date)
        mask = df['date'] == date_dt

        if not mask.any():
            return None

        idx = mask.idxmax()
        if idx == 0:
            return None

        return float(df.iloc[idx - 1]['close'])

    # ------------------------------------------------------------------
    # 绩效计算
    # ------------------------------------------------------------------
    def _calculate_metrics(self, daily_nav_df, trades) -> dict:
        """计算绩效指标

        基础指标:
        - total_return: 总收益率
        - annual_return: 年化收益率
        - max_drawdown: 最大回撤
        - sharpe_ratio: 夏普比率
        - win_rate: 胜率（以每笔完整卖出为单位）
        - profit_loss_ratio: 盈亏比
        - total_trades: 总交易笔数（完整买卖对）
        - trading_days: 回测交易天数
        """
        nav_series = daily_nav_df['nav'] if 'nav' in daily_nav_df.columns else pd.Series(dtype=float)
        trading_days = len(daily_nav_df)

        if trading_days == 0 or self.initial_capital <= 0:
            return self._zero_metrics(trading_days)

        final_nav = float(nav_series.iloc[-1])
        total_return = (final_nav - self.initial_capital) / self.initial_capital

        # 年化收益率（复利）
        if trading_days >= 1:
            annual_return = (final_nav / self.initial_capital) ** (252 / trading_days) - 1
        else:
            annual_return = 0.0

        # 最大回撤
        peak = nav_series.cummax()
        drawdown = (peak - nav_series) / peak
        max_drawdown = float(drawdown.max()) if len(drawdown) > 0 else 0.0

        # 夏普比率（无风险利率 2%）
        daily_returns = nav_series.pct_change().dropna()
        if len(daily_returns) > 1 and daily_returns.std() > 0:
            risk_free_daily = 0.02 / 252
            sharpe_ratio = (
                (daily_returns.mean() - risk_free_daily)
                / daily_returns.std()
                * np.sqrt(252)
            )
        else:
            sharpe_ratio = 0.0

        # 胜率与盈亏比（以卖出交易为准，直接使用已计算的 pnl）
        sell_trades = [t for t in trades if t['direction'] == 'sell']
        profits = [t.get('pnl', 0) or 0 for t in sell_trades]

        total_trades = len(profits)
        if total_trades > 0:
            win_count = sum(1 for p in profits if p > 0)
            win_rate = win_count / total_trades

            profit_list = [p for p in profits if p > 0]
            loss_list = [-p for p in profits if p < 0]

            avg_profit = sum(profit_list) / len(profit_list) if profit_list else 0.0
            avg_loss = sum(loss_list) / len(loss_list) if loss_list else 1.0

            profit_loss_ratio = (
                avg_profit / avg_loss if avg_loss > 0 else float('inf')
            )
        else:
            win_rate = 0.0
            profit_loss_ratio = 0.0

        return {
            'total_return': round(total_return, 6),
            'annual_return': round(annual_return, 6),
            'max_drawdown': round(max_drawdown, 6),
            'sharpe_ratio': round(sharpe_ratio, 6),
            'win_rate': round(win_rate, 6),
            'profit_loss_ratio': round(profit_loss_ratio, 6),
            'total_trades': total_trades,
            'trading_days': trading_days,
        }

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------
    def _empty_result(self) -> dict:
        """返回空结果结构"""
        return {
            'daily_nav': pd.DataFrame(columns=['date', 'nav']),
            'trades': [],
            'metrics': self._zero_metrics(0),
            'positions_history': [],
            'sell_type_stats': {
                'hard_stop': {'count': 0, 'avg_pnl': 0.0},
                'soft_stop': {'count': 0, 'avg_pnl': 0.0},
                'take_profit': {'count': 0, 'avg_pnl': 0.0},
                'time_stop': {'count': 0, 'avg_pnl': 0.0},
            },
        }

    @staticmethod
    def _zero_metrics(trading_days: int) -> dict:
        """返回零值指标"""
        return {
            'total_return': 0.0,
            'annual_return': 0.0,
            'max_drawdown': 0.0,
            'sharpe_ratio': 0.0,
            'win_rate': 0.0,
            'profit_loss_ratio': 0.0,
            'total_trades': 0,
            'trading_days': trading_days,
        }
