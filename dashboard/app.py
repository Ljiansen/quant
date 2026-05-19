# -*- coding: utf-8 -*-
"""
V3策略仪表盘 - Flask后端
端口: 8088
"""
import os
import json
import sys
import subprocess
import threading as _threading
from datetime import datetime as _dt_now, timedelta as _timedelta
from flask import Flask, jsonify, request, render_template

try:
    import psutil
    _PSUTIL_OK = True
except Exception:
    psutil = None
    _PSUTIL_OK = False

try:
    from xtquant import xtdata as _xtdata
    _XTDATA_OK = True
except Exception:
    _xtdata = None
    _XTDATA_OK = False

# 项目根目录
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 监控的进程配置
PROCESS_TARGETS = {
    'live': 'run_live_v4.py',
}

# 已启动的进程句柄 {mode: Popen}
_launched_procs = {}

# params_v3.json 路径（动态参数配置文件）
_PARAMS_FILE = os.path.join(BASE_DIR, 'params_v3.json')

# 过热过滤缓存（按日重置，避免每次 API 请求都读磁盘）
_overheat_cache: dict = {}
_overheat_cache_date: str = ''


def _check_overheat_local(bare_code: str) -> tuple:
    """从本地日线 CSV 判断股票是否过热
    返回 (is_overheat: bool, reason: str)
    """
    global _overheat_cache, _overheat_cache_date
    import pandas as _pd_oh

    # 加载参数
    lookback  = 20
    threshold = 0.40
    try:
        sys.path.insert(0, BASE_DIR)
        import config as _cfg_oh
        lookback  = getattr(_cfg_oh, 'V3_OVERHEAT_LOOKBACK',  lookback)
        threshold = getattr(_cfg_oh, 'V3_OVERHEAT_THRESHOLD', threshold)
    except Exception:
        pass

    # 按日重置缓存
    today_str = _dt_now.today().strftime('%Y%m%d')
    if _overheat_cache_date != today_str:
        _overheat_cache.clear()
        _overheat_cache_date = today_str

    if bare_code in _overheat_cache:
        return _overheat_cache[bare_code]

    sub   = 'SH' if bare_code.startswith('6') else 'SZ'
    fpath = os.path.join('D:/daily_data', sub, f'price_{bare_code}.csv')
    if not os.path.exists(fpath):
        _overheat_cache[bare_code] = (False, '')
        return False, ''
    try:
        df = _pd_oh.read_csv(fpath, usecols=['timetag', 'close'])
        df = df.sort_values('timetag').tail(lookback + 1).reset_index(drop=True)
        closes = df['close'].astype(float).values
        if len(closes) < 2:
            _overheat_cache[bare_code] = (False, '')
            return False, ''
        base    = float(closes[0])
        current = float(closes[-1])
        if base <= 0:
            _overheat_cache[bare_code] = (False, '')
            return False, ''
        cumret = (current - base) / base
        if cumret >= threshold:
            reason = f'近{lookback}日累计涨幅{cumret*100:.1f}%，超过{threshold*100:.0f}%冷却阈值'
            _overheat_cache[bare_code] = (True, reason)
            return True, reason
        _overheat_cache[bare_code] = (False, '')
        return False, ''
    except Exception:
        _overheat_cache[bare_code] = (False, '')
        return False, ''

# 模拟结果存档目录
SIM_RESULTS_DIR = os.path.join(BASE_DIR, 'sim_results')

# 参数合法范围约束
_PARAM_RANGES = {
    'main_board': {
        'min_change_pct':    (0.001, 0.20),
        'max_change_pct':    (0.01,  0.50),
        'hard_stop_loss':    (0.01,  0.50),
        'trailing_activate': (0.005, 0.50),
        'trailing_stop':     (0.005, 0.30),
        'limit_up':          (0.05,  0.22),
    },
    'star_board': {
        'min_change_pct':    (0.001, 0.20),
        'max_change_pct':    (0.01,  0.50),
        'hard_stop_loss':    (0.01,  0.50),
        'trailing_activate': (0.005, 0.50),
        'trailing_stop':     (0.005, 0.30),
        'limit_up':          (0.05,  0.22),
    },
    'general': {
        'max_positions': (1, 10),
        'prev_bar_up':   (0, 1),
    },
}


