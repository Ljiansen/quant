# -*- coding: utf-8 -*-
"""
回测引擎模块
提供逐bar回放、资金管理、模拟撮合及绩效指标计算
"""

import math
from itertools import product

import numpy as np
import pandas as pd

from strategy.base_strategy import Signal


class BacktestEngine:
    """
    回测引擎

    按日线逐bar回放，支持资金管理、滑点、佣金及印花税模拟。
    """

    def __init__(
        self,
        strategy,
        data_manager,
        initial_capital=500000,
        commission_rate=0.0003,
        slippage=0.001,
        stamp_tax_rate=0.001,
    ):
        """
        初始化回测引擎

        Args:
            strategy: BaseStrategy 实例
            data_manager: DataManager 实例
            initial_capital: 初始资金
            commission_rate: 佣金费率（双边）
            slippage: 滑点比例
            stamp_tax_rate: 印花税率（仅卖出）
        """
        self.strategy = strategy
        self.data_manager = data_manager
        self.initial_capital = initial_capital
        self.commission_rate = commission_rate
        self.slippage = slippage
        self.stamp_tax_rate = stamp_tax_rate

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def run(self, symbol, start_date, end_date) -> dict:
        """
        运行单次回测

        按日线逐bar回放：
        1. 获取历史数据
        2. 逐bar调用 strategy.on_bar()
        3. 收到BUY信号 -> 模拟买入（下一bar开盘价+滑点成交，扣佣金）
        4. 收到SELL信号 -> 模拟卖出（下一bar开盘价-滑点成交，扣佣金+印花税）
        5. 记录每日净值

        Args:
            symbol: 股票代码
            start_date: 开始日期，格式 'YYYYMMDD'
            end_date: 结束日期，格式 'YYYYMMDD'

        Returns:
            dict，包含 daily_nav、trades、metrics、params
        """
        df = self.data_manager.get_daily_bars(symbol, start_date, end_date)
        if df.empty or len(df) < 2:
            print("[BacktestEngine] 数据为空或不足，无法回测")
            return self._empty_result()

        df = df.sort_values("date").reset_index(drop=True)

        # 策略初始化
        self.strategy.init(df)

        cash = float(self.initial_capital)
        position = 0
        trades = []
        daily_nav_list = []
        pending_action = None  # 上一bar产生的信号

        for i in range(len(df)):
            date = df["date"].iloc[i]
            open_price = float(df["open"].iloc[i])
            close_price = float(df["close"].iloc[i])

            # 1. 执行上一bar产生的信号（本bar开盘成交）
            if pending_action == Signal.BUY and position == 0:
                execute_price = open_price * (1 + self.slippage)
                invest_amount = cash * 0.9
                volume = int(invest_amount / execute_price / 100) * 100
                if volume > 0:
                    commission = execute_price * volume * self.commission_rate
                    total_cost = execute_price * volume + commission
                    if total_cost <= cash:
                        cash -= total_cost
                        position += volume
                        trades.append(
                            {
                                "date": date,
                                "symbol": symbol,
                                "direction": "BUY",
                                "price": round(execute_price, 4),
                                "volume": volume,
                                "commission": round(commission, 4),
                                "stamp_tax": 0.0,
                            }
                        )

            elif pending_action == Signal.SELL and position > 0:
                execute_price = open_price * (1 - self.slippage)
                volume = position
                commission = execute_price * volume * self.commission_rate
                stamp_tax = execute_price * volume * self.stamp_tax_rate
                cash += execute_price * volume - commission - stamp_tax
                position = 0
                trades.append(
                    {
                        "date": date,
                        "symbol": symbol,
                        "direction": "SELL",
                        "price": round(execute_price, 4),
                        "volume": volume,
                        "commission": round(commission, 4),
                        "stamp_tax": round(stamp_tax, 4),
                    }
                )

            # 2. 计算当前bar的信号（用于下一bar执行）
            pending_action = None
            if i < len(df) - 1:
                bar = df.iloc[i]
                history = df.iloc[: i + 1]
                signal = self.strategy.on_bar(bar, history)
                if signal in (Signal.BUY, Signal.SELL):
                    pending_action = signal

            # 3. 收盘后计算净值
            nav = cash + position * close_price
            daily_nav_list.append({"date": date, "nav": nav})

        daily_nav = pd.DataFrame(daily_nav_list)

        # 买入持有基准
        first_close = float(df["close"].iloc[0])
        if first_close > 0:
            shares = self.initial_capital / first_close
            daily_nav["benchmark"] = df["close"] * shares
        else:
            daily_nav["benchmark"] = self.initial_capital

        metrics = self._calculate_metrics(daily_nav, trades)

        return {
            "daily_nav": daily_nav,
            "trades": trades,
            "metrics": metrics,
            "params": self.strategy.get_params(),
        }

    def run_optimization(
        self, symbol, start_date, end_date, param_space=None
    ) -> list:
        """
        多参数组合批量回测

        遍历所有参数组合（笛卡尔积），每组运行一次回测，
        返回结果按年化收益率降序排列。

        Args:
            symbol: 股票代码
            start_date: 开始日期
            end_date: 结束日期
            param_space: 参数空间，默认从 strategy.get_param_space() 获取

        Returns:
            list[dict]，按年化收益排序的回测结果列表
        """
        param_space = param_space or self.strategy.get_param_space()
        if not param_space:
            print("[BacktestEngine] 参数空间为空，仅运行默认参数")
            return [self.run(symbol, start_date, end_date)]

        keys = list(param_space.keys())
        values = list(param_space.values())
        results = []
        total = math.prod(len(v) for v in values)
        count = 0

        for combo in product(*values):
            count += 1
            params = dict(zip(keys, combo))
            print(
                f"[BacktestEngine] 优化进度 {count}/{total}，"
                f"参数: {params}"
            )

            new_strategy = self.strategy.__class__(params)
            engine = BacktestEngine(
                new_strategy,
                self.data_manager,
                self.initial_capital,
                self.commission_rate,
                self.slippage,
                self.stamp_tax_rate,
            )
            result = engine.run(symbol, start_date, end_date)
            results.append(result)

        # 按年化收益率降序排列
        results.sort(
            key=lambda x: x["metrics"].get("annual_return", -float("inf")),
            reverse=True,
        )
        return results

    # ------------------------------------------------------------------
    # 绩效计算
    # ------------------------------------------------------------------
    def _calculate_metrics(self, daily_nav, trades) -> dict:
        """
        计算回测绩效指标

        Args:
            daily_nav: DataFrame，包含 date、nav、benchmark
            trades: 交易记录列表

        Returns:
            dict，包含总收益率、年化收益、最大回撤、夏普比率、胜率、盈亏比等
        """
        nav_series = daily_nav["nav"]
        trading_days = len(daily_nav)

        if trading_days == 0 or self.initial_capital <= 0:
            return self._zero_metrics(trading_days)

        final_nav = float(nav_series.iloc[-1])
        total_return = (final_nav - self.initial_capital) / self.initial_capital

        # 年化收益率（复利）
        if trading_days >= 1:
            annual_return = (final_nav / self.initial_capital) ** (
                252 / trading_days
            ) - 1
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

        # 胜率与盈亏比（按完整交易对统计）
        buy_trades = [t for t in trades if t["direction"] == "BUY"]
        sell_trades = [t for t in trades if t["direction"] == "SELL"]

        profits = []
        for buy, sell in zip(buy_trades, sell_trades):
            buy_cost = buy["price"] * buy["volume"] + buy["commission"]
            sell_revenue = (
                sell["price"] * sell["volume"]
                - sell["commission"]
                - sell["stamp_tax"]
            )
            profits.append(sell_revenue - buy_cost)

        total_trades = len(profits)
        if total_trades > 0:
            win_count = sum(1 for p in profits if p > 0)
            win_rate = win_count / total_trades

            profit_list = [p for p in profits if p > 0]
            loss_list = [-p for p in profits if p < 0]

            avg_profit = sum(profit_list) / len(profit_list) if profit_list else 0.0
            avg_loss = sum(loss_list) / len(loss_list) if loss_list else 1.0

            profit_loss_ratio = (
                avg_profit / avg_loss if avg_loss > 0 else float("inf")
            )
        else:
            win_rate = 0.0
            profit_loss_ratio = 0.0

        return {
            "total_return": round(total_return, 6),
            "annual_return": round(annual_return, 6),
            "max_drawdown": round(max_drawdown, 6),
            "sharpe_ratio": round(sharpe_ratio, 6),
            "win_rate": round(win_rate, 6),
            "profit_loss_ratio": round(profit_loss_ratio, 6),
            "total_trades": total_trades,
            "trading_days": trading_days,
        }

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------
    def _empty_result(self) -> dict:
        """返回空结果结构"""
        return {
            "daily_nav": pd.DataFrame(
                columns=["date", "nav", "benchmark"]
            ),
            "trades": [],
            "metrics": self._zero_metrics(0),
            "params": self.strategy.get_params(),
        }

    @staticmethod
    def _zero_metrics(trading_days: int) -> dict:
        """返回零值指标"""
        return {
            "total_return": 0.0,
            "annual_return": 0.0,
            "max_drawdown": 0.0,
            "sharpe_ratio": 0.0,
            "win_rate": 0.0,
            "profit_loss_ratio": 0.0,
            "total_trades": 0,
            "trading_days": trading_days,
        }
