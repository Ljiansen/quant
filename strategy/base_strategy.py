from abc import ABC, abstractmethod
import pandas as pd


class Signal:
    """交易信号"""
    BUY = 'BUY'
    SELL = 'SELL'
    HOLD = 'HOLD'


class BaseStrategy(ABC):
    """策略基类，所有策略必须继承此类"""

    def __init__(self, params: dict = None):
        """初始化策略
        params: 策略参数字典
        """
        self.params = params or {}
        self.name = self.__class__.__name__

    @abstractmethod
    def init(self, history_data: pd.DataFrame):
        """策略初始化，传入历史数据用于计算指标
        history_data: 历史日线数据 (date, open, high, low, close, volume, amount)
        """
        pass

    @abstractmethod
    def on_bar(self, bar: pd.Series, history: pd.DataFrame) -> str:
        """每根K线触发
        bar: 当前bar数据
        history: 截至当前的历史数据
        返回: Signal.BUY / Signal.SELL / Signal.HOLD
        """
        pass

    @classmethod
    def get_param_space(cls) -> dict:
        """返回策略参数空间，用于参数优化
        返回格式: {'param_name': [value1, value2, ...], ...}
        """
        return {}

    def get_params(self) -> dict:
        """获取当前策略参数"""
        return self.params.copy()
