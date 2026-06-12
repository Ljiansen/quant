# -*- coding: utf-8 -*-
"""
每日数据增量更新脚本
- 数据源：akshare（前复权日线，无需 miniQMT，免费）
- 触发时机：每个交易日 15:30 后（由 Windows 任务计划程序自动调用）
- 功能：
    1. 检测本地数据最新日期
    2. 增量下载缺失日期的数据，追加到已有 CSV
    3. 可选 --add-new：获取 akshare 全量股票列表，为新上市的股票创建 CSV

用法：
  python update_daily_data.py               # 正常增量更新（15:30 后生效）
  python update_daily_data.py --force       # 忽略时间检查，强制运行
  python update_daily_data.py --add-new     # 同时为新上市股票建立 CSV
  python update_daily_data.py --date 20260430  # 指定截止日期
"""

import os
import sys
import time
import json
import random
import argparse
import logging
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

import pandas as pd

# ──────────────────────────────────────────────────────────────
# 配置
# ──────────────────────────────────────────────────────────────
DATA_DIR   = 'D:/daily_data'
SH_DIR     = os.path.join(DATA_DIR, 'SH')
SZ_DIR     = os.path.join(DATA_DIR, 'SZ')
LOG_FILE   = 'd:/miniqmt_quant/logs/update_daily_data.log'

# 每次 API 请求之间的随机延时范围（秒），避免被限频
# baostock 稳定，可以稍快一些
DELAY_MIN  = 0.05
DELAY_MAX  = 0.15

# baostock 单次请求超时（秒）和重试次数
BS_TIMEOUT = 30
BS_MAX_RETRIES = 3

# 项目根目录（用于定位 state_v4.json 等文件）
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))

# 失败股票记录文件
FAILED_FILE = os.path.join(BASE_DIR, 'logs', 'daily_update_failed.json')

# CSV 列名（与现有本地数据保持一致，volumn 是 xtquant 原始拼写）
CSV_COLS   = ['timetag', 'open', 'high', 'low', 'close', 'volumn', 'amount']

# ── 超时包装 ──
_executor = ThreadPoolExecutor(max_workers=1)

def _run_with_timeout(fn, timeout=BS_TIMEOUT, desc=''):
    """在线程池中执行 fn()，超时抛出 FuturesTimeoutError"""
    future = _executor.submit(fn)
    try:
        return future.result(timeout=timeout)
    except FuturesTimeoutError:
        log.warning(f'  ⏱️  超时({timeout}s): {desc}')
        raise

# ──────────────────────────────────────────────────────────────
# 日志初始化
# ──────────────────────────────────────────────────────────────
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────────────────────

def _get_file_path(code: str) -> str:
    """根据股票代码返回对应的 CSV 路径"""
    if code.startswith('6'):
        return os.path.join(SH_DIR, f'price_{code}.csv')
    return os.path.join(SZ_DIR, f'price_{code}.csv')


def get_latest_local_date() -> int | None:
    """扫描本地文件，返回所有股票中最新的 timetag（YYYYMMDD 整数）"""
    for subdir in [SH_DIR, SZ_DIR]:
        if not os.path.exists(subdir):
            continue
        for fname in os.listdir(subdir):
            if not (fname.startswith('price_') and fname.endswith('.csv')):
                continue
            path = os.path.join(subdir, fname)
            if os.path.getsize(path) < 500:
                continue
            try:
                df = pd.read_csv(path, usecols=['timetag'], nrows=9999)
                if not df.empty:
                    return int(df['timetag'].max())
            except Exception:
                continue
    return None


def get_all_local_codes() -> list[str]:
    """返回本地所有已有 CSV 的股票代码列表"""
    codes = []
    for subdir in [SH_DIR, SZ_DIR]:
        if not os.path.exists(subdir):
            continue
        for fname in os.listdir(subdir):
            if fname.startswith('price_') and fname.endswith('.csv'):
                code = fname.replace('price_', '').replace('.csv', '')
                # 跳过上证指数文件（sh000001），由 update_sh_index() 单独处理
                if not code[0].isdigit():
                    continue
                if os.path.getsize(os.path.join(subdir, fname)) > 200:
                    codes.append(code)
    return sorted(codes)


# baostock 全局登录状态
_bs_logged_in = False


