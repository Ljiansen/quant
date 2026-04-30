# -*- coding: utf-8 -*-
"""
统一数据管理器
封装 akshare、baostock 等多种数据源，支持本地 CSV 缓存
"""

import os

import pandas as pd

from .akshare_data import AkshareData
from .baostock_data import BaostockData
from .local_data import LocalDailyData


class DataManager:
    """
    统一数据管理器

    支持切换数据源（akshare / baostock），并自动管理本地 CSV 缓存。
    """

    def __init__(
        self, source: str = "akshare", cache_dir: str = "d:/miniqmt_quant/data_cache"
    ):
        """
        初始化数据管理器

        Args:
            source: 数据源，可选 'akshare' 或 'baostock'
            cache_dir: CSV 缓存目录路径
        """
        self.source = source.lower()
        self.cache_dir = cache_dir

        # 创建缓存目录
        if not os.path.exists(self.cache_dir):
            os.makedirs(self.cache_dir, exist_ok=True)
            print(f"[DataManager] 缓存目录已创建: {self.cache_dir}")

        # 初始化数据源
        if self.source == "akshare":
            self._provider = AkshareData()
        elif self.source == "baostock":
            self._provider = BaostockData()
        elif self.source == "local":
            self._provider = LocalDailyData()
        else:
            raise ValueError(f"[DataManager] 不支持的数据源: {source}")

        print(f"[DataManager] 初始化完成，数据源: {self.source}")

    def _get_cache_path(self, symbol: str, start_date: str, end_date: str) -> str:
        """
        生成缓存文件路径

        命名规则: {symbol}_{start}_{end}.csv
        """
        # 去除代码中可能的非法字符
        safe_symbol = symbol.replace(".", "_").replace("/", "_")
        filename = f"{safe_symbol}_{start_date}_{end_date}.csv"
        return os.path.join(self.cache_dir, filename)

    def _load_cache(self, cache_path: str) -> pd.DataFrame:
        """从本地 CSV 加载缓存数据"""
        try:
            df = pd.read_csv(cache_path)
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"])
            print(f"[DataManager] 从缓存加载: {cache_path}")
            return df
        except Exception as e:
            print(f"[DataManager] 读取缓存失败: {cache_path}, 错误: {e}")
            return pd.DataFrame()

    def _save_cache(self, df: pd.DataFrame, cache_path: str):
        """保存 DataFrame 到本地 CSV 缓存"""
        try:
            df.to_csv(cache_path, index=False)
            print(f"[DataManager] 已保存缓存: {cache_path}")
        except Exception as e:
            print(f"[DataManager] 保存缓存失败: {cache_path}, 错误: {e}")

    def _find_compatible_cache(self, symbol: str, start_date: str, end_date: str):
        """查找能覆盖请求范围的已有缓存文件

        当精确缓存不存在时，尝试找到一个缓存范围包含请求范围的文件。
        命名规则: {symbol}_{cache_start}_{cache_end}.csv
        """
        safe_symbol = symbol.replace(".", "_").replace("/", "_")
        prefix = f"{safe_symbol}_"
        try:
            files = os.listdir(self.cache_dir)
        except Exception:
            return None

        for fname in files:
            if not fname.startswith(prefix) or not fname.endswith(".csv"):
                continue
            # 解析文件名中的日期范围
            base = fname[len(prefix):-4]  # 去掉前缀和 .csv
            parts = base.split("_")
            if len(parts) != 2:
                continue
            cache_start, cache_end = parts
            # 检查缓存是否与请求范围有足够交集
            # 条件：缓存结束 >= 请求结束，且缓存开始 <= 请求结束（即有交集）
            if cache_end >= end_date and cache_start <= end_date:
                return os.path.join(self.cache_dir, fname)
        return None

    def get_daily_bars(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        use_cache: bool = True,
    ) -> pd.DataFrame:
        """
        统一接口获取日线数据，支持本地缓存

        Args:
            symbol: 股票代码，如 '000001' 或 '600000'
            start_date: 开始日期，格式 'YYYYMMDD'
            end_date: 结束日期，格式 'YYYYMMDD'
            use_cache: 是否使用本地缓存

        Returns:
            DataFrame, 统一列: date(datetime), open, high, low, close, volume, amount
        """
        cache_path = self._get_cache_path(symbol, start_date, end_date)

        # 优先读取精确缓存
        if use_cache and os.path.exists(cache_path):
            df = self._load_cache(cache_path)
            if not df.empty:
                return df

        # 尝试找覆盖范围的兼容缓存
        if use_cache:
            compat_path = self._find_compatible_cache(symbol, start_date, end_date)
            if compat_path:
                df = self._load_cache(compat_path)
                if not df.empty:
                    # 过滤到请求结束日期（开始日期可能比请求的晚）
                    end_dt = pd.to_datetime(end_date)
                    df = df[df['date'] <= end_dt].copy()
                    if not df.empty:
                        return df

        # 从数据源获取
        df = self._provider.get_daily_bars(symbol, start_date, end_date)

        if not df.empty:
            # 确保 date 列为 datetime 类型
            df["date"] = pd.to_datetime(df["date"])

            # 统一列名检查
            unified_cols = [
                "date",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "amount",
            ]
            for col in unified_cols:
                if col not in df.columns:
                    df[col] = None
            df = df[unified_cols].copy()

            # 保存缓存
            if use_cache:
                self._save_cache(df, cache_path)
        else:
            print(f"[DataManager] 获取日线数据为空: {symbol}")

        return df

    def get_realtime_snapshot(self, symbols: list) -> pd.DataFrame:
        """
        获取实时快照行情

        Args:
            symbols: 股票代码列表

        Returns:
            DataFrame, 实时行情快照
        """
        print(f"[DataManager] 获取实时快照，数据源: {self.source}")

        if hasattr(self._provider, "get_realtime_snapshot"):
            return self._provider.get_realtime_snapshot(symbols)
        else:
            print(
                f"[DataManager] 当前数据源 {self.source} 不支持实时快照"
            )
            return pd.DataFrame()

    def get_financial_data(self, symbol: str) -> pd.DataFrame:
        """
        获取财务数据

        Args:
            symbol: 股票代码

        Returns:
            DataFrame, 财务数据
        """
        print(f"[DataManager] 获取财务数据，数据源: {self.source}")
        return self._provider.get_financial_data(symbol)

    def clear_cache(self):
        """清除所有本地缓存文件"""
        print(f"[DataManager] 开始清除缓存目录: {self.cache_dir}")
        removed_count = 0

        try:
            for filename in os.listdir(self.cache_dir):
                if filename.endswith(".csv"):
                    filepath = os.path.join(self.cache_dir, filename)
                    os.remove(filepath)
                    removed_count += 1
                    print(f"[DataManager] 已删除缓存: {filename}")

            print(
                f"[DataManager] 缓存清除完成，共删除 {removed_count} 个文件"
            )
        except Exception as e:
            print(f"[DataManager] 清除缓存失败: {e}")

    def get_all_stock_spot(self) -> pd.DataFrame:
        """获取全市场A股实时行情快照
        注意：此功能仅 akshare 支持
        如果当前数据源是 baostock，临时切换到 akshare 获取
        """
        print(f"[DataManager] 获取全市场快照，当前数据源: {self.source}")

        if self.source == "akshare":
            return self._provider.get_all_stock_spot()

        # 当前为 baostock，临时使用 akshare 获取
        print("[DataManager] 当前数据源不支持全市场快照，临时使用 akshare")
        try:
            temp_provider = AkshareData()
            return temp_provider.get_all_stock_spot()
        except Exception as e:
            print(f"[DataManager] 临时使用 akshare 获取全市场快照失败: {e}")
            return pd.DataFrame()

    def get_multi_stock_daily(
        self,
        symbols: list,
        start_date: str,
        end_date: str,
        use_cache: bool = True,
    ) -> dict:
        """批量获取多只股票日线数据，支持缓存
        返回: {symbol: DataFrame, ...}
        缓存策略：每只股票独立缓存（复用现有的单股票缓存机制）
        """
        print(
            f"[DataManager] 批量获取日线数据: 共 {len(symbols)} 只, "
            f"起始={start_date}, 结束={end_date}, 缓存={use_cache}"
        )

        result = {}
        total = len(symbols)

        for idx, symbol in enumerate(symbols, start=1):
            if idx % 100 == 0 or idx == total:
                print(f"[DataManager] 批量获取进度: {idx}/{total}")

            try:
                df = self.get_daily_bars(symbol, start_date, end_date, use_cache)
                if not df.empty:
                    result[symbol] = df
            except Exception as e:
                print(f"[DataManager] 获取 {symbol} 日线失败: {e}")

        print(f"[DataManager] 批量获取完成, 成功 {len(result)}/{total} 只")
        return result

    def switch_source(self, source: str):
        """
        切换数据源

        Args:
            source: 'akshare' 或 'baostock'
        """
        source = source.lower()
        if source == self.source:
            print(f"[DataManager] 数据源已是 {source}，无需切换")
            return

        # 释放旧数据源（如 baostock 需要登出）
        if hasattr(self._provider, "logout"):
            self._provider.logout()

        if source == "akshare":
            self._provider = AkshareData()
        elif source == "baostock":
            self._provider = BaostockData()
        elif source == "local":
            self._provider = LocalDailyData()
        else:
            raise ValueError(f"[DataManager] 不支持的数据源: {source}")

        self.source = source
        print(f"[DataManager] 数据源已切换为: {source}")
