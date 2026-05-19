"""V4策略真实实盘 (OPT-bull: BA选股 + 5min入场 + trail/hs出场 + DYN仓位)

策略规格：quant.txt (2026-05-15终版)
回测业绩：+194.92% / MDD 11.37% / Sharpe 2.92 (2025-01-01 ~ 2026-04-30)

用法:
    python run_live_v4.py            # 运行完整实盘（主循环）
    python run_live_v4.py --status   # 查看当前持仓/资金状态（不连接miniQMT）
    python run_live_v4.py --init     # 初始化/重置V4状态文件（首次部署时）

警告：
    ⚠️  V4与V3互相独立，使用独立状态文件(state_v4.json/wait_queue_v4.json等)
    ⚠️  首次使用请先运行 --init 初始化状态，然后在config.py中确认资金设置
"""

import argparse
import json as _json
import os
import sys
import time
from datetime import datetime, date

sys.path.insert(0, 'd:/miniqmt_quant')

# ───────────────────────────────────────────────────────────
# 路径常量（与 live_engine_v4.py 保持一致）
# ───────────────────────────────────────────────────────────
BASE_DIR       = 'd:/miniqmt_quant'
STATE_FILE_V4  = os.path.join(BASE_DIR, 'state_v4.json')
TRADES_FILE_V4 = os.path.join(BASE_DIR, 'trades_v4.json')
QUEUE_FILE_V4  = os.path.join(BASE_DIR, 'wait_queue_v4.json')
DEFERRED_FILE_V4 = os.path.join(BASE_DIR, 'deferred_sells_v4.json')
PENDING_FILE_V4  = os.path.join(BASE_DIR, 'pending_sells_v4.json')


# ───────────────────────────────────────────────────────────
# 辅助：收盘后日线数据更新
# ───────────────────────────────────────────────────────────

def _wait_until_19():
    """阻塞等待直到当日19:00（日线数据约18:00后才完整发布）"""
    while True:
        now = datetime.now()
        if now.hour >= 19:
            return
        remaining_mins = 19 * 60 - (now.hour * 60 + now.minute)
        print(f"[收盘后] 当前 {now.strftime('%H:%M')}，等待至 19:00 再更新数据"
              f"（还剩约 {remaining_mins} 分钟）...")
        time.sleep(min(remaining_mins * 60, 5 * 60))


def _daily_data_updated_today() -> bool:
    """幂等检查：今日日线数据是否已更新（读哨兵文件）"""
    today = date.today().strftime('%Y%m%d')
    sentinel = os.path.join(BASE_DIR, f'.v4_daily_update_done_{today}')
    return os.path.exists(sentinel)


def _mark_daily_data_updated():
    today = date.today().strftime('%Y%m%d')
    sentinel = os.path.join(BASE_DIR, f'.v4_daily_update_done_{today}')
    try:
        with open(sentinel, 'w', encoding='utf-8') as f:
            f.write(datetime.now().isoformat())
    except Exception as e:
        print(f"[收盘后] 写哨兵文件失败（不影响结果）: {e}")


def _ba_pool_precomputed_today() -> bool:
    """幂等检查：今日BA pool是否已预算（读哨兵文件）"""
    today = date.today().strftime('%Y%m%d')
    sentinel = os.path.join(BASE_DIR, f'.v4_ba_pool_done_{today}')
    return os.path.exists(sentinel)


def _mark_ba_pool_precomputed():
    today = date.today().strftime('%Y%m%d')
    sentinel = os.path.join(BASE_DIR, f'.v4_ba_pool_done_{today}')
    try:
        with open(sentinel, 'w', encoding='utf-8') as f:
            f.write(datetime.now().isoformat())
    except Exception as e:
        print(f"[收盘后] 写BA pool哨兵文件失败（不影响结果）: {e}")


# ───────────────────────────────────────────────────────────
# --init：初始化 V4 状态文件
# ───────────────────────────────────────────────────────────

def init_v4_state(initial_capital: float = 300_000.0, force: bool = False):
    """
    首次部署时初始化 V4 状态文件。
    force=True 时强制重置（会清空持仓，慎用）。
    """
    if os.path.exists(STATE_FILE_V4) and not force:
        print(f"[V4-init] state_v4.json 已存在，跳过（使用 --force 强制重置）")
        _print_v4_status()
        return

    # state_v4.json
    state = {
        'initial_capital': initial_capital,
        'cash': initial_capital,
        'total_value': initial_capital,
        'positions': {},
        'pending_sells': [],
        '_last_increment_date': '',
        '_today_str': '',
        'last_update': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'engine': 'V4',
    }
    _write_json(STATE_FILE_V4, state)
    _write_json(TRADES_FILE_V4, [])
    _write_json(QUEUE_FILE_V4, {})
    _write_json(DEFERRED_FILE_V4, {})
    _write_json(PENDING_FILE_V4, [])

    print(f"[V4-init] V4状态文件初始化完成")
    print(f"  state_v4.json    ← 初始资金 {initial_capital:,.0f} 元，持仓=空")
    print(f"  trades_v4.json   ← 空")
    print(f"  wait_queue_v4.json ← 空")
    print(f"  deferred_sells_v4.json ← 空")
    print(f"  pending_sells_v4.json  ← 空")


