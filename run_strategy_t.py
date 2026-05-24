"""
策略T 独立启动脚本

用法:
  python run_strategy_t.py --live                             # 实盘：今日收盘后执行一次
  python run_strategy_t.py --backtest 2024-01-01 2026-04-30  # 历史回测
  python run_strategy_t.py --status                          # 查看当前持仓与状态
  python run_strategy_t.py --signal                          # 仅检查今日信号（不交易）

资金：9 万（独立于 BA 的 30 万，不共享状态）
会话：session_id=888888（BA 用 654321，互不干扰）
"""

import sys
import os
import argparse
from datetime import date

# 将 engine 目录加入 path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'engine'))
from strategy_t_engine import StrategyTEngine

# ═══════════════════════════════════════════════
# 配置（与 BA 账号相同，策略T 资金由引擎自行管理）
# ═══════════════════════════════════════════════
T_CAPITAL      = 90_000.0
ACCOUNT_ID     = '1520023160'
USERDATA_PATH  = r'D:\迅投QMT交易终端浙商证券金桥版\userdata_mini'


# ──────────────────────────────────────────────
# 实盘模式（每日收盘后 15:30+ 手动或定时执行一次）
# ──────────────────────────────────────────────

def run_live():
    engine = StrategyTEngine(capital=T_CAPITAL)
    engine.load_state()
    engine.load_daily_data()

    today = date.today().strftime('%Y-%m-%d')
    if today not in engine._all_trading_dates:
        print(f"[策略T] ⚠️  {today} 不在交易日历中，可能非交易日或数据未更新")
        return

    # 尝试连接 xtquant（失败则以模拟模式运行，仍更新状态文件）
    xt_ok = engine.connect_xt(ACCOUNT_ID, USERDATA_PATH)
    if not xt_ok:
        print("[策略T] xtquant 不可用，以模拟模式运行（状态文件正常更新）")

    try:
        engine.on_new_day(today)
        engine.print_status()
    finally:
        engine.disconnect_xt()


# ──────────────────────────────────────────────
# 历史回测
# ──────────────────────────────────────────────

def run_backtest(start_date: str, end_date: str):
    import pandas as pd

    engine = StrategyTEngine(capital=T_CAPITAL)
    engine.load_daily_data(ref_date=end_date)

    dates = [d for d in engine._all_trading_dates
             if start_date <= d <= end_date]
    print(f"[策略T] 回测区间: {start_date} ~ {end_date}  ({len(dates)} 个交易日)")
    if not dates:
        print("[策略T] ⚠️  区间内无交易日，请检查日线数据是否已更新")
        return

    # 逐日推进
    for d in dates:
        engine.on_new_day(d)

    # 计算最终净值（以 end_date 收盘价估算）
    last_dt = pd.to_datetime(end_date)
    total   = engine.cash
    for code, pos in engine.positions.items():
        df = engine.daily_data.get(code)
        if df is not None:
            rows  = df[df['date'] <= last_dt]
            price = float(rows['close'].iloc[-1]) if not rows.empty else pos['buy_price']
        else:
            price = pos['buy_price']
        total += pos['quantity'] * price

    nav = total / T_CAPITAL - 1
    print(f"\n{'=' * 48}")
    print(f"  策略T 回测结果  {start_date} ~ {end_date}")
    print(f"{'=' * 48}")
    print(f"  初始资金: {T_CAPITAL:>12.0f}")
    print(f"  最终净值: {total:>12.0f}  ({nav:+.2%})")
    print(f"  剩余现金: {engine.cash:>12.0f}")
    print(f"  未平仓位: {len(engine.positions)} 只")
    print(f"{'=' * 48}\n")


# ──────────────────────────────────────────────
# 状态查看
# ──────────────────────────────────────────────

def show_status():
    engine = StrategyTEngine(capital=T_CAPITAL)
    engine.load_state()
    engine.load_daily_data()
    engine.print_status()


# ──────────────────────────────────────────────
# 仅检查信号（不更新状态文件）
# ──────────────────────────────────────────────

def check_signal():
    engine = StrategyTEngine(capital=T_CAPITAL)
    engine.load_daily_data()

    today = date.today().strftime('%Y-%m-%d')
    if today not in engine._all_trading_dates:
        # 尝试最近一个交易日
        if engine._all_trading_dates:
            today = engine._all_trading_dates[-1]
            print(f"[策略T] 使用最近交易日: {today}")
        else:
            print("[策略T] ⚠️  无交易日历，请先更新日线数据")
            return

    idx = engine._all_trading_dates.index(today)
    if idx < 7:
        print(f"[策略T] 数据不足 7 天，无法计算信号")
        return

    from strategy_t_engine import T_SH_LOOKBACK
    import pandas as pd

    # 手动检查上证 7 日收益率（不修改 last_signal_day_idx）
    if engine.sh_df is None:
        print("[策略T] ⚠️  上证指数数据缺失")
        return

    today_dt   = pd.to_datetime(today)
    sh_today   = engine.sh_df[engine.sh_df['date'] == today_dt]
    prev7_str  = engine._all_trading_dates[idx - T_SH_LOOKBACK]
    sh_prev7   = engine.sh_df[engine.sh_df['date'] == pd.to_datetime(prev7_str)]

    if sh_today.empty or sh_prev7.empty:
        print("[策略T] ⚠️  上证数据缺行，无法计算")
        return

    sh_close      = float(sh_today['close'].iloc[0])
    sh_close_7ago = float(sh_prev7['close'].iloc[0])
    ret = (sh_close - sh_close_7ago) / sh_close_7ago

    from strategy_t_engine import T_SH_THRESHOLD, T_SIGNAL_COOLDOWN
    print(f"\n[策略T] 今日 {today}  上证 7 日收益: {ret:+.2%}  (阈值 {T_SH_THRESHOLD:.0%})")
    triggered = ret < T_SH_THRESHOLD
    print(f"[策略T] 信号状态: {'🚨 触发' if triggered else '⬜ 未触发'}")

    engine.load_state()
    elapsed   = idx - engine.last_signal_day_idx
    in_cool   = 0 <= elapsed <= T_SIGNAL_COOLDOWN
    remaining = T_SIGNAL_COOLDOWN - elapsed if in_cool else 0
    print(f"[策略T] 冷却状态: {'冷却中，剩 ' + str(remaining) + ' 天' if in_cool else '已过冷却期'}")

    if triggered and not in_cool:
        print(f"[策略T] ✅ 若明日开盘，将触发选股买入")
    elif triggered and in_cool:
        print(f"[策略T] ⏳ 信号触发但处于冷却期，不会买入")


# ──────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == '--live':
        run_live()

    elif cmd == '--backtest':
        if len(sys.argv) < 4:
            print("用法: python run_strategy_t.py --backtest YYYY-MM-DD YYYY-MM-DD")
            sys.exit(1)
        run_backtest(sys.argv[2], sys.argv[3])

    elif cmd == '--status':
        show_status()

    elif cmd == '--signal':
        check_signal()

    else:
        print(f"未知命令: {cmd}")
        print(__doc__)
        sys.exit(1)