def _validate_params(data):
    """校验参数范围，返回 (cleaned_dict, error_msg_or_None)"""
    result = {}
    for section, fields in _PARAM_RANGES.items():
        if section not in data:
            return None, f'缺少字段: {section}'
        result[section] = {}
        src = data[section]
        for key, (lo, hi) in fields.items():
            if key not in src:
                return None, f'缺少字段: {section}.{key}'
            try:
                val = int(src[key]) if key in ('time_stop_days', 'max_positions', 'prev_bar_up') else float(src[key])
            except (ValueError, TypeError):
                return None, f'参数类型错误: {section}.{key}'
            if not (lo <= val <= hi):
                return None, f'参数超范围: {section}.{key}={val} (应在 {lo}~{hi})'
            result[section][key] = val
    # 保留 general 中除校验字段外的只读字段
    for extra_key in ('top_n', 'buy_time', 'daily_min_amount', 'daily_amount_days', 'initial_capital'):
        if extra_key in data.get('general', {}):
            result['general'][extra_key] = data['general'][extra_key]
    return result, None


# 调仓池重建运行状态
_pool_rebuild_state = {
    'running':      False,
    'strategy':     'ba',
    'msg':          '',
    'last_rebuild': '',
}


def _rebuild_pool_bg(strategy: str):
    """后台线程：调用 init_rebalance_pool.main(strategy) 重新生成调仓池"""
    global _pool_rebuild_state
    _pool_rebuild_state['running'] = True
    _pool_rebuild_state['msg'] = f'正在用 {strategy} 策略重新选股（约需2分钟）...'
    try:
        import sys as _sys
        _sys.path.insert(0, BASE_DIR)
        import importlib
        import init_rebalance_pool as _irp
        importlib.reload(_irp)
        _irp.main(strategy=strategy)
        _pool_rebuild_state['strategy'] = strategy
        _pool_rebuild_state['msg'] = '选股完成 ✓'
        _pool_rebuild_state['last_rebuild'] = _dt_now.now().strftime('%m-%d %H:%M')
    except Exception as _e:
        _pool_rebuild_state['msg'] = f'失败: {_e}'
    finally:
        _pool_rebuild_state['running'] = False


def _find_running_process(script_name):
    """查找正在运行指定脚本的 Python 进程，返回 (is_running, pid)"""
    if not _PSUTIL_OK:
        return False, None
    try:
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                name = (proc.info.get('name') or '').lower()
                if 'python' not in name:
                    continue
                cmdline = proc.info.get('cmdline') or []
                if any(script_name in str(arg) for arg in cmdline):
                    return True, proc.info['pid']
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass
    return False, None

# 确保能import config
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# stock_names.json 本地名称持久缓存
_NAMES_FILE = os.path.join(BASE_DIR, 'stock_names.json')
_akshare_names_loaded = False  # 是否已从 akshare 批量加载过


def _load_name_cache_from_file():
    """启动时从 stock_names.json 加载本地名称缓存"""
    try:
        if os.path.exists(_NAMES_FILE):
            with open(_NAMES_FILE, 'r', encoding='utf-8') as _f:
                _loaded = json.load(_f)
            _stock_name_cache.update(_loaded)
    except Exception:
        pass


def _save_name_cache_to_file():
    """将名称缓存写入 stock_names.json"""
    try:
        with open(_NAMES_FILE, 'w', encoding='utf-8') as _f:
            json.dump(_stock_name_cache, _f, ensure_ascii=False)
    except Exception:
        pass


def _try_load_akshare_names():
    """通过 akshare 批量加载全部股票名称，加载成功后持久化到文件"""
    global _akshare_names_loaded
    if _akshare_names_loaded:
        return
    try:
        import akshare as ak
        df = ak.stock_info_a_code_name()
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                code = str(row.get('code', row.get('\u80a1\u7968\u4ee3\u7801', ''))).strip().zfill(6)
                name = str(row.get('name', row.get('\u80a1\u7968\u7b80\u79f0', ''))).strip()
                if code and name and code != 'nan':
                    _stock_name_cache[code] = name
            _akshare_names_loaded = True
            _save_name_cache_to_file()
    except Exception:
        pass