def _bs_ensure_login():
    """确保 baostock 已登录"""
    global _bs_logged_in
    if not _bs_logged_in:
        import baostock as bs
        lg = bs.login()
        _bs_logged_in = (lg.error_code == '0')
        if not _bs_logged_in:
            raise RuntimeError(f'baostock 登录失败: {lg.error_msg}')


def _bs_logout():
    """登出 baostock"""
    global _bs_logged_in
    if _bs_logged_in:
        import baostock as bs
        bs.logout()
        _bs_logged_in = False


def fetch_bs_hist(code: str, start_yyyymmdd: str, end_yyyymmdd: str) -> pd.DataFrame:
    """
    通过 baostock 获取单只股票前复权日线数据。
    返回格式与本地 CSV 一致：timetag(int), open, high, low, close, volumn, amount
    注意：baostock volume 单位为股，需除以 100 转为手（与 xtquant/本地 CSV 一致）
    支持 BS_TIMEOUT 超时 + BS_MAX_RETRIES 次重试，失败返回空 DataFrame。
    """
    import baostock as bs

    # 日期格式 YYYYMMDD → YYYY-MM-DD
    def _fmt(d): return f'{d[:4]}-{d[4:6]}-{d[6:]}'
    start = _fmt(start_yyyymmdd)
    end   = _fmt(end_yyyymmdd)

    # 股票代码转 baostock 格式
    bs_code = f'sh.{code}' if code.startswith('6') else f'sz.{code}'

    for attempt in range(1, BS_MAX_RETRIES + 1):
        try:
            def _do_query():
                _bs_ensure_login()
                rs = bs.query_history_k_data_plus(
                    code=bs_code,
                    fields='date,open,high,low,close,volume,amount',
                    start_date=start,
                    end_date=end,
                    frequency='d',
                    adjustflag='2',  # 前复权
                )
                if rs.error_code != '0':
                    raise RuntimeError(f'baostock 查询错误: {rs.error_msg}')
                rows = []
                while rs.next():
                    rows.append(rs.get_row_data())
                return rows, rs.fields if rows else None

            rows, fields = _run_with_timeout(_do_query, timeout=BS_TIMEOUT,
                                              desc=f'{code}({start}~{end})')

            if not rows:
                return pd.DataFrame()

            df = pd.DataFrame(rows, columns=fields)
            df['timetag'] = df['date'].str.replace('-', '', regex=False).astype(int)
            for col in ['open', 'high', 'low', 'close', 'volume', 'amount']:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)
            df['volumn'] = df['volume'] / 100.0
            df = df[CSV_COLS].copy()
            return df

        except FuturesTimeoutError:
            global _bs_logged_in
            _bs_logged_in = False  # 超时后 baostock 状态不可信，重新登录
            if attempt < BS_MAX_RETRIES:
                wait = 1.0 * attempt + random.random()
                log.warning(f'  {code} 第{attempt}次超时，等待{wait:.1f}s后重试')
                time.sleep(wait)
            else:
                log.error(f'  {code} 获取失败（已重试{BS_MAX_RETRIES}次均超时）')
        except Exception as e:
            _bs_logged_in = False
            if attempt < BS_MAX_RETRIES:
                time.sleep(1.0 * attempt + random.random())
            else:
                log.error(f'  {code} 获取失败（已重试{BS_MAX_RETRIES}次）: {e}')
    return pd.DataFrame()


def append_new_rows(code: str, new_df: pd.DataFrame) -> int:
    """
    将 new_df 中尚未存在于本地 CSV 的行追加进去。
    返回实际追加的行数。
    """
    path = _get_file_path(code)
    if not os.path.exists(path):
        # 新股：直接写入（不追加，包含表头）
        os.makedirs(os.path.dirname(path), exist_ok=True)
        new_df.to_csv(path, index=False)
        return len(new_df)

    try:
        existing = pd.read_csv(path, usecols=['timetag'])
        last_timetag = int(existing['timetag'].max()) if not existing.empty else 0
    except Exception:
        last_timetag = 0

    to_append = new_df[new_df['timetag'] > last_timetag]
    if to_append.empty:
        return 0

    to_append.to_csv(path, mode='a', header=False, index=False)
    return len(to_append)


