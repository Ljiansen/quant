# -*- coding: utf-8 -*-
"""
baostock 数据接口封装
提供统一格式的股票行情、财务数据查询功能
"""

import datetime

import pandas as pd

import baostock as bs


class BaostockData:
    """baostock 数据源封装类"""

    def __init__(self):
        """初始化并登录 baostock"""
        self._login_status = False
        self._login()

    def _login(self):
        """登录 baostock"""
        try:
            lg = bs.login()
            if lg.error_code == "0":
                self._login_status = True
                print("[BaostockData] baostock 登录成功")
            else:
                self._login_status = False
                print(
                    f"[BaostockData] baostock 登录失败: "
                    f"code={lg.error_code}, msg={lg.error_msg}"
                )
        except Exception as e:
            self._login_status = False
            print(f"[BaostockData] baostock 登录异常: {e}")

    def _ensure_login(self):
        """确保已登录，若未登录则尝试重新登录"""
        if not self._login_status:
            print("[BaostockData] 检测到未登录，尝试重新登录...")
            self._login()

    def _to_baostock_code(self, symbol: str) -> str:
        """
        将股票代码转换为 baostock 格式
        如 '600000' -> 'sh.600000', '000001' -> 'sz.000001'
        """
        symbol = symbol.strip().upper()
        if symbol.startswith("SH.") or symbol.startswith("SZ."):
            parts = symbol.split(".")
            return f"{parts[0].lower()}.{parts[1]}"
        if "." in symbol:
            parts = symbol.split(".")
            return f"{parts[1].lower()}.{parts[0]}"
        # 根据代码规则判断交易所
        if symbol.startswith("6"):
            return f"sh.{symbol}"
        else:
            return f"sz.{symbol}"

    @staticmethod
    def _normalize_date(date_str: str) -> str:
        """
        将日期格式统一转换为 baostock 所需的 'YYYY-MM-DD' 格式
        支持输入格式: 'YYYYMMDD' 或 'YYYY-MM-DD'
        """
        date_str = date_str.strip()
        if len(date_str) == 8 and date_str.isdigit():
            return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
        return date_str

    def get_daily_bars(
        self, symbol: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        """
        获取日线行情，返回统一格式 DataFrame

        Args:
            symbol: 股票代码，如 '600000' 或 '000001'
            start_date: 开始日期，格式 'YYYYMMDD' 或 'YYYY-MM-DD'
            end_date: 结束日期，格式 'YYYYMMDD' 或 'YYYY-MM-DD'

        Returns:
            DataFrame, 统一列: date, open, high, low, close, volume, amount
        """
        code = self._to_baostock_code(symbol)
        start = self._normalize_date(start_date)
        end = self._normalize_date(end_date)
        print(
            f"[BaostockData] 获取日线: {code}, 起始={start}, 结束={end}"
        )

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            self._ensure_login()
            try:
                fields = "date,open,high,low,close,volume,amount"
                rs = bs.query_history_k_data_plus(
                    code=code,
                    fields=fields,
                    start_date=start,
                    end_date=end,
                    frequency="d",
                    adjustflag="2",  # 前复权
                )

                if rs.error_code != "0":
                    print(
                        f"[BaostockData] 查询失败: code={rs.error_code}, "
                        f"msg={rs.error_msg}"
                    )
                    # 网络错误时重试
                    if "网络" in rs.error_msg or "10038" in str(rs.error_code):
                        if attempt < max_retries:
                            print(f"[BaostockData] 网络错误，正在重试 ({attempt}/{max_retries})...")
                            self._login_status = False
                            try:
                                bs.logout()
                            except Exception:
                                pass
                            import time
                            time.sleep(1 + attempt)
                            continue
                    return pd.DataFrame(
                        columns=["date", "open", "high", "low", "close", "volume", "amount"]
                    )

                data_list = []
                while (rs.error_code == "0") & rs.next():
                    data_list.append(rs.get_row_data())

                if not data_list:
                    print(f"[BaostockData] 未获取到数据: {code}")
                    return pd.DataFrame(
                        columns=["date", "open", "high", "low", "close", "volume", "amount"]
                    )

                df = pd.DataFrame(data_list, columns=rs.fields)

                # 转换数据类型
                df["date"] = pd.to_datetime(df["date"])
                for col in ["open", "high", "low", "close", "volume", "amount"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce")

                # 确保列顺序统一
                unified_cols = ["date", "open", "high", "low", "close", "volume", "amount"]
                for col in unified_cols:
                    if col not in df.columns:
                        df[col] = None
                df = df[unified_cols].copy()

                print(f"[BaostockData] 获取日线成功: {code}, 共 {len(df)} 条")
                return df

            except Exception as e:
                print(f"[BaostockData] 获取日线失败: {code}, 错误: {e}")
                if attempt < max_retries:
                    print(f"[BaostockData] 正在重试 ({attempt}/{max_retries})...")
                    self._login_status = False
                    try:
                        bs.logout()
                    except Exception:
                        pass
                    import time
                    time.sleep(1 + attempt)
                else:
                    return pd.DataFrame(
                        columns=["date", "open", "high", "low", "close", "volume", "amount"]
                    )

        return pd.DataFrame(
            columns=["date", "open", "high", "low", "close", "volume", "amount"]
        )

    def get_financial_data(self, symbol: str) -> pd.DataFrame:
        """
        获取财务数据（盈利数据）

        Args:
            symbol: 股票代码，如 '600000' 或 '000001'

        Returns:
            DataFrame, 财务盈利数据
        """
        self._ensure_login()
        code = self._to_baostock_code(symbol)
        print(f"[BaostockData] 获取财务数据: {code}")

        try:
            # 获取当前年份和季度
            now = datetime.datetime.now()
            year = now.year
            quarter = (now.month - 1) // 3 + 1

            rs = bs.query_profit_data(
                code=code, year=year, quarter=quarter
            )

            if rs.error_code != "0":
                print(
                    f"[BaostockData] 查询财务数据失败: "
                    f"code={rs.error_code}, msg={rs.error_msg}"
                )
                return pd.DataFrame()

            data_list = []
            while (rs.error_code == "0") & rs.next():
                data_list.append(rs.get_row_data())

            if not data_list:
                print(f"[BaostockData] 未获取到财务数据: {code}")
                return pd.DataFrame()

            df = pd.DataFrame(data_list, columns=rs.fields)

            print(f"[BaostockData] 财务数据获取成功: {code}, 共 {len(df)} 条")
            return df

        except Exception as e:
            print(f"[BaostockData] 获取财务数据失败: {code}, 错误: {e}")
            return pd.DataFrame()

    def logout(self):
        """登出 baostock"""
        try:
            bs.logout()
            self._login_status = False
            print("[BaostockData] baostock 已登出")
        except Exception as e:
            print(f"[BaostockData] baostock 登出异常: {e}")

    def __del__(self):
        """析构时自动登出"""
        if self._login_status:
            self.logout()
