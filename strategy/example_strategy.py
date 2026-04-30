from .base_strategy import BaseStrategy, Signal


class DualMAStrategy(BaseStrategy):
    """双均线策略示例
    当短期均线上穿长期均线时买入，下穿时卖出
    """

    def __init__(self, params=None):
        default_params = {'short_period': 5, 'long_period': 20}
        if params:
            default_params.update(params)
        super().__init__(default_params)

    def init(self, history_data):
        """计算均线指标"""
        pass  # 在 on_bar 中动态计算

    def on_bar(self, bar, history):
        """基于均线金叉/死叉产生信号"""
        short = self.params['short_period']
        long_ = self.params['long_period']

        if len(history) < long_ + 1:
            return Signal.HOLD

        # 计算当前和前一天的均线
        short_ma_now = history['close'].iloc[-short:].mean()
        short_ma_prev = history['close'].iloc[-short - 1:-1].mean()
        long_ma_now = history['close'].iloc[-long_:].mean()
        long_ma_prev = history['close'].iloc[-long_ - 1:-1].mean()

        # 金叉买入，死叉卖出
        if short_ma_prev <= long_ma_prev and short_ma_now > long_ma_now:
            return Signal.BUY
        if short_ma_prev >= long_ma_prev and short_ma_now < long_ma_now:
            return Signal.SELL

        return Signal.HOLD

    @classmethod
    def get_param_space(cls):
        return {
            'short_period': [3, 5, 10],
            'long_period': [15, 20, 30, 60]
        }