# ──────────────────────────────────────────────────────────────
# 上证指数专用更新
# ──────────────────────────────────────────────────────────────

SH_INDEX_FILE = os.path.join(SH_DIR, 'price_sh000001.csv')


def update_sh_index(start_yyyymmdd: str, end_yyyymmdd: str) -> bool:
    """
    增量更新上证指数日线到 D:/daily_data/SH/price_sh000001.csv。
    字段与普通股票 CSV 保持一致：timetag, open, high, low, close, volumn, amount
    adjustflag='3'（指数不复权）
    返回 True 表示有新数据写入，False 表示无新数据或失败。
    """
    import baostock as bs

    def _fmt(d): return f'{d[:4]}-{d[4:6]}-{d[6:]}'
    start = _fmt(start_yyyymmdd)
    end   = _fmt(end_yyyymmdd)

    for attempt in range(1, 4):
        try:
            _bs_ensure_login()
            rs = bs.query_history_k_data_plus(
                code='sh.000001',
                fields='date,open,high,low,close,volume,amount',
                start_date=start,
                end_date=end,
                frequency='d',
                adjustflag='3',   # 指数不复权
            )
            if rs.error_code != '0':
                raise RuntimeError(f'baostock 上证指数查询错误: {rs.error_msg}')

            rows = []
            while rs.next():
                rows.append(rs.get_row_data())

            if not rows:
                log.info('上证指数: 无新数据')
                return False

            df = pd.DataFrame(rows, columns=rs.fields)
            df['timetag'] = df['date'].str.replace('-', '', regex=False).astype(int)
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)
            # 指数文件只保留 6 列，与原始文件格式保持一致（不含 amount/volumn）
            df = df[['timetag', 'open', 'high', 'low', 'close', 'volume']].copy()

            # 追加到文件（去重）
            os.makedirs(SH_DIR, exist_ok=True)
            if os.path.exists(SH_INDEX_FILE):
                existing = pd.read_csv(SH_INDEX_FILE, usecols=['timetag'])
                last_tag = int(existing['timetag'].max()) if not existing.empty else 0
                df = df[df['timetag'] > last_tag]

            if df.empty:
                log.info('上证指数: 已是最新，无需追加')
                return False

            write_header = not os.path.exists(SH_INDEX_FILE)
            df.to_csv(SH_INDEX_FILE, mode='a', header=write_header, index=False)
            log.info(f'上证指数: 新增 {len(df)} 行，最新日期 {df["timetag"].max()}')
            return True

        except Exception as e:
            global _bs_logged_in
            _bs_logged_in = False
            if attempt < 3:
                time.sleep(1.0 * attempt + random.random())
            else:
                log.warning(f'上证指数更新失败（已重试3次）: {e}')
    return False


# ──────────────────────────────────────────────────────────────
# 核心更新逻辑
# ──────────────────────────────────────────────────────────────

def update_existing(codes: list[str], start_yyyymmdd: str,
                    end_yyyymmdd: str,
                    skip_codes: set = None) -> dict:
    """
    批量更新已有股票 CSV 文件。
    skip_codes: 跳过的股票集合（如除权股票，将单独全量重下）
    返回 {success, up_to_date, no_data, ex_rights, failed, failed_codes}
    """
    total   = len(codes)
    success = 0
    up_to_date = 0   # 本地已有最新数据
    no_data = 0      # baostock 无新数据（停牌/未上市）
    ex_rights = 0    # 除权跳过
    failed  = 0
    failed_codes = []
    skip_codes = skip_codes or set()

    for i, code in enumerate(codes, 1):
        if code in skip_codes:
            ex_rights += 1
            continue

        if i % 20 == 0 or i == 1 or i == total:
            log.info(f'  进度: {i}/{total}  更新={success} 已最新={up_to_date}'
                     f' 无数据={no_data} 失败={failed}')

        try:
            new_df = fetch_bs_hist(code, start_yyyymmdd, end_yyyymmdd)
            if new_df.empty:
                no_data += 1
            else:
                added = append_new_rows(code, new_df)
                if added > 0:
                    success += 1
                else:
                    up_to_date += 1
        except Exception as e:
            log.warning(f'  {code} 处理异常: {e}')
            failed += 1
            failed_codes.append(code)

        time.sleep(DELAY_MIN + random.random() * (DELAY_MAX - DELAY_MIN))

    return {
        'success': success, 'up_to_date': up_to_date,
        'no_data': no_data, 'ex_rights': ex_rights,
        'failed': failed, 'failed_codes': failed_codes,
    }