def _write_json(path: str, obj):
    with open(path, 'w', encoding='utf-8') as f:
        _json.dump(obj, f, ensure_ascii=False, indent=2)
    print(f"  写入: {path}")


# ───────────────────────────────────────────────────────────
# --status：打印当前 V4 持仓状态
# ───────────────────────────────────────────────────────────

def _print_v4_status():
    """打印当前V4实盘状态（不连接miniQMT）"""
    print("\n" + "=" * 60)
    print("V4策略实盘状态 (state_v4.json)")
    print("=" * 60)

    if not os.path.exists(STATE_FILE_V4):
        print("[警告] state_v4.json 不存在，请先运行 --init 初始化")
        return

    try:
        with open(STATE_FILE_V4, 'r', encoding='utf-8') as f:
            state = _json.load(f)
    except Exception as e:
        print(f"[错误] 读取state_v4.json失败: {e}")
        return

    init_cap   = state.get('initial_capital', 300_000)
    cash       = state.get('cash', 0)
    total_val  = state.get('total_value', cash)
    positions  = state.get('positions', {})
    last_upd   = state.get('last_update', 'N/A')

    pnl        = total_val - init_cap
    pnl_pct    = pnl / init_cap * 100 if init_cap > 0 else 0

    print(f"  更新时间:    {last_upd}")
    print(f"  初始资金:    {init_cap:>12,.2f} 元")
    print(f"  当前现金:    {cash:>12,.2f} 元")
    print(f"  总资产:      {total_val:>12,.2f} 元")
    print(f"  累计盈亏:    {pnl:>+12,.2f} 元  ({pnl_pct:+.2f}%)")
    print(f"  当前持仓:    {len(positions)} / 5 只")

    if positions:
        print()
        print(f"  {'代码':<10} {'买入价':>8} {'数量':>8} {'买入日期':<12} {'持有天数':>6}")
        print(f"  {'-'*52}")
        for code, pos in positions.items():
            print(f"  {code:<10} {pos.get('buy_price', 0):>8.3f} "
                  f"{pos.get('quantity', 0):>8} {pos.get('buy_date', 'N/A'):<12} "
                  f"{pos.get('days_held', 0):>6}天")

    # 冷却队列
    if os.path.exists(QUEUE_FILE_V4):
        try:
            with open(QUEUE_FILE_V4, 'r', encoding='utf-8') as f:
                wait_q = _json.load(f)
            if wait_q:
                print(f"\n  冷却队列: {len(wait_q)} 只  {list(wait_q.keys())[:5]}{'...' if len(wait_q)>5 else ''}")
        except Exception:
            pass

    # deferred_sells
    if os.path.exists(DEFERRED_FILE_V4):
        try:
            with open(DEFERRED_FILE_V4, 'r', encoding='utf-8') as f:
                deferred = _json.load(f)
            if deferred:
                print(f"\n  Deferred sells: {deferred}")
        except Exception:
            pass

    # 最近5笔交易
    if os.path.exists(TRADES_FILE_V4):
        try:
            with open(TRADES_FILE_V4, 'r', encoding='utf-8') as f:
                trades = _json.load(f)
            if trades:
                print(f"\n  最近交易 (共{len(trades)}笔，最新5笔):")
                print(f"  {'时间':<20} {'方向':<6} {'代码':<10} {'价格':>8} {'数量':>8} {'原因':<16} {'盈亏':>10}")
                print(f"  {'-'*80}")
                for t in trades[-5:]:
                    pnl_str = f"{t.get('pnl', 0):>+10.2f}" if t.get('type') == 'sell' else ' ' * 11
                    print(f"  {t.get('timestamp', 'N/A'):<20} "
                          f"{t.get('type', 'N/A'):<6} "
                          f"{t.get('code', 'N/A'):<10} "
                          f"{t.get('price', 0):>8.3f} "
                          f"{t.get('quantity', 0):>8} "
                          f"{t.get('reason', 'N/A'):<16} "
                          f"{pnl_str}")
        except Exception:
            pass

    print("=" * 60)


