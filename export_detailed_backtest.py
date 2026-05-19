# -*- coding: utf-8 -*-
"""
导出 2026-01-01~2026-04-30 回测细化数据，用于 Windows/Mac 跨平台对比。

输出文件（OUT_DIR 下）：
  1. detailed_buys.csv          ── 完整买入清单
  2. detailed_sells.csv         ── 完整卖出清单
  3. snapshot_20260430.csv      ── 末日持仓快照 + NAV拆分
  4. daily_ba_pools.csv         ── 每个交易日完整 BA 池（全量，含分数和排名）
  5. bars_002866_20260330.csv   ── 002866 当天全量5min K

用法：
  python export_detailed_backtest.py 2026-01-02 2026-04-30
"""

import sys
import os
import io
import csv
import json
from contextlib import redirect_stdout

sys.path.insert(0, 'd:/miniqmt_quant')

import pandas as pd
from engine.offline_sim_engine_v4 import OfflineSimEngineV4
from engine.live_engine_v4 import (
    _TIME_POINTS, BASE_DIR,
    HARD_STOP, TRAIL_STOP, TRAIL_ACT, STOP_LIMIT_SLIP,
    NEW_STOCK_HARD_STOP, SLIPPAGE,
    compute_ba_pool,
)

# ─────────────────────────────────────────────────────────────────────────────
SNAPSHOT_DATE  = '2026-04-30'
BARS_CODE      = '002866'
BARS_DATE      = '2026-03-30'
OUT_DIR        = os.path.dirname(os.path.abspath(__file__))


# ─────────────────────────────────────────────────────────────────────────────
# 1. 注入式引擎子类
# ─────────────────────────────────────────────────────────────────────────────