def add_new_stocks(start_yyyymmdd: str, end_yyyymmdd: str,
                   existing_codes: set[str]) -> int:
    """
    从 baostock 获取全量 A 股列表，对本地不存在的新股创建 CSV。
    返回新增的股票数。
    """
    import baostock as bs
    log.info('获取全市场股票列表，检测新上市股票...')

    try:
        _bs_ensure_login()
        rs = bs.query_stock_basic(code_name='')
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        df_list = pd.DataFrame(rows, columns=rs.fields)
        # 只保留上市状态、沪深主板/中小板/创业板（type=1）
        df_list = df_list[df_list['type'] == '1']
        all_codes = df_list['code'].apply(lambda x: x.split('.')[-1]).tolist()
    except Exception as e:
        log.warning(f'获取股票列表失败: {e}')
        return 0

    # 过滤：只要沪深A股（排除北交所8/4开头）
    new_codes = [
        c for c in all_codes
        if c not in existing_codes
        and not c.startswith('8')
        and not c.startswith('4')
        and (c.startswith('6') or c.startswith('0') or c.startswith('3'))
    ]

    if not new_codes:
        log.info('没有发现新上市股票')
        return 0

    log.info(f'发现 {len(new_codes)} 只新股，开始下载历史数据...')
    added = 0

    for i, code in enumerate(new_codes, 1):
        if i % 20 == 0 or i == 1:
            log.info(f'  新股进度: {i}/{len(new_codes)}')
        try:
            # 新股从 2022-01-01 开始拉取全部历史
            df = fetch_bs_hist(code, '20220101', end_yyyymmdd)
            if not df.empty:
                path = _get_file_path(code)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                df.to_csv(path, index=False)
                added += 1
                log.info(f'  新股 {code} 已创建 ({len(df)} 行)')
        except Exception as e:
            log.debug(f'  新股 {code} 处理失败: {e}')

        time.sleep(0.15 + random.random() * 0.1)

    return added


# ──────────────────────────────────────────────────────────────
# 除权检测与处理
# ──────────────────────────────────────────────────────────────

def query_dividends_today(today_str: str) -> dict:
    """
    查询今天发生除权除息的股票。
    仅检查当前持仓的股票（baostock query_dividend_data 需逐只查询 code）。
    today_str: 'YYYY-MM-DD'
    返回 {code: {ex_date, factor, bonus, transfer, cash_div, plan}}
    """
    import baostock as bs
    _bs_ensure_login()

    year = today_str[:4]
    events = {}

    # 检查范围：当前持仓 + 近期交易过的股票
    state_path = os.path.join(BASE_DIR, 'state_v4.json')
    trades_path = os.path.join(BASE_DIR, 'trades_v4.json')
    check_codes = set()

    # 持仓股票
    if os.path.exists(state_path):
        import json
        with open(state_path, 'r', encoding='utf-8') as f:
            state = json.load(f)
        check_codes.update(state.get('positions', {}).keys())

    # 近期交易过的股票（trades_v4.json 中最近 30 天的记录）
    if os.path.exists(trades_path):
        import json
        from datetime import datetime as _dt, timedelta as _td
        with open(trades_path, 'r', encoding='utf-8') as f:
            trades = json.load(f)
        cutoff = (_dt.now() - _td(days=30)).strftime('%Y-%m-%d')
        for t in trades:
            if t.get('timestamp', '') >= cutoff:
                check_codes.add(t.get('code', ''))

    if not check_codes:
        log.info('无持仓/近期交易股票，跳过除权检测')
        return events

    log.info(f'检查 {len(check_codes)} 只股票（持仓+近期交易）的除权事件')

    for code in check_codes:
        try:
            bs_code = f'sh.{code}' if code.startswith('6') else f'sz.{code}'
            rs = bs.query_dividend_data(code=bs_code, year=year)
            while rs.error_code == '0' and rs.next():
                row = rs.get_row_data()
                fields = rs.fields
                d = dict(zip(fields, row))

                ex_date = d.get('dividOperateDate', '')
                if ex_date != today_str:
                    continue

                bonus    = float(d.get('dividStocksPs', 0) or 0)
                transfer = float(d.get('dividReserveToStockPs', 0) or 0)
                cash_div = float(d.get('dividCashPsBeforeTax', 0) or 0)

                factor = 1.0 + bonus + transfer
                if factor <= 1.0 and cash_div <= 0:
                    continue

                events[code] = {
                    'ex_date':   ex_date,
                    'factor':    round(factor, 6),
                    'bonus':     bonus,
                    'transfer':  transfer,
                    'cash_div':  cash_div,
                    'plan':      d.get('dividCashStock', ''),
                    'reg_date':  d.get('dividRegistDate', ''),
                }
        except Exception as e:
            log.warning(f'  {code} 除权查询失败: {e}')

    if events:
        log.info(f'今日({today_str})除权除息股票: {len(events)} 只')
        for code, ev in events.items():
            log.info(f'  {code}: {ev["plan"]}  复权因子={ev["factor"]:.4f}')
    return events


