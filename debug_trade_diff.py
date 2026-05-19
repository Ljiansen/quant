# -*- coding: utf-8 -*-
"""
精确定位 Windows vs Mac 5 笔卖价差异的根因。

原理：子类覆盖 _process_hm / _process_hm_1455，
     在每次 _execute_sell 前记录当时的 (hm, bar)，
     回测结束后 dump 问题股票的触发K + 当日全量5min CSV。

用法：
  python debug_trade_diff.py 2026-01-02 2026-04-30
  # 结果写到 debug_trade_diff_output.txt
"""
import sys
import os
import json
import io
from contextlib import redirect_stdout

sys.path.insert(0, 'd:/miniqmt_quant')

import pandas as pd
from engine.offline_sim_engine_v4 import OfflineSimEngineV4
from engine.live_engine_v4 import _TIME_POINTS

# ── 需要精确对比的5笔问题交易 ──────────────────────────
DEBUG_TARGETS = {
    ('2026-01-15', '000960'),
    ('2026-01-13', '688258'),
    ('2026-01-15', '300045'),
    ('2026-04-01', '300137'),
    ('2026-04-03', '002866'),
}

OUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'debug_trade_diff_output.txt')


class DebugEngine(OfflineSimEngineV4):
    """注入 trigger_hm / trigger_bar 到每笔 sell trade"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._current_hm = None   # 当前正在处理的 K 时间点

    # ── 覆盖 _process_hm：注入 _current_hm ──────────────
    def _process_hm(self, hm: tuple, today_str: str):
        self._current_hm = hm
        super()._process_hm(hm, today_str)
        self._current_hm = None

    def _process_hm_1455(self, today_str: str):
        self._current_hm = (14, 55)
        super()._process_hm_1455(today_str)
        self._current_hm = None

    # ── 覆盖 _log_trade：附加 trigger_hm + trigger_bar ──
    def _log_trade(self, side, code, price, qty, reason, **kwargs):
        if side == 'sell' and self._current_hm is not None:
            bar = self.bars_today.get(code, {}).get(self._current_hm)
            kwargs['trigger_hm']  = f"{self._current_hm[0]:02d}:{self._current_hm[1]:02d}"
            kwargs['trigger_open']  = round(bar['open'],  3) if bar else None
            kwargs['trigger_high']  = round(bar['high'],  3) if bar else None
            kwargs['trigger_low']   = round(bar['low'],   3) if bar else None
            kwargs['trigger_close'] = round(bar['close'], 3) if bar else None
        super()._log_trade(side, code, price, qty, reason, **kwargs)


def dump_5min_bars(engine: DebugEngine, code: str, date_str: str) -> str:
    """读取问题股票的当日全量5min K，返回格式化字符串"""
    sub  = 'SH' if (code.startswith('6') or code.startswith('5')) else 'SZ'
    path = os.path.join(engine.data_5min_dir, sub, f'{code}.csv')
    if not os.path.exists(path):
        return f"  [5min CSV 不存在: {path}]\n"
    try:
        df = pd.read_csv(path, dtype={'time': str})
        day_df = df[df['date'] == date_str].copy()
        if day_df.empty:
            return f"  [当日({date_str})无5min数据]\n"
        lines = [f"  {'时间':>8} {'open':>8} {'high':>8} {'low':>8} {'close':>8} {'volume':>12}"]
        lines.append("  " + "-" * 60)
        for _, row in day_df.iterrows():
            t = str(row['time'])
            hm_str = f"{t[8:10]}:{t[10:12]}" if len(t) >= 12 else t
            lines.append(f"  {hm_str:>8} {float(row['open']):>8.3f} {float(row['high']):>8.3f} "
                         f"{float(row['low']):>8.3f} {float(row['close']):>8.3f} "
                         f"{float(row['volume']):>12.0f}")
        return "\n".join(lines) + "\n"
    except Exception as e:
        return f"  [读取失败: {e}]\n"


def main():
    start_date = sys.argv[1] if len(sys.argv) > 1 else '2026-01-02'
    end_date   = sys.argv[2] if len(sys.argv) > 2 else '2026-04-30'

    print(f"[debug] 运行回测 {start_date} ~ {end_date}...")
    engine = DebugEngine(capital=300_000.0)

    # 静默运行（输出重定向到黑洞）
    buf = io.StringIO()
    with redirect_stdout(buf):
        engine.run(start_date=start_date, end_date=end_date)

    # ── 提取问题交易 ──────────────────────────────────────
    problem_sells = []
    for t in engine._all_trades:
        if t['side'] != 'sell':
            continue
        key = (t['date'], t['code'])
        if key in DEBUG_TARGETS:
            problem_sells.append(t)

    # ── 输出报告 ──────────────────────────────────────────
    lines = []
    lines.append("=" * 80)
    lines.append("Windows 端 问题5笔 触发K详情 + 当日全量5min数据")
    lines.append(f"回测区间: {start_date} ~ {end_date}")
    lines.append("=" * 80)

    found_keys = set()
    for t in problem_sells:
        key = (t['date'], t['code'])
        found_keys.add(key)
        lines.append(f"\n{'─'*60}")
        lines.append(f"日期={t['date']}  代码={t['code']}  sell_type={t['reason']}")
        lines.append(f"卖出价={t['price']}  数量={t['qty']}  PnL={t.get('pnl', '?'):.2f}")
        lines.append(f"trigger_hm={t.get('trigger_hm','?')}  "
                     f"bar: O={t.get('trigger_open','?')} "
                     f"H={t.get('trigger_high','?')} "
                     f"L={t.get('trigger_low','?')} "
                     f"C={t.get('trigger_close','?')}")
        lines.append(f"\n  当日全量5min K ({t['code']} @ {t['date']}):")
        lines.append(dump_5min_bars(engine, t['code'], t['date']))

    # 报告未命中的目标（可能未被买入）
    missing = DEBUG_TARGETS - found_keys
    if missing:
        lines.append(f"\n{'─'*60}")
        lines.append(f"以下目标未出现在卖出记录中（可能未被买入或日期有偏差）:")
        for key in sorted(missing):
            lines.append(f"  {key[0]}  {key[1]}")

    # ── 全量回测结果摘要 ──────────────────────────────────
    lines.append(f"\n{'='*80}")
    lines.append("全量回测摘要")
    lines.append("=" * 80)
    sells = [t for t in engine._all_trades if t['side'] == 'sell']
    total_pnl = sum(t.get('pnl', 0) for t in sells)
    hs_cnt    = sum(1 for t in sells if 'hard_stop'    in t.get('reason',''))
    trail_cnt = sum(1 for t in sells if 'trailing_stop' in t.get('reason',''))
    lines.append(f"总卖出笔数={len(sells)}  hard_stop={hs_cnt}  trailing_stop={trail_cnt}")
    lines.append(f"PnL合计={total_pnl:+.2f}")
    if engine._equity_curve:
        last = engine._equity_curve[-1]
        final_eq = last['total_value']
        ret = last['return_pct']
        lines.append(f"最终净值={final_eq:.2f}  收益率={ret:+.2f}%")

    report = "\n".join(lines)
    print(report)
    with open(OUT_FILE, 'w', encoding='utf-8') as f:
        f.write(report)
    print(f"\n[debug] 报告已写入: {OUT_FILE}")


if __name__ == '__main__':
    main()
