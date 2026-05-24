"""
策略T v4.0 独立引擎 — 大盘超跌反弹策略

完全独立于 LiveEngineV4，有自己的状态文件和启动脚本。
与 BA 唯一的共同点：读取同一个 D:/daily_data 目录的日线 CSV。

信号逻辑（逐字翻译 mac strategy_t_v4.py）：
  - 信号：上证 7 日收益率 < -5%（T-1 日数据，反 lookahead）
  - 选股：5 日跌幅最大 4 只，20 日均成交额>3 亿，5~200 元
  - 排除：688/301/001/689 开头的科创/新股
  - 买入：T+1 开盘价 × (1 + 1.5% 滑点)
  - 持仓：最多 8 天到期强平
  - 止盈：Trailing Stop（浮盈>=12% 激活，从高点回落 6% 触发）
  - 信号去重：前后 10 个交易日内不重复触发
"""

import os
import math
import json
import time
import pandas as pd
from datetime import datetime, date
from typing import Dict, List, Optional

# ═══════════════════════════════════════════════
# 策略T 参数（对齐 mac strategy_t_v4.py）
# ═══════════════════════════════════════════════
T_SH_THRESHOLD        = -0.05   # 上证 7 日跌幅阈值
T_SH_LOOKBACK         = 7       # 上证回看天数
T_MAX_POSITIONS       = 4       # 最大持仓数
T_STOCK_RET_LOOKBACK  = 5       # 选股：5 日跌幅排序（修正拼写）
T_MIN_AVG_AMOUNT      = 3e8     # 20 日均成交额 > 3 亿
T_MIN_PRICE           = 5.0     # 最低价格
T_MAX_PRICE           = 200.0   # 最高价格
T_EXCLUDE_PREFIXES    = ('688', '301', '001', '689')  # 排除前缀
T_HOLD_DAYS           = 8       # 最大持仓天数
T_TRAILING_ACTIVATION = 0.12    # trailing 激活阈值（浮盈 12%）
T_TRAILING_STOP_PCT   = 0.06    # trailing 回落幅度（6%）
T_BUY_SLIPPAGE        = 0.015   # 买入滑点 1.5%
T_SELL_SLIPPAGE       = 0.01    # 卖出滑点 1%
T_COMMISSION          = 0.00025 # 佣金 0.025%
T_STAMP_TAX           = 0.0005  # 印花税 0.05%（卖出）
T_LIMIT_THRESHOLD     = 0.095   # 涨停判定阈值
T_SIGNAL_COOLDOWN     = 10      # 信号去重天数

# 日线数据目录（与 BA 共享同一物理目录，独立读取）
DAILY_DATA_DIR = 'D:/daily_data'

# 状态文件（与 BA 完全隔离）
BASE_DIR      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T_STATE_FILE  = os.path.join(BASE_DIR, 'state_t_v4.json')
T_TRADES_FILE = os.path.join(BASE_DIR, 'trades_t_v4.json')


# ═══════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════

def _now_str() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        raise RuntimeError(f"[策略T] 状态文件损坏 {path}: {e}")


def _save_json(path: str, obj):
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as e:
        print(f"[{_now_str()}] [策略T] 保存失败 {path}: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass


def _load_daily_csv(code: str) -> Optional[pd.DataFrame]:
    """从本地 CSV 加载日线数据（格式与 BA 一致）"""
    sub = 'SH' if (code.startswith('6') or code.startswith('5')) else 'SZ'
    path = os.path.join(DAILY_DATA_DIR, sub, f'price_{code}.csv')
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path)
        df = df.rename(columns={'timetag': 'date', 'volumn': 'volume'})
        df['date'] = pd.to_datetime(df['date'].astype(str).str[:8], format='%Y%m%d')
        df = df.sort_values('date').reset_index(drop=True)
        for col in ['open', 'high', 'low', 'close', 'volume', 'amount']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
        # 若无 amount 列，用 close × volume 估算
        if 'amount' not in df.columns:
            df['amount'] = df['close'] * df['volume']
        return df
    except Exception as e:
        print(f"[{_now_str()}] [策略T] 加载日线失败 {code}: {e}")
        return None


