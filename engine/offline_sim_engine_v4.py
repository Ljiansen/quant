# -*- coding: utf-8 -*-
"""
V4 离线回测引擎
继承 LiveEngineV4，覆盖 xtquant 相关接口，用本地5分钟CSV驱动回测。
"""
import os
import sys
import json

sys.path.insert(0, 'd:/miniqmt_quant')

import pandas as pd
from datetime import date as _date
from typing import Optional

from engine.live_engine_v4 import (
    LiveEngineV4, _TIME_POINTS, _now_str, compute_ba_pool,
    _buy_commission, _sell_net, _buy_qty, SLIPPAGE, MAX_POSITIONS,
    _load_daily_csv, BASE_DIR, HARD_STOP,
    build_sh_ma_cache, _load_sh_index_daily,
    _load_sh_features_g34, G34_PARAMS,
)


# 5分钟数据目录（每股一个CSV，含全历史）
_5MIN_DIR = 'D:/5min_data'


class OfflineSimEngineV4(LiveEngineV4):
    """V4 离线历史回测引擎（不连接 miniQMT，用本地CSV数据驱动）"""

    ENGINE_NAME = 'OfflineSimV4'

    def __init__(self, capital: float = 300_000.0,
                 data_5min_dir: str = _5MIN_DIR):
        super().__init__()
        self.initial_capital = capital
        self.cash = capital
        self.data_5min_dir = data_5min_dir

        # 回测结果收集
        self._equity_curve: list = []
        self._all_trades: list = []

    # ──────────────────────────────────────────────────
    # 覆盖：xtquant 接口 → 模拟实现
    # ──────────────────────────────────────────────────

    def _connect_xt(self) -> bool:
        self.cash = self.initial_capital
        return True

    def _place_buy_order(self, code: str, qty: int, price: float) -> bool:
        return True

    def _place_sell_order(self, code: str, qty: int, price: float) -> bool:
        return True

    def _route_buy_price(self, code: str, bar_c: float) -> float:
        """回测模式：固定走降级路径，禁止调用 get_full_tick 避免实时价污染历史回测。"""
        return round(bar_c * (1 + SLIPPAGE), 2)

    def _route_sell_price(self, code: str, sell_price: float) -> float:
        """回测模式：固定走降级路径，禁止调用 get_full_tick 避免实时价污染历史回测。"""
        return round(sell_price * (1 - SLIPPAGE), 2)

    def _subscribe_quotes(self, codes):
        pass

    def _save_state(self, force: bool = False):
        """offline 回测不需要持久化。force 参数与 live 签名对齐，避免 _execute_buy/sell 传 force=True 时 TypeError。"""
        pass

    def _log_trade(self, side, code, price, qty, reason, **kwargs):
        self._all_trades.append({
            'date':   self._today_str,
            'side':   side,
            'code':   code,
            'price':  round(float(price), 3),
            'qty':    qty,
            'reason': reason,
            **{k: (round(float(v), 3) if isinstance(v, float) else v)
               for k, v in kwargs.items()},
        })

    # ──────────────────────────────────────────────────
    # 覆盖：5分钟K线 → 本地CSV加载
    # ──────────────────────────────────────────────────

    def _resolve_5min_path(self, code: str) -> str:
        """找到5min CSV文件路径：D:/5min_data/{SH|SZ}/{code}.csv"""
        sub = 'SH' if (code.startswith('6') or code.startswith('5')) else 'SZ'
        return os.path.join(self.data_5min_dir, sub, f'{code}.csv')

    def _preload_5min_for_day(self, codes, date_str: str):
        """
        预先把当日全部5min bars 载入 bars_today 和 day_open_cache。
        数据源: D:/5min_data/{SH|SZ}/{code}.csv
        列: date, time, open, high, low, close, volume, amount
        time格式: 20260513093500000（YYYYMMDDHHMMSS+ms，K线结束时刻）
        """
        self.bars_today = {}
        self.day_open_cache = {}

        for code in codes:
            path = self._resolve_5min_path(code)
            if not os.path.exists(path):
                continue
            try:
                df = pd.read_csv(path, dtype={'time': str})
                # 只取当日数据
                day_df = df[df['date'] == date_str]
                if day_df.empty:
                    continue
                bars = {}
                for _, row in day_df.iterrows():
                    t = str(row['time'])  # '20260513093500000'
                    if len(t) < 12:
                        continue
                    hm = (int(t[8:10]), int(t[10:12]))
                    bars[hm] = {
                        'open':   float(row['open']),
                        'high':   float(row['high']),
                        'low':    float(row['low']),
                        'close':  float(row['close']),
                        'volume': float(row['volume']),
                    }
                if bars:
                    self.bars_today[code] = bars
                    # 最早K的open = 9:30开盘价（用于gap_filter）
                    first_hm = min(bars.keys())
                    self.day_open_cache[code] = bars[first_hm]['open']
            except Exception as e:
                pass  # 无数据视为停牌

    def _fetch_5min_bars(self, codes, today_str: str, hm: tuple):
        """离线模式：数据已由 _preload_5min_for_day 预先载入，无需操作"""
        pass

    def _process_hm_1455(self, today_str: str):
        """离线模式重写：执行14:55逻辑，但不写 deferred/pending 状态文件"""
        hm = (14, 55)
        # _fetch_5min_bars 已是 no-op，数据已预加载

        # 3a. deferred_sells 兜底
        for code, ds_info in list(self.deferred_sells.items()):
            if code not in self.positions:
                del self.deferred_sells[code]
                continue
            bar = self.bars_today.get(code, {}).get(hm)
            sell_price = bar['close'] if bar else (
                self.positions[code]['buy_price'] * 0.95)
            self._execute_sell(code, self.positions[code], sell_price,
                               ds_info['sell_type'], today_str)
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
                self._execute_sell(code, pos, sell_price, reason, today_str)
            elif action == 'add_pending':
                self.pending_sells.append({
                    'code': code, 'quantity': pos['quantity'], 'sell_type': reason})
        # 离线模式：不写 DEFERRED_FILE / PENDING_FILE

    # ──────────────────────────────────────────────────
    # 主回测入口
    # ──────────────────────────────────────────────────

    def run(self, start_date: str, end_date: str):
        """
        离线回测主入口，逐交易日模拟完整策略流程。
        start_date / end_date: 'YYYY-MM-DD'
        """
        print(f"\n[{self.ENGINE_NAME}] ====== V4 离线回测 {start_date} ~ {end_date} ======")
        print(f"[{self.ENGINE_NAME}] 初始资金: {self.initial_capital:,.0f}")

        # ── 1. 加载日线数据 ──
        print(f"[{self.ENGINE_NAME}] 加载日线数据...")
        self._load_all_daily_data(end_date)
        if not self.daily_data:
            print("无日线数据，退出")
            return

        # ── 2. 构建交易日历 ──
        ref_df = self.daily_data.get('000001')
        if ref_df is None:
            ref_df = next(iter(self.daily_data.values()))
        end_dt = pd.to_datetime(end_date)
        all_dates = sorted(
            ref_df[ref_df['date'] <= end_dt]['date']
            .dt.strftime('%Y-%m-%d').tolist())
        trading_days = [d for d in all_dates if start_date <= d <= end_date]

        print(f"[{self.ENGINE_NAME}] 交易日历共 {len(all_dates)} 天，回测区间 {len(trading_days)} 天")
        self.all_trading_dates = all_dates

        # ── 3. 初始化状态（仅在首次启动时执行，跨日保持 positions）──
        # BUGFIX 2026-05-19 (Root cause #1): 与 mac precompute/run_backtest.py 主循环对齐
        # mac 是单次大循环, positions 跨日持续累积; win 之前每次 run() 都清空, 导致跨日丢仓
        # 注意：positions/cash/deferred_sells/pending_sells/wait_queue 在多日回测中必须保持
        if not hasattr(self, '_backtest_initialized'):
            self.positions = {}
            self.cash = self.initial_capital
            self.deferred_sells = {}
            self.pending_sells = []
            self.wait_queue = {}
            self._backtest_initialized = True
        # 记录回测起始日, 供 _can_buy_dyn (Root cause #3 修复) 对齐 mac sim_dates 边界
        # 严禁删除下面这一行, 提示词 #3 依赖它
        self._backtest_start_str = start_date
        self._equity_curve = []
        self._all_trades = []

        # ── 上证 MA20/5日斜率/close<MA20 缓存 + G3.4 SH特征缓存（一次性预算）──
        sh_df = _load_sh_index_daily()
        self.sh_ma_cache  = build_sh_ma_cache(sh_df)
        self.sh_g34_cache = _load_sh_features_g34(sh_df)
        print(f"[DBG_SH] sh_df is None: {sh_df is None}")
        if sh_df is not None:
            print(f"[DBG_SH] sh_df rows: {len(sh_df)}, date range: {sh_df['date'].min()} ~ {sh_df['date'].max()}")
            print(f"[DBG_SH] sh_df 末尾 5 行 close: {sh_df.sort_values('date').tail(5)[['date','close']].values.tolist()}")
        print(f"[DBG_SH] sh_ma_cache len: {len(self.sh_ma_cache)}, sh_g34_cache len: {len(self.sh_g34_cache)}")
        for _d in ['2025-12-31', '2026-01-12', '2026-03-31', '2026-04-22']:
            print(f"[DBG_SH] {_d}: ma_cache={self.sh_ma_cache.get(_d)} g34={self.sh_g34_cache.get(_d)}")

        # ── 4. 逐日回测 ──
        for day_idx, day_str in enumerate(trading_days):
            self._today_str = day_str
            day_dt = pd.to_datetime(day_str)

            print(f"\n{'─'*60}")
            print(f"[{self.ENGINE_NAME}] 交易日: {day_str} (第{day_idx+1}/{len(trading_days)}天)")

            # [早盘前] days_held 递增
            for pos in self.positions.values():
                if pos.get('buy_date') != day_str:
                    pos['days_held'] = pos.get('days_held', 0) + 1

            # ★★★ G3.4 HOTFIX (2026-05-18): 每日按 G3.4 regime 决策刷新 cur_max_pos ★★★
            # 替代原 G1 HOTFIX v2；sh_g34_cache 已在 run() 开始时一次性预算好
            _dec = self._g34_regime_decide(day_str)
            self.cur_max_pos = int(_dec['max_positions'])
            self.cur_regime  = _dec['regime']
            _sp = self._g34_stock_params(day_str)
            _feat = self.sh_g34_cache.get(self._prev_trading_date(day_str), {})
            print(
                f"  [G3.4] regime={_dec['regime']} sh_below={_feat.get('below','?')} "
                f"streak={_feat.get('streak','?')} ret_30d={_feat.get('ret_30d',0.0):+.2%} "
                f"vol_30d={_feat.get('vol_30d',0.0):.4f} → max_pos={self.cur_max_pos} "
                f"hs={_sp['hs']:.3f} ta={_sp['trail_act']:.2f} ts={_sp['trail_stop']:.3f}"
            )
            # ★★★ G3.4 HOTFIX END ★★★

            # [9:15] 处理 pending_sells（集合竞价：昨收×0.99卖出）
            if self.pending_sells:
                print(f"  [9:15] 处理 pending_sells: {len(self.pending_sells)} 笔")
                for ps in list(self.pending_sells):
                    code = ps['code']
                    if code not in self.positions:
                        continue
                    pos = self.positions[code]
                    prev_c = self.prev_close_cache.get(code, pos.get('buy_price', 0))
                    sell_px = prev_c * 0.99 if prev_c > 0 else pos['buy_price'] * 0.95
                    self._execute_sell(code, pos, sell_px,
                                       ps.get('sell_type', 'pending'), day_str)
                self.pending_sells = []

            # ── 找上一个交易日 ──
            if day_idx > 0:
                prev_day = trading_days[day_idx - 1]
            else:
                # 第一天：找 start_date 之前最近的交易日（用全量 all_dates）
                pre = [d for d in all_dates if d < day_str]
                prev_day = pre[-1] if pre else None

            # ── BA pool（用前一交易日作 ref） ──
            if prev_day:
                cache_path = os.path.join(BASE_DIR, f'ba_pool_v4_{prev_day}.json')
                if os.path.exists(cache_path):
                    with open(cache_path, encoding='utf-8') as f:
                        cached = json.load(f)
                    self.today_pool = [tuple(x) for x in cached['pool']]
                    print(f"  [BA] 读缓存 {prev_day}: {len(self.today_pool)}只")
                else:
                    self.today_pool = compute_ba_pool(
                        self.daily_data, prev_day, all_dates, top_n=50)
                    print(f"  [BA] 实时计算 {prev_day}: {len(self.today_pool)}只")
            else:
                self.today_pool = []

            if not self.today_pool:
                print(f"  [跳过] BA pool为空")
                self._record_equity(day_str)
                continue

            # ── 过滤链 ──
            self._build_filter_chain(day_str)

            # ── prev_close_cache ──
            all_codes = set(self.buy_candidates) | set(self.positions.keys())
            self.prev_close_cache = {}
            for code in all_codes:
                df = self.daily_data.get(code)
                if df is not None:
                    hist = df[df['date'] < day_dt]
                    if not hist.empty:
                        self.prev_close_cache[code] = float(hist['close'].iloc[-1])


            # ── 预加载当日5min数据 ──
            self._preload_5min_for_day(all_codes, day_str)

            # ── gap_min 过滤（9:30 open） ──
            self._apply_gap_filter(day_str)

            # ── 重置当日标志 ──
            self._bought_today = set()
            self._processed_hms = set()
            self._premarket_done = True
            self._gap_filter_done = True
            self._close_check_done = False
            self._auction_done = True
            self._postmarket_done = True  # offline 不调盘后预算，标记已完成避免继承路径误触发

            codes_with_data = len(self.bars_today)
            print(f"  [盘前] 候选={len(self.buy_candidates)} 持仓={len(self.positions)} "
                  f"5min数据={codes_with_data}只")

            # ── 逐5min K处理（14:55 也走完整 intraday 流程） ──
            for hm in _TIME_POINTS:
                self._process_hm(hm, day_str)

            # ── 14:55 收盘前补充处理（deferred 兜底 + close signal，与 intraday 不重复）──
            self._process_hm_1455(day_str)

            # ── 日终统计 ──
            mkt_val = 0.0
            pos_details = []
            for code, pos in self.positions.items():
                bars = self.bars_today.get(code, {})
                if bars:
                    latest_hm = max(bars.keys())
                    price = bars[latest_hm]['close']
                else:
                    price = pos['buy_price']
                mkt_val += price * pos['quantity']
                unrealized = (price / pos['buy_price'] - 1) if pos['buy_price'] > 0 else 0
                pos_details.append(f"{code}({unrealized:+.1%})")

            total_asset = self.cash + mkt_val
            ret_pct = (total_asset / self.initial_capital - 1)

            print(f"  [15:00] 持仓={len(self.positions)}{' '+'/'.join(pos_details) if pos_details else ''} "
                  f"现金={self.cash:,.0f} 市值={mkt_val:,.0f} "
                  f"总资产={total_asset:,.0f} 累计收益={ret_pct:+.2%}")

            self._equity_curve.append({
                'date':             day_str,
                'total_value':      round(total_asset, 2),
                'cash':             round(self.cash, 2),
                'positions_count':  len(self.positions),
                'return_pct':       round(ret_pct * 100, 3),
            })

        # ── 5. 末日强平（end_of_sim）──
        if self.positions and trading_days:
            last_day = trading_days[-1]
            last_dt  = pd.to_datetime(last_day)
            print(f"\n[{self.ENGINE_NAME}] 末日强平 {last_day}：{len(self.positions)}只持仓")
            # BUGFIX 2026-05-19 (Root cause #4): 与 mac precompute/run_backtest.py L2019-2024 对齐
            # mac 用 bars_idx[code][last_day][max(keys)]['close'] = 15:00 close
            # win bars_today 由 _TIME_POINTS 喂入, 只到 14:55, 不含 (15,0)
            # 必须重新读 5min CSV 取 (15,0) close, 否则末日 ~0.16pp 精度差异
            for code, pos in list(self.positions.items()):
                sell_px = None
                # 1) 重新读 5min CSV, 取末根 (必是 15:00)
                try:
                    path = self._resolve_5min_path(code)
                    if os.path.exists(path):
                        _df5 = pd.read_csv(path, dtype={'time': str})
                        _day_df = _df5[_df5['date'] == last_day]
                        if not _day_df.empty:
                            _hms = []
                            for _, _r in _day_df.iterrows():
                                _t = str(_r['time'])
                                if len(_t) >= 12:
                                    _hms.append(((int(_t[8:10]), int(_t[10:12])), float(_r['close'])))
                            if _hms:
                                _hms.sort(key=lambda x: x[0])
                                sell_px = _hms[-1][1]
                except Exception:
                    sell_px = None
                # 2) 兜底: bars_today 末根 close
                if sell_px is None:
                    bars = self.bars_today.get(code, {})
                    if bars:
                        sell_px = bars[max(bars.keys())]['close']
                # 3) 兜底: 日线 close
                if sell_px is None:
                    df = self.daily_data.get(code)
                    if df is not None:
                        row = df[df['date'] == last_dt]
                        if not row.empty:
                            sell_px = float(row['close'].iloc[0])
                # 4) 最后兜底
                if sell_px is None:
                    sell_px = pos['buy_price']
                self._execute_sell(code, pos, sell_px, 'end_of_sim', last_day)

            # 强平后更新净值曲线最后一条（反映实际清仓后现金）
            if self._equity_curve and self._equity_curve[-1]['date'] == last_day:
                self._equity_curve[-1]['total_value'] = round(self.cash, 2)
                self._equity_curve[-1]['cash']        = round(self.cash, 2)
                self._equity_curve[-1]['positions_count'] = 0
                self._equity_curve[-1]['return_pct']  = round(
                    (self.cash / self.initial_capital - 1) * 100, 3)
            else:
                self._equity_curve.append({
                    'date':             last_day,
                    'total_value':      round(self.cash, 2),
                    'cash':             round(self.cash, 2),
                    'positions_count':  0,
                    'return_pct':       round((self.cash / self.initial_capital - 1) * 100, 3),
                })

        # ── 6. 汇总报告 ──
        self._print_summary()

    def _record_equity(self, day_str: str):
        """跳过日（无5min数据）时用日线收盘价估算市值"""
        day_dt = pd.to_datetime(day_str)
        mkt_val = 0.0
        for code, pos in self.positions.items():
            # 优先用5min最新K收盘价，其次用当日日线收盘价，最后用买入价
            bars = self.bars_today.get(code, {})
            if bars:
                price = bars[max(bars.keys())]['close']
            else:
                df = self.daily_data.get(code)
                if df is not None:
                    row = df[df['date'] == day_dt]
                    price = float(row['close'].iloc[0]) if not row.empty else pos['buy_price']
                else:
                    price = pos['buy_price']
            mkt_val += price * pos['quantity']
        total = self.cash + mkt_val
        self._equity_curve.append({
            'date':            day_str,
            'total_value':     round(total, 2),
            'cash':            round(self.cash, 2),
            'positions_count': len(self.positions),
            'return_pct':      round((total / self.initial_capital - 1) * 100, 3),
        })

    def _print_summary(self):
        if not self._equity_curve:
            print(f"\n[{self.ENGINE_NAME}] 无净值数据")
            return

        init  = self.initial_capital
        final = self._equity_curve[-1]['total_value']
        total_ret = (final / init - 1)

        buys  = [t for t in self._all_trades if t['side'] == 'buy']
        sells = [t for t in self._all_trades if t['side'] == 'sell']
        wins  = [t for t in sells if t.get('pnl', 0) > 0]
        win_rate = len(wins) / len(sells) if sells else 0

        max_dd = 0.0
        peak = init
        for e in self._equity_curve:
            v = e['total_value']
            if v > peak:
                peak = v
            dd = (peak - v) / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd

        print(f"\n{'='*60}")
        print(f"[{self.ENGINE_NAME}] ====== 回测结果汇总 ======")
        print(f"  初始资金:   {init:>12,.0f}")
        print(f"  最终资产:   {final:>12,.0f}")
        print(f"  总收益率:   {total_ret:>+12.2%}")
        print(f"  最大回撤:   {max_dd:>+12.2%}")
        print(f"  买入笔数:   {len(buys):>12}")
        print(f"  卖出笔数:   {len(sells):>12}")
        print(f"  胜率:       {win_rate:>12.1%}  ({len(wins)}/{len(sells)})")

        if sells:
            pnls = [t.get('pnl', 0) for t in sells]
            print(f"  平均单笔:   {sum(pnls)/len(pnls):>+12.2f}")
            print(f"  总PnL:      {sum(pnls):>+12.2f}")

        print(f"\n净值曲线:")
        print(f"  {'日期':<12} {'总资产':>10} {'当日持仓':>6} {'累计收益':>10}")
        print(f"  {'─'*44}")
        for e in self._equity_curve:
            bar = '█' * max(0, int(e['return_pct'] * 2))
            minus = '▼' if e['return_pct'] < 0 else ' '
            print(f"  {e['date']:<12} {e['total_value']:>10,.0f} "
                  f"{e['positions_count']:>6}只   {e['return_pct']:>+6.2f}%  {bar}")

        # 保存结果
        out_path = os.path.join(BASE_DIR, 'backtest_v4_result.json')
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump({
                'equity_curve': self._equity_curve,
                'trades':       self._all_trades,
                'summary': {
                    'initial_capital':  init,
                    'final_value':      final,
                    'total_return_pct': round(total_ret * 100, 3),
                    'max_drawdown_pct': round(max_dd * 100, 3),
                    'trade_count':      len(buys),
                    'win_rate_pct':     round(win_rate * 100, 1),
                }
            }, f, ensure_ascii=False, indent=2)
        print(f"\n结果已保存: {out_path}")
