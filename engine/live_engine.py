# -*- coding: utf-8 -*-
"""
实盘引擎模块
执行一次信号检查与下单，对接真实交易执行器
"""

from datetime import datetime, timedelta

import pandas as pd

from strategy.base_strategy import Signal


class LiveEngine:
    """
    实盘引擎

    获取历史数据与最新行情，调用策略产生信号，并通过 TradeExecutor 下单。
    """

    def __init__(self, strategy, data_manager, executor):
        """
        初始化实盘引擎

        Args:
            strategy: BaseStrategy 实例
            data_manager: DataManager 实例
            executor: TradeExecutor 实例
        """
        self.strategy = strategy
        self.data_manager = data_manager
        self.executor = executor
        self.trade_log = []

    def run_once(self, symbols: list):
        """
        执行一次信号检查和下单

        流程：
        1. 获取历史数据（最近120天，用于策略计算）
        2. 获取最新行情快照
        3. 对每个标的调用 strategy.on_bar()
        4. 根据信号调用 executor.buy()/sell()
        5. 记录交易日志

        Args:
            symbols: 股票代码列表
        """
        today = datetime.now()
        today_str = today.strftime("%Y%m%d")
        start_dt = today - timedelta(days=120)
        start_str = start_dt.strftime("%Y%m%d")

        for symbol in symbols:
            try:
                # 1. 获取历史数据
                history = self.data_manager.get_daily_bars(
                    symbol, start_str, today_str
                )
                if history.empty:
                    print(
                        f"[LiveEngine] 获取历史数据为空，跳过: {symbol}"
                    )
                    continue

                history = history.sort_values("date").reset_index(drop=True)
                self.strategy.init(history)

                # 2. 获取最新行情
                snapshot = self.data_manager.get_realtime_snapshot([symbol])
                if snapshot.empty:
                    print(
                        f"[LiveEngine] 获取实时行情为空，跳过: {symbol}"
                    )
                    continue

                # 构造当前 bar
                latest = snapshot.iloc[0]
                current_bar = pd.Series(
                    {
                        "date": pd.Timestamp.now().normalize(),
                        "open": latest.get(
                            "open", latest.get("latest_price", 0)
                        ),
                        "high": latest.get(
                            "high", latest.get("latest_price", 0)
                        ),
                        "low": latest.get(
                            "low", latest.get("latest_price", 0)
                        ),
                        "close": latest.get(
                            "latest_price", latest.get("close", 0)
                        ),
                        "volume": latest.get("volume", 0),
                        "amount": latest.get("amount", 0),
                    }
                )

                # 3. 策略信号
                signal = self.strategy.on_bar(current_bar, history)

                # 4. 查询当前持仓
                positions = self.executor.query_positions()
                position_volume = 0
                for pos in positions:
                    if pos.get("symbol") == symbol:
                        position_volume = pos.get("volume", 0)
                        break

                # 5. 执行信号
                if signal == Signal.BUY and position_volume == 0:
                    price = float(current_bar["close"])
                    if price > 0:
                        asset = self.executor.query_asset()
                        cash = asset.get("cash", 0)
                        volume = int(cash * 0.9 / price / 100) * 100
                        if volume > 0:
                            order_id = self.executor.buy(
                                symbol,
                                price,
                                volume,
                                order_remark="LiveEngine",
                            )
                            self._log_trade(
                                symbol, "BUY", price, volume, order_id
                            )

                elif signal == Signal.SELL and position_volume > 0:
                    price = float(current_bar["close"])
                    if price > 0:
                        order_id = self.executor.sell(
                            symbol,
                            price,
                            position_volume,
                            order_remark="LiveEngine",
                        )
                        self._log_trade(
                            symbol, "SELL", price, position_volume, order_id
                        )

            except Exception as e:
                print(
                    f"[LiveEngine] 处理标的 {symbol} 时发生异常: {e}"
                )

    def get_trade_log(self) -> list:
        """
        获取交易日志

        Returns:
            list[dict]，每条记录包含 time、symbol、direction、price、volume、order_id
        """
        return self.trade_log.copy()

    def _log_trade(self, symbol, direction, price, volume, order_id):
        """记录交易日志"""
        self.trade_log.append(
            {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "symbol": symbol,
                "direction": direction,
                "price": price,
                "volume": volume,
                "order_id": order_id,
            }
        )