# 股票名称缓存
_stock_name_cache = {}

# 启动时自动加载本地名称缓存
_load_name_cache_from_file()


# ==================== 新股监控缓存 ====================
_new_stock_cache = []         # [{code, name, open_date}, ...]
_new_stock_cache_date = None  # '2026-04-30' 格式，当天有效
_new_stock_loading = False


def _build_new_stock_cache_bg():
    """后台线程：扫描最近60日上市新股，通过 xtquant 获取上市日期"""
    global _new_stock_cache, _new_stock_cache_date, _new_stock_loading
    try:
        today = _dt_now.now()
        cutoff = today - _timedelta(days=61)
        cutoff_int = int(cutoff.strftime('%Y%m%d'))

        all_stocks = _xtdata.get_stock_list_in_sector('沪深A股')
        if not all_stocks:
            return

        new_stocks = []
        for sym in all_stocks:
            bare = sym.split('.')[0]
            if bare.startswith('8') or bare.startswith('4'):
                continue
            try:
                detail = _xtdata.get_instrument_detail(sym)
                if not detail:
                    continue
                raw_open = detail.get('OpenDate', 0) or 0
                if not raw_open:
                    continue
                open_int = int(raw_open)
                if open_int >= cutoff_int and open_int > 0:
                    name = detail.get('InstrumentName', '') or _get_stock_name(bare)
                    od_str = str(open_int)
                    open_date_disp = (
                        f"{od_str[:4]}-{od_str[4:6]}-{od_str[6:]}"
                        if len(od_str) == 8 else od_str
                    )
                    new_stocks.append({
                        'code': bare,
                        'name': name,
                        'open_date': open_date_disp,
                        '_sort': open_int,
                    })
            except Exception:
                continue

        new_stocks.sort(key=lambda x: x['_sort'], reverse=True)
        for s in new_stocks:
            del s['_sort']

        _new_stock_cache = new_stocks
        _new_stock_cache_date = today.strftime('%Y-%m-%d')
    except Exception:
        pass
    finally:
        _new_stock_loading = False


def _backfill_pnl(trades):
    """对历史卖出记录补算盈亏（处理旧数据未记录 pnl/buy_price 的情况）。
    逻辑：对每条无 pnl 的卖出记录，向前扫描找到同一股票最近的买入记录来定价。
    trades 已按时间倒序排序。
    """
    # 先恢复正序（时间升序）以便配对
    ordered = sorted(trades, key=lambda x: x.get('timestamp', x.get('time', '')))
    # buy_map: code -> 最近一次买入的 buy_price
    buy_map = {}
    for t in ordered:
        ttype = t.get('type', t.get('action', t.get('direction', '')))
        code = t.get('code', t.get('stock_code', ''))
        if ttype == 'buy':
            bp = t.get('price', 0)
            if bp > 0:
                buy_map[code] = bp
        elif ttype == 'sell' and t.get('pnl') is None:
            buy_price = t.get('buy_price') or buy_map.get(code)
            if buy_price and buy_price > 0:
                sell_price = t.get('price', 0)
                qty = t.get('quantity', t.get('volume', 0))
                fee = t.get('fee', t.get('commission', 0))
                cost = buy_price * qty
                pnl = round((sell_price - buy_price) * qty - fee, 2)
                pnl_pct = round(pnl / cost * 100, 3) if cost > 0 else 0
                t['buy_price'] = round(buy_price, 3)
                t['pnl'] = pnl
                t['pnl_pct'] = pnl_pct


def _get_stock_name(code):
    """获取股票名称。优先序列：本地缓存 → xtquant → akshare"""
    bare = str(code).split('.')[0]
    # 1. 本地缓存命中
    if bare in _stock_name_cache and _stock_name_cache[bare]:
        return _stock_name_cache[bare]
    # 2. xtquant 在线查询
    if _XTDATA_OK:
        try:
            if bare.startswith('6'):
                symbol = bare + '.SH'
            else:
                symbol = bare + '.SZ'
            detail = _xtdata.get_instrument_detail(symbol)
            name = ''
            if detail:
                name = detail.get('InstrumentName', '') or ''
            _stock_name_cache[bare] = name
            return name
        except Exception:
            _stock_name_cache[bare] = ''
    # 3. akshare 回退（批量加载一次，后续从缓存读）
    _try_load_akshare_names()
    return _stock_name_cache.get(bare, '')


