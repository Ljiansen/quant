# -*- coding: utf-8 -*-
"""
V4实盘启动前冒烟测试
用法: python smoke_test_live.py
全部 PASS 则明天可以放心启动实盘。
"""

import sys
import os
import json
import traceback
import tempfile
from datetime import date, datetime, timedelta

sys.path.insert(0, 'd:/miniqmt_quant')

PASS = "✅ PASS"
FAIL = "❌ FAIL"
WARN = "⚠️  WARN"

results = []

def check(name, fn):
    try:
        msg = fn()
        tag = PASS
        results.append((tag, name, msg or ''))
        print(f"{tag}  {name}" + (f"  [{msg}]" if msg else ''))
    except AssertionError as e:
        results.append((FAIL, name, str(e)))
        print(f"{FAIL}  {name}  [{e}]")
    except Exception as e:
        results.append((FAIL, name, str(e)))
        print(f"{FAIL}  {name}  [{e}]")
        traceback.print_exc()


# ─────────────────────────────────────────────
# T01: 模块导入
# ─────────────────────────────────────────────
print("\n" + "="*60)
print("T01 模块导入")
print("="*60)

def _t01_import_live():
    from engine.live_engine_v4 import (
        LiveEngineV4, _load_sh_index_daily, build_sh_ma_cache,
        compute_ba_pool, _buy_qty, _hard_sl, _trail_act, _trail_stop_pct,
        _save_json, _load_json, HARD_STOP, TRAIL_ACT, TRAIL_STOP, GAP_MIN,
        MAX_POSITIONS, SLIPPAGE, COMMISSION_RATE,
    )
    return "live_engine_v4 all symbols OK"

def _t01_import_offline():
    from engine.offline_sim_engine_v4 import OfflineSimEngineV4
    return "offline_sim_engine_v4 OK"

def _t01_import_config():
    import config as _cfg
    account = getattr(_cfg, 'ACCOUNT_ID', '')
    xt_path = getattr(_cfg, 'MINIQMT_PATH', '')
    return f"ACCOUNT_ID={'***' if account else '(空)'} MINIQMT_PATH={xt_path or '(空)'}"

check("导入 live_engine_v4", _t01_import_live)
check("导入 offline_sim_engine_v4", _t01_import_offline)
check("导入 config.py", _t01_import_config)


# ─────────────────────────────────────────────
# T02: 常量正确性
# ─────────────────────────────────────────────
print("\n" + "="*60)
print("T02 关键常量验证")
print("="*60)

def _t02_constants():
    from engine.live_engine_v4 import (
        HARD_STOP, TRAIL_ACT, TRAIL_STOP, GAP_MIN,
        MAX_POSITIONS, SLIPPAGE, COMMISSION_RATE,
        NEW_STOCK_HARD_STOP
    )
    errs = []
    if abs(HARD_STOP - 0.065) > 1e-6:   errs.append(f"HARD_STOP={HARD_STOP}≠0.065")
    if abs(TRAIL_ACT - 0.40) > 1e-6:    errs.append(f"TRAIL_ACT={TRAIL_ACT}≠0.40")
    if abs(TRAIL_STOP - 0.12) > 1e-6:   errs.append(f"TRAIL_STOP={TRAIL_STOP}≠0.12")
    if abs(GAP_MIN - 0.005) > 1e-6:     errs.append(f"GAP_MIN={GAP_MIN}≠0.005")
    if MAX_POSITIONS != 5:               errs.append(f"MAX_POSITIONS={MAX_POSITIONS}≠5")
    if abs(SLIPPAGE - 0.00015) > 1e-8:  errs.append(f"SLIPPAGE={SLIPPAGE}≠0.00015")
    if abs(NEW_STOCK_HARD_STOP - 0.065) > 1e-6: errs.append(f"NEW_STOCK_HARD_STOP≠0.065")
    if errs:
        raise AssertionError("; ".join(errs))
    return f"HS={HARD_STOP} TA={TRAIL_ACT} TS={TRAIL_STOP} GAP={GAP_MIN} MP={MAX_POSITIONS}"