def _load_sh_index() -> Optional[pd.DataFrame]:
    """加载上证指数日线（多路径兜底）"""
    candidates = [
        os.path.join(DAILY_DATA_DIR, 'SH', 'price_sh000001.csv'),
        os.path.join(DAILY_DATA_DIR, 'SH', 'price_000001.csv'),
        os.path.join(DAILY_DATA_DIR, 'INDEX', 'sh000001_daily.csv'),
    ]
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            df = pd.read_csv(path)
            if 'timetag' in df.columns:
                df = df.rename(columns={'timetag': 'date'})
                df['date'] = pd.to_datetime(df['date'].astype(str).str[:8], format='%Y%m%d')
            else:
                df['date'] = pd.to_datetime(df['date'])
            df = df.sort_values('date').reset_index(drop=True)
            for col in ['open', 'high', 'low', 'close', 'volume']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
            print(f"[{_now_str()}] [策略T] 上证指数加载: {path} ({len(df)}行)")
            return df
        except Exception as e:
            print(f"[{_now_str()}] [策略T] 上证指数加载失败 {path}: {e}")
    return None


# ═══════════════════════════════════════════════
# 策略T 独立引擎
# ═══════════════════════════════════════════════

class StrategyTEngine:
    """
    策略T 独立实盘引擎

    完全独立于 LiveEngineV4：
      - 独立状态文件（state_t_v4.json）
      - 独立交易记录（trades_t_v4.json）
      - 独立加载日线数据
      - 可在独立进程中运行

    每日调用流程（15:30+ 收盘后触发）：
      on_new_day(today_str)
        ① 执行 pending_buys（今日开盘买入，昨日信号产生）
        ② 管理持仓（trailing stop + 到期强平，用今日收盘价）
        ③ 生成明日信号（用今日收盘数据，反 lookahead）
    """

    def __init__(self, capital: float = 90000.0):
        self.initial_capital  = capital
        self.cash             = capital
        self.positions: Dict[str, dict] = {}
        self.pending_buys: List[str]    = []
        self.last_signal_day_idx: int   = -999
        self._all_trading_dates: List[str] = []
        self._today_day_idx: int        = 0
        self.daily_data: Dict[str, pd.DataFrame] = {}
        self.sh_df: Optional[pd.DataFrame] = None
        # 实盘 xtquant（可选）
        self._xt_trader   = None
        self._account_id: Optional[str] = None

    # ──────────────── 状态持久化 ────────────────

    def load_state(self):
        state = _load_json(T_STATE_FILE, {})
        self.positions           = state.get('positions', {})
        self.cash                = float(state.get('cash', self.initial_capital))
        self.initial_capital     = float(state.get('initial_capital', self.initial_capital))
        self.pending_buys        = state.get('pending_buys', [])
        self.last_signal_day_idx = state.get('last_signal_day_idx', -999)
        print(f"[{_now_str()}] [策略T] 状态加载: 持仓={len(self.positions)} "
              f"现金={self.cash:.0f} 待买={self.pending_buys}")

    def save_state(self):
        _save_json(T_STATE_FILE, {
            'initial_capital':     self.initial_capital,
            'cash':                round(self.cash, 2),
            'positions':           self.positions,
            'pending_buys':        self.pending_buys,
            'last_signal_day_idx': self.last_signal_day_idx,
            'last_update':         _now_str(),
        })

    # ──────────────── 数据加载（独立于 BA）────────────────

    def load_daily_data(self, ref_date: str = None):
        """独立加载全市场日线数据"""
        print(f"[{_now_str()}] [策略T] 加载日线数据...")
        self.sh_df = _load_sh_index()
        if self.sh_df is None:
            print(f"[{_now_str()}] [策略T] ⚠️  无法加载上证指数，信号检查将跳过!")

        count = 0
        for sub in ['SH', 'SZ']:
            d = os.path.join(DAILY_DATA_DIR, sub)
            if not os.path.isdir(d):
                continue
            for fn in os.listdir(d):
                if not (fn.startswith('price_') and fn.endswith('.csv')):
                    continue
                code = fn[6:-4]
                # 只保留纯数字 6 位代码（排除 sh000001 等指数文件名）
                if not (code.isdigit() and len(code) == 6):
                    continue
                if code in self.daily_data:
                    continue
                df = _load_daily_csv(code)
                if df is not None and not df.empty:
                    self.daily_data[code] = df
                    count += 1

        # 构建交易日历（用 000001 平安银行 作为参考，或任意股票）
        ref_df = self.daily_data.get('000001')
        if (ref_df is None or ref_df.empty) and self.daily_data:
            ref_df = next(iter(self.daily_data.values()))
        if ref_df is not None and not ref_df.empty:
            cut = pd.to_datetime(ref_date) if ref_date else ref_df['date'].max()
            self._all_trading_dates = sorted(
                ref_df[ref_df['date'] <= cut]['date']
                .dt.strftime('%Y-%m-%d').tolist())

        print(f"[{_now_str()}] [策略T] 日线加载完成: {count} 只, "
              f"交易日历: {len(self._all_trading_dates)} 天")

    # ──────────────── 主流程 ────────────────

    def on_new_day(self, today_str: str):
        """
        每个交易日收盘后调用（15:30+），today_str 的完整行情已可用。

        对于回测：按交易日循环逐日调用。
        对于实盘：由 run_strategy_t.py 在 15:30 后定时触发。
        """
        if today_str in self._all_trading_dates:
            idx = self._all_trading_dates.index(today_str)
        else:
            idx = len(self._all_trading_dates)
        self._today_day_idx = idx

        print(f"[{_now_str()}] [策略T] ═══ {today_str} (day_idx={idx}) ═══  "
              f"持仓={len(self.positions)} 现金={self.cash:.0f} 待买={self.pending_buys}")

        # ① 执行 pending_buys（今日开盘，昨日信号触发的买入）
        if self.pending_buys:
            self._execute_pending_buys(today_str)

        # ② 管理持仓：trailing stop + 到期强平（用今日收盘价）
        self._manage_positions(today_str)

        # ③ 生成明日信号（用今日收盘数据，无 lookahead）
        if self.sh_df is not None and idx >= T_SH_LOOKBACK:
            self._generate_signal(today_str, idx)

        self.save_state()

    # ──────────────── ① 执行买入 ────────────────

    def _execute_pending_buys(self, today_str: str):
        today_dt   = pd.to_datetime(today_str)
        executed: List[str] = []
        n_to_buy   = min(len(self.pending_buys), T_MAX_POSITIONS - len(self.positions))
        allocation = self.cash / max(1, n_to_buy)  # 等分剩余现金

        for code in list(self.pending_buys):
            if len(self.positions) >= T_MAX_POSITIONS:
                print(f"[{_now_str()}] [策略T] 满仓({T_MAX_POSITIONS})，跳过剩余待买")
                break
            if code in self.positions:
                executed.append(code)
                continue

            df = self.daily_data.get(code)
            if df is None:
                print(f"[{_now_str()}] [策略T] [DBG-T] {code} 无日线数据，跳过")
                executed.append(code)
                continue

            today_row = df[df['date'] == today_dt]
            if today_row.empty:
                print(f"[{_now_str()}] [策略T] [DBG-T] {code} 今日无K线，跳过")
                executed.append(code)
                continue

            open_price = float(today_row['open'].iloc[0])
            if open_price <= 0:
                print(f"[{_now_str()}] [策略T] [DBG-T] {code} open=0，跳过")
                executed.append(code)
                continue

            # 涨停检查：开盘价相对昨收涨幅 >= 9.5% 跳过
            prev_rows  = df[df['date'] < today_dt]
            prev_close = float(prev_rows['close'].iloc[-1]) if not prev_rows.empty else 0
            if prev_close > 0 and (open_price / prev_close - 1) >= T_LIMIT_THRESHOLD:
                print(f"[{_now_str()}] [策略T] [DBG-T] {code} 涨停跳过 "
                      f"open={open_price:.2f} prev_close={prev_close:.2f}")
                executed.append(code)
                continue

            # 仓位计算
            buy_price  = open_price * (1 + T_BUY_SLIPPAGE)
            qty        = math.floor(allocation / buy_price / 100) * 100
            if qty < 100:
                print(f"[{_now_str()}] [策略T] [DBG-T] {code} qty<100 "
                      f"(cash={self.cash:.0f} alloc={allocation:.0f} px={buy_price:.3f})，跳过")
                continue

            commission = qty * buy_price * T_COMMISSION
            cost       = qty * buy_price + commission
            if cost > self.cash:
                print(f"[{_now_str()}] [策略T] [DBG-T] {code} 资金不足 "
                      f"cost={cost:.0f} > cash={self.cash:.0f}")
                continue

            # 实盘下单（可选）
            if self._xt_trader is not None:
                ok = self._place_order_live(code, 'buy', round(buy_price, 2), qty)
                if not ok:
                    print(f"[{_now_str()}] [策略T] ⚠️  {code} 委托失败，跳过")
                    continue

            # 记录成交
            self.cash -= cost
            self.positions[code] = {
                'code':          code,
                'buy_price':     round(buy_price, 4),
                'open_price':    round(open_price, 4),
                'quantity':      qty,
                'buy_date':      today_str,
                'days_held':     0,
                'highest_price': open_price,
            }
            executed.append(code)
            self._record_trade({
                'date': today_str, 'code': code, 'action': 'buy',
                'price': round(buy_price, 4), 'qty': qty,
                'cost': round(cost, 2), 'cash': round(self.cash, 2),
            })
            print(f"[{_now_str()}] [策略T] ✅ 买入 {code} qty={qty} "
                  f"px={buy_price:.3f} cost={cost:.0f} cash剩={self.cash:.0f}")

        self.pending_buys = [c for c in self.pending_buys if c not in executed]

    # ──────────────── ② 管理持仓 ────────────────

    def _manage_positions(self, today_str: str):
        today_dt = pd.to_datetime(today_str)
        for code, pos in list(self.positions.items()):
            df = self.daily_data.get(code)
            if df is None:
                continue
            row = df[df['date'] == today_dt]
            if row.empty:
                continue

            close = float(row['close'].iloc[0])
            high  = float(row['high'].iloc[0])

            # 买入当天（days_held=0）：只更新高点，不参与卖出判断（T+1 限制）
            if pos.get('buy_date') == today_str:
                pos['highest_price'] = max(pos.get('highest_price', close), high, close)
                continue

            # 更新 days_held 和最高价
            pos['days_held']     = pos.get('days_held', 0) + 1
            pos['highest_price'] = max(pos.get('highest_price', close), high, close)
            hp   = pos['highest_price']
            days = pos['days_held']
            bp   = pos['buy_price']

            # D2. Trailing Stop：浮盈 >= 12% 激活，从高点回落 6% 触发
            if hp >= bp * (1 + T_TRAILING_ACTIVATION):
                trail_trigger = hp * (1 - T_TRAILING_STOP_PCT)
                if close <= trail_trigger:
                    sell_price = close * (1 - T_SELL_SLIPPAGE)
                    print(f"[{_now_str()}] [策略T] 📉 {code} trailing触发 "
                          f"hp={hp:.3f} trigger={trail_trigger:.3f} close={close:.3f} "
                          f"sell={sell_price:.3f}")
                    self._execute_sell(code, pos, sell_price, today_str, 'trailing_stop')
                    continue
                else:
                    print(f"[{_now_str()}] [策略T] [K-T] {code} days={days} "
                          f"C={close:.3f} hp={hp:.3f} trail={trail_trigger:.3f}(·) hold")
            else:
                act_px = bp * (1 + T_TRAILING_ACTIVATION)
                print(f"[{_now_str()}] [策略T] [K-T] {code} days={days} "
                      f"C={close:.3f} hp={hp:.3f} trail=未激活(需{act_px:.3f}) hold")

            # D1. 到期强平：持仓 >= T_HOLD_DAYS 天
            if days >= T_HOLD_DAYS:
                sell_price = close * (1 - T_SELL_SLIPPAGE)
                print(f"[{_now_str()}] [策略T] ⏰ {code} 持仓到期({days}天) "
                      f"close={close:.3f} sell={sell_price:.3f}")
                self._execute_sell(code, pos, sell_price, today_str, 'hold_days_expiry')

    # ──────────────── ③ 信号生成 ────────────────

    def _generate_signal(self, today_str: str, idx: int):
        # 冷却检查
        elapsed = idx - self.last_signal_day_idx
        if 0 <= elapsed <= T_SIGNAL_COOLDOWN:
            remaining = T_SIGNAL_COOLDOWN - elapsed
            print(f"[{_now_str()}] [策略T] 信号冷却中，剩余 {remaining} 个交易日")
            return

        # 上证 7 日收益率（T-1 数据，反 lookahead）
        today_dt   = pd.to_datetime(today_str)
        sh_today   = self.sh_df[self.sh_df['date'] == today_dt]
        if sh_today.empty:
            print(f"[{_now_str()}] [策略T] [DBG-T] 上证今日数据缺失，跳过信号检查")
            return
        sh_close = float(sh_today['close'].iloc[0])

        prev7_str  = self._all_trading_dates[idx - T_SH_LOOKBACK]
        sh_prev7   = self.sh_df[self.sh_df['date'] == pd.to_datetime(prev7_str)]
        if sh_prev7.empty:
            return
        sh_close_7ago = float(sh_prev7['close'].iloc[0])
        if sh_close_7ago <= 0:
            return

        sh_7d_ret = (sh_close - sh_close_7ago) / sh_close_7ago
        print(f"[{_now_str()}] [策略T] SH 7日收益: {sh_7d_ret:+.2%} "
              f"(阈值 {T_SH_THRESHOLD:.0%}，冷却已过 {elapsed} 天)")

        if sh_7d_ret >= T_SH_THRESHOLD:
            return  # 未触发

        print(f"[{_now_str()}] [策略T] 🚨 信号触发! SH7d={sh_7d_ret:+.2%} < {T_SH_THRESHOLD:.0%}，开始选股...")

        # 选股：5 日跌幅最大 4 只
        candidates = []
        for code, df in self.daily_data.items():
            # 排除前缀过滤
            if self._is_excluded(code):
                continue
            row_today = df[df['date'] == today_dt]
            if row_today.empty:
                continue
            today_close = float(row_today['close'].iloc[0])

            # 价格过滤
            if not (T_MIN_PRICE <= today_close <= T_MAX_PRICE):
                continue

            # 流动性过滤：20 日均成交额 > 3 亿
            recent = df[df['date'] <= today_dt].tail(22)
            if len(recent) < 20:
                continue
            avg_amount = recent['amount'].tail(20).mean()
            if avg_amount < T_MIN_AVG_AMOUNT:
                continue

            # 5 日收益率
            if idx < T_STOCK_RET_LOOKBACK:
                continue
            prev5_str  = self._all_trading_dates[idx - T_STOCK_RET_LOOKBACK]
            prev5_row  = df[df['date'] == pd.to_datetime(prev5_str)]
            if prev5_row.empty:
                continue
            prev5_close = float(prev5_row['close'].iloc[0])
            if prev5_close <= 0:
                continue

            ret_5d = (today_close - prev5_close) / prev5_close
            candidates.append((code, ret_5d))

        # 排序：5 日跌幅最大（最负）排前
        candidates.sort(key=lambda x: x[1])
        selected = [c for c, _ in candidates[:T_MAX_POSITIONS]]

        if selected:
            self.pending_buys        = selected
            self.last_signal_day_idx = idx
            for c, r in candidates[:T_MAX_POSITIONS]:
                print(f"[{_now_str()}] [策略T]   📌 {c} 5日={r:+.2%}")
            print(f"[{_now_str()}] [策略T] 明日待买: {selected}")
        else:
            print(f"[{_now_str()}] [策略T] ⚠️  信号触发但无满足条件的候选股")

    # ──────────────── 卖出执行 ────────────────

    def _execute_sell(self, code: str, pos: dict, sell_price: float,
                      today_str: str, reason: str):
        qty = pos['quantity']

        if self._xt_trader is not None:
            ok = self._place_order_live(code, 'sell', round(sell_price, 2), qty)
            if not ok:
                print(f"[{_now_str()}] [策略T] ⚠️  {code} 卖出委托失败!")
                return

        commission = qty * sell_price * T_COMMISSION
        stamp_tax  = qty * sell_price * T_STAMP_TAX
        proceeds   = qty * sell_price - commission - stamp_tax
        self.cash  += proceeds

        bp  = pos['buy_price']
        pnl = (sell_price - bp) / bp
        self._record_trade({
            'date': today_str, 'code': code, 'action': 'sell',
            'reason': reason, 'buy_price': round(bp, 4),
            'price': round(sell_price, 4), 'qty': qty,
            'proceeds': round(proceeds, 2), 'pnl': round(pnl, 4),
            'cash': round(self.cash, 2),
        })
        del self.positions[code]
        emoji = '✅' if pnl > 0 else ('➖' if pnl == 0 else '❌')
        print(f"[{_now_str()}] [策略T] {emoji} 卖出 {code} qty={qty} "
              f"px={sell_price:.3f} pnl={pnl:+.2%} [{reason}] cash={self.cash:.0f}")

    # ──────────────── xtquant 实盘接口（可选）────────────────

    def connect_xt(self, account_id: str, userdata_path: str) -> bool:
        """连接 xtquant，session_id=888888（与 BA 的 654321 隔离）"""
        try:
            from xtquant.xttrader import XtQuantTrader
            from xtquant.xttype import StockAccount
            session_id = 888888  # 与 BA(654321) 不同，避免冲突
            self._xt_trader = XtQuantTrader(userdata_path, session_id)
            self._xt_trader.start()
            conn = self._xt_trader.connect()
            if conn != 0:
                print(f"[{_now_str()}] [策略T] xtquant 连接失败: {conn}")
                self._xt_trader = None
                return False
            account = StockAccount(account_id, 'STOCK')
            ret = self._xt_trader.subscribe(account)
            if ret != 0:
                print(f"[{_now_str()}] [策略T] 账号订阅失败: {ret}")
                return False
            self._account_id = account_id
            print(f"[{_now_str()}] [策略T] xtquant 连接成功 account={account_id} session=888888")
            return True
        except ImportError:
            print(f"[{_now_str()}] [策略T] xtquant 不可用，将以模拟模式运行")
            return False
        except Exception as e:
            print(f"[{_now_str()}] [策略T] xtquant 连接异常: {e}")
            return False

    def disconnect_xt(self):
        if self._xt_trader:
            try:
                self._xt_trader.disconnect()
            except Exception:
                pass
            self._xt_trader = None

    def _place_order_live(self, code: str, direction: str,
                          price: float, qty: int) -> bool:
        """通过 xtquant 下实盘限价单"""
        try:
            from xtquant.xttype import StockAccount
            from xtquant import xtconstant
            account     = StockAccount(self._account_id, 'STOCK')
            xt_code     = f"{code}.SH" if code.startswith('6') else f"{code}.SZ"
            direction_f = (xtconstant.STOCK_BUY if direction == 'buy'
                           else xtconstant.STOCK_SELL)
            order_id = self._xt_trader.order_stock(
                account, xt_code, direction_f, qty,
                xtconstant.FIX_PRICE, price,
                'strategy_t', code
            )
            if order_id == -1:
                print(f"[{_now_str()}] [策略T] ⚠️  {code} {direction} 委托返回-1")
                return False
            print(f"[{_now_str()}] [策略T] 📋 委托 {direction} {code} "
                  f"qty={qty} px={price:.2f} order_id={order_id}")
            time.sleep(3)  # 等待委托处理
            return True
        except Exception as e:
            print(f"[{_now_str()}] [策略T] 下单异常 {code}: {e}")
            return False

    # ──────────────── 工具 ────────────────

    def _is_excluded(self, code: str) -> bool:
        return code.startswith(T_EXCLUDE_PREFIXES)

    def _record_trade(self, trade: dict):
        trades = _load_json(T_TRADES_FILE, [])
        trades.append(trade)
        _save_json(T_TRADES_FILE, trades)

    def print_status(self):
        """打印当前状态汇总"""
        print(f"\n{'=' * 56}")
        print(f"  策略T 状态  |  初始资金: {self.initial_capital:.0f}")
        print(f"{'=' * 56}")
        print(f"  现金:   {self.cash:.2f}")

        total_mkt = 0.0
        for code, pos in self.positions.items():
            df   = self.daily_data.get(code)
            last = pos['buy_price']
            if df is not None and not df.empty:
                last = float(df['close'].iloc[-1])
            mkt  = pos['quantity'] * last
            pnl  = (last - pos['buy_price']) / pos['buy_price']
            hp   = pos.get('highest_price', last)
            trail_active = hp >= pos['buy_price'] * (1 + T_TRAILING_ACTIVATION)
            trail_str = f"trail={hp*(1-T_TRAILING_STOP_PCT):.3f}" if trail_active else "trail=未激活"
            print(f"  持仓: {code}  qty={pos['quantity']}  "
                  f"bp={pos['buy_price']:.3f}  last={last:.3f}  "
                  f"pnl={pnl:+.2%}  days={pos.get('days_held',0)}  {trail_str}")
            total_mkt += mkt

        total = self.cash + total_mkt
        nav   = total / self.initial_capital - 1
        print(f"  持仓市值: {total_mkt:.0f}")
        print(f"  总净值:   {total:.0f}  ({nav:+.2%})")
        if self.pending_buys:
            print(f"  待买入:   {self.pending_buys}")
        if self.last_signal_day_idx >= 0:
            print(f"  上次信号: day_idx={self.last_signal_day_idx}")
        print(f"{'=' * 56}\n")