class ExportEngine(OfflineSimEngineV4):
    """在 OfflineSimEngineV4 基础上注入采集点，不改变任何交易逻辑。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._current_hm     = None
        self._buy_extra_ctx  = {}   # code -> ctx dict（_execute_buy 调用前填入）
        self._sell_extra_ctx = {}   # code -> ctx dict（_execute_sell 调用前填入）

        # 末日 14:55 后持仓快照
        self._final_positions: dict = {}
        self._final_bars: dict      = {}
        self._final_cash: float     = 0.0

    # ── 追踪当前 K 时间 ──────────────────────────────────────────────────────

    def _process_hm(self, hm: tuple, today_str: str):
        self._current_hm = hm
        super()._process_hm(hm, today_str)
        # 末日 14:55 结束后保存持仓快照（此时 end_of_sim 还没跑）
        if today_str == SNAPSHOT_DATE and hm == (14, 55):
            self._save_final_snapshot()
        self._current_hm = None

    def _process_hm_1455(self, today_str: str):
        self._current_hm = (14, 55)
        super()._process_hm_1455(today_str)
        # 注意：14:55 deferred/close_signal 卖出也在这里，已通过 _execute_sell 注入
        self._current_hm = None

    def _save_final_snapshot(self):
        """14:55 K 处理完毕后保存持仓状态（end_of_sim 前的真实持仓）"""
        self._final_positions = {
            code: {
                'buy_price':     pos['buy_price'],
                'buy_date':      pos.get('buy_date', ''),
                'quantity':      pos['quantity'],
                'days_held':     pos.get('days_held', 0),
                'highest_price': pos.get('highest_price', pos['buy_price']),
                'trend_type':    pos.get('trend_type', ''),
                'is_new_stock':  pos.get('is_new_stock', False),
            }
            for code, pos in self.positions.items()
        }
        self._final_bars  = {code: dict(bars) for code, bars in self.bars_today.items()}
        self._final_cash  = self.cash

    # ── 买入注入 ─────────────────────────────────────────────────────────────

    def _execute_buy(self, code: str, buy_px: float, qty: int,
                     meta: dict, today_str: str, hm: tuple = None):
        _hm      = hm or self._current_hm
        bar      = self.bars_today.get(code, {}).get(_hm)
        prev_c   = self.prev_close_cache.get(code, 0)
        day_open = self.day_open_cache.get(code, 0)
        chg      = (bar['close'] - prev_c) / prev_c if (prev_c > 0 and bar) else None
        self._buy_extra_ctx[code] = {
            'buy_hm':      _hm,
            'prev_close':  prev_c,
            'day_open':    day_open,
            'bar':         bar,
            'current_chg': chg,
            'buy_px_raw':  buy_px,
        }
        super()._execute_buy(code, buy_px, qty, meta, today_str, hm=hm)

    # ── 卖出注入 ─────────────────────────────────────────────────────────────

    def _execute_sell(self, code: str, pos: dict, sell_price: float,
                      reason: str, today_str: str, hm: tuple = None):
        _hm        = hm or self._current_hm
        bar        = self.bars_today.get(code, {}).get(_hm) if _hm else None
        buy_price  = pos['buy_price']
        hp         = pos.get('highest_price', buy_price)
        is_new     = pos.get('is_new_stock', False)
        # G3.4: 优先用 entry 时刻快照参数，回退到全局常量（兼容旧持仓）
        snap_hs    = pos.get('snapshot_hs', NEW_STOCK_HARD_STOP if is_new else HARD_STOP)
        snap_ts    = pos.get('snapshot_ts', TRAIL_STOP)
        eff_hs     = snap_hs
        trigger    = hp * (1 - snap_ts)            # trailing_stop 触发价
        limit_px   = trigger * (1 - STOP_LIMIT_SLIP)  # trailing_stop 限价
        self._sell_extra_ctx[code] = {
            'sell_hm':                  _hm,
            'buy_price':                buy_price,
            'highest_price_at_trigger': hp,
            'bar':                      bar,
            'hard_stop_px':             round(buy_price * (1 - eff_hs), 4),
            'trigger_px':               round(trigger,   4),
            'limit_px':                 round(limit_px,  4),
        }
        super()._execute_sell(code, pos, sell_price, reason, today_str, hm=hm)

    # ── _log_trade 附加细化字段 ──────────────────────────────────────────────

    def _log_trade(self, side, code, price, qty, reason, **kwargs):
        if side == 'buy' and code in self._buy_extra_ctx:
            ctx  = self._buy_extra_ctx.pop(code)
            bar  = ctx['bar']
            hm   = ctx['buy_hm']
            kwargs['buy_hm']          = f"{hm[0]:02d}:{hm[1]:02d}" if hm else ''
            kwargs['prev_close']      = round(ctx['prev_close'], 4)
            kwargs['day_open']        = round(ctx['day_open'], 4)
            kwargs['bar_open']        = round(bar['open'],  4) if bar else ''
            kwargs['bar_high']        = round(bar['high'],  4) if bar else ''
            kwargs['bar_low']         = round(bar['low'],   4) if bar else ''
            kwargs['bar_close']       = round(bar['close'], 4) if bar else ''
            kwargs['current_chg_pct'] = (round(ctx['current_chg'] * 100, 4)
                                         if ctx['current_chg'] is not None else '')
            kwargs['buy_px_raw']      = round(ctx['buy_px_raw'], 4)
            # price 已是 actual_px（buy_px_raw * (1+SLIPPAGE)）
            kwargs['actual_px']       = round(float(price), 4)

        elif side == 'sell' and code in self._sell_extra_ctx:
            ctx  = self._sell_extra_ctx.pop(code)
            bar  = ctx['bar']
            hm   = ctx['sell_hm']
            kwargs['sell_hm']                  = f"{hm[0]:02d}:{hm[1]:02d}" if hm else ''
            kwargs['buy_price_pos']            = round(ctx['buy_price'], 4)
            kwargs['highest_price_at_trigger'] = round(ctx['highest_price_at_trigger'], 4)
            kwargs['bar_open']                 = round(bar['open'],  4) if bar else ''
            kwargs['bar_high']                 = round(bar['high'],  4) if bar else ''
            kwargs['bar_low']                  = round(bar['low'],   4) if bar else ''
            kwargs['bar_close']                = round(bar['close'], 4) if bar else ''
            kwargs['hard_stop_px']             = ctx['hard_stop_px']
            kwargs['trigger_px']               = ctx['trigger_px']
            kwargs['limit_px']                 = ctx['limit_px']

        super()._log_trade(side, code, price, qty, reason, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# 2. 导出函数
# ─────────────────────────────────────────────────────────────────────────────

def _write_csv(path: str, rows: list, fieldnames: list):
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)
    print(f"  已写入: {os.path.basename(path)}  ({len(rows)} 行)")


def export_buys(engine: ExportEngine, out_dir: str):
    """1. 完整买入清单"""
    fields = [
        'date', 'code',
        'buy_hm', 'prev_close', 'day_open',
        'bar_open', 'bar_high', 'bar_low', 'bar_close',
        'current_chg_pct',
        'buy_px_raw', 'actual_px',
        'qty', 'fee', 'cash_after',
    ]
    rows = [t for t in engine._all_trades if t['side'] == 'buy']
    _write_csv(os.path.join(out_dir, 'detailed_buys.csv'), rows, fields)


def export_sells(engine: ExportEngine, out_dir: str):
    """2. 完整卖出清单"""
    fields = [
        'date', 'code',
        'sell_hm', 'sell_type',
        'buy_price_pos', 'highest_price_at_trigger',
        'bar_open', 'bar_high', 'bar_low', 'bar_close',
        'hard_stop_px', 'trigger_px', 'limit_px',
        'sell_price',
        'qty', 'pnl', 'days_held', 'fee', 'cash_after',
    ]
    rows = []
    for t in engine._all_trades:
        if t['side'] != 'sell':
            continue
        row = dict(t)
        row['sell_type']  = t.get('reason', '')
        row['sell_price'] = t['price']
        rows.append(row)
    _write_csv(os.path.join(out_dir, 'detailed_sells.csv'), rows, fields)


def export_snapshot(engine: ExportEngine, out_dir: str):
    """3. 末日 2026-04-30 持仓快照 + final NAV 拆分"""
    positions = engine._final_positions
    bars      = engine._final_bars
    cash      = engine._final_cash
    hm_1455   = (14, 55)

    snap_rows = []
    total_mkt = 0.0
    for code, pos in positions.items():
        code_bars  = bars.get(code, {})
        last_px    = (code_bars[hm_1455]['close'] if hm_1455 in code_bars
                      else (max(code_bars.items())[1]['close'] if code_bars
                            else pos['buy_price']))
        mkt_val    = last_px * pos['quantity']
        total_mkt += mkt_val
        unreal_pct = (last_px / pos['buy_price'] - 1) * 100 if pos['buy_price'] > 0 else 0
        snap_rows.append({
            'code':           code,
            'buy_date':       pos['buy_date'],
            'buy_price':      round(pos['buy_price'], 4),
            'qty':            pos['quantity'],
            'days_held':      pos['days_held'],
            'highest_price':  round(pos['highest_price'], 4),
            'last_price_1455': round(last_px, 4),
            'mkt_val':        round(mkt_val, 2),
            'unrealized_pct': round(unreal_pct, 3),
            'trend_type':     pos['trend_type'],
        })

    total_nav = cash + total_mkt
    init_cap  = engine.initial_capital
    ret_pct   = (total_nav / init_cap - 1) * 100 if init_cap > 0 else 0

    # 末行写 NAV 汇总
    snap_rows.append({
        'code':           '── SUMMARY ──',
        'buy_date':       '',
        'buy_price':      '',
        'qty':            '',
        'days_held':      '',
        'highest_price':  '',
        'last_price_1455': '',
        'mkt_val':        round(total_mkt, 2),
        'unrealized_pct': '',
        'trend_type':     (f'cash={round(cash,2)}  mkt_val={round(total_mkt,2)}'
                           f'  total_nav={round(total_nav,2)}  return={ret_pct:+.3f}%'),
    })

    fields = [
        'code', 'buy_date', 'buy_price', 'qty', 'days_held',
        'highest_price', 'last_price_1455', 'mkt_val', 'unrealized_pct', 'trend_type',
    ]
    _write_csv(os.path.join(out_dir, 'snapshot_20260430.csv'), snap_rows, fields)

    print(f"    cash={cash:,.2f}  mkt_val={total_mkt:,.2f}  "
          f"total_nav={total_nav:,.2f}  return={ret_pct:+.3f}%")


def export_daily_ba_pools(engine: ExportEngine, out_dir: str, trading_dates: list):
    """4b. 每个交易日完整调仓池（全量50只 + 得分），用于逐日跨平台对比"""
    all_dates = trading_dates
    rows = []
    missing = []

    for trading_date in all_dates:
        idx = all_dates.index(trading_date)
        if idx == 0:
            # 第一天找 start 之前最近交易日（engine.all_trading_dates 包含更早日期）
            full = getattr(engine, 'all_trading_dates', all_dates)
            pre = [d for d in full if d < trading_date]
            prev_day = pre[-1] if pre else None
        else:
            prev_day = all_dates[idx - 1]

        if not prev_day:
            continue

        cache_path = os.path.join(BASE_DIR, f'ba_pool_v4_{prev_day}.json')
        if os.path.exists(cache_path):
            with open(cache_path, encoding='utf-8') as f:
                cached = json.load(f)
            pool = cached.get('pool', [])
        else:
            missing.append(prev_day)
            # 无缓存：实时计算（较慢）
            pool = list(compute_ba_pool(
                engine.daily_data, prev_day,
                getattr(engine, 'all_trading_dates', all_dates), top_n=50))

        # pool 每项格式：(code, rank, score) 3元素（来自 compute_ba_pool 或 JSON缓存）
        for enum_rank, item in enumerate(pool, start=1):
            if isinstance(item, (list, tuple)) and len(item) >= 3:
                code  = item[0]
                score = round(float(item[2]), 6)
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                code  = item[0]
                score = round(float(item[1]), 6)
            else:
                code  = item
                score = ''
            rows.append({
                'trading_date': trading_date,
                'ref_date':     prev_day,
                'rank':         enum_rank,
                'code':         code,
                'score':        score,
            })

    if missing:
        print(f"  [INFO] 以下 {len(missing)} 个 ref_date 无缓存，已实时计算: {missing[:5]}{'...' if len(missing)>5 else ''}")

    fields = ['trading_date', 'ref_date', 'rank', 'code', 'score']
    _write_csv(os.path.join(out_dir, 'daily_ba_pools.csv'), rows, fields)


def export_5min_bars(engine: ExportEngine, code: str, date_str: str, out_dir: str):
    """5. 指定股票指定日期的全量5min K线"""
    sub  = 'SH' if (code.startswith('6') or code.startswith('5')) else 'SZ'
    path = os.path.join(engine.data_5min_dir, sub, f'{code}.csv')
    if not os.path.exists(path):
        print(f"  [WARN] 5min CSV 不存在: {path}")
        return

    df     = pd.read_csv(path, dtype={'time': str})
    day_df = df[df['date'] == date_str].copy()
    if day_df.empty:
        print(f"  [WARN] {code} @ {date_str} 无5min数据")
        return

    rows = []
    for _, row in day_df.iterrows():
        t = str(row['time'])
        hm_str = f"{t[8:10]}:{t[10:12]}" if len(t) >= 12 else t
        rows.append({
            'time_hm': hm_str,
            'open':    round(float(row['open']),   4),
            'high':    round(float(row['high']),   4),
            'low':     round(float(row['low']),    4),
            'close':   round(float(row['close']),  4),
            'volume':  int(float(row['volume'])),
            'amount':  round(float(row.get('amount', 0)), 2),
        })

    fname = f'bars_{code}_{date_str.replace("-","")}.csv'
    fields = ['time_hm', 'open', 'high', 'low', 'close', 'volume', 'amount']
    _write_csv(os.path.join(out_dir, fname), rows, fields)


# ─────────────────────────────────────────────────────────────────────────────
# 3. 主入口
# ─────────────────────────────────────────────────────────────────────────────

def main():
    start_date = sys.argv[1] if len(sys.argv) > 1 else '2026-01-02'
    end_date   = sys.argv[2] if len(sys.argv) > 2 else '2026-04-30'

    print(f"\n[export] 运行回测 {start_date} ~ {end_date}...")
    engine = ExportEngine(capital=300_000.0)

    buf = io.StringIO()
    with redirect_stdout(buf):
        engine.run(start_date=start_date, end_date=end_date)

    # 获取完整交易日历（all_trading_dates 在 run() 内被赋值）
    trading_dates = getattr(engine, 'all_trading_dates', [])
    # 只保留回测区间内的交易日（避免遍历全量日历）
    backtest_days = [d for d in trading_dates if start_date <= d <= end_date]

    print(f"\n[export] 回测完成，开始导出 CSV...")
    print(f"[export] 输出目录: {OUT_DIR}")

    export_buys(engine, OUT_DIR)
    export_sells(engine, OUT_DIR)
    export_snapshot(engine, OUT_DIR)
    export_daily_ba_pools(engine, OUT_DIR, backtest_days)
    export_5min_bars(engine, BARS_CODE, BARS_DATE, OUT_DIR)

    # 打印简要汇总（供肉眼核对）
    buys   = [t for t in engine._all_trades if t['side'] == 'buy']
    sells  = [t for t in engine._all_trades if t['side'] == 'sell']
    hs_cnt = sum(1 for t in sells if t.get('reason') == 'hard_stop')
    ts_cnt = sum(1 for t in sells if t.get('reason') == 'trailing_stop')
    es_cnt = sum(1 for t in sells if t.get('reason') == 'end_of_sim')
    total_pnl = sum(t.get('pnl', 0) for t in sells)
    last_ec   = engine._equity_curve[-1] if engine._equity_curve else {}

    print(f"\n[export] ── 回测摘要 ──────────────────────────────────")
    print(f"  买入笔数: {len(buys)}  卖出笔数: {len(sells)}")
    print(f"  hard_stop={hs_cnt}  trailing_stop={ts_cnt}  end_of_sim={es_cnt}")
    print(f"  PnL合计: {total_pnl:+,.2f}")
    print(f"  最终净值: {last_ec.get('total_value', 0):,.2f}  "
          f"收益率: {last_ec.get('return_pct', 0):+.3f}%")
    print(f"\n[export] 完成！输出文件：")
    for fname in ['detailed_buys.csv', 'detailed_sells.csv',
                  f'snapshot_{SNAPSHOT_DATE.replace("-","")}.csv',
                  'daily_ba_pools.csv',
                  f'bars_{BARS_CODE}_{BARS_DATE.replace("-","")}.csv']:
        fpath = os.path.join(OUT_DIR, fname)
        exists = '✓' if os.path.exists(fpath) else '✗'
        print(f"  {exists} {fpath}")


if __name__ == '__main__':
    main()