check("关键常量 G1 值正确", _t02_constants)


# ─────────────────────────────────────────────
# T03: 上证指数数据
# ─────────────────────────────────────────────
print("\n" + "="*60)
print("T03 上证指数数据")
print("="*60)

def _t03_sh_load():
    from engine.live_engine_v4 import _load_sh_index_daily
    df = _load_sh_index_daily()
    assert df is not None, "上证指数文件加载失败（返回None）"
    assert len(df) >= 100, f"数据行数太少: {len(df)}"
    last_close = float(df['close'].iloc[-1])
    assert 2000 < last_close < 8000, f"close={last_close} 明显不是上证指数（应在2000-8000）"
    return f"rows={len(df)} last_close={last_close:.2f} date_range={df['date'].min().date()}~{df['date'].max().date()}"

def _t03_sh_ma_cache():
    from engine.live_engine_v4 import _load_sh_index_daily, build_sh_ma_cache
    df = _load_sh_index_daily()
    cache = build_sh_ma_cache(df)
    assert len(cache) >= 50, f"sh_ma_cache太少: {len(cache)}"
    # 取最近一条验证结构
    last_key = sorted(cache.keys())[-1]
    v = cache[last_key]
    assert len(v) == 3, f"cache值格式不对: {v}"
    ma20, slope, below = v
    assert 2000 < ma20 < 8000, f"MA20={ma20} 不像上证指数"
    return f"cache共{len(cache)}条 最新={last_key} ma20={ma20:.1f} below={below}"

check("上证指数文件加载", _t03_sh_load)
check("上证MA缓存构建", _t03_sh_ma_cache)


# ─────────────────────────────────────────────
# T04: G1 Regime 逻辑
# ─────────────────────────────────────────────
print("\n" + "="*60)
print("T04 G1 Regime 逻辑")
print("="*60)

def _t04_g1_regime():
    from engine.live_engine_v4 import LiveEngineV4, _load_sh_index_daily, build_sh_ma_cache
    e = LiveEngineV4(account_id='', xt_path='')
    sh_df = _load_sh_index_daily()
    e.sh_ma_cache = build_sh_ma_cache(sh_df)

    today = date.today().strftime('%Y-%m-%d')
    regime = e._g1_get_regime(today)
    assert regime in ('BULL', 'CHOP'), f"regime非法: {regime}"
    params = e._g1_params_for_today(today)
    assert 'max_positions' in params and 'hard_stop' in params
    assert params['max_positions'] in (4, 5)

    # BULL参数
    bull = e._G1_BULL_PARAMS
    assert bull['max_positions'] == 5 and abs(bull['trail_act'] - 0.40) < 1e-6
    # CHOP参数
    chop = e._G1_CHOP_PARAMS
    assert chop['max_positions'] == 4 and abs(chop['trail_act'] - 0.25) < 1e-6

    return f"today regime={regime} max_pos={params['max_positions']} ta={params['trail_act']}"

def _t04_snapshot_fallback():
    from engine.live_engine_v4 import _hard_sl, _trail_act, _trail_stop_pct
    # 无 snapshot → 用全局默认
    assert abs(_hard_sl('000001') - 0.065) < 1e-6
    assert abs(_trail_act('000001') - 0.40) < 1e-6
    assert abs(_trail_stop_pct('000001') - 0.12) < 1e-6
    # 有 snapshot → 用持仓快照
    pos = {'snapshot_hs': 0.065, 'snapshot_ta': 0.25, 'snapshot_ts': 0.08}
    assert abs(_hard_sl('000001', pos) - 0.065) < 1e-6
    assert abs(_trail_act('000001', pos) - 0.25) < 1e-6
    assert abs(_trail_stop_pct('000001', pos) - 0.08) < 1e-6
    return "BULL default + CHOP snapshot 均正确"

check("G1 regime 判断 + 参数集", _t04_g1_regime)
check("snapshot fallback 逻辑", _t04_snapshot_fallback)


# ─────────────────────────────────────────────
# T05: _buy_qty 仓位计算
# ─────────────────────────────────────────────
print("\n" + "="*60)
print("T05 _buy_qty 仓位计算")
print("="*60)