def create_app():
    app = Flask(__name__, template_folder='templates')

    def _read_json(filepath):
        """安全读取JSON文件，不存在则返回None"""
        try:
            if os.path.exists(filepath):
                with open(filepath, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception:
            pass
        return None

    def _get_latest_ba_pool():
        """读取最新的 ba_pool_v4_{date}.json，返回 dict 或 None"""
        import glob as _glob
        pattern = os.path.join(BASE_DIR, 'ba_pool_v4_*.json')
        files = sorted(_glob.glob(pattern), reverse=True)
        for fpath in files:
            d = _read_json(fpath)
            if d is not None:
                return d
        return None

    def _get_state_file(mode):
        # V4 只有实盘模式
        return os.path.join(BASE_DIR, 'state_v4.json')

    def _get_trades_file(mode):
        return os.path.join(BASE_DIR, 'trades_v4.json')

    @app.route('/')
    def index():
        return render_template('index.html')

    @app.route('/api/status')
    def api_status():
        mode = request.args.get('mode', 'sim')
        state = _read_json(_get_state_file(mode))
        if state is None:
            return jsonify({
                'cash': 0,
                'total_value': 0,
                'initial_capital': 300000,
                'positions': [],
                'pending_sells': [],
                'last_update': '',
                'profit': 0,
                'profit_pct': 0
            })
        # 优先用 state 文件里的 initial_capital，保证实盘/模拟各自独立
        initial_capital = state.get('initial_capital')
        if not initial_capital:
            try:
                import config as cfg
                initial_capital = getattr(cfg, 'V3_INITIAL_CAPITAL', 300000)
            except Exception:
                initial_capital = 300000

        total_value = state.get('total_value', state.get('portfolio_value', 0))
        cash = state.get('cash', 0)
        # profit / profit_pct 将在获取实时持仓价格后重新计算

        positions = state.get('positions', {})
        pending_sells = state.get('pending_sells', [])

        # V4: positions 是 dict {code: {...}}，归一化为 list
        if isinstance(positions, dict):
            positions = [{'code': k, **v} for k, v in positions.items()]

        # 为持仓添加名称，并获取实时价格计算盈亏%
        pos_codes = [p.get('code', p.get('stock_code', '')) for p in positions]
        tick_map = _get_tick_data(pos_codes)
        for p in positions:
            c = p.get('code', p.get('stock_code', ''))
            p['name'] = _get_stock_name(c)
            bare = str(c).split('.')[0]
            tick = tick_map.get(bare)
            if tick:
                try:
                    last_price = tick.get('lastPrice') or tick.get('last_price') or 0
                    if last_price > 0:
                        p['current_price'] = round(last_price, 3)
                    else:
                        p['current_price'] = None
                except Exception:
                    p['current_price'] = None
            else:
                p['current_price'] = None

        # 用实时价格重算持仓总市値，保证页头和持仓表数字一致
        realtime_pos_val = 0
        for p in positions:
            cur = p.get('current_price')
            qty = p.get('quantity', p.get('volume', 0))
            buy = p.get('buy_price', p.get('cost', 0))
            realtime_pos_val += (cur if cur else buy) * qty
        realtime_total = cash + realtime_pos_val
        profit = realtime_total - initial_capital
        profit_pct = (profit / initial_capital * 100) if initial_capital else 0

        # 为待卖出添加名称
        for s in pending_sells:
            c = s.get('code', s.get('stock_code', ''))
            s['name'] = _get_stock_name(c)

        return jsonify({
            'cash': round(cash, 3),
            'total_value': round(realtime_total, 3),
            'initial_capital': initial_capital,
            'positions': positions,
            'pending_sells': pending_sells,
            'last_update': state.get('last_update', state.get('update_time', '')),
            'profit': round(profit, 3),
            'profit_pct': round(profit_pct, 3)
        })

    @app.route('/api/trades')
    def api_trades():
        mode = request.args.get('mode', 'sim')
        limit = int(request.args.get('limit', 100))
        trades = _read_json(_get_trades_file(mode))
        if trades is None:
            trades = []
        # 按时间倒序
        if isinstance(trades, list):
            trades = sorted(trades, key=lambda x: x.get('time', x.get('timestamp', '')), reverse=True)
            trades = trades[:limit]
            # 为每条交易记录添加名称
            for t in trades:
                c = t.get('code', t.get('stock_code', ''))
                t['name'] = _get_stock_name(c)
            # 对历史卖出记录补算盈亏（兼容旧数据未记录 pnl 的情况）
            _backfill_pnl(trades)
        return jsonify(trades)

    @app.route('/api/pool')
    def api_pool():
        d = _get_latest_ba_pool()
        if d is None:
            return jsonify({})
        pool_list = [
            {'code': item[0], 'rank': item[1], 'score': round(float(item[2]), 4)}
            for item in d.get('pool', [])
            if isinstance(item, (list, tuple)) and len(item) >= 3
        ]
        return jsonify({
            'strategy':       'V4 BA',
            'rebalance_date': d.get('ref_date', ''),
            'pool':           pool_list,
            'count':          d.get('count', len(pool_list)),
        })

    @app.route('/api/pool/status')
    def api_pool_status():
        """Return current pool strategy and rebuild status"""
        d         = _get_latest_ba_pool()
        ref_date  = d.get('ref_date', '') if d else ''
        pool_size = (d.get('count') or len(d.get('pool', []))) if d else 0
        return jsonify({
            'strategy':       'ba',
            'strategy_name':  'V4 BA',
            'rebalance_date': ref_date,
            'pool_size':      pool_size,
            'rebuilding':     _pool_rebuild_state['running'],
            'last_msg':       _pool_rebuild_state['msg'],
            'last_rebuild':   _pool_rebuild_state['last_rebuild'],
        })

    @app.route('/api/pool/rebuild', methods=['POST'])
    def api_pool_rebuild():
        """Trigger async pool rebuild - DISABLED"""
        return jsonify({'ok': False, 'msg': '选股重建功能已禁用，请通过命令行执行'}), 403


    def _get_board_info(code):
        """返回 (board_name, threshold, limit_up_pct)"""
        c = str(code).split('.')[0]
        if c.startswith('688') or c.startswith('30'):
            return '创业/科创板', 0.02, 0.198
        return '主板', 0.01, 0.098

    def _get_tick_data(codes):
        """调用 xtdata.get_full_tick 获取实时行情，返回 {code: tick_dict}"""
        if not _XTDATA_OK or not codes:
            return {}
        try:
            # xtdata 使用 XSHG/XSHE 后缀格式
            symbols = []
            code_map = {}  # symbol -> bare_code
            for c in codes:
                bare = c.split('.')[0]
                if bare.startswith('6'):
                    sym = bare + '.SH'
                else:
                    sym = bare + '.SZ'
                symbols.append(sym)
                code_map[sym] = bare
            tick_result = _xtdata.get_full_tick(symbols)
            result = {}
            for sym, tick in (tick_result or {}).items():
                bare = code_map.get(sym, sym.split('.')[0])
                result[bare] = tick
            return result
        except Exception:
            return {}

    @app.route('/api/candidates')
    def api_candidates():
        mode = request.args.get('mode', 'live')
        state = _read_json(_get_state_file(mode))
        d = _get_latest_ba_pool()

        positions = []
        pending_sells = []
        if state:
            pos_raw = state.get('positions', {})
            # V4: positions 是 dict {code: {...}}，归一化为 list
            if isinstance(pos_raw, dict):
                positions = [{'code': k, **v} for k, v in pos_raw.items()]
            else:
                positions = pos_raw
            pending_sells = state.get('pending_sells', [])

        # 当前持仓代码集合
        held_codes = set()
        for p in positions:
            code = p.get('code', p.get('stock_code', ''))
            if code:
                held_codes.add(str(code).split('.')[0])

        # 候选（V4 BA池，pool 格式为 [[code, rank, score], ...]）
        raw_candidates = []  # list of (seq_rank, bare_code)
        if d:
            pool_stocks = d.get('pool', [])
            for seq_rank, item in enumerate(pool_stocks):
                if isinstance(item, (list, tuple)) and len(item) >= 1:
                    code = str(item[0])
                elif isinstance(item, dict):
                    code = item.get('code', item.get('stock_code', ''))
                else:
                    code = str(item)
                bare = code.split('.')[0] if code else ''
                if bare and bare not in held_codes:
                    raw_candidates.append((seq_rank, bare))

        # 获取实时行情
        tick_map = _get_tick_data([bare for _, bare in raw_candidates])

        candidates = []
        for rank, bare in raw_candidates:
            board_name, threshold, limit_up = _get_board_info(bare)
            # 过热检查（从本地日线 CSV 读取）
            _is_hot, _hot_reason = _check_overheat_local(bare)
            item = {
                'code': bare,
                'name': _get_stock_name(bare),
                'symbol': bare + ('.SH' if bare.startswith('6') else '.SZ'),
                'board': board_name,
                'buy_threshold': round(threshold * 100, 1),
                'last_price': None,
                'pre_close': None,
                'open': None,
                'change_pct': None,
                'is_positive': None,
                'meets_buy_condition': False,
                'status': '过热' if _is_hot else '无行情',
                'pool_rank': rank,      # 池子原始排名（0=最高分）
                'is_overheat': _is_hot,
                'overheat_reason': _hot_reason,
            }
            tick = tick_map.get(bare)
            if tick and not _is_hot:
                try:
                    last_price = tick.get('lastPrice') or tick.get('last_price') or 0
                    pre_close  = tick.get('lastClose') or tick.get('pre_close') or tick.get('preClose') or 0
                    open_price = tick.get('open') or 0
                    volume     = tick.get('volume') or 0
                    if pre_close and pre_close > 0:
                        change_pct = (last_price - pre_close) / pre_close * 100
                    else:
                        change_pct = 0.0
                    is_positive = last_price > open_price if open_price > 0 else None
                    limit_pct = limit_up * 100
                    meets = (change_pct > threshold * 100) and (is_positive is True) and (change_pct < limit_pct)
                    # 未开盘判断：volume=0（最可靠）或 open=0 均视为未开盘
                    not_opened = (volume == 0 or open_price <= 0 or last_price <= 0)
                    # 状态判断
                    if not_opened:
                        status = '待开市'
                    elif change_pct >= limit_pct:
                        status = '涨停'
                    elif not is_positive:
                        status = '收阴'
                    elif change_pct <= threshold * 100:
                        status = '涨幅不足'
                    else:
                        status = '可买入'
                    item.update({
                        'last_price': round(last_price, 3),
                        'pre_close':  round(pre_close, 3),
                        'open':       round(open_price, 3),
                        'change_pct': round(change_pct, 2),
                        'is_positive': is_positive,
                        'meets_buy_condition': meets,
                        'status': status
                    })
                except Exception:
                    pass
            elif tick and _is_hot:
                # 过热时仍返回行情数据，但 meets_buy_condition=False 且 status=过热
                try:
                    last_price = tick.get('lastPrice') or tick.get('last_price') or 0
                    pre_close  = tick.get('lastClose') or tick.get('pre_close') or tick.get('preClose') or 0
                    open_price = tick.get('open') or 0
                    if pre_close and pre_close > 0:
                        change_pct = (last_price - pre_close) / pre_close * 100
                    else:
                        change_pct = 0.0
                    item.update({
                        'last_price': round(last_price, 3),
                        'pre_close':  round(pre_close, 3),
                        'open':       round(open_price, 3),
                        'change_pct': round(change_pct, 2),
                        'meets_buy_condition': False,
                        'status': '过热'
                    })
                except Exception:
                    pass
            candidates.append(item)

        # 排序：按池子原始排名（= quality_score 降序），保持选股优先级
        # meets_buy_condition 通过前端绿色行高亮区分，不破坏评分顺序
        candidates.sort(key=lambda x: x.get('pool_rank', 9999))

        return jsonify({
            'candidates': candidates,
            'pending_sells': pending_sells
        })

    @app.route('/api/config', methods=['GET', 'POST'])
    def api_config():
        if request.method == 'POST':
            return jsonify({'ok': False, 'msg': '参数调整功能已禁用，请直接修改 live_engine_v4.py'}), 403
        # GET: 读取 V4 引擎关键参数
        try:
            from engine import live_engine_v4 as _v4
            data = {
                'v4': {
                    'hard_stop':     getattr(_v4, 'HARD_STOP',           0.08),
                    'new_stock_hs':  getattr(_v4, 'NEW_STOCK_HARD_STOP', 0.08),
                    'trail_act':     getattr(_v4, 'TRAIL_ACT',           0.28),
                    'trail_stop':    getattr(_v4, 'TRAIL_STOP',          0.08),
                    'max_positions': getattr(_v4, 'MAX_POSITIONS',       5),
                    'ba_min_chg':    getattr(_v4, 'BA_MIN_CHG',          0.0),
                    'ba_max_chg':    getattr(_v4, 'BA_MAX_CHG',          0.30),
                    'cool_ret_max':  getattr(_v4, 'COOL_RET_MAX',        0.20),
                    'vol_ratio_min': getattr(_v4, 'VOL_RATIO_MIN',       0.5),
                    'vol_ratio_max': getattr(_v4, 'VOL_RATIO_MAX',       5.0),
                    'gap_min':       getattr(_v4, 'GAP_MIN',             0.005),
                }
            }
        except Exception as e:
            data = {'error': str(e)}
        return jsonify(data)

    # ==================== 进程监控接口 ====================

    @app.route('/api/processes')
    def api_processes():
        """Wreturn the running status of sim/live processes"""
        result = {}
        for mode, script in PROCESS_TARGETS.items():
            running, pid = _find_running_process(script)
            # 如果 psutil 不可用，尝试用句柄检测
            if not _PSUTIL_OK:
                p = _launched_procs.get(mode)
                running = (p is not None and p.poll() is None)
                pid = p.pid if running else None
            result[mode] = {'running': running, 'pid': pid, 'script': script}
        return jsonify(result)

    @app.route('/api/processes/start', methods=['POST'])
    def api_process_start():
        """Start a trading process - DISABLED"""
        return jsonify({'ok': False, 'msg': '进程启动功能已禁用，请直接在命令行启动'}), 403
        if mode not in PROCESS_TARGETS:
            return jsonify({'ok': False, 'msg': '未知模式: ' + str(mode)})
        script = PROCESS_TARGETS[mode]
        running, pid = _find_running_process(script)
        if not _PSUTIL_OK:
            p = _launched_procs.get(mode)
            running = (p is not None and p.poll() is None)
        if running:
            return jsonify({'ok': False, 'msg': f'进程已在运行 (PID {pid})，无需重复启动'})
        try:
            python_exe = sys.executable
            script_path = os.path.join(BASE_DIR, script)
            # Windows 下开新控制台窗口启动
            kwargs = {}
            if sys.platform == 'win32':
                kwargs['creationflags'] = subprocess.CREATE_NEW_CONSOLE
            proc = subprocess.Popen(
                [python_exe, script_path],
                cwd=BASE_DIR,
                **kwargs
            )
            _launched_procs[mode] = proc
            return jsonify({'ok': True, 'msg': f'{script} 已启动 (PID {proc.pid})'})
        except Exception as e:
            return jsonify({'ok': False, 'msg': str(e)})

    @app.route('/api/processes/stop', methods=['POST'])
    def api_process_stop():
        """Stop a trading process - DISABLED"""
        return jsonify({'ok': False, 'msg': '进程停止功能已禁用，请直接在命令行操作'}), 403
        if mode not in PROCESS_TARGETS:
            return jsonify({'ok': False, 'msg': '未知模式'})
        script = PROCESS_TARGETS[mode]
        running, pid = _find_running_process(script)
        if not running:
            return jsonify({'ok': False, 'msg': '进程未运行'})
        try:
            if _PSUTIL_OK and pid:
                proc = psutil.Process(pid)
                proc.terminate()
            else:
                p = _launched_procs.get(mode)
                if p:
                    p.terminate()
            return jsonify({'ok': True, 'msg': f'PID {pid} 已发送终止信号'})
        except Exception as e:
            return jsonify({'ok': False, 'msg': str(e)})

    # ==================== 模拟结果存档接口 ====================

    @app.route('/api/sim/list')
    def api_sim_list():
        """列出所有模拟存档（仅元数据，不含 equity_curve / trades 明细）"""
        if not os.path.exists(SIM_RESULTS_DIR):
            return jsonify([])
        results = []
        for fname in sorted(os.listdir(SIM_RESULTS_DIR), reverse=True):
            if not fname.endswith('.json'):
                continue
            fpath = os.path.join(SIM_RESULTS_DIR, fname)
            try:
                with open(fpath, 'r', encoding='utf-8') as _f:
                    data = json.load(_f)
                results.append({
                    'run_id':          data.get('run_id', ''),
                    'start_date':      data.get('start_date', ''),
                    'end_date':        data.get('end_date', ''),
                    'run_time':        data.get('run_time', ''),
                    'initial_capital': data.get('initial_capital', 300000),
                    'summary':         data.get('summary', {}),
                })
            except Exception:
                continue
        return jsonify(results)

    @app.route('/api/sim/<run_id>')
    def api_sim_detail(run_id):
        """返回某次模拟的完整数据（含 equity_curve 和 trades）"""
        data, _ = _read_sim_file(run_id)
        if data is None:
            return jsonify({'error': f'未找到 run_id={run_id}'}), 404
        return jsonify(data)

    @app.route('/api/sim/<run_id>', methods=['DELETE'])
    def api_sim_delete(run_id):
        """删除某次模拟存档"""
        _, fpath = _read_sim_file(run_id)
        if fpath is None:
            return jsonify({'ok': False, 'msg': f'未找到 run_id={run_id}'})
        try:
            os.remove(fpath)
            return jsonify({'ok': True})
        except Exception as _e:
            return jsonify({'ok': False, 'msg': str(_e)})

    # ==================== 新股监控接口 ====================

    @app.route('/api/new_stocks')
    def api_new_stocks():
        global _new_stock_loading
        if not _XTDATA_OK:
            return jsonify({'stocks': [], 'xt_offline': True, 'loading': False})

        today_str = _dt_now.now().strftime('%Y-%m-%d')

        # 当天缓存失效或未加载时，触发后台刷新
        if _new_stock_cache_date != today_str and not _new_stock_loading:
            _new_stock_loading = True
            t = _threading.Thread(target=_build_new_stock_cache_bg, daemon=True)
            t.start()

        # 追加实时行情
        codes = [s['code'] for s in _new_stock_cache]
        tick_map = _get_tick_data(codes) if codes else {}

        result = []
        for s in _new_stock_cache:
            item = dict(s)
            tick = tick_map.get(s['code'])
            if tick:
                try:
                    last_price = tick.get('lastPrice') or 0
                    pre_close = (tick.get('lastClose') or tick.get('preClose') or 0)
                    if pre_close > 0 and last_price > 0:
                        item['change_pct'] = round((last_price - pre_close) / pre_close * 100, 2)
                        item['last_price'] = round(last_price, 3)
                        item['pre_close'] = round(pre_close, 3)
                    else:
                        item['change_pct'] = None
                        item['last_price'] = None
                        item['pre_close'] = None
                except Exception:
                    item['change_pct'] = None
                    item['last_price'] = None
                    item['pre_close'] = None
            else:
                item['change_pct'] = None
                item['last_price'] = None
                item['pre_close'] = None
            result.append(item)

        return jsonify({
            'stocks': result,
            'loading': _new_stock_loading,
            'cache_date': _new_stock_cache_date
        })

    return app


def _read_sim_file(run_id: str):
    """按 run_id 前缀定位并读取 sim_results 中的 JSON 文件，返回 (data, filepath)"""
    if not os.path.exists(SIM_RESULTS_DIR):
        return None, None
    for fname in os.listdir(SIM_RESULTS_DIR):
        if fname.startswith(run_id) and fname.endswith('.json'):
            fpath = os.path.join(SIM_RESULTS_DIR, fname)
            try:
                with open(fpath, 'r', encoding='utf-8') as _f:
                    return json.load(_f), fpath
            except Exception:
                return None, fpath
    return None, None
