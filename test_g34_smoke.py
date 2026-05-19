# -*- coding: utf-8 -*-
"""G3.4 冒烟测试"""
import sys
sys.path.insert(0, 'd:/miniqmt_quant')

from engine.live_engine_v4 import (
    _load_sh_index_daily, _load_sh_features_g34,
    build_sh_ma_cache, G34_PARAMS, LiveEngineV4
)

# 1. SH特征加载
sh_df = _load_sh_index_daily()
print(f'SH日线数据: {len(sh_df) if sh_df is not None else None} 行')
cache = _load_sh_features_g34(sh_df)
print(f'G34特征缓存: {len(cache)} 条')

recent = sorted(cache.keys())[-5:]
for d in recent:
    f = cache[d]
    print(f"  {d}: below={f['below']} streak={f['streak']} ret_5d={f['ret_5d']:+.3f} ret_30d={f['ret_30d']:+.3f} vol_30d={f['vol_30d']:.4f}")

# 2. Regime 决策测试
eng = LiveEngineV4.__new__(LiveEngineV4)
eng.sh_g34_cache = cache
eng.sh_ma_cache = build_sh_ma_cache(sh_df)
eng.all_trading_dates = sorted(sh_df['date'].dt.strftime('%Y-%m-%d').tolist()) if sh_df is not None else []

for d in ['2026-05-09', '2026-05-12', '2026-05-15', '2026-05-16']:
    dec = eng._g34_regime_decide(d)
    sp = eng._g34_stock_params(d)
    print(f"regime({d}): {dec['regime']} max_pos={dec['max_positions']} -> hs={sp['hs']:.3f} ta={sp['trail_act']:.2f} ts={sp['trail_stop']:.3f}")

# 3. 参数完整性检查
p = G34_PARAMS
assert p['bull_mp'] == 5
assert p['chop_init_mp'] == 4
assert p['chop_else_mp'] == 3
assert p['bull_hs'] == 0.065
assert p['chop_init_hs'] == 0.068
assert p['chop_else_hs'] == 0.085
assert p['panic_thr'] == -0.06
assert p['vol_thr'] == 0.022
assert p['init_bnd'] == 3
print('参数完整性检查 PASSED')

# 4. 安全网测试（需要把测试日期加入交易日历）
eng.all_trading_dates += ['2099-01-01', '2099-01-02']

feat_panic = {'below': True, 'streak': 5, 'ret_5d': -0.02, 'ret_30d': -0.07, 'vol_30d': 0.01}
eng.sh_g34_cache['2099-01-01'] = feat_panic
dec = eng._g34_regime_decide('2099-01-02')
assert dec['regime'] == 'panic_30d', f"期望 panic_30d 但得到 {dec['regime']}"
print('安全网1(panic_30d) PASSED')

feat_vol = {'below': True, 'streak': 5, 'ret_5d': -0.02, 'ret_30d': -0.01, 'vol_30d': 0.03}
eng.sh_g34_cache['2099-01-01'] = feat_vol
dec = eng._g34_regime_decide('2099-01-02')
assert dec['regime'] == 'vol_30d', f"期望 vol_30d 但得到 {dec['regime']}"
print('安全网2(vol_30d) PASSED')

feat_ret5 = {'below': True, 'streak': 5, 'ret_5d': -0.02, 'ret_30d': -0.01, 'vol_30d': 0.01}
eng.sh_g34_cache['2099-01-01'] = feat_ret5
dec = eng._g34_regime_decide('2099-01-02')
assert dec['regime'] == 'chop_else_ret5', f"期望 chop_else_ret5 但得到 {dec['regime']}"
print('安全网3(chop_else_ret5) PASSED')

# 5. chop_else 正常路径
feat_else = {'below': True, 'streak': 5, 'ret_5d': 0.0, 'ret_30d': -0.01, 'vol_30d': 0.01}
eng.sh_g34_cache['2099-01-01'] = feat_else
dec = eng._g34_regime_decide('2099-01-02')
sp = eng._g34_stock_params('2099-01-02')
assert dec['regime'] == 'chop_else' and dec['max_positions'] == 3, f"期望 chop_else/3 但得到 {dec}"
assert sp['hs'] == 0.085, f"chop_else hs 应为0.085但得到{sp['hs']}"
print('chop_else 正常路径 PASSED')

# 6. chop_init
feat_init = {'below': True, 'streak': 2, 'ret_5d': 0.0, 'ret_30d': -0.01, 'vol_30d': 0.01}
eng.sh_g34_cache['2099-01-01'] = feat_init
dec = eng._g34_regime_decide('2099-01-02')
sp = eng._g34_stock_params('2099-01-02')
assert dec['regime'] == 'chop_init' and dec['max_positions'] == 4
assert sp['hs'] == 0.068 and sp['trail_act'] == 0.24 and sp['trail_stop'] == 0.010
print('chop_init 正常路径 PASSED')

print('\n========== G3.4 冒烟测试全部通过 ==========')
