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
import random
import argparse
import logging
from datetime import datetime, timedelta

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

# CSV 列名（与现有本地数据保持一致，volumn 是 xtquant 原始拼写）
CSV_COLS   = ['timetag', 'open', 'high', 'low', 'close', 'volumn', 'amount']

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
    """
    import baostock as bs

    # 日期格式 YYYYMMDD → YYYY-MM-DD
    def _fmt(d): return f'{d[:4]}-{d[4:6]}-{d[6:]}'
    start = _fmt(start_yyyymmdd)
    end   = _fmt(end_yyyymmdd)

    # 股票代码转 baostock 格式
    bs_code = f'sh.{code}' if code.startswith('6') else f'sz.{code}'

    for attempt in range(1, 4):
        try:
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

            if not rows:
                return pd.DataFrame()

            df = pd.DataFrame(rows, columns=rs.fields)

            # 日期 'YYYY-MM-DD' → YYYYMMDD 整数
            df['timetag'] = df['date'].str.replace('-', '', regex=False).astype(int)

            # 数值类型
            for col in ['open', 'high', 'low', 'close', 'volume', 'amount']:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)

            # baostock volume(股) ÷ 100 → volumn(手)，与本地 CSV 保持一致
            df['volumn'] = df['volume'] / 100.0

            df = df[CSV_COLS].copy()
            return df

        except Exception as e:
            global _bs_logged_in
            _bs_logged_in = False  # 下次重新登录
            if attempt < 3:
                time.sleep(1.0 * attempt + random.random())
            else:
                log.debug(f'  {code} 获取失败（已重试3次）: {e}')
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
# 核心更新逻辑
# ──────────────────────────────────────────────────────────────

def update_existing(codes: list[str], start_yyyymmdd: str,
                    end_yyyymmdd: str) -> tuple[int, int, int]:
    """
    批量更新已有股票 CSV 文件。
    返回 (success, skipped, failed)
    """
    total   = len(codes)
    success = 0
    skipped = 0
    failed  = 0

    for i, code in enumerate(codes, 1):
        if i % 100 == 0 or i == 1 or i == total:
            log.info(f'  进度: {i}/{total}  成功={success} 跳过={skipped} 失败={failed}')

        try:
            new_df = fetch_bs_hist(code, start_yyyymmdd, end_yyyymmdd)
            if new_df.empty:
                skipped += 1
            else:
                added = append_new_rows(code, new_df)
                if added > 0:
                    success += 1
                else:
                    skipped += 1
        except Exception as e:
            log.debug(f'  {code} 处理异常: {e}')
            failed += 1

        time.sleep(DELAY_MIN + random.random() * (DELAY_MAX - DELAY_MIN))

    return success, skipped, failed


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

    # 预计耗时提示（baostock 每次约 0.1s）
    est_min = round(len(codes) * ((DELAY_MIN + DELAY_MAX) / 2 + 0.1) / 60, 1)
    log.info(f'预计耗时: ~{est_min} 分钟')

    # baostock 登录
    _bs_ensure_login()
    log.info('baostock 登录成功')

    # 执行增量更新
    t0 = time.time()
    success, skipped, failed = update_existing(codes, start_date, end_date)
    elapsed = (time.time() - t0) / 60

    # baostock 登出
    _bs_logout()

    log.info(f'增量更新完成: 成功={success} 跳过={skipped} 失败={failed}，耗时 {elapsed:.1f} 分钟')

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
