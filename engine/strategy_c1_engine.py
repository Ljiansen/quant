# -*- coding: utf-8 -*-
"""
策略C1 — 温和下跌反弹选超跌股
纯日线策略，与策略T/BA策略完全独立

信号条件(3个同时满足):
1. 上证7个交易日跌幅在 [-5%, -3%] 之间
2. 是连续触发窗口的"第一天"（前一交易日不满足条件1）
3. 当日上证收盘 > 前一日收盘（止跌反弹确认）

选股: 3日跌幅最大的Top5（用T-1日数据，反lookahead）
过滤: 20日均成交额>500万 + 股价>2元 + 近5日无停牌 + 涨跌停正确区分
买入: 信号日T+1开盘买入, 买入价 = open * (1 + 0.5%)
卖出: 持有15个交易日后T+1开盘卖出, 卖出价 = open * (1 - 0.5%)
      到期日涨跌停无法卖出时顺延到下一个可卖日

与策略T信号互斥: T是7d跌>5%, C1是7d跌-5%~-3%, 不会同一天触发
"""
import os
import sys
import math
import json
import time
import numpy as np
import pandas as pd
from datetime import datetime, date
from typing import Dict, List, Optional

# ═══════════════════════════════════════════════
# C1 策略参数
# ═══════════════════════════════════════════════
C1_SH_7D_LOW = -0.05           # 上证7日跌幅下限(含)
C1_SH_7D_HIGH = -0.03          # 上证7日跌幅上限(含)
C1_HOLD_DAYS = 15              # 固定持有天数
C1_TOP_N = 5                   # 选股数量
C1_STOCK_RET_LOOKBACK = 3      # 选股因子: 3日跌幅
C1_MIN_AMOUNT_20D = 5_000_000  # 20日均成交额下限
C1_MIN_PRICE = 2.0             # 股价下限(排除ST/面退)
C1_BUY_SLIP = 0.005            # 买入滑点0.5%
C1_SELL_SLIP = 0.005           # 卖出滑点0.5%

# 日线数据目录
DAILY_DATA_DIR = 'D:/daily_data'

# 状态/交易记录文件(与T/BA完全隔离)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
C1_STATE_FILE = os.path.join(BASE_DIR, 'state_c1.json')
C1_TRADES_FILE = os.path.join(BASE_DIR, 'trades_c1.json')


def _now_str():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _get_limit_threshold(code: str) -> float:
    """创业板(300/301)/科创板(688/689)涨跌停20%, 主板10%"""
    if code.startswith(('300', '301', '688', '689')):
        return 0.195
    return 0.095


def _load_daily_csv(code: str) -> Optional[pd.DataFrame]:
    """加载单只股票日线CSV"""
    sub = 'SH' if (code.startswith('6') or code.startswith('5')) else 'SZ'
    path = os.path.join(DAILY_DATA_DIR, sub, f'price_{code}.csv')
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path)
        df = df.rename(columns={'timetag': 'date', 'volumn': 'volume'})
        df['date'] = pd.to_datetime(df['date'].astype(str), format='%Y%m%d')
        df = df.sort_values('date').reset_index(drop=True)
        for col in ['open', 'high', 'low', 'close', 'volume', 'amount']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
        return df
    except Exception:
        return None


def _load_sh_index() -> Optional[pd.DataFrame]:
    """加载上证指数日线"""
    for path in [
        os.path.join(DAILY_DATA_DIR, 'SH', 'price_sh000001.csv'),
        os.path.join(DAILY_DATA_DIR, 'INDEX', 'sh000001_daily.csv'),
    ]:
        if os.path.exists(path):
            try:
                df = pd.read_csv(path)
                if 'timetag' in df.columns:
                    df = df.rename(columns={'timetag': 'date'})
                    df['date'] = pd.to_datetime(df['date'].astype(str), format='%Y%m%d')
                else:
                    df['date'] = pd.to_datetime(df['date'])
                df = df.sort_values('date').reset_index(drop=True)
                for col in ['open', 'high', 'low', 'close', 'volume']:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
                return df
            except Exception:
                continue
    return None