def reprocess_ex_rights_stocks(today_str: str, events: dict) -> None:
    """
    对发生除权的股票：全量重新下载日线 + 调整 state_v4.json 持仓。
    """
    if not events:
        return

    affected_codes = list(events.keys())
    log.info(f'除权检测: {len(affected_codes)} 只股票需要处理')

    # 1. 全量重下日线数据
    for i, code in enumerate(affected_codes, 1):
        if i % 50 == 0:
            log.info(f'  重下进度: {i}/{len(affected_codes)}')
        try:
            df = fetch_bs_hist(code, '20200101', today_str.replace('-', ''))
            if not df.empty:
                path = _get_file_path(code)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                df.to_csv(path, index=False)
                log.info(f'  {code}: 全量重下完成 ({len(df)} 行)')
            else:
                log.warning(f'  {code}: 重下数据为空')
        except Exception as e:
            log.warning(f'  {code}: 重下失败 ({e})')
        time.sleep(DELAY_MIN + random.random() * (DELAY_MAX - DELAY_MIN))

    # 2. 调整 state_v4.json 持仓
    state_path = os.path.join(BASE_DIR, 'state_v4.json')
    if not os.path.exists(state_path):
        log.warning(f'state 文件不存在: {state_path}')
        return

    import json
    with open(state_path, 'r', encoding='utf-8') as f:
        state = json.load(f)

    positions = state.get('positions', {})
    adjusted = 0

    for code, ev in events.items():
        if code not in positions:
            continue
        pos = positions[code]
        factor = ev['factor']
        if factor <= 1.0:
            continue

        old_bp = pos.get('buy_price', 0)
        old_hp = pos.get('highest_price', 0)
        old_qty = pos.get('quantity', 0)

        pos['buy_price']     = round(old_bp / factor, 3)
        pos['highest_price'] = round(old_hp / factor, 3)
        pos['quantity']      = int(round(old_qty * factor))

        log.info(
            f'  持仓调整 {code}: '
            f'factor={factor:.4f}  '
            f'buy_price {old_bp:.3f}→{pos["buy_price"]:.3f}  '
            f'qty {old_qty}→{pos["quantity"]}'
        )
        adjusted += 1

    if adjusted > 0:
        state['last_update'] = today_str + ' ex-rights'
        with open(state_path, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        log.info(f'state_v4.json 已更新 ({adjusted} 只持仓调整)')


# ──────────────────────────────────────────────────────────────
# 主入口
# ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='每日本地数据增量更新')
    parser.add_argument('--force',   action='store_true', help='忽略时间检查强制执行')
    parser.add_argument('--add-new', action='store_true', help='同时为新上市股票建立 CSV')
    parser.add_argument('--date',    type=str, default=None, help='指定截止日期 YYYYMMDD')
    args = parser.parse_args()

    now = datetime.now()
    log.info('=' * 60)
    log.info(f'每日数据更新  {now.strftime("%Y-%m-%d %H:%M:%S")}')
    log.info('=' * 60)

    # 时间检查：15:30 之前不执行（市场尚未收盘/数据未就绪）
    if not args.force and now.hour < 15 or (now.hour == 15 and now.minute < 30):
        log.warning(f'当前时间 {now.strftime("%H:%M")}，数据尚未就绪（需 15:30 后）')
        log.warning('使用 --force 可强制执行')
        sys.exit(1)

    # 确定截止日期
    end_date = args.date if args.date else now.strftime('%Y%m%d')
    log.info(f'截止日期: {end_date}')

    # 获取本地最新日期
    latest = get_latest_local_date()
    if latest is None:
        log.error('无法读取本地数据目录，请检查 D:/daily_data')
        sys.exit(1)
    log.info(f'本地数据最新日期: {latest}')

    # 计算需要更新的起始日期
    if latest >= int(end_date) and not args.force:
        log.info('本地数据已是最新，无需更新')
        sys.exit(0)

    start_dt   = datetime.strptime(str(latest), '%Y%m%d') + timedelta(days=1)
    start_date = start_dt.strftime('%Y%m%d')
    log.info(f'增量区间: {start_date} ~ {end_date}')

    # 获取本地所有股票代码
    codes = get_all_local_codes()
    log.info(f'本地股票总数: {len(codes)}')

    # baostock 登录
    _bs_ensure_login()
    log.info('baostock 登录成功')

    # ── Step 1: 除权检测（前置，避免增量更新白跑）──
    today_fmt = f'{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}'
    log.info('── 除权检测 ──')
    div_events = query_dividends_today(today_fmt)
    ex_rights_codes = set(div_events.keys()) if div_events else set()
    if ex_rights_codes:
        log.info(f'检测到 {len(ex_rights_codes)} 只除权股票，增量更新时将跳过')
    else:
        log.info('今日无除权事件')

    # ── Step 2: 增量更新（跳过除权股票）──
    est_min = round(len(codes) * ((DELAY_MIN + DELAY_MAX) / 2 + 0.1) / 60, 1)
    log.info(f'预计耗时: ~{est_min} 分钟')

    t0 = time.time()
    result = update_existing(codes, start_date, end_date, skip_codes=ex_rights_codes)
    elapsed = (time.time() - t0) / 60

    _bs_logout()
    log.info(f'增量更新完成: 更新={result["success"]} 已最新={result["up_to_date"]}'
             f' 无数据={result["no_data"]} 除权跳过={result["ex_rights"]}'
             f' 失败={result["failed"]}，耗时 {elapsed:.1f} 分钟')

    # ── 记录失败股票 ──
    failed_codes = result['failed_codes']
    if failed_codes:
        os.makedirs(os.path.dirname(FAILED_FILE), exist_ok=True)
        fail_record = {
            'date': end_date,
            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'count': len(failed_codes),
            'codes': failed_codes,
        }
        with open(FAILED_FILE, 'w', encoding='utf-8') as f:
            json.dump(fail_record, f, ensure_ascii=False, indent=2)
        log.warning(f'⚠️  {len(failed_codes)} 只股票失败，已记录到 {FAILED_FILE}')
        log.warning(f'    失败列表: {failed_codes[:20]}{"..." if len(failed_codes) > 20 else ""}')
        log.info('    可重新运行脚本进行补下（幂等保护）')
    else:
        # 全部成功时清除旧的失败记录
        if os.path.exists(FAILED_FILE):
            os.remove(FAILED_FILE)

    # ── Step 3: 上证指数更新 ──
    log.info('── 更新上证指数 ──')
    update_sh_index(start_date, end_date)

    # ── Step 4: 除权股票全量重下 + state 持仓调整 ──
    if div_events:
        reprocess_ex_rights_stocks(today_fmt, div_events)

    # 可选：添加新股
    if args.add_new:
        new_added = add_new_stocks(start_date, end_date, set(codes))
        log.info(f'新股添加完成: 新增 {new_added} 只')

    # 钉钉通知（同步发送）
    try:
        sys.path.insert(0, 'd:/miniqmt_quant')
        from utils.notifier import _do_send
        msg = (
            f'【量化 每日数据已更新】\n'
            f'增量区间: {start_date} ~ {end_date}\n'
            f'成功: {success} 只  失败: {failed} 只\n'
            f'耗时: {elapsed:.1f} 分钟\n'
            f'时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'
        )
        _do_send(msg)
        log.info('钉钉通知已发送')
    except Exception as e:
        log.warning(f'钉钉通知发送失败: {e}')


if __name__ == '__main__':
    main()