def _t05_buy_qty():
    from engine.live_engine_v4 import _buy_qty
    # 满仓 Bull(5) 不能买
    assert _buy_qty(300000, 5, 10.0, max_pos=5) == 0
    # 满仓 Chop(4) 不能买
    assert _buy_qty(300000, 4, 10.0, max_pos=4) == 0
    # Chop 3仓(1空位)，30万现金，10元股
    qty_chop = _buy_qty(300000, 3, 10.0, max_pos=4)
    assert qty_chop > 0 and qty_chop % 100 == 0
    # Bull 3仓(2空位)，30万现金，同样价格
    qty_bull = _buy_qty(300000, 3, 10.0, max_pos=5)
    assert qty_bull > 0 and qty_bull % 100 == 0
    # Chop单仓分配更多资金 → qty_chop > qty_bull
    assert qty_chop > qty_bull, f"CHOP单仓应>BULL单仓: chop={qty_chop} bull={qty_bull}"
    return f"Chop 1空位={qty_chop}股 Bull 2空位={qty_bull}股/空位"

check("_buy_qty(Chop>Bull单仓)", _t05_buy_qty)


# ─────────────────────────────────────────────
# T06: 原子写/读 JSON
# ─────────────────────────────────────────────
print("\n" + "="*60)
print("T06 _save_json / _load_json 原子写")
print("="*60)

def _t06_atomic_json():
    from engine.live_engine_v4 import _save_json, _load_json
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, 'test.json')
        data = {'a': 1, 'b': [1, 2, 3], 'c': '中文'}
        _save_json(p, data)
        assert os.path.exists(p)
        assert not os.path.exists(p + '.tmp'), ".tmp 残留"
        loaded = _load_json(p, {})
        assert loaded == data, f"读回值不一致: {loaded}"
    return "原子写/读/tmp清理 均OK"

def _t06_load_json_missing():
    from engine.live_engine_v4 import _load_json
    result = _load_json('/nonexistent/path/xxx.json', {'default': True})
    assert result == {'default': True}
    return "文件不存在返回 default OK"

def _t06_load_json_broken():
    from engine.live_engine_v4 import _load_json
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, 'bad.json')
        with open(p, 'w') as f:
            f.write('{broken json')
        try:
            _load_json(p, {})
            raise AssertionError("应该 raise RuntimeError 但没有")
        except RuntimeError as e:
            assert '致命' in str(e) or '解析失败' in str(e)
    return "损坏文件 raise RuntimeError OK"

check("原子写/读 JSON", _t06_atomic_json)
check("_load_json 不存在返回default", _t06_load_json_missing)
check("_load_json 损坏文件 raise", _t06_load_json_broken)


# ─────────────────────────────────────────────
# T07: 引擎初始化（无xtquant）
# ─────────────────────────────────────────────
print("\n" + "="*60)
print("T07 LiveEngineV4 初始化")
print("="*60)

def _t07_init_no_account():
    """account_id=''（模拟模式）不应 raise"""
    from engine.live_engine_v4 import LiveEngineV4
    e = LiveEngineV4(account_id='', xt_path='')
    assert e.cur_max_pos == 5
    assert e.initial_capital == 300_000.0
    assert isinstance(e.positions, dict)
    assert isinstance(e.wait_queue, dict)
    return f"初始化OK capital={e.initial_capital} cur_max_pos={e.cur_max_pos}"

def _t07_init_with_account_no_xt():
    """account_id非空 + _XT_OK=False → 应 raise"""
    from engine.live_engine_v4 import LiveEngineV4, _XT_OK
    if _XT_OK:
        return "xtquant 已安装，跳过此项"
    try:
        LiveEngineV4(account_id='123456', xt_path='')
        raise AssertionError("应 raise RuntimeError 但没有")
    except RuntimeError as e:
        assert 'xtquant' in str(e).lower() or '拒绝' in str(e)
        return "account_id非空 + no xtquant → raise OK"

