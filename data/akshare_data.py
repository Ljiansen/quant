# -*- coding: utf-8 -*-
"""
akshare 数据接口封装
提供统一格式的股票行情、财务数据、实时快照等查询功能
"""

import time
import random

import pandas as pd

import akshare as ak


class AkshareData:
    """akshare 数据源封装类"""

    def __init__(self):
        """初始化 akshare 数据接口"""
        print("[AkshareData] akshare 数据接口已初始化")

    def _normalize_symbol(self, symbol: str) -> str:
        """
        标准化股票代码，移除交易所后缀
        akshare 的 stock_zh_a_hist 接口只需要纯数字代码
        """
        symbol = symbol.strip().upper()
        if symbol.endswith(".SH") or symbol.endswith(".SZ"):
            symbol = symbol.split(".")[0]
        return symbol

    def _get_exchange_symbol(self, code: str) -> str:
        """根据股票代码自动判断交易所并拼接前缀
        深交所: 000/001/002/003/300/301/302 -> sz
        上交所: 600/601/603/605/688 -> sh
        北交所: 8/4开头 -> bj（不纳入A股回测池，忽略）
        """
        if code.startswith(('6',)):
            return f'sh{code}'
        else:
            return f'sz{code}'

    def get_daily_bars(
        self, symbol: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        """
        获取日线行情，返回统一格式 DataFrame

        Args:
            symbol: 股票代码，如 '000001' 或 '600000'
            start_date: 开始日期，格式 'YYYYMMDD'
            end_date: 结束日期，格式 'YYYYMMDD'

        Returns:
            DataFrame, 统一列: date, open, high, low, close, volume, amount
        """
        code = self._normalize_symbol(symbol)
        print(
            f"[AkshareData] 获取日线: {code}, 起始={start_date}, 结束={end_date}"
        )

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                # 优先使用 stock_zh_a_daily（新浪财经），和 stock_zh_a_hist 相比连接更稳定
                ex_code = self._get_exchange_symbol(code)
                df = ak.stock_zh_a_daily(
                    symbol=ex_code,
                    start_date=start_date,
                    end_date=end_date,
                    adjust="qfq",
                )

                if df is None or df.empty:
                    print(f"[AkshareData] 未获取到数据: {code}")
                    return pd.DataFrame(
                        columns=["date", "open", "high", "low", "close", "volume", "amount"]
                    )

                # stock_zh_a_daily 列名已是英文标准格式
                # 确保 date 列为 datetime 类型
                df["date"] = pd.to_datetime(df["date"])

                # 选择统一列
                unified_cols = ["date", "open", "high", "low", "close", "volume", "amount"]
                for col in unified_cols:
                    if col not in df.columns:
                        df[col] = None
                df = df[unified_cols].copy()

                # 数值列转换
                for col in ["open", "high", "low", "close"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
                for col in ["volume", "amount"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce")

                print(f"[AkshareData] 获取日线成功: {code}, 共 {len(df)} 条")
                return df

            except Exception as e:
                print(f"[AkshareData] 获取日线失败: {code}, 错误: {e}")
                if attempt < max_retries:
                    wait = 2 ** attempt + random.random() * 2
                    time.sleep(wait)
                else:
                    return pd.DataFrame(
                        columns=["date", "open", "high", "low", "close", "volume", "amount"]
                    )

    def get_realtime_snapshot(self, symbols: list) -> pd.DataFrame:
        """
        获取实时快照行情

        Args:
            symbols: 股票代码列表，如 ['000001', '600000']

        Returns:
            DataFrame, 包含实时行情快照
        """
        print(f"[AkshareData] 获取实时快照: {symbols}")

        try:
            df = ak.stock_zh_a_spot_em()

            if df is None or df.empty:
                print("[AkshareData] 未获取到实时快照数据")
                return pd.DataFrame()

            # 标准化传入的代码列表
            target_codes = [self._normalize_symbol(s) for s in symbols]

            # 根据代码过滤（akshare 返回的代码列通常为 '代码'）
            code_col = None
            for possible_col in ["代码", "股票代码", "symbol", "code"]:
                if possible_col in df.columns:
                    code_col = possible_col
                    break

            if code_col is None:
                print("[AkshareData] 实时快照中未找到代码列")
                return pd.DataFrame()

            df[code_col] = df[code_col].astype(str).str.strip()
            df_filtered = df[df[code_col].isin(target_codes)].copy()

            print(
                f"[AkshareData] 实时快照获取成功, 匹配 {len(df_filtered)} 条"
            )
            return df_filtered

        except Exception as e:
            print(f"[AkshareData] 获取实时快照失败: {e}")
            return pd.DataFrame()

    def get_financial_data(self, symbol: str) -> pd.DataFrame:
        """
        获取财务数据

        Args:
            symbol: 股票代码，如 '000001' 或 '600000'

        Returns:
            DataFrame, 财务摘要数据
        """
        code = self._normalize_symbol(symbol)
        print(f"[AkshareData] 获取财务数据: {code}")

        try:
            df = ak.stock_financial_abstract_em(symbol=code)

            if df is None or df.empty:
                print(f"[AkshareData] 未获取到财务数据: {code}")
                return pd.DataFrame()

            print(f"[AkshareData] 财务数据获取成功: {code}, 共 {len(df)} 条")
            return df

        except Exception as e:
            print(f"[AkshareData] 获取财务数据失败: {code}, 错误: {e}")
            return pd.DataFrame()

    def get_stock_list(self) -> pd.DataFrame:
        """
        获取 A 股股票列表

        Returns:
            DataFrame, 包含股票代码和名称
        """
        print("[AkshareData] 获取 A 股股票列表")

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                df = ak.stock_info_a_code_name()

                if df is None or df.empty:
                    print("[AkshareData] 未获取到股票列表")
                    return pd.DataFrame()

                print(f"[AkshareData] 股票列表获取成功, 共 {len(df)} 条")
                return df

            except Exception as e:
                print(f"[AkshareData] 获取股票列表失败 (尝试 {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    import time
                    time.sleep(2 ** attempt)
                else:
                    return pd.DataFrame()

    def get_etf_list(self) -> pd.DataFrame:
        """
        获取 ETF 列表

        Returns:
            DataFrame, 包含 ETF 信息
        """
        print("[AkshareData] 获取 ETF 列表")

        try:
            df = ak.fund_etf_spot_em()

            if df is None or df.empty:
                print("[AkshareData] 未获取到 ETF 列表")
                return pd.DataFrame()

            print(f"[AkshareData] ETF 列表获取成功, 共 {len(df)} 条")
            return df

        except Exception as e:
            print(f"[AkshareData] 获取 ETF 列表失败: {e}")
            return pd.DataFrame()

    def get_all_stock_spot(self) -> pd.DataFrame:
        """获取全市场A股实时行情快照
        调用 ak.stock_zh_a_spot_em()

        返回 DataFrame 统一列名:
        code, name, close, open, high, low, volume, amount, change_pct, pre_close

        其中 code 格式为纯数字如 '600000'（不带后缀）
        change_pct 为百分比（如涨5%则值为5.0）
        """
        print("[AkshareData] 获取全市场A股实时行情快照")

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                df = ak.stock_zh_a_spot_em()

                if df is None or df.empty:
                    print("[AkshareData] 未获取到全市场快照数据")
                    return pd.DataFrame()

                # 列名映射到统一格式
                column_mapping = {
                    "代码": "code",
                    "名称": "name",
                    "最新价": "close",
                    "今开": "open",
                    "最高": "high",
                    "最低": "low",
                    "成交量": "volume",
                    "成交额": "amount",
                    "涨跌幅": "change_pct",
                    "昨收": "pre_close",
                }

                rename_dict = {}
                for old_name, new_name in column_mapping.items():
                    if old_name in df.columns:
                        rename_dict[old_name] = new_name
                df = df.rename(columns=rename_dict)

                # 确保 code 为纯数字字符串
                if "code" in df.columns:
                    df["code"] = df["code"].astype(str).str.strip()
                    df["code"] = df["code"].apply(
                        lambda x: x.split(".")[0] if "." in x else x
                    )

                # 处理 change_pct：akshare 通常返回百分比数值，但兼容小数形式
                if "change_pct" in df.columns:
                    df["change_pct"] = pd.to_numeric(df["change_pct"], errors="coerce")
                    # 如果最大值绝对值小于1且不为0，大概率是小数形式，乘100
                    max_abs = df["change_pct"].abs().max()
                    if pd.notna(max_abs) and max_abs > 0 and max_abs < 1:
                        df["change_pct"] = df["change_pct"] * 100

                # 数值列转换
                numeric_cols = ["close", "open", "high", "low", "volume", "amount", "pre_close"]
                for col in numeric_cols:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce")

                # 选择统一列
                unified_cols = [
                    "code", "name", "close", "open", "high", "low",
                    "volume", "amount", "change_pct", "pre_close",
                ]
                for col in unified_cols:
                    if col not in df.columns:
                        df[col] = None
                df = df[unified_cols].copy()

                print(f"[AkshareData] 全市场快照获取成功, 共 {len(df)} 条")
                return df

            except Exception as e:
                print(f"[AkshareData] 获取全市场快照失败 (尝试 {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    import time
                    time.sleep(2 ** attempt)
                else:
                    return pd.DataFrame()

    def get_multi_stock_daily(
        self, symbols: list, start_date: str, end_date: str
    ) -> dict:
        """批量获取多只股票日线数据
        symbols: 股票代码列表（纯数字格式如 ['600000', '000001']）
        start_date/end_date: 'YYYYMMDD' 格式

        返回: {symbol: DataFrame, ...}
        每个 DataFrame 格式同 get_daily_bars: date, open, high, low, close, volume, amount

        注意：
        - 需要加 try/except 处理单只股票获取失败的情况
        - 每次请求之间加短暂延时(0.1-0.3s)避免被限频
        - 打印进度信息（每100只打印一次）
        """
        print(
            f"[AkshareData] 批量获取日线数据: 共 {len(symbols)} 只, "
            f"起始={start_date}, 结束={end_date}"
        )

        result = {}
        total = len(symbols)

        for idx, symbol in enumerate(symbols, start=1):
            if idx % 100 == 0 or idx == total:
                print(f"[AkshareData] 批量获取进度: {idx}/{total}")

            try:
                df = self.get_daily_bars(symbol, start_date, end_date)
                if not df.empty:
                    result[symbol] = df
            except Exception as e:
                print(f"[AkshareData] 获取 {symbol} 日线失败: {e}")

            # 短暂延时，避免被限频
            time.sleep(0.3 + random.random() * 0.4)

        print(f"[AkshareData] 批量获取完成, 成功 {len(result)}/{total} 只")
        return result
