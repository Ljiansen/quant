# -*- coding: utf-8 -*-
"""
V4策略实盘引擎 —— OPT-bull (BA选股 + 5min入场 + V3 trail/hs出场 + DYN仓位管理)

策略规格来源：quant.txt (2026-05-15终版)
回测业绩：+194.92% / MDD 11.37% / Sharpe 2.92 (2025-01-01 ~ 2026-04-30, 16个月)

关键参数(不许改):
  MAX_POSITIONS=5(Bull)/4(Chop), HARD_STOP=6.5%,
  TRAIL_ACT=40%(Bull)/25%(Chop), TRAIL_STOP=12%(Bull)/8%(Chop)
  MIN_CHG主板=1%, MAX_CHG主板=3.5%, STAR_MIN=0.1%, STAR_MAX=5.5%
  VOL_RATIO=[1.5,3.0], GAP_MIN=+0.5%(今开>昨收), COOL_RET_MAX=100%(≈关闭), BA_CHG=[1%,7%]
  COMMISSION=万0.854, STAMP=千0.5, SLIPPAGE=0.015%
"""

# ─── G3.4 升级说明 (2026-05-18) ────────────────────────────────
# 对齐源: run_g34_verify.py (mac, 三审通过)
# 业绩: +278.58% (24=-22.42%/22=-22.42%/23=-18.74%/25=+242.72%/26=+69.56%)
# Bull/Chop_init/Chop_else 三态 + 三层安全网
# ─────────────────────────────────────────────────────────────────

import json
import math
import os
import subprocess
import sys
import time
import traceback
from bisect import bisect_right
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

sys.path.insert(0, 'd:/miniqmt_quant')
import config as _cfg

# xtquant
try:
    from xtquant import xtdata as _xtdata
    from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
    from xtquant.xttype import StockAccount
    _XT_OK = True
except Exception:
    _XT_OK = False
    _xtdata = None

# TradeExecutor (V3验证可用的交易封装)
try:
    from trade.executor import TradeExecutor as _TradeExecutor
    _EXECUTOR_OK = True
except Exception:
    _EXECUTOR_OK = False
    _TradeExecutor = None

# 可选：钉钉通知
try:
    from utils.notifier import notify_buy as _notify_buy
    from utils.notifier import notify_sell as _notify_sell
    from utils.notifier import notify_system as _notify_system
    from utils.notifier import notify_buy_signal as _notify_buy_signal
    from utils.notifier import notify_sell_signal as _notify_sell_signal
    from utils.notifier import notify_pending_sell as _notify_pending_sell
    _NOTIFIER_OK = True
except Exception:
    _NOTIFIER_OK = False

# ───────────────────────────────────────────────────────────
# 全局常量 (OPTIMAL_KW / OPTIMAL_MODULE，逐字翻译quant.txt第7节)
# ───────────────────────────────────────────────────────────
MAX_POSITIONS       = 5
DYNAMIC_POSITION    = True          # G3.4: 启用动态仓位
DYN_PNL_THRESHOLD   = 0.0
DYN_SH_EXEMPT       = True          # G3.4: 安全网降仓时现有持仓豁免（不强卖）

# 买入涨幅阈值
MIN_CHG             = 0.01       # 主板最低涨幅
MAX_CHG             = 0.035      # 主板最高涨幅(防追高)
STAR_MIN_CHG        = 0.001      # 科创/创业板最低涨幅
STAR_MAX_CHG        = 0.055      # 科创/创业板最高涨幅

# ─── G3.4 regime-aware 参数包 ─────────────────────────────
# 对齐: run_g34_verify.py COMMON + per_day_fn + per_stock_fn
# G3.7 升级 (2026-05-19): init_bnd 动态化 + 宏观熊安全网
G34_PARAMS = dict(
    # Bull 段 (SH >= MA20)
    bull_mp=5,   bull_hs=0.065, bull_ta=0.40, bull_ts=0.12,
    # Chop_init 段 (SH < MA20, streak ∈ [1, cur_init_bnd])
    chop_init_mp=4, chop_init_hs=0.068, chop_init_ta=0.24, chop_init_ts=0.010,
    # Chop_else 段 (SH < MA20, streak > cur_init_bnd)
    chop_else_mp=3, chop_else_hs=0.085, chop_else_ta=0.22, chop_else_ts=0.08,
    # G3.7: Regime 切换边界 (动态): close > MA60 → 3, 否则 → 5
    init_bnd_bull=3,
    init_bnd_chop=5,
    # 安全网 (基于 prev_day SH 特征)
    panic_thr=-0.06,            # ret_30d < -0.06 → mp=0 (空仓)
    vol_thr=0.022,              # vol_30d > 0.022 → mp=0 (空仓)
    chop_else_ret5_min=-0.01,   # chop_else段额外: ret_5d < -0.01 → mp=0
    # G3.7 新增: 宏观熊安全网
    macro_bear_thr=-0.05,       # ret_60d < -0.05 → mp=0 (空仓)
)

# Bull 段参数作为模块级别名（供现有代码兼容）
HARD_STOP           = G34_PARAMS['bull_hs']   # 0.065
NEW_STOCK_HARD_STOP = G34_PARAMS['bull_hs']   # 0.065
TRAIL_ACT           = G34_PARAMS['bull_ta']   # 0.40
TRAIL_STOP          = G34_PARAMS['bull_ts']   # 0.12
STOP_LIMIT_SLIP     = 0.002      # 条件单fill偏差0.2%(trail用)
ENABLE_TIME_STOP    = False      # 时间止损关闭

# 涨停保护
LIMIT_UP_MAIN       = 0.098
LIMIT_UP_STAR       = 0.198

# 手续费
COMMISSION_RATE     = 0.0000854  # 万0.854
MIN_COMMISSION      = 5.0
STAMP_TAX_RATE      = 0.0005
SLIPPAGE            = 0.00015
AUCTION_FACTOR      = 0.99       # 集合竞价限价=昨收×0.99

# 过滤参数
DAILY_MIN_AMOUNT    = 100_000_000  # 1亿日均额下限
DAILY_AMOUNT_DAYS   = 10
VOL_RATIO_MIN       = 1.5
VOL_RATIO_MAX       = 3.0
GAP_MIN             = 0.005     # G1: 今开 > 昨收 +0.5% 才入场 (Phase5 对齐)

# 冷却队列
COOL_RET_MAX        = 1       # 近20日累计>100%才入冷却队列（≈关闭过热保护；原值0.4即40%会吃掉大量利润）
COOL_DAYS_MAX       = 15

# E1 死票早卖
E1_LOOKBACK_N       = 0
E1_RED_RATIO        = 1.0

# BA选股
BA_LOOKBACK         = 120
BA_MA20_DAYS        = 20
BA_MIN_CHG          = 0.01       # A关单日最低涨幅 > 1%
BA_MAX_CHG          = 0.07       # A关单日最高涨幅 < 7%
BA_MIN_HIST         = 60

# 新股
NEW_STOCK_MIN_DAYS  = 20
NEW_STOCK_MAX_DAYS  = 60
MAX_NEW_STOCK_POSITIONS = 1

# 14:55 close模式(A模式：直接卖，不进pending)
TRAIL_CLOSE_MODE    = 'current_day'
VOL_STOP_THRESHOLD  = 999        # 等价关闭
VOL_STOP_POS        = 0.618

# 状态文件路径
BASE_DIR     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE   = os.path.join(BASE_DIR, 'state_v4.json')
TRADES_FILE  = os.path.join(BASE_DIR, 'trades_v4.json')
QUEUE_FILE   = os.path.join(BASE_DIR, 'wait_queue_v4.json')
DEFERRED_FILE= os.path.join(BASE_DIR, 'deferred_sells_v4.json')
PENDING_FILE = os.path.join(BASE_DIR, 'pending_sells_v4.json')

# 日线数据目录
DAILY_DATA_DIR = 'D:/daily_data'

# 5min时间点列表 (48根K)
# 上午: 9:30~9:55(6) + 10:00~10:55(12) + 11:00~11:30(7) = 25根
# 下午: 13:05~13:55(11) + 14:00~14:55(12) = 23根
# 合计: 48根
_TIME_POINTS = (
    [(9, m) for m in range(35, 60, 5)] +   # 9:35,9:40,...,9:55 (不含 9:30，A股首K=9:35)
    [(10, m) for m in range(0, 60, 5)] +   # 10:00,10:05,...,10:55
    [(11, m) for m in range(0, 35, 5)] +   # 11:00,11:05,...,11:30
    [(13, m) for m in range(5, 60, 5)] +   # 13:05,13:10,...,13:55
    [(14, m) for m in range(0, 60, 5)]     # 14:00,14:05,...,14:55
)


# ───────────────────────────────────────────────────────────
# 工具函数
# ───────────────────────────────────────────────────────────

def _now_str() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _format_symbol(code: str) -> str:
    c = str(code).strip().split('.')[0]
    return f"{c}.SH" if (c.startswith('6') or c.startswith('5')) else f"{c}.SZ"


def _strip_suffix(symbol: str) -> str:
    return str(symbol).strip().split('.')[0]


def _is_star(c: str) -> bool:
    """科创板(688)或创业板(30x)"""
    return c.startswith('688') or c.startswith('30')


def _min_chg(c: str) -> float:
    return STAR_MIN_CHG if _is_star(c) else MIN_CHG


def _max_chg(c: str) -> float:
    return STAR_MAX_CHG if _is_star(c) else MAX_CHG


def _limit_up(c: str) -> float:
    return LIMIT_UP_STAR if _is_star(c) else LIMIT_UP_MAIN


def _hard_sl(c: str, pos: Optional[dict] = None) -> float:
    if pos is not None and pos.get('snapshot_hs') is not None:
        return float(pos['snapshot_hs'])
    return HARD_STOP


def _trail_act(c: str, pos: Optional[dict] = None) -> float:
    if pos is not None and pos.get('snapshot_ta') is not None:
        return float(pos['snapshot_ta'])
    return TRAIL_ACT


def _trail_stop_pct(c: str, pos: Optional[dict] = None) -> float:
    if pos is not None and pos.get('snapshot_ts') is not None:
        return float(pos['snapshot_ts'])
    return TRAIL_STOP


def _buy_qty(cash: float, n_pos: int, price: float, max_pos: Optional[int] = None) -> int:
    """资金分配公式 (quant.txt 4.3节，逐字翻译)"""
    cap = max_pos if max_pos is not None else MAX_POSITIONS
    empty = cap - n_pos
    if empty <= 0 or price <= 0:
        return 0
    actual = price * (1 + SLIPPAGE)
    qty = math.floor((cash / empty) / actual / 100) * 100
    return qty if qty >= 100 else 0


def _buy_commission(price: float, qty: int) -> float:
    """买入手续费 (quant.txt 4.4节)"""
    return max(price * (1 + SLIPPAGE) * qty * COMMISSION_RATE, MIN_COMMISSION)


def _sell_net(price: float, qty: int) -> Tuple[float, float, float]:
    """卖出净值 → (net, commission, stamp_tax)"""
    actual = price * (1 - SLIPPAGE)
    commission = max(actual * qty * COMMISSION_RATE, MIN_COMMISSION)
    stamp_tax = actual * qty * STAMP_TAX_RATE
    net = actual * qty - commission - stamp_tax
    return net, commission, stamp_tax


def _market_is_open() -> bool:
    t = datetime.now().hour * 60 + datetime.now().minute
    return 9 * 60 <= t <= 15 * 60 + 1


