# -*- coding: utf-8 -*-
"""
V3策略仪表盘 - Flask后端
端口: 8088
"""
import os
import json
import sys
import subprocess
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
    'sim':  'run_simulation_v3.py',
    'live': 'run_live_v3.py',
}

# 已启动的进程句柄 {mode: Popen}
_launched_procs = {}


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

# 股票名称缓存
_stock_name_cache = {}


def _get_stock_name(code):
    """获取股票名称，优先从缓存，失败返回空字符串"""
    bare = str(code).split('.')[0]
    if bare in _stock_name_cache:
        return _stock_name_cache[bare]
    if not _XTDATA_OK:
        return ''
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
        return ''


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

    def _get_state_file(mode):
        if mode == 'live':
            return os.path.join(BASE_DIR, 'state_v3.json')
        return os.path.join(BASE_DIR, 'state_v3_sim.json')

    def _get_trades_file(mode):
        if mode == 'live':
            return os.path.join(BASE_DIR, 'trades_v3.json')
        return os.path.join(BASE_DIR, 'trades_v3_sim.json')

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

        positions = state.get('positions', [])
        pending_sells = state.get('pending_sells', [])

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
            'cash': round(cash, 2),
            'total_value': round(realtime_total, 2),
            'initial_capital': initial_capital,
            'positions': positions,
            'pending_sells': pending_sells,
            'last_update': state.get('last_update', state.get('update_time', '')),
            'profit': round(profit, 2),
            'profit_pct': round(profit_pct, 2)
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
        return jsonify(trades)

    @app.route('/api/pool')
    def api_pool():
        pool = _read_json(os.path.join(BASE_DIR, 'state_v3_rebalance.json'))
        if pool is None:
            pool = {}
        return jsonify(pool)

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
        mode = request.args.get('mode', 'sim')
        state = _read_json(_get_state_file(mode))
        pool = _read_json(os.path.join(BASE_DIR, 'state_v3_rebalance.json'))

        positions = []
        pending_sells = []
        if state:
            positions = state.get('positions', [])
            pending_sells = state.get('pending_sells', [])

        # 当前持仓代码集合
        held_codes = set()
        for p in positions:
            code = p.get('code', p.get('stock_code', ''))
            if code:
                held_codes.add(str(code).split('.')[0])

        # 候选（调仓池中未持仓的股票）
        raw_candidates = []
        if pool:
            pool_stocks = pool.get('pool', pool.get('stocks', []))
            if isinstance(pool_stocks, list):
                for item in pool_stocks:
                    if isinstance(item, dict):
                        code = item.get('code', item.get('stock_code', ''))
                    else:
                        code = str(item)
                    bare = code.split('.')[0] if code else ''
                    if bare and bare not in held_codes:
                        raw_candidates.append(bare)

        # 获取实时行情
        tick_map = _get_tick_data(raw_candidates)

        candidates = []
        for bare in raw_candidates:
            board_name, threshold, limit_up = _get_board_info(bare)
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
                'status': '无行情'
            }
            tick = tick_map.get(bare)
            if tick:
                try:
                    last_price = tick.get('lastPrice') or tick.get('last_price') or 0
                    pre_close  = tick.get('lastClose') or tick.get('pre_close') or tick.get('preClose') or 0
                    open_price = tick.get('open') or 0
                    if pre_close and pre_close > 0:
                        change_pct = (last_price - pre_close) / pre_close * 100
                    else:
                        change_pct = 0.0
                    is_positive = last_price > open_price if open_price > 0 else False
                    limit_pct = limit_up * 100
                    meets = (change_pct > threshold * 100) and is_positive and (change_pct < limit_pct)
                    # 状态判断
                    if change_pct >= limit_pct:
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
            candidates.append(item)

        # 排序：满足买入条件优先，再按涨跌幅降序
        def sort_key(x):
            pct = x.get('change_pct') if x.get('change_pct') is not None else -999
            meets = 1 if x.get('meets_buy_condition') else 0
            return (-meets, -pct)

        candidates.sort(key=sort_key)

        return jsonify({
            'candidates': candidates,
            'pending_sells': pending_sells
        })

    @app.route('/api/config')
    def api_config():
        try:
            import config as cfg
            data = {
                'main_board': {
                    'min_change_pct': getattr(cfg, 'V3_MIN_CHANGE_PCT', 0.01),
                    'take_profit': getattr(cfg, 'V3_TAKE_PROFIT', 0.05),
                    'hard_stop_loss': getattr(cfg, 'V3_HARD_STOP_LOSS', 0.05),
                    'soft_stop_loss': getattr(cfg, 'V3_SOFT_STOP_LOSS', 0.03),
                    'time_stop_days': getattr(cfg, 'V3_TIME_STOP_DAYS', 5),
                    'limit_up': 0.098,
                    'trailing_activate': getattr(cfg, 'V3_TRAILING_ACTIVATE', 0.03),
                    'trailing_stop': getattr(cfg, 'V3_TRAILING_STOP', 0.02),
                },
                'star_board': {
                    'min_change_pct': getattr(cfg, 'V3_STAR_MIN_CHANGE_PCT', 0.02),
                    'take_profit': getattr(cfg, 'V3_STAR_TAKE_PROFIT', 0.15),
                    'hard_stop_loss': getattr(cfg, 'V3_STAR_HARD_STOP_LOSS', 0.05),
                    'soft_stop_loss': getattr(cfg, 'V3_STAR_SOFT_STOP_LOSS', 0.03),
                    'time_stop_days': getattr(cfg, 'V3_STAR_TIME_STOP_DAYS', 5),
                    'limit_up': getattr(cfg, 'V3_STAR_LIMIT_UP', 0.198),
                    'trailing_activate': getattr(cfg, 'V3_STAR_TRAILING_ACTIVATE', 0.08),
                    'trailing_stop': getattr(cfg, 'V3_STAR_TRAILING_STOP', 0.05),
                },
                'general': {
                    'top_n': getattr(cfg, 'V3_TOP_N', 50),
                    'max_positions': getattr(cfg, 'V3_MAX_POSITIONS', 3),
                    'buy_time': getattr(cfg, 'V3_BUY_TIME', '14:30'),
                    'initial_capital': getattr(cfg, 'V3_INITIAL_CAPITAL', 300000),
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
        """Start a trading process"""
        data = request.get_json() or {}
        mode = data.get('mode')
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
        """Stop a trading process"""
        data = request.get_json() or {}
        mode = data.get('mode')
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

    return app