class StrategyC1Engine:
    """
    策略C1 回测引擎（纯日线策略）

    用矩阵化方式批量计算，与 strategy_t_v4.py 的 StrategyTV4 结构类似。
    """

    def __init__(self, capital: float = 90_000.0):
        self.capital = capital

    def run_backtest(self, start_date: str, end_date: str) -> dict:
        """运行C1回测"""
        print(f"[{_now_str()}] [C1] 加载数据...")

        # 加载上证指数
        sh_df = _load_sh_index()
        if sh_df is None:
            print(f"[{_now_str()}] [C1] ⚠️ 无法加载上证指数!")
            return {'total_trades': 0, 'error': 'no_sh_index'}

        # 加载全部个股
        all_stocks = {}
        for sub in ['SH', 'SZ']:
            sub_dir = os.path.join(DAILY_DATA_DIR, sub)
            if not os.path.isdir(sub_dir):
                continue
            for fn in os.listdir(sub_dir):
                if not fn.startswith('price_') or not fn.endswith('.csv'):
                    continue
                code = fn.replace('price_', '').replace('.csv', '')
                # 排除指数文件(sh000xxx/sz399xxx混入个股目录)
                if code.startswith(('sh', 'sz', 'SH', 'SZ')):
                    continue
                df = _load_daily_csv(code)
                if df is not None:
                    all_stocks[code] = df

        print(f"[{_now_str()}] [C1] 股票数: {len(all_stocks)}")

        # 构建矩阵
        sh_df_indexed = sh_df.set_index('date')
        sh_close = sh_df_indexed['close']

        # 往前扩展数据(算因子需要历史)
        full_start = pd.Timestamp(start_date) - pd.Timedelta(days=60)
        date_range = sh_close.loc[full_start:end_date].index

        close_df = pd.DataFrame(
            {code: df.set_index('date')['close'].reindex(date_range)
             for code, df in all_stocks.items()},
            index=date_range
        )
        open_df = pd.DataFrame(
            {code: df.set_index('date')['open'].reindex(date_range)
             for code, df in all_stocks.items()},
            index=date_range
        )
        amount_df = pd.DataFrame(
            {code: df.set_index('date')['amount'].reindex(date_range)
             for code, df in all_stocks.items()},
            index=date_range
        )
        vol_df = pd.DataFrame(
            {code: df.set_index('date')['volume'].reindex(date_range)
             for code, df in all_stocks.items()},
            index=date_range
        )

        prev_close = close_df.shift(1)
        sh_ret_1d = sh_close.reindex(date_range) / sh_close.reindex(date_range).shift(1) - 1
        sh_ret_7d = sh_close.reindex(date_range) / sh_close.reindex(date_range).shift(7) - 1

        # 选股因子: 3日跌幅(用T-1数据, 反lookahead)
        ret_3d = prev_close / close_df.shift(4) - 1

        # 流动性
        avg_amount_20 = amount_df.rolling(20).mean().shift(1)
        liquid_mask = avg_amount_20 > C1_MIN_AMOUNT_20D

        # 涨跌停(区分板块)
        open_ret = open_df / prev_close - 1
        is_wide = pd.DataFrame(False, index=close_df.index, columns=close_df.columns)
        for c in close_df.columns:
            if c.startswith(('300', '301', '688', '689')):
                is_wide[c] = True
        limit_thresh = is_wide.astype(float) * 0.195 + (~is_wide).astype(float) * 0.095
        not_limit = open_ret.abs() < limit_thresh

        # ST过滤: 股价>2元 + 近5日无停牌
        price_ok = prev_close > C1_MIN_PRICE
        vol_min5 = vol_df.rolling(5).min().shift(1)
        no_halt = vol_min5 > 0

        tradeable = liquid_mask & not_limit & prev_close.notna() & (prev_close > 0) & price_ok & no_halt

        # 交易日序列(只取回测区间)
        bt_start = pd.Timestamp(start_date)
        all_dates = sorted([d for d in close_df.index if d >= bt_start])
        dates_arr = np.array(all_dates)

        # T策略信号日(互斥排除)
        t_signal_dates = set(
            dt for dt in all_dates
            if dt in sh_ret_7d.index and not pd.isna(sh_ret_7d.get(dt))
            and sh_ret_7d.get(dt) <= -0.05
        )

        # C1信号日
        u_base = [
            dt for dt in all_dates
            if dt in sh_ret_7d.index and not pd.isna(sh_ret_7d.get(dt))
            and C1_SH_7D_LOW <= sh_ret_7d.get(dt) <= C1_SH_7D_HIGH
            and dt not in t_signal_dates
        ]
        u_set = set(u_base)

        # "首日"判定: 前一交易日不在u_base中
        u_first = []
        for dt in u_base:
            di = np.searchsorted(dates_arr, dt)
            if di == 0 or dates_arr[di - 1] not in u_set:
                u_first.append(dt)

        # 最终信号: 首日 + 当日上证上涨
        c1_signals = [
            dt for dt in u_first
            if dt in sh_ret_1d.index and not pd.isna(sh_ret_1d.get(dt))
            and sh_ret_1d.get(dt) > 0
        ]

        print(f"[{_now_str()}] [C1] 回测期: {start_date} ~ {end_date}")
        print(f"[{_now_str()}] [C1] C1信号日: {len(c1_signals)}, T信号日(排除): {len(t_signal_dates)}")

        # 回测主循环
        trades = []
        for sig_dt in c1_signals:
            di = np.searchsorted(dates_arr, sig_dt)
            if di + 1 >= len(dates_arr):
                continue
            buy_dt = dates_arr[di + 1]

            # 选股
            if sig_dt not in tradeable.index:
                continue
            mask = tradeable.loc[sig_dt]
            valid = mask[mask].index.tolist()
            if not valid or sig_dt not in ret_3d.index:
                continue
            scores = ret_3d.loc[sig_dt, valid].dropna().nsmallest(C1_TOP_N)

            for code, score in scores.items():
                if buy_dt not in open_df.index or code not in open_df.columns:
                    continue
                buy_open = open_df.at[buy_dt, code]
                buy_prev = prev_close.at[buy_dt, code]
                if pd.isna(buy_open) or pd.isna(buy_prev) or buy_open <= 0:
                    continue
                code_limit = _get_limit_threshold(code)
                if abs(buy_open / buy_prev - 1) >= code_limit:
                    continue

                buy_price = buy_open * (1 + C1_BUY_SLIP)
                # 等额分配
                alloc = self.capital / C1_TOP_N
                qty = math.floor(alloc / buy_price / 100) * 100
                if qty < 100:
                    continue

                # 卖出: 持hold_days天后T+1开盘, 涨跌停则顺延
                sell_price = None
                sell_dt_final = None
                for extra in range(0, 10):
                    si = di + 1 + C1_HOLD_DAYS + extra
                    if si >= len(dates_arr):
                        break
                    sdt = dates_arr[si]
                    if sdt not in open_df.index:
                        continue
                    so = open_df.at[sdt, code]
                    sp = prev_close.at[sdt, code]
                    if pd.isna(so) or so <= 0:
                        continue
                    if not pd.isna(sp) and abs(so / sp - 1) < code_limit:
                        sell_price = so * (1 - C1_SELL_SLIP)
                        sell_dt_final = sdt
                        break

                if sell_price and sell_price > 0:
                    pnl = sell_price / buy_price - 1
                    trades.append({
                        'signal_date': str(sig_dt.date()),
                        'buy_date': str(buy_dt.date()),
                        'sell_date': str(sell_dt_final.date()),
                        'code': code,
                        'buy_price': round(buy_price, 3),
                        'sell_price': round(sell_price, 3),
                        'qty': qty,
                        'pnl': round(pnl, 6),
                        'pnl_amount': round(pnl * buy_price * qty, 2),
                        'year': sig_dt.year,
                        'sh_7d': round(float(sh_ret_7d.get(sig_dt, 0)), 4),
                        'sh_1d': round(float(sh_ret_1d.get(sig_dt, 0)), 4),
                    })

        # 汇总
        result = self._summarize(trades)
        return result

    def _summarize(self, trades: list) -> dict:
        if not trades:
            print(f"[{_now_str()}] [C1] 无交易")
            return {'total_trades': 0, 'trades': []}

        trades_df = pd.DataFrame(trades)
        n = len(trades_df)
        win_rate = (trades_df['pnl'] > 0).mean()
        avg_pnl = trades_df['pnl'].mean()

        yearly = {}
        for y in sorted(trades_df['year'].unique()):
            t = trades_df[trades_df['year'] == y]
            yearly[str(y)] = {
                'trades': len(t),
                'win_rate': round((t['pnl'] > 0).mean(), 4),
                'avg_pnl': round(t['pnl'].mean(), 6),
            }

        print(f"\n[{_now_str()}] [C1] === 回测结果 ===")
        print(f"[C1] {n}笔 胜率={win_rate:.0%} 均收={avg_pnl:+.2%}")
        for y, info in yearly.items():
            print(f"[C1]   {y}: {info['trades']}笔 {info['win_rate']:.0%} {info['avg_pnl']:+.2%}")

        # 逐信号日明细
        print(f"\n[C1] --- 逐信号日明细 ---")
        for sig in sorted(set(t['signal_date'] for t in trades)):
            batch = [t for t in trades if t['signal_date'] == sig]
            stocks = ', '.join([f"{t['code']}({t['pnl']:+.1%})" for t in batch])
            batch_avg = np.mean([t['pnl'] for t in batch])
            print(f"[C1]   {sig}: {len(batch)}笔 均收={batch_avg:+.1%} [{stocks}]")

        return {
            'total_trades': n,
            'win_rate': round(win_rate, 4),
            'avg_pnl': round(avg_pnl, 6),
            'yearly': yearly,
            'trades': trades,
        }
