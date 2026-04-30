# -*- coding: utf-8 -*-
"""
data 数据层模块
统一封装 akshare、baostock 等数据源接口
"""

from .akshare_data import AkshareData
from .baostock_data import BaostockData
from .local_data import LocalDailyData
from .data_manager import DataManager

__all__ = [
    "AkshareData",
    "BaostockData",
    "LocalDailyData",
    "DataManager",
]