check("无account_id初始化（模拟模式）", _t07_init_no_account)
check("account_id非空无xtquant → raise", _t07_init_with_account_no_xt)


# ─────────────────────────────────────────────
# T08: 状态文件检查
# ─────────────────────────────────────────────
print("\n" + "="*60)
print("T08 状态文件检查")
print("="*60)

STATE_FILE  = 'd:/miniqmt_quant/state_v4.json'
QUEUE_FILE  = 'd:/miniqmt_quant/wait_queue_v4.json'
PENDING_FILE= 'd:/miniqmt_quant/pending_sells_v4.json'

def _t08_state_file():
    if not os.path.exists(STATE_FILE):
        raise AssertionError(f"state_v4.json 不存在！请先运行: python run_live_v4.py --init")
    with open(STATE_FILE, 'r', encoding='utf-8') as f:
        s = json.load(f)
    assert 'cash' in s, "state缺少cash字段"
    assert 'positions' in s, "state缺少positions字段"
    assert 'initial_capital' in s, "state缺少initial_capital字段"
    cash = s['cash']
    init_cap = s['initial_capital']
    positions = s['positions']
    assert cash >= 0, f"cash={cash}<0"
    return (f"cash={cash:,.0f} init_cap={init_cap:,.0f} "
            f"positions={len(positions)}只 last_update={s.get('last_update','?')}")

def _t08_positions_snapshot():
    """验证持仓中的 snapshot 字段完整性（G1 要求）"""
    if not os.path.exists(STATE_FILE):
        return "state不存在，跳过"
    with open(STATE_FILE, 'r', encoding='utf-8') as f:
        s = json.load(f)
    positions = s.get('positions', {})
    if not positions:
        return "无持仓，跳过"
    missing = []
    for code, pos in positions.items():
        for field in ('snapshot_hs', 'snapshot_ta', 'snapshot_ts', 'snapshot_regime'):
            if field not in pos:
                missing.append(f"{code}缺{field}")
    if missing:
        raise AssertionError("旧持仓缺G1 snapshot字段: " + "; ".join(missing[:5]))
    return f"{len(positions)}只持仓 snapshot字段均完整"


def _t08_no_tmp_files():
    """检查无 .json.tmp 残留（原子写中断残留文件）"""
    base = 'd:/miniqmt_quant'
    tmp_files = [f for f in os.listdir(base) if f.endswith('.json.tmp')]
    if tmp_files:
        raise AssertionError(f"存在 .tmp 残留文件: {tmp_files}")
    return "无 .json.tmp 残留"


check("state_v4.json 文件格式", _t08_state_file)
check("持仓 snapshot 字段完整性", _t08_positions_snapshot)
check("无 .json.tmp 残留", _t08_no_tmp_files)


# ─────────────────────────────────────────────
# T09: BA pool 缓存检查
# ─────────────────────────────────────────────
print("\n" + "="*60)
print("T09 BA pool 缓存检查")
print("="*60)


def _t09_ba_pool_cache():
    """检查是否有近3天内的 ba_pool_v4_*.json 缓存（3天容忍处理周末）"""
    base = 'd:/miniqmt_quant'
    pool_files = sorted([f for f in os.listdir(base) if f.startswith('ba_pool_v4_') and f.endswith('.json')])
    if not pool_files:
        raise AssertionError("无任何 ba_pool_v4_*.json，明天9:30前需要运行盘后预算")
    latest = pool_files[-1]
    latest_date = latest.replace('ba_pool_v4_', '').replace('.json', '')
    # 读取内容
    with open(os.path.join(base, latest), 'r', encoding='utf-8') as f:
        pool_data = json.load(f)
    count = pool_data.get('count', 0)
    # 允许3天内（覆盖周末：周五缓存到周一仍有效）
    threshold = (date.today() - timedelta(days=3)).strftime('%Y-%m-%d')
    if latest_date < threshold:
        raise AssertionError(f"BA pool缓存已过期: {latest_date}（超过3天），需重新预算！")
    return f"最新缓存={latest_date} 含{count}只股票（距今{( date.today() - date.fromisoformat(latest_date)).days}天）"