def _load_json(path: str, default):
    """加载 JSON 文件。文件不存在返回 default；存在但解析失败则 raise（防止持仓被清零）。"""
    if not os.path.exists(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        raise RuntimeError(
            f"[致命] 状态文件 {path} 存在但解析失败: {e}\n"
            f"请手动检查文件内容后再重启，避免持仓被清零。") from e


def _save_json(path: str, obj):
    """原子写：先写 .tmp 再 os.replace，避免写入中断产生半破文件。"""
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as e:
        print(f"[{_now_str()}] 保存文件失败 {path}: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass


# ───────────────────────────────────────────────────────────
# 日线数据加载
# ───────────────────────────────────────────────────────────

def _load_daily_csv(code: str) -> Optional[pd.DataFrame]:
    """从本地 CSV 加载某只股票日线数据"""
    sub = 'SH' if (code.startswith('6') or code.startswith('5')) else 'SZ'
    path = os.path.join(DAILY_DATA_DIR, sub, f'price_{code}.csv')
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path)
        # 字段: timetag,open,high,low,close,volumn,amount
        df = df.rename(columns={'timetag': 'date', 'volumn': 'volume'})
        df['date'] = pd.to_datetime(df['date'].astype(str), format='%Y%m%d')
        df = df.sort_values('date').reset_index(drop=True)
        for col in ['open', 'high', 'low', 'close', 'volume', 'amount']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
        return df
    except Exception as e:
        print(f"[{_now_str()}] 加载日线数据失败 {code}: {e}")
        return None


def _load_sh_index_daily() -> Optional[pd.DataFrame]:
    """加载上证指数日线 (用于 9:30 弱市判断 + 弱势市低动量过滤)

    路径优先级：
    1. D:/daily_data/SH/price_sh000001.csv（akshare 下载的指数专用文件，timetag 格式与 daily_data 一致）
    2. D:/daily_data/INDEX/sh000001_daily.csv（兑底）
    """
    # 优先：专用上证指数文件（close ~ 3000-4500，不是股票数据）
    path1 = os.path.join(DAILY_DATA_DIR, 'SH', 'price_sh000001.csv')
    if os.path.exists(path1):
        try:
            df = pd.read_csv(path1)
            df = df.rename(columns={'timetag': 'date'})
            df['date'] = pd.to_datetime(df['date'].astype(str), format='%Y%m%d')
            df = df.sort_values('date').reset_index(drop=True)
            for col in ['open', 'high', 'low', 'close', 'volume']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
            return df
        except Exception as e:
            print(f"[{_now_str()}] 加载上证指数失败 {path1}: {e}")
    # 兑底：INDEX 子目录
    path2 = os.path.join(DAILY_DATA_DIR, 'INDEX', 'sh000001_daily.csv')
    if os.path.exists(path2):
        try:
            df = pd.read_csv(path2)
            df['date'] = pd.to_datetime(df['date'])
            return df.sort_values('date').reset_index(drop=True)
        except Exception:
            pass
    return None


# ───────────────────────────────────────────────────────────
# BA 选股算法 (quant.txt 2.3节，逐字翻译，不许改)
# ───────────────────────────────────────────────────────────

def compute_ba_pool(daily_data: Dict[str, pd.DataFrame],
                    ref_date_str: str,
                    all_trading_dates: List[str],
                    top_n: int = 50) -> List[Tuple[str, int, int]]:
    """
    BA选股算法。返回 [(code, rank, score), ...] 按rank升序。
    ref_date_str: 'YYYY-MM-DD'，通常为昨日（无lookahead）
    """
    if ref_date_str not in all_trading_dates:
        prev = [d for d in all_trading_dates if d <= ref_date_str]
        if not prev:
            return []
        ref_date_str = prev[-1]

    cur_idx = bisect_right(all_trading_dates, ref_date_str) - 1
    lookback_start = max(0, cur_idx - BA_LOOKBACK)
    ma20_start     = max(0, cur_idx - BA_MA20_DAYS)
    ref_dt         = pd.to_datetime(ref_date_str)

    lb_set   = set(pd.to_datetime(all_trading_dates[lookback_start: cur_idx + 1]))
    ma20_set = set(pd.to_datetime(all_trading_dates[ma20_start:     cur_idx + 1]))

    results = []
    for code, df in daily_data.items():
        # 排除新股：截止ref_date的行数 < 60
        if int((df['date'] <= ref_dt).sum()) < BA_MIN_HIST:
            continue

        period_df = df[df['date'].isin(lb_set)]
        if len(period_df) < 5:
            continue

        ma20_df = df[df['date'].isin(ma20_set)]
        if ma20_df.empty:
            continue

        # B关：close > MA20（float32确保跨机精度一致）
        last_close = np.float32(period_df['close'].iloc[-1])
        ma20       = np.float32(ma20_df['close'].astype('float32').mean())
        if last_close <= ma20:
            continue

        # A关：信号天数（涨幅(1%,7%) + 阳线，全程float32）
        period_sorted = period_df.sort_values('date')
        close32 = period_sorted['close'].astype('float32')
        open32  = period_sorted['open'].astype('float32')
        prev_closes = close32.shift(1)
        chg = (close32 - prev_closes) / prev_closes
        mask = ((chg > np.float32(BA_MIN_CHG)) &   # 严格 > 1%
                (chg < np.float32(BA_MAX_CHG)) &   # 严格 < 7%
                (close32 > open32))
        score = int(mask.sum())
        results.append((code, score))

    if not results:
        return []
    results.sort(key=lambda x: (-x[1], x[0]))   # score降序，同分按code升序（跨机稳定）
    return [(code, i + 1, score) for i, (code, score) in enumerate(results[:top_n])]


# ───────────────────────────────────────────────────────────
# 趋势分类 (quant.txt 3.2节)
# ───────────────────────────────────────────────────────────

def classify_trend(hist_df: pd.DataFrame) -> Tuple:
    """
    返回 (type, ma20, slope, current_price, low_20d, vol)
    type: 'RISING' / 'FALLING' / 'OSCILLATING' / 'UNKNOWN'
    """
    if len(hist_df) < 25:
        return 'UNKNOWN', 0, 0, 0, 0, 0

    close = hist_df['close'].values
    low   = hist_df['low'].values

    ma20      = float(np.mean(close[-20:]))
    ma20_prev = float(np.mean(close[-25:-5]))
    slope     = (ma20 - ma20_prev) / ma20_prev if ma20_prev > 0 else 0

    returns = (close[1:] / close[:-1]) - 1
    vol = float(np.std(returns[-20:])) if len(returns) >= 20 else 0.0

    current_price = float(close[-1])
    low_20d       = float(np.min(low[-20:])) if len(low) >= 20 else float(np.min(low))

    if slope < -0.015:
        return 'FALLING',     ma20, slope, current_price, low_20d, vol
    elif slope > 0.015:
        return 'RISING',      ma20, slope, current_price, low_20d, vol
    else:
        return 'OSCILLATING', ma20, slope, current_price, low_20d, vol


# ───────────────────────────────────────────────────────────
# 上证指数MA缓存 (用于deferred_sells i==0 弱市判断)
# ───────────────────────────────────────────────────────────

def build_sh_ma_cache(sh_df: Optional[pd.DataFrame]) -> Dict[str, tuple]:
    """
    计算上证指数每日 (ma20, slope_5d, close_below_ma) 缓存
    sh_weak = close_below_ma and slope < -0.01
    """
    if sh_df is None or len(sh_df) < 25:
        return {}
    cache = {}
    close_arr = sh_df['close'].values
    dates_arr = sh_df['date'].values
    for i in range(24, len(sh_df)):
        ma20 = float(np.mean(close_arr[i-19:i+1]))
        # 5日MA20斜率
        if i >= 29:
            ma20_prev5 = float(np.mean(close_arr[i-24:i-4]))
            slope = (ma20 - ma20_prev5) / ma20_prev5 if ma20_prev5 > 0 else 0
        else:
            slope = 0.0
        close_below_ma = bool(close_arr[i] < ma20)
        day_str = pd.Timestamp(dates_arr[i]).strftime('%Y-%m-%d')
        cache[day_str] = (ma20, slope, close_below_ma)
    return cache


# ───────────────────────────────────────────────────────────
# G3.4 SH 特征缓存 (1:1 对齐 run_g34_verify.py _load_sh_features, L96-127)
# 每日特征: below / streak / ret_5d / ret_30d / vol_30d
# ───────────────────────────────────────────────────────────

def _load_sh_features_g34(sh_df: Optional[pd.DataFrame]) -> Dict[str, dict]:
    """
    从上证指数日线 DataFrame 计算 G3.7 所需每日特征，返回
    {date_str: {'below': bool, 'streak': int, 'ret_5d': float,
                'ret_30d': float, 'vol_30d': float,
                'close_gt_ma60': bool, 'ret_60d': float}}
    ⚠️ 反 lookahead: 实盘用 prev_trading_day 的特征
    对齐: run_g37_verify.py _load_sh_features() (G3.7 新增 close_gt_ma60 + ret_60d)
    """
    if sh_df is None or len(sh_df) < 61:
        return {}

    close_arr = sh_df['close'].values.astype(float)
    dates_arr = sh_df['date'].values
    n = len(close_arr)

    # log returns（用于 vol_30d）
    log_ret = np.zeros(n)
    log_ret[1:] = np.log(close_arr[1:] / np.where(close_arr[:-1] > 0, close_arr[:-1], 1))

    cache: Dict[str, dict] = {}
    for i in range(60, n):  # 至少需要 60 根数据 (MA60 + ret_60d)
        ma20 = float(np.mean(close_arr[i - 19: i + 1]))
        below = bool(close_arr[i] < ma20)

        # streak: 连续 below 天数
        streak = 0
        if below:
            j = i
            while j >= 0:
                ma20_j = float(np.mean(close_arr[max(0, j - 19): j + 1]))
                if close_arr[j] < ma20_j:
                    streak += 1
                    j -= 1
                else:
                    break

        # ret_5d = (close[i] / close[i-5]) - 1
        ret_5d = float(close_arr[i] / close_arr[i - 5] - 1) if close_arr[i - 5] > 0 else 0.0

        # ret_30d = (close[i] / close[i-30]) - 1
        ret_30d = float(close_arr[i] / close_arr[i - 30] - 1) if close_arr[i - 30] > 0 else 0.0

        # vol_30d = std of log_ret[-30:]
        vol_30d = float(np.std(log_ret[i - 29: i + 1])) if i >= 30 else 0.0

        # G3.7 新增: MA60 / close_gt_ma60 / ret_60d
        ma60 = float(np.mean(close_arr[i - 59: i + 1]))
        close_gt_ma60 = bool(close_arr[i] > ma60)
        ret_60d = float(close_arr[i] / close_arr[i - 60] - 1) if close_arr[i - 60] > 0 else 0.0

        day_str = pd.Timestamp(dates_arr[i]).strftime('%Y-%m-%d')
        cache[day_str] = {
            'below':  below,
            'streak': streak,
            'ret_5d': ret_5d,
            'ret_30d': ret_30d,
            'vol_30d': vol_30d,
            'close_gt_ma60': close_gt_ma60,  # G3.7
            'ret_60d': ret_60d,               # G3.7
        }
    return cache


# ───────────────────────────────────────────────────────────
# 盘后预算 BA pool（日线更新后调用）
# ───────────────────────────────────────────────────────────

def precompute_ba_pool_save(ref_date_str: str,
                            daily_data: Dict[str, pd.DataFrame],
                            all_trading_dates: List[str],
                            top_n: int = 50) -> str:
    """
    盘后预算BA pool并持久化到 ba_pool_v4_{ref_date_str}.json。
    ref_date_str: 通常为今日日期（含今日行情，日线已更新）。
    返回缓存文件路径。
    """
    pool = compute_ba_pool(daily_data, ref_date_str, all_trading_dates, top_n=top_n)
    cache = {
        'ref_date': ref_date_str,
        'computed_at': _now_str(),
        'count': len(pool),
        'pool': pool,
    }
    path = os.path.join(BASE_DIR, f'ba_pool_v4_{ref_date_str}.json')
    _save_json(path, cache)
    print(f"[{_now_str()}] BA pool盘后预算完成: {len(pool)}只 → {path}")
    return path


# ───────────────────────────────────────────────────────────
# LiveEngineV4
# ───────────────────────────────────────────────────────────

class LiveEngineV4:
    """
    V4策略实盘引擎 — OPT-bull
    按 quant.txt 规格逐字实现，不做任何"优化"。

    主循环架构（每30秒poll，对齐5min K时间点）：
      09:00 盘前处理（BA pool + 过滤链 + 持仓状态初始化）
      09:15 挂pending_sells集合竞价限价单
      09:30 gap_min过滤 + 开盘
      09:30~14:55 hm循环（持仓监控 + 买入扫描）
      14:55 deferred兜底 + evaluate_close_signals
      15:30 盘后保存
    """

    ENGINE_NAME = 'V4'

    def __init__(self, account_id: str = '', xt_path: str = '', capital: float = 300_000.0):
        # P0-2: 实盘模式必须安装 xtquant
        if account_id and not _XT_OK:
            raise RuntimeError(
                "❌ 实盘模式 (account_id 非空) 必须安装 xtquant，"
                "当前 _XT_OK=False，拒绝启动（避免假装下单）")

        self.account_id  = account_id
        self.xt_path     = xt_path

        # ── 持仓/资金 ──
        self.positions: Dict[str, dict] = {}   # {code: pos_dict}
        self.cash: float = 0.0
        self.initial_capital: float = capital  # P2-3: 接受构造参数

        # ── 跨日队列 ──
        self.wait_queue: Dict[str, dict]   = {}  # {code: {'score':0,'since_days':N}}
        self.pending_sells: List[dict]     = []  # trail_close_mode='current_day'下通常为空
        self.deferred_sells: Dict[str, dict] = {}

        # ── 行情缓存 ──
        self.bars_today: Dict[str, Dict[tuple, dict]] = {}  # {code: {(h,m): bar}}
        self.prev_close_cache: Dict[str, float] = {}        # {code: 昨收}
        self.day_open_cache:   Dict[str, float] = {}        # {code: 今日9:30 open}
        self.daily_data: Dict[str, pd.DataFrame] = {}       # 已加载的日线数据

        # ── 选股相关 ──
        self.today_pool: List[Tuple[str, int, int]] = []    # [(code,rank,score)]
        self.buy_candidates: List[str] = []
        self._premarket_filtered: Dict[str, str] = {}  # code → 盘前过滤原因（供 dashboard 展示）
        self.stock_meta: Dict[str, dict] = {}
        self.all_trading_dates: List[str] = []
        self.sh_ma_cache: Dict[str, tuple] = {}

        # ── G1→G3.4 Regime-Aware（保留字段名兼容旧state.json）──
        self.cur_max_pos: int = MAX_POSITIONS  # 每天 _on_new_day 按 G3.4 regime 覆盖
        self.cur_regime:  str = 'bull'         # G3.4: 当日 regime 名称（日志/对账用）
        self.sh_g34_cache: Dict[str, dict] = {}  # G3.4 SH特征缓存

        # ── 运行状态 ──
        self._today_str: str = ''
        self._last_increment_date: str = ''
        self._premarket_done: bool = False
        self._gap_filter_done: bool = False
        self._close_check_done: bool = False
        self._auction_done: bool = False
        self._postmarket_done: bool = False     # P1-3: 盘后预算幽等
        self._bought_today: set = set()
        self._processed_hms: set = set()
        self._last_save_ts: float = 0.0         # P2-2: _save_state 节流时间戳

        # ── xtquant 交易封装（复用V3已验证的TradeExecutor）──
        self.executor = None      # TradeExecutor 实例
        self._subscribed_codes: set = set()  # 已成功订阅 xtdata 5min 的代码集合

    # ─────────────────────────────────────────────
    # 启动入口
    # ─────────────────────────────────────────────

    def run(self):
        print(f"[{_now_str()}] [{self.ENGINE_NAME}] ====== 启动 V4 实盘引擎 ======")

        # 连接 miniQMT
        if not self._connect_xt():
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] miniQMT 连接失败，退出")
            return

        # 恢复状态
        self._load_state()

        # 开盘前等待
        while not _market_is_open():
            t = datetime.now().hour * 60 + datetime.now().minute
            if t > 15 * 60 + 5:
                break
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 等待开盘，休眠30秒...")
            time.sleep(30)

        # 主循环
        try:
            while _market_is_open():
                self._tick()
                time.sleep(30)
        except KeyboardInterrupt:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 收到中断信号，退出")

        self._save_state()
        if self.executor is not None:
            try:
                self.executor.disconnect()
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] miniQMT 连接已关闭")
            except Exception as _e:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] executor.disconnect() 异常(忽略): {_e}")
        print(f"[{_now_str()}] [{self.ENGINE_NAME}] ====== V4 引擎已停止 ======")

    # ─────────────────────────────────────────────
    # 主循环 tick（每30秒调用一次）
    # ─────────────────────────────────────────────

    def _tick(self):
        now  = datetime.now()
        h, m = now.hour, now.minute
        today_str = date.today().strftime('%Y-%m-%d')

        # 跨日：days_held递增
        if self._last_increment_date != today_str:
            self._on_new_day(today_str)

        try:
            # ── 08:30~09:14 盘前处理 ──
            _t_min = h * 60 + m
            if 8 * 60 + 30 <= _t_min < 9 * 60 + 15:
                if not self._premarket_done:
                    self._premarket_build(today_str)
                    self._premarket_done = True

            # ── 09:15~09:29 集合竞价挂单 ──
            elif h == 9 and 15 <= m < 30:
                if not self._auction_done:
                    self._execute_pending_auction(today_str)
                    self._auction_done = True

            # ── 09:30~15:00 盘中循环 ──
            elif (h == 9 and m >= 30) or (10 <= h <= 14) or (h == 15 and m == 0):
                # 迟启动补救：若盘前处理未执行（09:15后启动），立即补做
                if not self._premarket_done:
                    print(f"[{_now_str()}] [{self.ENGINE_NAME}] 迟启动补救：补执行盘前处理...")
                    self._premarket_build(today_str)
                    self._premarket_done = True

                # gap_min过滤（9:30后一次性）
                if not self._gap_filter_done:
                    # ⚠️ gap_min 需要今日开盘价（9:30 K 的 open）。
                    # _fetch_5min_bars 必须在 _apply_gap_filter 之前调用，
                    # 否则 bars_today 为空，_get_today_open 返回 0，导致全部候选被误删。
                    _pre_codes = list(set(self.buy_candidates) | set(self.positions.keys()))
                    if _pre_codes:
                        self._fetch_5min_bars(_pre_codes, today_str, (9, 30))
                    self._apply_gap_filter(today_str)
                    self._gap_filter_done = True

                # 获取当前5min K时间点
                hm = self._current_hm(h, m)
                if hm and hm not in self._processed_hms:
                    self._processed_hms.add(hm)
                    self._process_hm(hm, today_str)

                # 14:55 收盘前决策
                if h == 14 and m >= 55 and not self._close_check_done:
                    self._process_hm_1455(today_str)
                    self._close_check_done = True

            # ── 15:30 盘后保存 ──
            elif h == 15 and m >= 30:
                # P1-3: 盘后预算 BA pool（幽等，只跑一次）
                if not self._postmarket_done:
                    try:
                        self.postmarket_precompute(today_str)
                    except Exception as _pm_e:
                        print(f"[{_now_str()}] [{self.ENGINE_NAME}] postmarket异常(不阻断主流程): {_pm_e}")
                        traceback.print_exc()
                    self._postmarket_done = True
                self._save_state(force=True)

        except Exception as e:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] tick异常: {e}")
            traceback.print_exc()

    # ─────────────────────────────────────────────
    # 跨日处理
    # ─────────────────────────────────────────────

    def _on_new_day(self, today_str: str):
        """每天第一次进入时：days_held递增、重置当日标志"""
        # G3.4: 每天按 regime 更新今日容量上限（需等 _premarket_build 加载 sh_g34_cache 后才准确）
        # _on_new_day 在盘前处理之前调用，此时 sh_g34_cache 可能还是空的（昨日数据）
        # → 先做一个临时 regime 决策（基于昨日缓存），_premarket_build 后再刷新
        _dec = self._g34_regime_decide(today_str)
        self.cur_max_pos = int(_dec['max_positions'])
        self.cur_regime  = _dec['regime']
        print(f"[{_now_str()}] [{self.ENGINE_NAME}] [G3.4-预判] regime={_dec['regime']} "
              f"cur_max_pos={self.cur_max_pos} (盘前处理后将刷新)")
        # ↑↑↑ G3.4 结束 ↑↑↑
        for pos in self.positions.values():
            if pos.get('buy_date') != today_str:
                pos['days_held'] = pos.get('days_held', 0) + 1

        self._last_increment_date = today_str
        self._today_str   = today_str
        self._premarket_done   = False
        self._gap_filter_done  = False
        self._close_check_done = False
        self._auction_done     = False
        self._postmarket_done  = False      # P1-3: 新交易日重置盘后预算标志
        self._bought_today     = set()
        self._processed_hms    = set()
        self.bars_today        = {}
        self.day_open_cache    = {}
        self._subscribed_codes = set()  # 新交易日重置订阅集合（xtdata连接可能已恢复）

        # P0-1: 新交易日重置集合竞价提交标志，允许明日重试
        for ps in self.pending_sells:
            ps.pop('auction_submitted', None)
            ps.pop('auction_submit_ts', None)

        # 冷却队列过期清理
        self.wait_queue = {c: v for c, v in self.wait_queue.items()
                           if v.get('since_days', 0) < COOL_DAYS_MAX}
        self._save_state(force=True)  # 新交易日开始关键保存，避免被节流延迟
        print(f"[{_now_str()}] [{self.ENGINE_NAME}] 新交易日 {today_str}，days_held已递增")

    # ─────────────────────────────────────────────
    # 盘前处理 (09:00 执行)
    # ─────────────────────────────────────────────

    def _premarket_build(self, today_str: str):
        """
        9:00 完整盘前流程（quant.txt 8节 阶段1）：
        1. 加载日线数据
        2. 计算BA pool
        3. 过滤链（daily_filter/趋势/rank/冷却/vol_ratio）
        4. 订阅行情
        """
        print(f"[{_now_str()}] [{self.ENGINE_NAME}] 盘前处理开始...")

        # 1. 加载上证指数日线（用于sh_ma_cache + sh_g34_cache）
        sh_df = _load_sh_index_daily()
        self.sh_ma_cache  = build_sh_ma_cache(sh_df)
        self.sh_g34_cache = _load_sh_features_g34(sh_df)

        # 2. 加载所有日线数据（用昨日及之前）
        print(f"[{_now_str()}] [{self.ENGINE_NAME}] 加载日线数据...")
        self._load_all_daily_data(today_str)

        # G3.4: 日线加载完毕后刷新 regime
        # ⚠️ 必须在 _load_all_daily_data 之后：_prev_trading_date 依赖 all_trading_dates 已就绪
        _dec = self._g34_regime_decide(today_str)
        self.cur_max_pos = int(_dec['max_positions'])
        self.cur_regime  = _dec['regime']
        _sp = self._g34_stock_params(today_str)
        _prev_day = self._prev_trading_date(today_str)
        _feat = self.sh_g34_cache.get(_prev_day, {})
        print(
            f"[{_now_str()}] [G3.4] regime={_dec['regime']} prev={_prev_day} "
            f"sh_below={_feat.get('below', '?')} streak={_feat.get('streak', '?')} "
            f"ret_30d={_feat.get('ret_30d', 0.0):+.2%} vol_30d={_feat.get('vol_30d', 0.0):.4f} "
            f"→ max_pos={self.cur_max_pos} hs={_sp['hs']:.3f} "
            f"ta={_sp['trail_act']:.2f} ts={_sp['trail_stop']:.3f}"
        )

        # 3. 计算BA pool（优先读昨晚盘后预算缓存，无缓存则实时计算兜底）
        yest = self._prev_trading_date(today_str)
        if not yest:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 无法获取前一交易日，跳过")
            return
        cache_path = os.path.join(BASE_DIR, f'ba_pool_v4_{yest}.json')
        pool_loaded = False
        if os.path.exists(cache_path):
            try:
                cached = _load_json(cache_path, {})
                if cached.get('ref_date') == yest and cached.get('pool'):
                    self.today_pool = [tuple(x) for x in cached['pool']]
                    print(f"[{_now_str()}] [{self.ENGINE_NAME}] BA pool从缓存加载: "
                          f"{len(self.today_pool)}只 (ref={yest}, "
                          f"预算于{cached.get('computed_at','?')})")
                    pool_loaded = True
                else:
                    print(f"[{_now_str()}] [{self.ENGINE_NAME}] BA pool缓存格式不符，改为实时计算")
            except Exception as e:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] BA pool缓存读取失败({e})，改为实时计算")
        if not pool_loaded:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] BA pool实时计算，ref_date={yest}...")
            self.today_pool = compute_ba_pool(
                self.daily_data, yest, self.all_trading_dates, top_n=50)
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] BA pool: {len(self.today_pool)}只")

        # 4. 过滤链
        self._build_filter_chain(today_str)

        # 5. 构建prev_close_cache
        today_dt = pd.to_datetime(today_str)
        for code in list(self.stock_meta.keys()) + list(self.positions.keys()):
            df = self.daily_data.get(code)
            if df is None:
                continue
            hist = df[df['date'] < today_dt]
            if not hist.empty:
                self.prev_close_cache[code] = float(hist['close'].iloc[-1])

        # 6. 订阅行情
        all_codes = set(self.buy_candidates) | set(self.positions.keys())
        self._subscribe_quotes(list(all_codes))

        # subscribe_quote 是异步的，需等待本地缓存写入完成才能 get_market_data
        # 迟启动场景（盘中重启）等 5 秒，正常 9:00 启动最多等 3 秒
        h_now = datetime.now().hour
        m_now = datetime.now().minute
        _wait = 5 if (h_now > 9 or (h_now == 9 and m_now >= 30)) else 3
        print(f"[{_now_str()}] [{self.ENGINE_NAME}] 等待行情数据加载 {_wait}s...")
        time.sleep(_wait)

        print(f"[{_now_str()}] [{self.ENGINE_NAME}] 盘前处理完成，候选={len(self.buy_candidates)}只，"
              f"订阅={len(all_codes)}只")

    def _load_all_daily_data(self, today_str: str):
        """加载全市场日线CSV"""
        today_dt = pd.to_datetime(today_str)
        codes_to_load: set = set()

        # BA pool已存在则用缓存，否则扫描目录
        if self.today_pool:
            codes_to_load = {c for c, _, _ in self.today_pool}
        else:
            # 扫描SH/SZ目录
            for sub in ['SH', 'SZ']:
                d = os.path.join(DAILY_DATA_DIR, sub)
                if os.path.isdir(d):
                    for f in os.listdir(d):
                        if f.startswith('price_') and f.endswith('.csv'):
                            codes_to_load.add(f[6:-4])

        # 加入当前持仓
        codes_to_load |= set(self.positions.keys())

        loaded = 0
        for code in codes_to_load:
            if code in self.daily_data:
                continue  # 已有缓存
            df = _load_daily_csv(code)
            if df is not None:
                self.daily_data[code] = df
                loaded += 1

        # 更新交易日历（用000001或第一只有数据的股票）
        ref_df = self.daily_data.get('000001')
        if ref_df is None:
            ref_df = next(iter(self.daily_data.values())) if self.daily_data else None
        if ref_df is not None:
            self.all_trading_dates = sorted(
                ref_df[ref_df['date'] < today_dt]['date']
                .dt.strftime('%Y-%m-%d').tolist())

        print(f"[{_now_str()}] [{self.ENGINE_NAME}] 日线数据：新加载{loaded}只，缓存共{len(self.daily_data)}只")

    def _build_filter_chain(self, today_str: str):
        """
        过滤链（quant.txt 3.3节，顺序严格）：
        ① daily_filter（流动性+停牌）
        ② 趋势分类（排除FALLING）
        ③ prioritize_rank（⚠️ 必须在冷却之前）
        ④ 冷却队列
        ⑤ vol_ratio过滤（用昨日volume）
        gap_min在9:30后单独执行
        """
        today_dt = pd.to_datetime(today_str)
        raw_pool = [c for c, _, _ in self.today_pool]
        rank_map = {c: i for i, (c, _, _) in enumerate(self.today_pool)}

        # ① daily_filter + ② 趋势分类
        pool = []
        self.stock_meta = {}
        self._premarket_filtered = {}   # 每日重置；code → 盘前过滤原因
        _cnt_no_data = 0; _cnt_low_hist = 0; _cnt_low_amount = 0; _cnt_falling = 0
        for code in raw_pool:
            df = self.daily_data.get(code)
            if df is None:
                _cnt_no_data += 1
                self._premarket_filtered[code] = '无日线数据'
                continue
            hist = df[df['date'] < today_dt]
            if len(hist) < DAILY_AMOUNT_DAYS:
                _cnt_low_hist += 1
                self._premarket_filtered[code] = '历史数据不足'
                continue
            avg_amount = float(hist['amount'].iloc[-DAILY_AMOUNT_DAYS:].mean())
            if avg_amount < DAILY_MIN_AMOUNT:
                _cnt_low_amount += 1
                self._premarket_filtered[code] = '流动性不足'
                continue
            # ② 趋势分类（用昨日及之前）
            stype, ma20, slope, cp, low20, vol = classify_trend(hist)
            if stype == 'FALLING':
                _cnt_falling += 1
                self._premarket_filtered[code] = '下跌趋势'
                continue
            # is_new_stock判断
            n_rows = int((df['date'] <= today_dt).sum())
            is_new = NEW_STOCK_MIN_DAYS <= n_rows < NEW_STOCK_MAX_DAYS
            pool.append(code)
            self.stock_meta[code] = {
                'type': stype, 'ma20': ma20, 'slope': slope,
                'price': cp, 'low20': low20, 'vol': vol,
                'rsi': 50.0, 'vol_ratio': 1.0, 'is_new': is_new,
                'ret_20d': 0.0,
            }
        print(f"[{_now_str()}] [{self.ENGINE_NAME}] ①②过滤: "
              f"无数据={_cnt_no_data} 历史不足={_cnt_low_hist} "
              f"低流动性={_cnt_low_amount} FALLING={_cnt_falling} → 剩余={len(pool)}")

        # ③ prioritize_rank（在冷却之前！）
        pool.sort(key=lambda c: rank_map.get(c, 9999))

        # ④ 冷却队列（用昨日close算ret_20d）
        for code in pool:
            df = self.daily_data.get(code)
            hist = df[df['date'] < today_dt] if df is not None else pd.DataFrame()
            if len(hist) >= 20:
                c_now = float(hist['close'].iloc[-1])
                c20   = float(hist['close'].iloc[-20])
                self.stock_meta[code]['ret_20d'] = (c_now / c20 - 1) if c20 > 0 else 0.0
            else:
                self.stock_meta[code]['ret_20d'] = 0.0

        # 冷却队列过期清理
        self.wait_queue = {c: v for c, v in self.wait_queue.items()
                           if v.get('since_days', 0) < COOL_DAYS_MAX}
        cooled_list = []
        for code in pool:
            r = self.stock_meta[code]['ret_20d']
            if r > COOL_RET_MAX:
                if code not in self.wait_queue:
                    self.wait_queue[code] = {'score': 0, 'since_days': 0}
            else:
                if code in self.wait_queue:
                    cooled_list.append(code)
                    del self.wait_queue[code]
        # since_days递增
        for v in self.wait_queue.values():
            v['since_days'] = v.get('since_days', 0) + 1

        # buy_candidates = cooled + 未过热，去重保序
        not_hot = [c for c in pool if self.stock_meta[c]['ret_20d'] <= COOL_RET_MAX]
        seen = set()
        buy_candidates = []
        for c in cooled_list + not_hot:
            if c not in seen:
                seen.add(c)
                buy_candidates.append(c)
        # 记录过热股票到 _premarket_filtered
        for code in pool:
            if self.stock_meta[code]['ret_20d'] > COOL_RET_MAX and code not in cooled_list:
                r = self.stock_meta[code]['ret_20d']
                self._premarket_filtered[code] = f'过热({r*100:.0f}%/20d)'

        # ⑤ vol_ratio过滤（用昨日volume）
        filtered = []
        _vr_removed = []
        for code in buy_candidates:
            df = self.daily_data.get(code)
            if df is None:
                continue
            hist = df[df['date'] < today_dt]
            if len(hist) < 21:
                continue
            yest_vol = float(hist['volume'].iloc[-1])
            ma20_vol = float(hist['volume'].iloc[-21:-1].mean())
            if ma20_vol <= 0:
                continue
            vr = yest_vol / ma20_vol
            if VOL_RATIO_MIN <= vr <= VOL_RATIO_MAX:
                filtered.append(code)
            else:
                _vr_removed.append(f"{code}({vr:.2f})")
                self._premarket_filtered[code] = f'量比异常({vr:.2f}x)'

        self.buy_candidates = filtered
        if _vr_removed:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] ⑤vol_ratio移除({VOL_RATIO_MIN}-{VOL_RATIO_MAX}x): "
                  f"{_vr_removed}")

        # ⑥ 弱势市低动量过滤 (BUGFIX 2026-05-19 Root cause #2: 与 mac G3.4 对齐)
        # 之前误以为该规则被 G3.4 安全网替代, 实际 mac precompute/run_backtest.py L1295-1303
        # 在 G3.4 下仍保留 WEAK_MKT_MOMENTUM, 该过滤缺失导致弱市日多买低动量票占仓,
        # 串联全年累计拖累 ~24pp。
        # 规则: 昨日上证 < MA20 (sh_below=True) → 仅保留 ret_20d >= 0.20 的票
        WEAK_MKT_MOMENTUM_THRESHOLD = 0.20
        prev_day_for_sh = self._prev_trading_date(today_str) if hasattr(self, '_prev_trading_date') else None
        if prev_day_for_sh is None:
            # 兜底: 用 sh_ma_cache 里 < today_str 的最大日期
            _avail = [d for d in self.sh_ma_cache.keys() if d < today_str]
            prev_day_for_sh = max(_avail) if _avail else None
        sh_info_wm = self.sh_ma_cache.get(prev_day_for_sh) if prev_day_for_sh else None
        if sh_info_wm is not None:
            _ma20_wm, _slope_wm, _sh_below_wm = sh_info_wm
            if _sh_below_wm:  # 昨日上证 < MA20 = 弱市
                _prev_bc = self.buy_candidates
                self.buy_candidates = [
                    c for c in _prev_bc
                    if self.stock_meta.get(c, {}).get('ret_20d', 0.0) >= WEAK_MKT_MOMENTUM_THRESHOLD
                ]
                for _c in set(_prev_bc) - set(self.buy_candidates):
                    _r = self.stock_meta.get(_c, {}).get('ret_20d', 0.0)
                    self._premarket_filtered[_c] = f'弱市低动量({_r*100:.0f}%/20d)'

        _hot_count = len([c for c in pool if self.stock_meta.get(c, {}).get('ret_20d', 0) > COOL_RET_MAX])
        print(f"[{_now_str()}] [{self.ENGINE_NAME}] 过滤链完成: "
              f"raw={len(raw_pool)} daily_ok={len(pool)} hot={_hot_count} "
              f"vol_ratio_ok={len(filtered)}")
        print(f"[{_now_str()}] [{self.ENGINE_NAME}] 候选股票: {self.buy_candidates}")

    def _apply_gap_filter(self, today_str: str):
        """9:30后执行gap_min过滤（需要today_open）"""
        today_dt = pd.to_datetime(today_str)
        filtered = []
        removed_detail = []
        for code in self.buy_candidates:
            prev_c = self.prev_close_cache.get(code, 0)
            today_open = self._get_today_open(code, today_str)
            if today_open <= 0 or prev_c <= 0:
                removed_detail.append(f"{code}(无开盘价prev={prev_c:.3f} open={today_open:.3f})")
                continue
            gap = (today_open - prev_c) / prev_c
            if gap >= GAP_MIN:
                filtered.append(code)
            else:
                removed_detail.append(f"{code}(gap={gap:+.2%})")
                self._premarket_filtered[code] = f'今开差不足({gap:+.2%})'
        removed = len(self.buy_candidates) - len(filtered)
        self.buy_candidates = filtered
        print(f"[{_now_str()}] [{self.ENGINE_NAME}] gap_min({GAP_MIN:.1%})过滤: "
              f"移除{removed}只→剩余{len(filtered)}只  {removed_detail}")

    # ─────────────────────────────────────────────
    # 5min K 时间点处理
    # ─────────────────────────────────────────────

    def _current_hm(self, h: int, m: int) -> Optional[tuple]:
        """返回当前已完成的5min K时间点（在time_points中找 <= (h,m) 的最后一个）"""
        hm = (h, m)
        result = None
        for tp in _TIME_POINTS:
            if tp <= hm:
                result = tp
            else:
                break
        return result

    def _process_hm(self, hm: tuple, today_str: str):
        """处理一根5min K：持仓监控 + 买入扫描"""
        # 获取全部行情bars
        all_codes = set(self.buy_candidates) | set(self.positions.keys())
        self._fetch_5min_bars(list(all_codes), today_str, hm)

        # ── 候选股实时K线状态打印（每根K一行）
        for _c in self.buy_candidates:
            _b = self.bars_today.get(_c, {}).get(hm)
            _pc = self.prev_close_cache.get(_c, 0)
            if _b:
                _chg = (_b['close'] - _pc) / _pc if _pc > 0 else 0
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] [K] {_c} "
                      f"hm={hm[0]:02d}:{hm[1]:02d} "
                      f"bars={len(self.bars_today.get(_c,{}))}\u6839 "
                      f"O={_b['open']:.3f} H={_b['high']:.3f} "
                      f"L={_b['low']:.3f} C={_b['close']:.3f} "
                      f"chg={_chg:+.2%} vol={int(_b['volume'])}")
            else:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] [K] {_c} "
                      f"hm={hm[0]:02d}:{hm[1]:02d} "
                      f"bars={len(self.bars_today.get(_c,{}))}\u6839 [\u65e0bar\u6570\u636e]")

        # 持仓监控
        # P0-1: 提前计算一次，避免幽环内重复加建集合
        _auction_submitted_codes = {
            ps['code'] for ps in self.pending_sells if ps.get('auction_submitted')
        }
        for code, pos in list(self.positions.items()):
            bar = self.bars_today.get(code, {}).get(hm)
            if bar is None:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] [K-POS] {code} "
                      f"hm={hm[0]:02d}:{hm[1]:02d} 无bar数据，跳过持仓监控")
                continue

            # 更新highest_price（用bar['high']，每根K必须更新）
            pos['highest_price'] = max(pos.get('highest_price', pos.get('buy_price', 0)),
                                       bar['high'])
            # 累积history.bars_5min
            pos.setdefault('history', {'bars_5min': [], 'daily_post_buy': []})
            pos['history']['bars_5min'].append({
                'date': today_str, 'hm': hm,
                'o': bar['open'], 'h': bar['high'],
                'l': bar['low'],  'c': bar['close'], 'v': bar['volume'],
            })

            # ── [K-POS] 持仓状态与止损水位实时打印 ──
            _bp   = pos['buy_price']
            _hp   = pos.get('highest_price', _bp)
            _hs   = _hard_sl(code, pos)
            _hs_px = _bp * (1 - _hs) if _hs > 0 else 0
            _ta   = _trail_act(code, pos)
            _ts   = _trail_stop_pct(code, pos)
            _trail_active = _hp >= _bp * (1 + _ta)
            _trail_px = _hp * (1 - _ts) if _trail_active else None
            _pc_pos = self.prev_close_cache.get(code, _bp)
            _chg_pos = (bar['close'] - _pc_pos) / _pc_pos if _pc_pos > 0 else 0
            _days = pos.get('days_held', 0)
            if _trail_active and _trail_px is not None:
                _lim_trig = _trail_px * (1 - STOP_LIMIT_SLIP)
                _t_str = f"trail={_trail_px:.3f}({'★触发!' if bar['low'] <= _lim_trig else '·'})"
            else:
                _t_str = f"trail=未激活(需涨{_ta:.1%}激活)"
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] [K-POS] {code} "
                  f"hm={hm[0]:02d}:{hm[1]:02d} C={bar['close']:.3f} chg={_chg_pos:+.2%} "
                  f"hp={_hp:.3f} hs={_hs_px:.3f}(-{_hs:.1%}) {_t_str}"
                  + (f" days={_days}" if _days > 0 else " [T+0买入日]"))

            # T+1限制：买入当天days_held=0，只更新不卖出
            if pos.get('days_held', 0) == 0:
                continue

            # P0-1: 集合竞价委托已提交的持仓，跳过日内止换（避免重复挂单）
            if code in _auction_submitted_codes:
                continue

            i = _TIME_POINTS.index(hm) if hm in _TIME_POINTS else -1
            action, reason, sell_price = self._evaluate_intraday_sell(
                pos, bar, code, i, today_str)
            if action == 'sell_now':
                # 卖出信号触发通知（不管后续是否成交）
                if _NOTIFIER_OK:
                    try:
                        _bp = pos.get('buy_price', 0)
                        _pnl_pct = ((sell_price - _bp) / _bp * 100) if _bp > 0 else 0
                        _notify_sell_signal(code=code, price=sell_price,
                                            reason=reason,
                                            days_held=pos.get('days_held', 0),
                                            pnl_pct=_pnl_pct,
                                            hm=f"{hm[0]:02d}:{hm[1]:02d}")
                    except Exception:
                        pass
                self._execute_sell(code, pos, sell_price, reason, today_str, hm=hm)

        # 买入扫描（9:35起，14:55含）
        if hm < (9, 35):
            return
        if hm > (15, 0):
            return

        # DYN整体浮亏检查
        _dyn_ok = self._can_buy_dyn(today_str, current_hm=hm)
        if not _dyn_ok:
            # 计算实际浮亏供诊断
            _tot_cost = sum(p['buy_price'] * p['quantity'] for p in self.positions.values())
            _tot_mkt  = sum(
                (self.bars_today.get(c, {}).get(hm, {}) or {}).get('close', p['buy_price']) * p['quantity']
                for c, p in self.positions.items()
            )
            _pnl_pct = (_tot_mkt - _tot_cost) / _tot_cost if _tot_cost > 0 else 0
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] [DBG-DYN] hm={hm[0]:02d}:{hm[1]:02d} "
                  f"DYN浮亏拦截 pnl={_pnl_pct:+.2%} "
                  f"(cost={_tot_cost:.0f} mkt={_tot_mkt:.0f}) → 跳过买入扫描")
            return

        if len(self.positions) >= self.cur_max_pos:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] [DBG-BUY] hm={hm[0]:02d}:{hm[1]:02d} "
                  f"满仓 {len(self.positions)}/{self.cur_max_pos} → 跳过买入扫描")
            return

        for code in self.buy_candidates:
            if len(self.positions) >= self.cur_max_pos:
                break
            if code in self.positions:
                continue
            if code in self._bought_today:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] [DBG-BUY] {code} "
                      f"hm={hm[0]:02d}:{hm[1]:02d} 今日已买入/尝试过 → 跳过")
                continue

            meta = self.stock_meta.get(code)
            if not meta:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] [DBG-BUY] {code} "
                      f"hm={hm[0]:02d}:{hm[1]:02d} stock_meta缺失 → 跳过")
                continue

            # 新股仓位上限
            if meta.get('is_new'):
                cur_new = sum(1 for p in self.positions.values() if p.get('is_new_stock'))
                if cur_new >= MAX_NEW_STOCK_POSITIONS:
                    print(f"[{_now_str()}] [{self.ENGINE_NAME}] [DBG-BUY] {code} "
                          f"hm={hm[0]:02d}:{hm[1]:02d} 新股仓位上限 {cur_new}/{MAX_NEW_STOCK_POSITIONS} → 跳过")
                    continue

            bar = self.bars_today.get(code, {}).get(hm)
            if bar is None or bar['volume'] <= 0:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] [DBG-BUY] {code} "
                      f"hm={hm[0]:02d}:{hm[1]:02d} 无bar/零量(共{len(self.bars_today.get(code, {}))}根) → 跳过")
                continue

            # 9:30防御（实际hm<(9,35)已return，这里是冗余保护）
            i = _TIME_POINTS.index(hm) if hm in _TIME_POINTS else -1
            if i == 0:
                continue

            # 信号判定（RISING分支，quant.txt 4.2节[9]）
            if meta['type'] != 'RISING':
                if hm == (9, 35):  # 仅首根打印，避免全天刷屏
                    print(f"[{_now_str()}] [{self.ENGINE_NAME}] [DBG-BUY] {code} "
                          f"hm=09:35 type={meta.get('type','?')} 非RISING → 全天跳过")
                continue

            prev_c = self.prev_close_cache.get(code, 0)
            if prev_c <= 0:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] [DBG-BUY] {code} "
                      f"hm={hm[0]:02d}:{hm[1]:02d} prev_close缺失 → 跳过")
                continue
            current_chg = (bar['close'] - prev_c) / prev_c
            today_open = self.day_open_cache.get(code, 0)
            if today_open <= 0:
                today_open = self._get_today_open(code, self._today_str)

            _sig_ok = (current_chg > _min_chg(code) and
                       current_chg < _max_chg(code) and
                       bar['close'] > today_open and        # 收阳线：收盘价>当日9:30开盘价
                       current_chg < _limit_up(code))
            if not _sig_ok:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] [DBG-BUY] {code} "
                      f"hm={hm[0]:02d}:{hm[1]:02d} chg={current_chg:+.2%} "
                      f"min={_min_chg(code):.2%} max={_max_chg(code):.2%} "
                      f"close={bar['close']:.3f} open={today_open:.3f} "
                      f"above_open={bar['close']>today_open} "
                      f"limit_up={_limit_up(code):.2%}")
                continue

            # 买入信号触发通知（不管后续是否成交）
            if _NOTIFIER_OK:
                try:
                    _notify_buy_signal(code=code, price=bar['close'],
                                       change_pct=current_chg * 100,
                                       hm=f"{hm[0]:02d}:{hm[1]:02d}",
                                       regime=self.cur_regime)
                except Exception:
                    pass

            # 下单（V3风格实时价路由：优先卖一价，避免无谓加价）
            buy_px = bar['close']
            qty = _buy_qty(self.cash, len(self.positions), buy_px, self.cur_max_pos)
            if qty <= 0:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] [DBG-BUY] {code} "
                      f"hm={hm[0]:02d}:{hm[1]:02d} qty=0(cash={self.cash:.0f} px={buy_px:.3f} "
                      f"pos={len(self.positions)}/{self.cur_max_pos}) → 跳过")
                continue
            order_px   = self._route_buy_price(code, buy_px)  # 实时路由决定委托价
            commission = _buy_commission(buy_px, qty)
            total_cost = order_px * qty + commission
            if total_cost > self.cash:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] [DBG-BUY] {code} "
                      f"hm={hm[0]:02d}:{hm[1]:02d} 资金不足 cost={total_cost:.0f} > cash={self.cash:.0f} → 跳过")
                continue

            self._execute_buy(code, buy_px, qty, meta, today_str, hm=hm, order_price=order_px)

    def _can_buy_dyn(self, today_str: str, current_hm: Optional[tuple] = None) -> bool:
        """DYN整体浮亏检查（quant.txt 4.2节[3]）

        BUGFIX 2026-05-19 (Root cause #3 + BUG-002 + BUG-A2): 与 mac 真理源对齐
        (a) 补齐 OPT-B v2 强市豁免逻辑 (mac precompute/run_backtest.py L1496-1499)
            - 用前一日 sh_ma_cache 判断 sh_strong = not sh_below
            - DYN_SH_EXEMPT 且 sh_strong → 强市豁免, can_buy=True
        (b) 首日边界对齐 (Root cause #3)
            - mac 用 sim_dates[day_idx-1], 首日 day_idx=0 → prev_day=None → sh_strong=False
            - win 之前用 all_trading_dates 取 prev_day, 首日仍能取到回测期外日期, 误判 sh_strong=True
            - 修复: prev_day < _backtest_start_str 时强制视为 None
        (c) 反 lookahead (BUG-A2)
            - 计算 total_mkt 时用 current_hm 的 close 而非 max(keys)
            - current_hm 无 bar 时回退到 <= current_hm 的最晚一根
            - current_hm=None 时退化为旧行为 (向后兼容)
        """
        if not DYNAMIC_POSITION or not self.positions:
            return True
        # OPT-B v2 强市豁免
        prev_day = self._prev_trading_date(today_str)
        _bt_start = getattr(self, '_backtest_start_str', None)
        if _bt_start is not None and prev_day is not None and prev_day < _bt_start:
            prev_day = None  # 对齐 mac sim_dates 边界
        sh_info = self.sh_ma_cache.get(prev_day) if prev_day else None
        sh_strong = sh_info is not None and not sh_info[2]  # sh_info=(ma20, slope, sh_below)
        if DYN_SH_EXEMPT and sh_strong:
            return True
        # 弱市/中性: 按浮亏阈值检查
        total_cost = sum(p['buy_price'] * p['quantity'] for p in self.positions.values())
        total_mkt  = 0.0
        for code, pos in self.positions.items():
            bars = self.bars_today.get(code, {})
            price = None
            if bars and current_hm is not None:
                cur_bar = bars.get(current_hm)
                if cur_bar is not None:
                    price = cur_bar['close']
                else:
                    # 回退: 取 <= current_hm 的最晚一根 bar (不偷看未来)
                    earlier_hms = [k for k in bars.keys() if k <= current_hm]
                    if earlier_hms:
                        price = bars[max(earlier_hms)]['close']
            elif bars:
                # current_hm=None 兜底: 沿用旧行为
                latest_hm = max(bars.keys())
                price = bars[latest_hm]['close']
            if price is None:
                price = pos['buy_price']
            total_mkt += price * pos['quantity']
        if total_cost <= 0:
            return True
        return (total_mkt - total_cost) / total_cost >= DYN_PNL_THRESHOLD

    # ─────────────────────────────────────────────
    # 持仓卖出判定 (quant.txt 5.1节)
    # ─────────────────────────────────────────────

    def _evaluate_intraday_sell(self, pos: dict, bar: dict, code: str,
                                i: int, today_str: str) -> Tuple[str, Optional[str], Optional[float]]:
        """
        返回 ('sell_now', reason, price) 或 ('hold', None, None)
        前置：pos.days_held > 0（主循环已保证）
        """
        buy_price = pos['buy_price']
        hp        = pos.get('highest_price', buy_price)
        history_5min = pos.get('history', {}).get('bars_5min', [])

        # 新股用更宽硬止损
        eff_hs = NEW_STOCK_HARD_STOP if pos.get('is_new_stock') else _hard_sl(code, pos)

        # === E1 死票早卖 ===
        if E1_LOOKBACK_N > 0 and len(history_5min) >= E1_LOOKBACK_N:
            recent = history_5min[-E1_LOOKBACK_N:]
            n_red = sum(1 for b in recent if b['c'] < b['o'])
            max_close = max(b['c'] for b in recent)
            if (n_red / E1_LOOKBACK_N >= E1_RED_RATIO and
                    max_close <= buy_price):
                return ('sell_now', 'dead_stock', bar['close'])

        # === deferred_sells 监控 ===
        if code in self.deferred_sells:
            if bar['high'] >= self.deferred_sells[code]['trigger_px']:
                sell_price = self.deferred_sells[code]['trigger_px']
                sell_type  = self.deferred_sells[code]['sell_type']
                del self.deferred_sells[code]
                return ('sell_now', sell_type, sell_price)
            # 未触达，本K不卖，fall through

        # === RISING 分支 ===
        if pos.get('trend_type') == 'RISING':
            # D1. Hard Stop
            if eff_hs > 0:
                hard_stop_px = buy_price * (1 - eff_hs)
                if bar['low'] <= hard_stop_px:
                    if i == 0:
                        # 9:30 第一根K：判断上证弱市
                        prev_day = self._prev_trading_date(today_str)
                        sh_info  = self.sh_ma_cache.get(prev_day) if prev_day else None
                        sh_weak  = (sh_info is not None and sh_info[2] and sh_info[1] < -0.01)
                        if sh_weak:
                            sell_price = min(hard_stop_px, bar['open'])
                            return ('sell_now', 'hard_stop', sell_price)
                        else:
                            # 强市/中性：加入deferred，fall through
                            if code not in self.deferred_sells:
                                self.deferred_sells[code] = {
                                    'trigger_px': hard_stop_px, 'sell_type': 'hard_stop'}
                            # 不return，继续检查trail
                    else:
                        sell_price = min(hard_stop_px, bar['open'])
                        return ('sell_now', 'hard_stop', sell_price)

            # D2. Trail Stop
            trail_act_threshold = buy_price * (1 + _trail_act(code, pos))
            if hp >= trail_act_threshold:
                trigger  = hp * (1 - _trail_stop_pct(code, pos))
                limit_px = trigger * (1 - STOP_LIMIT_SLIP)
                if bar['low'] <= limit_px:
                    if i == 0:
                        if code not in self.deferred_sells:
                            self.deferred_sells[code] = {
                                'trigger_px': limit_px, 'sell_type': 'trailing_stop'}
                        return ('hold', None, None)
                    else:
                        sell_price = bar['open'] if bar['open'] <= limit_px else limit_px
                        return ('sell_now', 'trailing_stop', sell_price)

        return ('hold', None, None)

    # ─────────────────────────────────────────────
    # 14:55 收盘前决策 (quant.txt 5.2节)
    # ─────────────────────────────────────────────

    def _process_hm_1455(self, today_str: str):
        """14:55 deferred兜底 + evaluate_close_signals"""
        hm = (14, 55)
        self._fetch_5min_bars(list(self.positions.keys()), today_str, hm)

        # 3a. deferred_sells兜底
        for code, ds_info in list(self.deferred_sells.items()):
            if code not in self.positions:
                del self.deferred_sells[code]
                continue
            bar = self.bars_today.get(code, {}).get(hm)
            if bar:
                sell_price = bar['close']
            else:
                sell_price = self.positions[code]['buy_price'] * (1 - _hard_sl(code, self.positions[code]))
            self._execute_sell(code, self.positions[code], sell_price,
                               ds_info['sell_type'], today_str, hm=(14, 55))
            del self.deferred_sells[code]

        # 3b. evaluate_close_signals
        pending_codes = {ps['code'] for ps in self.pending_sells}
        for code, pos in list(self.positions.items()):
            if code in pending_codes:
                continue
            if pos.get('days_held', 0) == 0:
                continue
            bar = self.bars_today.get(code, {}).get(hm)
            if bar is None:
                continue

            action, reason, sell_price = self._evaluate_close_signals(pos, bar, code)
            if action == 'sell_now_close':
                # 卖出信号通知
                if _NOTIFIER_OK:
                    try:
                        _bp = pos.get('buy_price', 0)
                        _pnl_pct = ((sell_price - _bp) / _bp * 100) if _bp > 0 else 0
                        _notify_sell_signal(code=code, price=sell_price,
                                            reason=reason,
                                            days_held=pos.get('days_held', 0),
                                            pnl_pct=_pnl_pct, hm='14:55')
                    except Exception:
                        pass
                self._execute_sell(code, pos, sell_price, reason, today_str, hm=(14, 55))
            elif action == 'add_pending':
                # pending卖出信号通知
                if _NOTIFIER_OK:
                    try:
                        _notify_pending_sell(code=code, sell_type=reason,
                                             days_held=pos.get('days_held', 0),
                                             last_price=bar['close'])
                    except Exception:
                        pass
                self.pending_sells.append({
                    'code': code, 'quantity': pos['quantity'], 'sell_type': reason})

        # 保存deferred/pending状态
        _save_json(DEFERRED_FILE, self.deferred_sells)
        _save_json(PENDING_FILE,  self.pending_sells)

    def _evaluate_close_signals(self, pos: dict, bar: dict,
                                code: str) -> Tuple[str, Optional[str], Optional[float]]:
        """14:55 收盘前判定（quant.txt 5.2节）"""
        buy_price = pos['buy_price']
        hp        = pos.get('highest_price', buy_price)

        # vol_stop（默认999等价关闭）
        # ...（略，threshold=999，实际不触发）

        # trail close
        if hp >= buy_price * (1 + _trail_act(code, pos)):
            trigger = hp * (1 - _trail_stop_pct(code, pos))
            if bar['close'] <= trigger:
                if TRAIL_CLOSE_MODE == 'next_day':
                    return ('add_pending', 'trailing_stop', None)
                else:
                    return ('sell_now_close', 'trailing_stop', bar['close'])

        return ('hold', None, None)

    # ─────────────────────────────────────────────
    # G3.4 Regime 决策 (对齐 run_g34_verify.py per_day_fn + per_stock_fn)
    # ─────────────────────────────────────────────

    def _g34_regime_decide(self, today_str: str) -> dict:
        """G3.7 regime 决策，完全对齐 mac run_g37_verify.py per_day_fn.
        G3.7 升级 (2026-05-19):
          1. init_bnd 从常数 3 改为动态: close > MA60 → 3, 否则 5
          2. 新增安全网: ret_60d < -0.05 → 空仓 (宏观熊探测)
        ⚠️ 反 lookahead: 使用 prev_trading_day 的 SH 特征（不是今天）
        返回: {'max_positions': int, 'regime': str}
        """
        prev = self._prev_trading_date(today_str)
        feat = self.sh_g34_cache.get(prev) if prev else None
        p = G34_PARAMS
        if feat is None:
            return {'max_positions': p['bull_mp'], 'regime': 'bull_fallback'}
        # 安全网 1: 30日大跌 → 空仓
        if feat['ret_30d'] < p['panic_thr']:
            return {'max_positions': 0, 'regime': 'panic_30d'}
        # 安全网 2: 30日高波 → 空仓
        if feat['vol_30d'] > p['vol_thr']:
            return {'max_positions': 0, 'regime': 'vol_30d'}
        # G3.7 安全网 3: 宏观熊 (60日跌超 5%) → 空仓
        if feat['ret_60d'] < p['macro_bear_thr']:
            return {'max_positions': 0, 'regime': 'macro_bear_60d'}
        # Bull 段 (SH >= MA20)
        if not feat['below']:
            return {'max_positions': p['bull_mp'], 'regime': 'bull'}
        # G3.7: 动态 init_bnd (close > MA60 → 3, 否则 5)
        cur_init_bnd = p['init_bnd_bull'] if feat.get('close_gt_ma60', False) else p['init_bnd_chop']
        # Chop_init (streak <= cur_init_bnd)
        if feat['streak'] <= cur_init_bnd:
            return {'max_positions': p['chop_init_mp'], 'regime': 'chop_init'}
        # Chop_else 段安全网 4: 5日跌 → 空仓
        if feat['ret_5d'] < p['chop_else_ret5_min']:
            return {'max_positions': 0, 'regime': 'chop_else_ret5'}
        return {'max_positions': p['chop_else_mp'], 'regime': 'chop_else'}

    def _g34_stock_params(self, today_str: str) -> dict:
        """G3.7 持仓内参数，完全对齐 mac run_g37_verify.py per_stock_fn.
        G3.7: init_bnd 动态选择 (与 _g34_regime_decide 同步).
        ⚠️ 只在 entry 时刻调用一次，结果写入 position meta，持仓中永远不再重算。
        返回 entry 时刻锁定的 {hs, trail_act, trail_stop}
        """
        prev = self._prev_trading_date(today_str)
        feat = self.sh_g34_cache.get(prev) if prev else None
        p = G34_PARAMS
        if feat is None or not feat['below']:
            return dict(hs=p['bull_hs'], trail_act=p['bull_ta'], trail_stop=p['bull_ts'])
        # G3.7: 动态 init_bnd
        cur_init_bnd = p['init_bnd_bull'] if feat.get('close_gt_ma60', False) else p['init_bnd_chop']
        if feat['streak'] <= cur_init_bnd:
            return dict(hs=p['chop_init_hs'], trail_act=p['chop_init_ta'], trail_stop=p['chop_init_ts'])
        return dict(hs=p['chop_else_hs'], trail_act=p['chop_else_ta'], trail_stop=p['chop_else_ts'])

    # ─────────────────────────────────────────────
    # G1 旧方法（已被 G3.4 替代，保留供兼容旧 state.json snapshot 字段读取）
    # ─────────────────────────────────────────────

    _G1_BULL_PARAMS: dict = {
        'regime':        'BULL',
        'max_positions': 5,
        'hard_stop':     0.065,
        'trail_act':     0.40,
        'trail_stop':    0.12,
    }
    _G1_CHOP_PARAMS: dict = {
        'regime':        'CHOP',
        'max_positions': 4,
        'hard_stop':     0.065,
        'trail_act':     0.25,
        'trail_stop':    0.08,
    }

    def _g1_get_regime(self, today_str: str) -> str:
        """已废弃，仅供 state.json 历史兼容。新代码请用 _g34_regime_decide。"""
        prev_day = self._prev_trading_date(today_str)
        if not prev_day:
            return 'BULL'
        info = self.sh_ma_cache.get(prev_day)
        if info is None:
            return 'BULL'
        return 'CHOP' if bool(info[2]) else 'BULL'

    def _g1_params_for_today(self, today_str: str) -> dict:
        """已废弃，仅供 state.json 历史兼容。新代码请用 _g34_regime_decide。"""
        if self._g1_get_regime(today_str) == 'CHOP':
            return dict(self._G1_CHOP_PARAMS)
        return dict(self._G1_BULL_PARAMS)

    def _execute_pending_auction(self, today_str: str):
        """9:15 挂pending_sells集合竞价限价单（A模式通常为空）

        P0-1 修复: 委托提交成功 != 成交。
          - 不即刻移除 pending_sells，改为打标 auction_submitted=True。
          - _process_hm 检查该标志，防止当日重复触发止换揂单。
          - _on_new_day 重置标志，允许明日重试。
        """
        if not self.pending_sells:
            return
        print(f"[{_now_str()}] [{self.ENGINE_NAME}] 挂集合竞价pending_sells: {len(self.pending_sells)}笔")
        invalid_codes: set = set()
        for ps in self.pending_sells:
            code      = ps['code']
            qty       = ps['quantity']
            sell_type = ps.get('sell_type', 'pending')

            if code not in self.positions:
                # 持仓已不存在（可能是上一交易日已成交），标记待清理
                invalid_codes.add(code)
                continue

            prev_c = self.prev_close_cache.get(code, 0)
            limit_price = round(prev_c * AUCTION_FACTOR, 3) if prev_c > 0 else 0
            if limit_price <= 0:
                invalid_codes.add(code)
                continue

            sell_r = self._place_sell_order(code, qty, limit_price)
            if sell_r.get('ok'):
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] pending卖出挂单: {code} "
                      f"限价={limit_price:.3f} 数量={qty} 原因={sell_type}")
                # P0-1: 标记已提交委托，不移除（防止委托未成交时状态错乱）
                ps['auction_submitted'] = True
                ps['auction_submit_ts'] = _now_str()
            # else: 委托失败，保留在 pending_sells 下次重试

        # 只移除“持仓不存在”或“无有效价格”的无效条目
        self.pending_sells = [ps for ps in self.pending_sells if ps['code'] not in invalid_codes]

    # ─────────────────────────────────────────────
    # 买入/卖出执行
    # ─────────────────────────────────────────────

    def _get_tick_prices(self) -> Dict[str, float]:
        """获取持仓最新 tick 价格（用于 _save_state 计算 total_value，与 Dashboard 口径一致）"""
        if not (_XT_OK and _xtdata is not None):
            return {}
        result: Dict[str, float] = {}
        symbols = [_format_symbol(c) for c in self.positions]
        if not symbols:
            return result
        try:
            ticks = _xtdata.get_full_tick(symbols)
            if ticks:
                for code in self.positions:
                    sym = _format_symbol(code)
                    tick = ticks.get(sym)
                    if tick:
                        lp = float(tick.get('lastPrice', 0) or 0)
                        if lp > 0:
                            result[code] = lp
        except Exception:
            pass
        return result

    def _route_buy_price(self, code: str, bar_c: float) -> float:
        """V3风格实时价智能路由：获取卖一价，决定买入委托价（精确到0.01元）。
        规则（与 V3 live_engine_v3.py L1381-1408 对齐）：
          卖一价 ≤ bar_c              → 用卖一价（赚到更好价格）
          卖一价 ≤ bar_c + 0.3%      → 用卖一价（溢价可接受，快速成交）
          卖一价 > bar_c + 0.3%      → 挂 bar_c 等价格回落（被动等待）
          get_full_tick 失败/无数据   → 降级 bar_c + SLIPPAGE(0.015%)
        """
        if not (_XT_OK and _xtdata is not None):
            return round(bar_c * (1 + SLIPPAGE), 2)
        slip_max = getattr(_cfg, 'V3_LIVE_BUY_SLIP_MAX', 0.003)
        try:
            symbol = _format_symbol(code)
            ticks  = _xtdata.get_full_tick([symbol])
            if ticks and symbol in ticks:
                tick     = ticks[symbol]
                ask_list = tick.get('askPrice', [])
                ask      = float(ask_list[0]) if ask_list else 0.0
                if ask <= 0:
                    ask = float(tick.get('lastPrice', 0) or 0)
                if ask > 0:
                    slip = (ask - bar_c) / bar_c if ask > bar_c else 0.0
                    if ask <= bar_c:
                        order_px = round(ask, 2)
                        print(f"[{_now_str()}] [{self.ENGINE_NAME}] [路由] {code} "
                              f"卖一价{ask:.3f}≤close{bar_c:.3f}，以卖一价买入（获更优价格）")
                        return order_px
                    elif slip <= slip_max:
                        order_px = round(ask, 2)
                        print(f"[{_now_str()}] [{self.ENGINE_NAME}] [路由] {code} "
                              f"卖一价{ask:.3f} 溢价{slip:.2%}≤{slip_max:.2%}，接受实时卖一价")
                        return order_px
                    else:
                        order_px = round(bar_c, 2)
                        print(f"[{_now_str()}] [{self.ENGINE_NAME}] [路由] {code} "
                              f"卖一价{ask:.3f} 溢价{slip:.2%}>{slip_max:.2%}，"
                              f"挂close价{bar_c:.3f}等待回落")
                        return order_px
        except Exception as _e:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] [路由] get_full_tick异常({_e})，"
                  f"降级用close+{SLIPPAGE:.4%}")
        return round(bar_c * (1 + SLIPPAGE), 2)

    def _execute_buy(self, code: str, buy_px: float, qty: int,
                     meta: dict, today_str: str, hm: tuple = None,
                     order_price: float = None):
        """执行买入
        order_price: 由 _route_buy_price() 决定的实际委托价；
                     None 时降级为 buy_px * (1+SLIPPAGE)（兼容回测模式）。
        """
        actual_px  = order_price if order_price is not None else round(buy_px * (1 + SLIPPAGE), 2)
        commission = _buy_commission(buy_px, qty)
        total_cost = actual_px * qty + commission

        ok = self._place_buy_order(code, qty, actual_px)  # P1-1: 委托价用路由/滑点价
        if not ok:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] ⚠️ 买入委托失败(order_id=-1): {code} "
                  f"price={actual_px:.3f} qty={qty} "
                  f"— 检查 QMT 是否已登录并处于实盘模式")
            return

        # G3.4 入场瞬间锁定参数（per_stock_fn，持仓中永远不再重算）
        _sp = self._g34_stock_params(today_str)
        self.cash -= total_cost
        self.positions[code] = {
            'code': code, 'buy_price': round(actual_px, 3), 'buy_date': today_str,
            'quantity': qty, 'days_held': 0,
            'highest_price': actual_px,
            'trend_type': meta.get('type', 'RISING'),
            'is_new_stock': meta.get('is_new', False),
            'history': {'bars_5min': [], 'daily_post_buy': []},
            # G3.4 entry 时刻锁参 (持仓中不再切换)
            'snapshot_hs':      _sp['hs'],
            'snapshot_ta':      _sp['trail_act'],
            'snapshot_ts':      _sp['trail_stop'],
            'snapshot_regime':  self.cur_regime,
            'snapshot_max_pos': self.cur_max_pos,
        }
        self._bought_today.add(code)

        self._log_trade('buy', code, actual_px, qty, 'buy_signal',
                        fee=commission, cash_after=self.cash,
                        signal_px=round(buy_px, 4),
                        order_px=round(actual_px, 4),
                        slippage_amt=round(actual_px - buy_px, 6),
                        slippage_pct=SLIPPAGE,
                        trigger_hm=f"{hm[0]:02d}:{hm[1]:02d}" if hm else None,
                        regime=self.cur_regime)
        print(f"[{_now_str()}] [{self.ENGINE_NAME}] 买入: {code} "
              f"价={actual_px:.3f} 数量={qty} 佣金={commission:.2f} 现金剩余={self.cash:.2f}")
        if _NOTIFIER_OK:
            try:
                _prev_c = self.prev_close_cache.get(code, 0)
                _chg_pct = ((actual_px - _prev_c) / _prev_c * 100) if _prev_c > 0 else 0
                _notify_buy(code=code, price=actual_px, volume=qty,
                            amount=actual_px * qty, change_pct=_chg_pct)
            except Exception:
                pass
        self._save_state(force=True)  # P2-2: 买入为关键事件，强制写盘

    def _route_sell_price(self, code: str, sell_price: float) -> float:
        """V3风格实时价路由：获取买一价，决定卖出委托价（精确到0.01元）。
        规则（与 V3 live_engine_v3.py L1934-1958 对齐）：
          买一价 ≥ 止损价              → 用买一价（获更优价格）
          折价 ≤ 0.3%                 → 用买一价（可接受，快速成交）
          折价 > 0.3%                 → 仍用买一价（止损优先成交，打WARN）
          get_full_tick 失败/无数据   → 降级 sell_price - SLIPPAGE(0.015%)
        """
        if not (_XT_OK and _xtdata is not None):
            return round(sell_price * (1 - SLIPPAGE), 2)
        slip_max = getattr(_cfg, 'V3_LIVE_SELL_SLIP_MAX', 0.003)
        try:
            symbol = _format_symbol(code)
            ticks  = _xtdata.get_full_tick([symbol])
            if ticks and symbol in ticks:
                tick     = ticks[symbol]
                bid_list = tick.get('bidPrice', [])
                bid      = float(bid_list[0]) if bid_list else 0.0
                if bid <= 0:
                    bid = float(tick.get('lastPrice', 0) or 0)
                if bid > 0:
                    slip = (sell_price - bid) / sell_price if bid < sell_price else 0.0
                    if bid >= sell_price:
                        order_px = round(bid, 2)
                        print(f"[{_now_str()}] [{self.ENGINE_NAME}] [路由] {code} "
                              f"买一价{bid:.3f}≥止损价{sell_price:.3f}，获更优卖出价")
                        return order_px
                    elif slip <= slip_max:
                        order_px = round(bid, 2)
                        print(f"[{_now_str()}] [{self.ENGINE_NAME}] [路由] {code} "
                              f"买一价{bid:.3f} 折价{slip:.2%}≤{slip_max:.2%}，接受实时买一价")
                        return order_px
                    else:
                        order_px = round(bid, 2)  # 折价>阈值，仍用买一价（止损优先）
                        print(f"[{_now_str()}] [{self.ENGINE_NAME}] [路由][WARN] {code} "
                              f"买一价{bid:.3f} 折价{slip:.2%}>{slip_max:.2%}，"
                              f"超阈值但止损优先，强制用买一价")
                        return order_px
        except Exception as _e:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] [路由] get_full_tick异常({_e})，"
                  f"降级用sell_price-{SLIPPAGE:.4%}")
        return round(sell_price * (1 - SLIPPAGE), 2)

    def _execute_sell(self, code: str, pos: dict, sell_price: float,
                      reason: str, today_str: str, hm: tuple = None):
        """执行卖出"""
        qty = pos.get('quantity', 0)
        if qty <= 0:
            return

        # V3风格实时价路由：止损用买一价，确保快速成交；降级用 sell_price - SLIPPAGE
        order_price = self._route_sell_price(code, sell_price)
        sell_result = self._place_sell_order(code, qty, order_price)
        if not sell_result.get('ok'):
            return

        # 真实成交价：优先用 query_stock_trades 返回的加权均价，降级用路由价
        fill_price = sell_result.get('fill_price') or order_price
        fill_source = '券商成交' if sell_result.get('fill_price') else '路由价'

        # 用真实成交价计算净收入和盈亏
        net, commission, stamp_tax = _sell_net(fill_price, qty)
        pnl = net - pos['buy_price'] * qty

        self.cash += net
        slip_amt = round(sell_price - fill_price, 4)
        self._log_trade('sell', code, fill_price, qty, reason,
                        fee=commission + stamp_tax, pnl=pnl,
                        cash_after=self.cash,
                        days_held=pos.get('days_held', 0),
                        signal_px=round(sell_price, 4),
                        order_px=round(order_price, 4),
                        slippage_amt=slip_amt,
                        slippage_pct=round(slip_amt / sell_price, 6) if sell_price else 0,
                        trigger_hm=f"{hm[0]:02d}:{hm[1]:02d}" if hm else None,
                        buy_price_ref=round(pos.get('buy_price', 0), 4),
                        buy_date_ref=pos.get('buy_date', ''),
                        snapshot_hs=pos.get('snapshot_hs'),
                        snapshot_ta=pos.get('snapshot_ta'),
                        snapshot_ts=pos.get('snapshot_ts'),
                        snapshot_regime=pos.get('snapshot_regime', ''),
                        snapshot_max_pos=pos.get('snapshot_max_pos'),
                        bars_5min=pos.get('history', {}).get('bars_5min', []))
        del self.positions[code]

        print(f"[{_now_str()}] [{self.ENGINE_NAME}] 卖出: {code} "
              f"原因={reason} 成交价={fill_price:.3f}({fill_source},委托{order_price:.3f},信号{sell_price:.3f}) "
              f"数量={qty} PnL={pnl:+.2f} 现金={self.cash:.2f}")
        if _NOTIFIER_OK:
            try:
                _buy_px = pos.get('buy_price', 0)
                _pnl_pct = (pnl / (_buy_px * qty) * 100) if (_buy_px > 0 and qty > 0) else 0
                _notify_sell(code=code, price=fill_price, volume=qty,
                             sell_type=reason, buy_price=_buy_px,
                             days_held=pos.get('days_held', 0),
                             profit_pct=_pnl_pct)
            except Exception:
                pass
        self._save_state(force=True)  # P2-2: 卖出为关键事件，强制写盘

    # ─────────────────────────────────────────────
    # xtquant 接口封装
    # ─────────────────────────────────────────────

    def _connect_xt(self) -> bool:
        """验证 xtquant / TradeExecutor 可用性。
        V4 不维持持久 executor——每次下单由独立子进程（_place_order_worker.py）
        创建连接，避免 xtdata 与 XtQuantTrader 在同进程共存时 session_id 冲突。
        """
        if not _XT_OK:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] xtquant未安装，模拟模式运行")
            self.cash = self.initial_capital
            return True
        if not _EXECUTOR_OK or _TradeExecutor is None:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] TradeExecutor 导入失败，无法连接")
            return False
        print(f"[{_now_str()}] [{self.ENGINE_NAME}] xtquant 验证通过，下单将通过子进程隔离执行")
        return True

    def _subscribe_quotes(self, codes: List[str]):
        """xtdata 5min 订阅。xtquant subscribe_quote 只接受单个 stock_code 字符串，必须逐支订阅。"""
        if not _XT_OK or _xtdata is None:
            return
        ok, fail = 0, 0
        for code in codes:
            if code in self._subscribed_codes:
                continue
            symbol = _format_symbol(code)
            try:
                _xtdata.subscribe_quote(symbol, period='5m', count=-1)  # count=-1: 订阅并同步预加载当日历史K线（gap_min过滤依赖9:30开盘价，count=0不行）
                self._subscribed_codes.add(code)
                ok += 1
            except Exception as e:
                fail += 1
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] 订阅失败 {symbol}: {e}")
        print(f"[{_now_str()}] [{self.ENGINE_NAME}] 订阅行情: 成功={ok}只 失败={fail}只")

    def _fetch_5min_bars(self, codes: List[str], today_str: str, hm: tuple):
        """获取5min K线数据，存入bars_today[code][hm]"""
        if not _XT_OK or _xtdata is None:
            return
        for code in codes:
            symbol = _format_symbol(code)
            # 懒订阅：若该代码尚未订阅，09:30 后 xtdata 已连接，补订阅
            if code not in self._subscribed_codes:
                try:
                    _xtdata.subscribe_quote(symbol, period='5m', count=-1)  # count=-1: 预加载历史K线
                    self._subscribed_codes.add(code)
                except Exception:
                    pass
            try:
                # xtdata get_market_data 返回格式有两种：
                #   dict格式: data['close'][symbol] = {t_key: value, ...}  (旧版/离线)
                #   DataFrame格式: data['close'] = DataFrame, data['close'][symbol] = Series  (实盘)

                def _get_field_series(d, field, sym):
                    """从 get_market_data 返回值中安全提取某字段的时间序列
                    xtdata 实盘返回格式:
                      DataFrame转置格式: index=股票代码  columns=时间戳字符串(YYYYMMDDHHMMSS)
                      进行取行: data['close'].loc[symbol] → Series indexed by timestamp str
                    离线/旧版格式:
                      dict格式: data['close'][symbol] = {t_key: value, ...}
                    """
                    if not d:
                        return None
                    raw = d.get(field)
                    if raw is None:
                        return None
                    # DataFrame 格式（xtdata 实盘）
                    try:
                        import pandas as _pd
                        if isinstance(raw, _pd.DataFrame):
                            # 转置格式: index=股票代码, columns=时间戳
                            if sym in raw.index:
                                return raw.loc[sym]  # 返回 Series, index=时间戳字符串
                            # 兼容旧格式: columns=股票代码
                            if sym in raw.columns:
                                return raw[sym]
                            return None
                    except ImportError:
                        pass
                    # dict 格式（离线回测）
                    if isinstance(raw, dict):
                        return raw.get(sym)
                    return None

                def _series_empty(s):
                    if s is None:
                        return True
                    try:
                        import pandas as _pd
                        if isinstance(s, _pd.Series):
                            return s.empty
                    except ImportError:
                        pass
                    return len(s) == 0

                def _series_times(s):
                    """获取时间序列的 key 列表，兼容 dict 和 pandas Series"""
                    if s is None:
                        return []
                    try:
                        import pandas as _pd
                        if isinstance(s, _pd.Series):
                            return list(s.index)
                    except ImportError:
                        pass
                    if isinstance(s, dict):
                        return list(s.keys())
                    return []

                # ① 优先用日期范围拉取
                data = _xtdata.get_market_data(
                    field_list=['open', 'high', 'low', 'close', 'volume'],
                    stock_list=[symbol],
                    period='5m',
                    start_time=today_str.replace('-', ''),
                    end_time=today_str.replace('-', ''),
                    count=-1,
                )
                closes = _get_field_series(data, 'close', symbol)
                # ② 若日期范围返回空，降级用 count=200 拉最近K线
                if _series_empty(closes):
                    data = _xtdata.get_market_data(
                        field_list=['open', 'high', 'low', 'close', 'volume'],
                        stock_list=[symbol],
                        period='5m',
                        count=200,
                    )
                    closes = _get_field_series(data, 'close', symbol)
                # ③ 仍为空则等 2s 重试一次
                if _series_empty(closes):
                    time.sleep(2)
                    data = _xtdata.get_market_data(
                        field_list=['open', 'high', 'low', 'close', 'volume'],
                        stock_list=[symbol],
                        period='5m',
                        count=200,
                    )
                    closes = _get_field_series(data, 'close', symbol)
                if _series_empty(closes):
                    continue
                opens = _get_field_series(data, 'open',   symbol)
                highs = _get_field_series(data, 'high',   symbol)
                lows  = _get_field_series(data, 'low',    symbol)
                vols  = _get_field_series(data, 'volume', symbol)
                times = _series_times(closes)

                if not self.bars_today.get(code):
                    self.bars_today[code] = {}

                for t_key in times:
                    # t_key格式: 'YYYYMMDDHHMMSS' 字符串 或 pandas Timestamp/datetime
                    try:
                        if isinstance(t_key, str) and len(t_key) >= 12:
                            bar_date = t_key[:8]  # 'YYYYMMDD'
                            bh = int(t_key[8:10])
                            bm = int(t_key[10:12])
                        elif hasattr(t_key, 'hour'):
                            bar_date = t_key.strftime('%Y%m%d') if hasattr(t_key, 'strftime') else ''
                            bh, bm = t_key.hour, t_key.minute
                        elif isinstance(t_key, (int, float)):
                            # 整数时间戳（毫秒级）
                            import datetime as _dt
                            ts = _dt.datetime.fromtimestamp(t_key / 1000)
                            bar_date = ts.strftime('%Y%m%d')
                            bh, bm = ts.hour, ts.minute
                        else:
                            continue
                        # 过滤非今日K线（count=200降级时会拉到昨日数据）
                        if bar_date and bar_date != today_str.replace('-', ''):
                            continue
                        bar_hm = (bh, bm)
                        if bar_hm not in self.bars_today[code]:
                            self.bars_today[code][bar_hm] = {
                                'open':   float(opens[t_key]),
                                'high':   float(highs[t_key]),
                                'low':    float(lows[t_key]),
                                'close':  float(closes[t_key]),
                                'volume': float(vols[t_key]),
                            }
                        # 更新day_open_cache（9:30 open）
                        if bar_hm == (9, 30) and code not in self.day_open_cache:
                            self.day_open_cache[code] = float(opens[t_key])
                    except Exception:
                        continue
            except Exception as e:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] 获取5min K失败 {code}: {e}")

    def _get_today_open(self, code: str, today_str: str) -> float:
        """获取今日开盘价：优先 bars_today(9:30/9:35)，降级用 get_full_tick['open']"""
        if code in self.day_open_cache:
            return self.day_open_cache[code]
        # ① bars_today（兼容 9:30 / 9:35 两种时间戳）
        for _first_hm in ((9, 30), (9, 35)):
            bar = self.bars_today.get(code, {}).get(_first_hm)
            if bar and bar['open'] > 0:
                self.day_open_cache[code] = bar['open']
                return bar['open']
        # ② get_full_tick 同步降级（迟启动时 bars_today 为空，此路必走）
        # 字段名经诊断确认为 'open'（xtquant miniQMT sp3 v1.0）
        if _XT_OK and _xtdata is not None:
            try:
                symbol = _format_symbol(code)
                ticks = _xtdata.get_full_tick([symbol])
                if ticks and symbol in ticks:
                    tick = ticks[symbol]
                    open_px = float(tick.get('open', 0) or 0)
                    if open_px > 0:
                        self.day_open_cache[code] = open_px
                        return open_px
            except Exception as _e:
                pass
        return 0.0

    def _make_fresh_executor(self) -> object:
        """下单前创建全新 TradeExecutor 实例（与测试脚本行为一致，避免持久连接冲突）"""
        ex = _TradeExecutor(
            mini_qmt_path=self.xt_path,
            account_id=self.account_id,
        )
        ok = ex.connect()
        if not ok:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 临时 TradeExecutor 连接失败")
            return None
        time.sleep(1)  # 等待 XtQuantTrader 注册到 QMT 服务器
        return ex

    def _run_order_worker(self, action: str, code: str, qty: int, price: float) -> dict:
        """通过独立子进程下单，避免 xtdata 与 XtQuantTrader 在同进程共存时的干扰。
        子进程（_place_order_worker.py）在无 xtdata 的干净环境中连接 TradeExecutor。
        返回 dict: {ok, oid, n_orders, found_ids, error}
        """
        worker_script = os.path.join(os.path.dirname(__file__), '_place_order_worker.py')
        params = json.dumps({
            'xt_path':    self.xt_path,
            'account_id': self.account_id,
            'session_id': 654321,
            'action':     action,
            'symbol':     _format_symbol(code),
            'price':      float(price),
            'volume':     int(qty),
            'remark':     f'V4_{action}_{code}',
        }, ensure_ascii=False)
        try:
            result = subprocess.run(
                [sys.executable, worker_script, params],
                capture_output=True, text=True, timeout=20,
            )
            # 打印子进程完整日志（便于诊断）
            if result.stdout:
                for ln in result.stdout.strip().splitlines():
                    if ln.strip():
                        print(f"  [worker] {ln.strip()}")
            if result.stderr:
                for ln in result.stderr.strip().splitlines():
                    if ln.strip():
                        print(f"  [worker-err] {ln.strip()}")
            # 取最后一行 JSON（忽略 TradeExecutor 打印的其他日志）
            lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
            for line in reversed(lines):
                try:
                    return json.loads(line)
                except Exception:
                    continue
            stderr_info = result.stderr.strip()[:200] if result.stderr else ''
            return {'ok': False, 'oid': -1, 'error': f'no json output; stderr={stderr_info}'}
        except subprocess.TimeoutExpired:
            return {'ok': False, 'oid': -1, 'error': 'subprocess timeout'}
        except Exception as e:
            return {'ok': False, 'oid': -1, 'error': str(e)}

    def _place_buy_order(self, code: str, qty: int, price: float) -> bool:
        if not _XT_OK or not _EXECUTOR_OK:
            if self.account_id:
                raise RuntimeError(
                    f"xtquant 不可用，实盘拒绝买入委托 {code}（避免假装下单）")
            return True  # 回测模式
        import time as _time
        _MAX_ATTEMPTS = 2    # connect failed 最多重试 1 次
        _RETRY_WAIT   = 2.0  # session_id 释放等待时间(s)
        for _attempt in range(1, _MAX_ATTEMPTS + 1):
            _retry_tag = f"（第{_attempt}次尝试）" if _attempt > 1 else ""
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 子进程买入: {code} "
                  f"price={price:.3f} qty={qty} → 启动独立下单进程...{_retry_tag}")
            r = self._run_order_worker('buy', code, qty, price)
            oid  = r.get('oid', -1)
            ok   = r.get('ok', False)
            err  = r.get('error', '')
            n    = r.get('n_orders', '?')
            fids = r.get('found_ids', [])
            if ok:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] ✅ 买入委托确认: "
                      f"{code} order_id={oid} 已进入QMT委托列表（共{n}笔）")
                return True
            # connect failed 且仍有重试机会 → 等 session 释放后重试
            if 'connect failed' in err and _attempt < _MAX_ATTEMPTS:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] ⚠️ 连接失败，"
                      f"等待{_RETRY_WAIT:.0f}s后重试 ({_attempt}/{_MAX_ATTEMPTS})...")
                _time.sleep(_RETRY_WAIT)
                continue
            # 其他失败或已用完重试次数
            detail = err if err else f"order_id={oid} 不在QMT列表({fids})"
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] ❌ 买入委托失败: {code} — {detail}")
            return False

    def _place_sell_order(self, code: str, qty: int, price: float) -> dict:
        """卖出委托，返回 {ok, fill_price, fill_volume}。
        fill_price: 真实加权成交均价（query_stock_trades），未成交时为 None。
        """
        if not _XT_OK or not _EXECUTOR_OK:
            if self.account_id:
                raise RuntimeError(
                    f"xtquant 不可用，实盘拒绝卖出委托 {code}（避免假装下单）")
            return {'ok': True}  # 回测模式
        import time as _time
        _MAX_ATTEMPTS = 2
        _RETRY_WAIT   = 2.0
        for _attempt in range(1, _MAX_ATTEMPTS + 1):
            _retry_tag = f"（第{_attempt}次尝试）" if _attempt > 1 else ""
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 子进程卖出: {code} "
                  f"price={price:.3f} qty={qty} → 启动独立下单进程...{_retry_tag}")
            r = self._run_order_worker('sell', code, qty, price)
            oid = r.get('oid', -1)
            ok  = r.get('ok', False)
            err = r.get('error', '')
            if ok:
                fill_px = r.get('fill_price')
                fill_vol = r.get('fill_volume')
                fill_info = f" 真实成交={fill_px}(共{fill_vol}股)" if fill_px else ""
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] ✅ 卖出委托确认: "
                      f"{code} order_id={oid} 已进入QMT委托列表{fill_info}")
                return {'ok': True, 'fill_price': fill_px, 'fill_volume': fill_vol}
            if 'connect failed' in err and _attempt < _MAX_ATTEMPTS:
                print(f"[{_now_str()}] [{self.ENGINE_NAME}] ⚠️ 连接失败，"
                      f"等待{_RETRY_WAIT:.0f}s后重试 ({_attempt}/{_MAX_ATTEMPTS})...")
                _time.sleep(_RETRY_WAIT)
                continue
            detail = err if err else f"order_id={oid}"
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] ❌ 卖出委托失败: {code} — {detail}")
            return {'ok': False}

    # ─────────────────────────────────────────────
    # 状态持久化
    # ─────────────────────────────────────────────

    def _load_state(self):
        state = _load_json(STATE_FILE, {})
        self.positions        = state.get('positions', {})
        self.cash             = float(state.get('cash', self.initial_capital))
        self.initial_capital  = float(state.get('initial_capital', self.initial_capital))  # P2-3: fallback 用构造参数
        self._last_increment_date = state.get('_last_increment_date', '')
        self._today_str       = state.get('_today_str', '')
        self.wait_queue       = _load_json(QUEUE_FILE,    {})
        self.deferred_sells   = _load_json(DEFERRED_FILE, {})
        self.pending_sells    = _load_json(PENDING_FILE,  [])
        print(f"[{_now_str()}] [{self.ENGINE_NAME}] 状态加载: "
              f"持仓={len(self.positions)}只 现金={self.cash:.2f} "
              f"冷却队列={len(self.wait_queue)}只")
    
    def _save_state(self, force: bool = False):
        """P2-2: 容量节流（5秒内合并）。关键事件（买卖、收盘）传 force=True。"""
        now_ts = time.time()
        if not force and now_ts - self._last_save_ts < 5.0:
            return  # 5 秒内合并，不写盘
        self._last_save_ts = now_ts
    
        # 计算总价値（优先用 tick 实时价，与 Dashboard 口径一致）
        _tick_prices = self._get_tick_prices() if self.positions else {}
        pos_value = 0.0
        for code, pos in self.positions.items():
            tick_px = _tick_prices.get(code, 0)
            if tick_px > 0:
                pos_value += tick_px * pos['quantity']
            else:
                bars = self.bars_today.get(code, {})
                if bars:
                    latest = bars[max(bars.keys())]
                    pos_value += latest['close'] * pos['quantity']
                else:
                    pos_value += pos['buy_price'] * pos['quantity']
    
        # P2-2: 剥离 history 字段（history.bars_5min 只在内存用，不持久化避免膟胀）
        state_positions = {
            code: {k: v for k, v in pos.items() if k != 'history'}
            for code, pos in self.positions.items()
        }
    
        # 构建 sh_status（上证安全网状态，供 Dashboard 实时展示）
        _sh_status = {}
        _prev = self._prev_trading_date(self._today_str) if self._today_str else None
        _feat = self.sh_g34_cache.get(_prev) if _prev else None
        if _feat:
            _p = G34_PARAMS
            _sn_regime = self.cur_regime
            _sn_any = _sn_regime in ('panic_30d', 'vol_30d', 'macro_bear_60d', 'chop_else_ret5')
            _sh_status = {
                'date':             _prev,
                'ret_5d':           round(_feat.get('ret_5d', 0) * 100, 2),
                'ret_30d':          round(_feat.get('ret_30d', 0) * 100, 2),
                'vol_30d':          round(_feat.get('vol_30d', 0) * 100, 3),
                'ret_60d':          round(_feat.get('ret_60d', 0) * 100, 2),
                'below_ma20':       _feat.get('below', False),
                'streak':           _feat.get('streak', 0),
                'close_gt_ma60':    _feat.get('close_gt_ma60', False),
                'panic_triggered':  _feat.get('ret_30d', 0) < _p['panic_thr'],
                'vol_triggered':    _feat.get('vol_30d', 0) > _p['vol_thr'],
                'macro_triggered':  _feat.get('ret_60d', 0) < _p['macro_bear_thr'],
                'ret5_triggered':   _feat.get('ret_5d', 0) < _p.get('chop_else_ret5_min', -0.01),
                'any_triggered':    _sn_any,
                'regime':           _sn_regime,
            }

        state = {
            'initial_capital': self.initial_capital,
            'cash': round(self.cash, 2),
            'total_value': round(self.cash + pos_value, 2),
            'positions': state_positions,
            'pending_sells': self.pending_sells,
            '_last_increment_date': self._last_increment_date,
            '_today_str': self._today_str,
            '_cur_regime': self.cur_regime,
            '_cur_max_pos': self.cur_max_pos,
            'last_update': _now_str(),
            'engine': 'V4',
            'buy_candidates': list(self.buy_candidates),
            'premarket_filtered': getattr(self, '_premarket_filtered', {}),
            'sh_status': _sh_status,
        }
        _save_json(STATE_FILE,   state)
        _save_json(QUEUE_FILE,   self.wait_queue)
        _save_json(DEFERRED_FILE, self.deferred_sells)
        _save_json(PENDING_FILE,  self.pending_sells)

    def _log_trade(self, direction: str, code: str, price: float, qty: int,
                   reason: str, fee: float = 0.0, pnl: float = 0.0,
                   cash_after: float = 0.0, days_held: int = 0, **extra):
        trades = _load_json(TRADES_FILE, [])
        record = {
            'timestamp':  _now_str(),
            'type':       direction,
            'code':       code,
            'price':      round(price, 4),
            'quantity':   qty,
            'amount':     round(price * qty, 2),
            'fee':        round(fee, 2),
            'reason':     reason,
            'cash_after': round(cash_after, 2),
            'days_held':  days_held,
        }
        if direction == 'sell':
            record['pnl']     = round(pnl, 2)
            record['pnl_pct'] = round(pnl / (self.positions.get(code, {}).get('buy_price', price) * qty), 4) if qty > 0 else 0
        # 复盘扩展字段（signal_px/order_px/slippage/trigger_hm/buy_price_ref等）
        record.update(extra)
        trades.append(record)
        _save_json(TRADES_FILE, trades)

    # ─────────────────────────────────────────────
    # 辅助工具
    # ─────────────────────────────────────────────

    def postmarket_precompute(self, today_str: str):
        """
        盘后预算：重载日线数据（含今日新数据）→ 计算BA pool → 存缓存文件。
        由 run_live_v4.py 在日线数据更新后调用。
        today_str: 今日日期 'YYYY-MM-DD'（日线已入库，作为ref_date）
        """
        print(f"[{_now_str()}] [{self.ENGINE_NAME}] 盘后BA pool预算开始，ref_date={today_str}...")

        # 清空daily_data缓存，强制重载（拿到今日新下载的日线）
        self.daily_data = {}
        self.today_pool = []  # 置空，让_load_all_daily_data走全量扫描分支
        self._load_all_daily_data(today_str)

        if not self.daily_data:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 日线数据为空，盘后预算跳过")
            return

        # 构建含今日的交易日历（_load_all_daily_data 用 < today_dt，这里改为 <=）
        today_dt = pd.to_datetime(today_str)
        ref_df = self.daily_data.get('000001')
        if ref_df is None:
            ref_df = next(iter(self.daily_data.values())) if self.daily_data else None
        if ref_df is not None:
            dates_all = sorted(
                ref_df[ref_df['date'] <= today_dt]['date']
                .dt.strftime('%Y-%m-%d').tolist())
        else:
            dates_all = list(self.all_trading_dates)
            if today_str not in dates_all:
                dates_all.append(today_str)
                dates_all.sort()

        if not dates_all:
            print(f"[{_now_str()}] [{self.ENGINE_NAME}] 无法构建交易日历，盘后预算跳过")
            return

        path = precompute_ba_pool_save(today_str, self.daily_data, dates_all, top_n=50)
        print(f"[{_now_str()}] [{self.ENGINE_NAME}] 盘后预算完成 → {path}")

    def _prev_trading_date(self, today_str: str) -> Optional[str]:
        if not self.all_trading_dates:
            return None
        prev = [d for d in self.all_trading_dates if d < today_str]
        return prev[-1] if prev else None
