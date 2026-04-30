import os
import pandas as pd


class LocalDailyData:
    r"""本地CSV日线数据源 - D:\daily_data"""

    def __init__(self, data_dir='D:/daily_data'):
        self.data_dir = data_dir
        self.sh_dir = os.path.join(data_dir, 'SH')
        self.sz_dir = os.path.join(data_dir, 'SZ')

    def _get_filepath(self, symbol):
        """根据股票代码确定文件路径"""
        # 6开头在SH，其他在SZ
        if symbol.startswith('6'):
            return os.path.join(self.sh_dir, f'price_{symbol}.csv')
        else:
            return os.path.join(self.sz_dir, f'price_{symbol}.csv')

    def get_daily_bars(self, symbol, start_date, end_date):
        """读取本地CSV，返回统一格式 DataFrame
        返回列: date(datetime), open, high, low, close, volume, amount
        """
        filepath = self._get_filepath(symbol)
        if not os.path.exists(filepath):
            return pd.DataFrame()

        df = pd.read_csv(filepath)
        if df.empty or len(df) <= 1:
            return pd.DataFrame()

        # 列名映射
        df = df.rename(columns={'timetag': 'date', 'volumn': 'volume'})

        # 日期转换
        df['date'] = pd.to_datetime(df['date'].astype(str), format='%Y%m%d')

        # 日期过滤
        start_dt = pd.to_datetime(start_date, format='%Y%m%d')
        end_dt = pd.to_datetime(end_date, format='%Y%m%d')
        df = df[(df['date'] >= start_dt) & (df['date'] <= end_dt)]

        # 确保列顺序和类型
        cols = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount']
        for col in cols:
            if col not in df.columns:
                return pd.DataFrame()
        df = df[cols].sort_values('date').reset_index(drop=True)

        return df

    def get_stock_list(self):
        """获取本地数据中所有股票代码列表"""
        codes = []
        for d in [self.sh_dir, self.sz_dir]:
            if os.path.exists(d):
                for f in os.listdir(d):
                    if f.startswith('price_') and f.endswith('.csv'):
                        code = f.replace('price_', '').replace('.csv', '')
                        # 检查文件是否有数据（大于200字节避免空文件）
                        filepath = os.path.join(d, f)
                        if os.path.getsize(filepath) > 200:
                            codes.append(code)
        return sorted(codes)

    def get_multi_stock_daily(self, symbols, start_date, end_date):
        """批量获取多只股票日线"""
        result = {}
        total = len(symbols)
        for i, symbol in enumerate(symbols):
            if (i + 1) % 500 == 0:
                print(f"  加载本地数据: {i+1}/{total}")
            df = self.get_daily_bars(symbol, start_date, end_date)
            if not df.empty:
                result[symbol] = df
        print(f"  本地数据加载完成: {len(result)}/{total} 只股票有数据")
        return result