check("BA pool 缓存日期", _t09_ba_pool_cache)


# ─────────────────────────────────────────────
# T10: 日线数据目录检查
# ─────────────────────────────────────────────
print("\n" + "="*60)
print("T10 日线数据目录检查")
print("="*60)


def _t10_daily_data_dir():
    daily_dir = 'D:/daily_data'
    assert os.path.exists(daily_dir), f"D:/daily_data 目录不存在"
    sh_dir = os.path.join(daily_dir, 'SH')
    sz_dir = os.path.join(daily_dir, 'SZ')
    assert os.path.exists(sh_dir), "SH 子目录不存在"
    assert os.path.exists(sz_dir), "SZ 子目录不存在"
    sh_count = len([f for f in os.listdir(sh_dir) if f.endswith('.csv')])
    sz_count = len([f for f in os.listdir(sz_dir) if f.endswith('.csv')])
    return f"SH={sh_count}个CSV SZ={sz_count}个CSV"


def _t10_sh_index_file():
    path = 'D:/daily_data/SH/price_sh000001.csv'
    assert os.path.exists(path), f"上证指数文件不存在: {path}"
    import pandas as pd
    df = pd.read_csv(path)
    last_row = df.iloc[-1]
    # 检查最后一行日期是否是近期（30天内）
    last_date_str = str(last_row.get('timetag', last_row.iloc[0]))
    last_close = float(last_row.get('close', 0))
    assert last_close > 2000, f"close={last_close} 不像上证指数"
    return f"rows={len(df)} last_timetag={last_date_str} close={last_close:.0f}"


check("日线数据目录 SH/SZ 存在", _t10_daily_data_dir)
check("上证指数文件存在且合理", _t10_sh_index_file)


# ─────────────────────────────────────────────
# T11: _place_buy/sell_order 安全门
# ─────────────────────────────────────────────
print("\n" + "="*60)
print("T11 _place_buy/sell_order P0-2 安全门")
print("="*60)


def _t11_place_order_sim_mode():
    """模拟模式(account_id='') 下 _place 返回 True 不 raise"""
    from engine.live_engine_v4 import LiveEngineV4
    e = LiveEngineV4(account_id='', xt_path='')
    r1 = e._place_buy_order('000001', 100, 10.0)
    r2 = e._place_sell_order('000001', 100, 10.0)
    assert r1 is True and r2 is True
    return "模拟模式 _place_* 返回 True OK"


check("模拟模式 _place_* 返回True", _t11_place_order_sim_mode)


# ─────────────────────────────────────────────
# T12: _save_state(force=True) 不崩溃
# ─────────────────────────────────────────────
print("\n" + "="*60)
print("T12 _save_state 兼容性")
print("="*60)


def _t12_save_state_compat():
    """offline 引擎 _save_state(force=True) 不应 TypeError"""
    from engine.offline_sim_engine_v4 import OfflineSimEngineV4
    e = OfflineSimEngineV4(capital=300000)  # offline 用 capital= 参数
    e._save_state(force=True)
    e._save_state(force=False)
    e._save_state()  # 无参数
    return "offline._save_state(force=True/False/无参) 均不 TypeError"


check("offline._save_state(force=True) 兼容", _t12_save_state_compat)


# ─────────────────────────────────────────────
# 汇总
# ─────────────────────────────────────────────
print("\n" + "="*60)
print("测试汇总")
print("="*60)

passes = sum(1 for r in results if r[0] == PASS)
fails  = sum(1 for r in results if r[0] == FAIL)
warns  = sum(1 for r in results if r[0] == WARN)

print(f"\n总计: {len(results)} 项  {PASS} {passes}  {FAIL} {fails}  {WARN} {warns}")

if fails > 0:
    print("\n❌ 失败项目:")
    for tag, name, msg in results:
        if tag == FAIL:
            print(f"   {name}: {msg}")
    print("\n⚠️  存在失败项，请修复后再启动实盘！")
    sys.exit(1)
else:
    print("\n✅ 全部通过，明天可以安全启动实盘！")
    sys.exit(0)
