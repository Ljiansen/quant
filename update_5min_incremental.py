#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_5min_incremental.py

5分钟线增量更新脚本：
  - 扫描 D:/5min_data/SH/ 和 D:/5min_data/SZ/ 下所有已存在的文件
  - 读取每只股票文件的最后日期
  - 仅下载"最后日期+1天 ~ 今天"的新数据，追加到文件末尾
  - 对新股（文件不存在）从 NEW_STOCK_START 开始全量下载

用法:
    python update_5min_incremental.py          # 增量更新所有已有文件
    python update_5min_incremental.py --full   # 强制全量重新下载（覆盖）
"""

import os
import sys
import time
import datetime
import traceback
import socket
import threading
import queue
import argparse

import pandas as pd
import baostock as bs

# ─── 配置 ─────────────────────────────────────────────────────────────────────
FIVEMIN_DIR     = 'D:/5min_data'
DAILY_DATA_DIR  = 'D:/daily_data'
NEW_STOCK_START = '2025-01-01'       # 新股（无5分钟数据）的下载起点
MAX_RETRIES     = 3
DL_TIMEOUT_SEC  = 120
REQUEST_DELAY   = 0.15
LOG_FILE        = os.path.join(FIVEMIN_DIR, 'incremental_log.txt')
# ─────────────────────────────────────────────────────────────────────────────


def _log(msg: str, also_print=True):
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    if also_print:
        print(line, flush=True)
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def _bs_code(code: str) -> str:
    return f'sh.{code}' if code.startswith('6') else f'sz.{code}'


def _fivemin_path(code: str) -> str:
    sub = 'SH' if code.startswith('6') else 'SZ'
    return os.path.join(FIVEMIN_DIR, sub, f'{code}.csv')


def _get_last_date(fpath: str) -> str | None:
    """读取文件最后一行的 date 字段，返回 'YYYY-MM-DD'，失败返回 None"""
    try:
        df = pd.read_csv(fpath, usecols=['date'])
        if df.empty:
            return None
        last = str(df['date'].iloc[-1]).strip()
        # 格式可能是 'YYYY-MM-DD' 或 'YYYYMMDD'
        if len(last) == 8 and last.isdigit():
            last = f'{last[:4]}-{last[4:6]}-{last[6:]}'
        return last[:10]
    except Exception:
        return None


def _next_day(date_str: str) -> str:
    """返回 date_str 的下一天，格式 YYYY-MM-DD"""
    d = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
    return (d + datetime.timedelta(days=1)).strftime('%Y-%m-%d')


def _download_one_raw(code: str, start: str, end: str) -> pd.DataFrame:
    fields = 'date,time,code,open,high,low,close,volume,amount,adjustflag'
    rs = bs.query_history_k_data_plus(
        code=_bs_code(code),
        fields=fields,
        start_date=start,
        end_date=end,
        frequency='5',
        adjustflag='2',
    )
    if rs.error_code != '0':
        return pd.DataFrame()

    rows = []
    while rs.error_code == '0' and rs.next():
        rows.append(rs.get_row_data())
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=rs.fields)
    for col in ('open', 'high', 'low', 'close', 'volume', 'amount'):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
    keep = [c for c in ('date', 'time', 'open', 'high', 'low', 'close', 'volume', 'amount')
            if c in df.columns]
    return df[keep]


def _download_one(code: str, start: str, end: str) -> pd.DataFrame:
    """带超时 + 重试的下载封装"""
    for attempt in range(MAX_RETRIES):
        result_q = queue.Queue()

        def _worker():
            try:
                result_q.put(('ok', _download_one_raw(code, start, end)))
            except Exception as exc:
                result_q.put(('err', exc))

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        t.join(DL_TIMEOUT_SEC)

        if t.is_alive():
            _log(f'  [超时] {code} 第{attempt+1}次超时，重连baostock...')
            try:
                bs.logout()
            except Exception:
                pass
            time.sleep(3)
            try:
                bs.login()
            except Exception:
                pass
            time.sleep(2)
            if attempt < MAX_RETRIES - 1:
                continue
            return pd.DataFrame()

        status, data = result_q.get()
        if status == 'ok':
            return data
        else:
            _log(f'  [异常] {code} 第{attempt+1}次: {data}')
            if attempt < MAX_RETRIES - 1:
                time.sleep(5)
                try:
                    bs.logout(); bs.login()
                except Exception:
                    pass
    return pd.DataFrame()


def get_all_local_codes() -> list:
    """扫描 D:/daily_data 返回所有股票代码"""
    codes = []
    for sub in ('SH', 'SZ'):
        d = os.path.join(DAILY_DATA_DIR, sub)
        if not os.path.exists(d):
            continue
        for fname in os.listdir(d):
            if fname.startswith('price_') and fname.endswith('.csv'):
                code = fname[len('price_'):-len('.csv')]
                if os.path.getsize(os.path.join(d, fname)) > 200:
                    codes.append(code)
    return sorted(codes)


def get_existing_codes() -> list:
    """扫描 D:/5min_data 返回已有5分钟数据的股票代码"""
    codes = []
    for sub in ('SH', 'SZ'):
        d = os.path.join(FIVEMIN_DIR, sub)
        if not os.path.exists(d):
            continue
        for fname in os.listdir(d):
            if fname.endswith('.csv'):
                code = fname[:-4]
                fpath = os.path.join(d, fname)
                if os.path.getsize(fpath) > 200:
                    codes.append(code)
    return sorted(codes)


def run_incremental(force_full=False):
    """主流程：增量更新所有已有文件，新增文件全量下载"""
    # 在运行时获取当前日期（而非模块导入时），避免跨日运行时日期错误
    today_str = datetime.date.today().strftime('%Y-%m-%d')
    for sub in ('SH', 'SZ'):
        os.makedirs(os.path.join(FIVEMIN_DIR, sub), exist_ok=True)

    _log('=' * 60)
    _log(f'5分钟线增量更新任务启动  目标日期: {today_str}')
    _log(f'  force_full={force_full}')
    _log('=' * 60)

    existing_codes = set(get_existing_codes())
    _log(f'已有5分钟数据: {len(existing_codes)} 只')

    # 登录 baostock
    lg = bs.login()
    if lg.error_code != '0':
        _log(f'[错误] baostock登录失败: {lg.error_msg}')
        return

    _log('[baostock] 登录成功，开始增量更新...')

    done = skip = fail = 0
    total = len(existing_codes)

    try:
        for i, code in enumerate(sorted(existing_codes), 1):
            fpath = _fivemin_path(code)

            if force_full:
                # 强制全量：删除旧文件重下
                start_date = NEW_STOCK_START
                write_mode = 'overwrite'
            else:
                # 增量：读最后日期
                last_date = _get_last_date(fpath)
                if last_date is None:
                    start_date = NEW_STOCK_START
                    write_mode = 'overwrite'
                else:
                    start_date = _next_day(last_date)
                    write_mode = 'append'

                # 已是最新，跳过
                if start_date > today_str:
                    skip += 1
                    if i % 100 == 0:
                        _log(f'[进度] {i}/{total}  done={done} skip={skip}')
                    time.sleep(0.01)
                    continue

            df_new = _download_one(code, start_date, today_str)

            if df_new.empty:
                # 无新数据（非交易日或下载失败）
                skip += 1
            else:
                if write_mode == 'append':
                    df_new.to_csv(fpath, mode='a', header=False, index=False)
                    _log(f'  [追加] {code}  {start_date}~{today_str}  +{len(df_new)}行', also_print=False)
                else:
                    df_new.to_csv(fpath, index=False)
                done += 1

            if i % 50 == 0 or i == total:
                pct = i / total * 100
                _log(f'[进度] {i}/{total} ({pct:.1f}%)  done={done} skip={skip}')

            time.sleep(REQUEST_DELAY)

    except KeyboardInterrupt:
        _log('[中断] 用户终止，已完成部分支持重新运行')
    except Exception as e:
        _log(f'[致命错误] {e}')
        _log(traceback.format_exc())
    finally:
        bs.logout()
        _log('[baostock] 已登出')

    _log('=' * 60)
    _log(f'增量更新完成  done={done}  skip={skip}  total={total}')
    _log('=' * 60)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='5分钟线增量更新')
    parser.add_argument('--full', action='store_true', help='强制全量重新下载（覆盖现有文件）')
    args = parser.parse_args()
    run_incremental(force_full=args.full)
