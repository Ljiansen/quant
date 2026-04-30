# -*- coding: utf-8 -*-
"""
模拟引擎模块
与 LiveEngine 逻辑完全一致，但使用 SimulatedExecutor
"""

from trade.executor import SimulatedExecutor
from .live_engine import LiveEngine


class SimulationEngine(LiveEngine):
    """
    模拟引擎

    继承自 LiveEngine，仅将 executor 替换为 SimulatedExecutor，
    用于在无真实交易环境的情况下验证策略信号与下单逻辑。
    """

    def __init__(self, strategy, data_manager):
        """
        初始化模拟引擎

        Args:
            strategy: BaseStrategy 实例
            data_manager: DataManager 实例
        """
        executor = SimulatedExecutor()
        super().__init__(strategy, data_manager, executor)