# ───────────────────────────────────────────────────────────
# 主入口
# ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='V4策略真实实盘 (OPT-bull)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python run_live_v4.py           # 正常启动实盘
  python run_live_v4.py --status  # 查看持仓状态
  python run_live_v4.py --init    # 首次初始化（默认30万初始资金）
  python run_live_v4.py --init --capital 500000   # 初始化，50万资金
  python run_live_v4.py --init --force            # 强制重置（清空持仓！）
        """
    )
    parser.add_argument('--status',  action='store_true', help='查看当前持仓状态（不连接miniQMT）')
    parser.add_argument('--init',    action='store_true', help='初始化V4状态文件（首次部署）')
    parser.add_argument('--force',   action='store_true', help='配合--init强制重置（⚠️ 清空持仓）')
    parser.add_argument('--capital', type=float, default=300_000.0,
                        help='初始资金（与--init配合使用，默认300000）')
    parser.add_argument('--precompute', action='store_true',
                        help='手动触发盘后BA pool预算（日线更新后可单独调用）')
    args = parser.parse_args()

    # ── --init ──
    if args.init:
        if args.force:
            ans = input("⚠️  --force 将清空所有V4持仓和交易记录！确认输入 yes: ").strip()
            if ans.lower() != 'yes':
                print("已取消")
                return
        init_v4_state(initial_capital=args.capital, force=args.force)
        return

    # ── --status ──
    if args.status:
        _print_v4_status()
        return

    # ── --precompute（手动触发盘后预算）──
    if args.precompute:
        _run_postmarket(force_ba=True)
        return

    # ── 正常启动实盘 ──
    try:
        import config as _cfg
        account_id = getattr(_cfg, 'ACCOUNT_ID', '')
        xt_path    = getattr(_cfg, 'MINIQMT_PATH', '')
    except Exception as e:
        print(f"[错误] 读取config.py失败: {e}")
        account_id = ''
        xt_path    = ''

    try:
        from engine.live_engine_v4 import LiveEngineV4
    except Exception as e:
        print(f"[错误] 导入V4引擎失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # 检查状态文件
    if not os.path.exists(STATE_FILE_V4):
        print("[警告] state_v4.json 不存在，自动初始化（默认30万资金）")
        init_v4_state(initial_capital=300_000.0, force=False)

    print("=" * 60)
    print("V4策略真实实盘 - OPT-bull")
    print(f"  账号: {account_id}")
    print(f"  路径: {xt_path}")
    print("=" * 60)

    try:
        engine = LiveEngineV4(account_id=account_id, xt_path=xt_path)
        # ↑ capital 不需要传：_load_state 会从 state_v4.json 读取 initial_capital，
        #   如果 state 文件不存在（首次启动）则 _load_state 会用构造参数默认値 300K。
        #   如需自定义资金，请先运行： python run_live_v4.py --init --capital 500000
        engine.run()

        # ── 收盘后流程 ──
        _run_postmarket(engine=engine)

        # ── 打印收盘状态 ──
        print()
        _print_v4_status()

    except KeyboardInterrupt:
        print("\n[信息] 收到中断信号，程序退出")
    except Exception as e:
        print(f"\n[错误] 运行异常: {e}")
        import traceback
        traceback.print_exc()


def _run_postmarket(engine=None, force_ba: bool = False):
    """
    收盘后标准流程：
      1. 等待 19:00
      2. 更新日线数据（update_daily_data.py --force）
      3. 预算明日 BA pool（engine.postmarket_precompute）
    engine: LiveEngineV4 实例（已运行完毕，daily_data 还在内存中）
    force_ba: True 时即使今日已预算也强制重跑
    """
    import subprocess
    from engine.live_engine_v4 import LiveEngineV4

    # ── 等待至 19:00 ──
    print("\n" + "=" * 60)
    print("[收盘后] 主循环结束，等待 19:00 更新日线数据...")
    print("=" * 60)
    _wait_until_19()

    # ── 更新日线数据（幂等）──
    print("\n" + "=" * 60)
    print("[收盘后] 更新日线数据（D:/daily_data）...")
    print("=" * 60)
    if _daily_data_updated_today():
        print("[收盘后] 日线数据今日已更新，跳过（幂等保护）")
    else:
        try:
            update_script = os.path.join(BASE_DIR, 'update_daily_data.py')
            print(f"[收盘后] 调用 {update_script} --force...")
            ret = subprocess.run([sys.executable, update_script, '--force'], check=False)
            if ret.returncode == 0:
                _mark_daily_data_updated()
                print("[收盘后] 日线数据更新完成 ✓")
            else:
                print(f"[收盘后] 日线数据更新异常(returncode={ret.returncode})，继续盘后预算")
        except Exception as e:
            import traceback
            print(f"[收盘后] 日线数据更新失败（不影响今日交易）: {e}")
            traceback.print_exc()

    # ── 预算明日 BA pool（幂等）──
    print("\n" + "=" * 60)
    print("[收盘后] 预算明日 BA pool...")
    print("=" * 60)
    if not force_ba and _ba_pool_precomputed_today():
        print("[收盘后] BA pool 今日已预算，跳过（幂等保护）")
    else:
        try:
            # 复用已有引擎（daily_data 还在内存），或新建一个轻量实例
            if engine is None:
                engine = LiveEngineV4()
            today_str = date.today().strftime('%Y-%m-%d')
            engine.postmarket_precompute(today_str)
            _mark_ba_pool_precomputed()
            print("[收盘后] BA pool 预算完成 ✓")
        except Exception as e:
            import traceback
            print(f"[收盘后] BA pool 预算失败（不影响今日交易）: {e}")
            traceback.print_exc()


if __name__ == '__main__':
    main()
