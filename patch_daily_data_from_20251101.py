# -*- coding: utf-8 -*-
"""
patch_daily_data_from_20251101.py
==================================
针对性脚本：重新下载 2025-11-01 至今的所有股票前复权日线数据，
用新数据覆盖 D:/daily_data/ 中对应日期区间的旧数据（前复权修正）。

流程：
  1. 先将新数据下载到临时目录  D:/daily_data_patch_20251101_{today}/
  2. 对每只股票：截断旧CSV中 >= 20251101 的行，再追加新数据
  3. 打印摘要统计

用法：
  python patch_daily_data_from_20251101.py           # 下载 + 应用补丁
  python patch_daily_data_from_20251101.py --dry-run # 只下载，不写入主目录
  python patch_daily_data_from_20251101.py --apply-only  # 跳过下载，直接应用已有临时目录
"""

import os, sys, time, random, argparse, glob
from datetime import date, datetime

import pandas as pd

# ── 配置 ─────────────────────────────────────────────────────────────────────
PATCH_START   = '20251101'          # 补丁起始日（含）
MAIN_DIR      = 'D:/daily_data'     # 主数据目录
SH_DIR        = os.path.join(MAIN_DIR, 'SH')
SZ_DIR        = os.path.join(MAIN_DIR, 'SZ')

TODAY_STR     = date.today().strftime('%Y%m%d')
PATCH_DIR     = f'D:/daily_data_patch_{PATCH_START}_{TODAY_STR}'   # 临时目录
PATCH_SH_DIR  = os.path.join(PATCH_DIR, 'SH')
PATCH_SZ_DIR  = os.path.join(PATCH_DIR, 'SZ')

CSV_COLS      = ['timetag', 'open', 'high', 'low', 'close', 'volumn', 'amount']

DELAY_MIN     = 0.05
DELAY_MAX     = 0.15

# ── baostock 工具 ─────────────────────────────────────────────────────────────
_bs_logged_in = False

def _bs_login():
    global _bs_logged_in
    if not _bs_logged_in:
        import baostock as bs
        ret = bs.login()
        if ret.error_code != '0':
            raise RuntimeError(f'baostock 登录失败: {ret.error_msg}')
        _bs_logged_in = True

def _bs_logout():
    global _bs_logged_in
    if _bs_logged_in:
        import baostock as bs
        bs.logout()
        _bs_logged_in = False


def fetch_bs(code: str, start: str, end: str) -> pd.DataFrame:
    """
    从 baostock 下载单只股票前复权日线。
    start/end 格式 YYYYMMDD。返回与 CSV_COLS 对齐的 DataFrame。
    """
    import baostock as bs
    def _fmt(d): return f'{d[:4]}-{d[4:6]}-{d[6:]}'

    bs_code = f'sh.{code}' if code.startswith('6') else f'sz.{code}'

    for attempt in range(1, 4):
        try:
            _bs_login()
            rs = bs.query_history_k_data_plus(
                code=bs_code,
                fields='date,open,high,low,close,volume,amount',
                start_date=_fmt(start),
                end_date=_fmt(end),
                frequency='d',
                adjustflag='2',   # 前复权
            )
            if rs.error_code != '0':
                raise RuntimeError(f'baostock 查询错误: {rs.error_msg}')

            rows = []
            while rs.next():
                rows.append(rs.get_row_data())

            if not rows:
                return pd.DataFrame()

            df = pd.DataFrame(rows, columns=rs.fields)
            df['timetag'] = df['date'].str.replace('-', '', regex=False).astype(int)
            for col in ['open', 'high', 'low', 'close', 'volume', 'amount']:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)
            df['volumn'] = df['volume'] / 100.0   # 股 → 手
            return df[CSV_COLS].copy()

        except Exception as e:
            global _bs_logged_in
            _bs_logged_in = False
            if attempt < 3:
                time.sleep(1.0 * attempt + random.random())
            else:
                print(f'  [{code}] 下载失败（重试3次）: {e}')
    return pd.DataFrame()


# ── 获取本地所有股票代码 ───────────────────────────────────────────────────────
def get_all_local_codes() -> list:
    codes = []
    for sub in [SH_DIR, SZ_DIR]:
        if not os.path.exists(sub):
            continue
        for fname in os.listdir(sub):
            if fname.startswith('price_') and fname.endswith('.csv'):
                code = fname[len('price_'):-len('.csv')]
                if os.path.getsize(os.path.join(sub, fname)) > 200:
                    codes.append(code)
    return sorted(codes)


# ── 从 baostock 获取全量 A 股代码 ─────────────────────────────────────────────
def get_all_market_codes() -> list:
    """
    从 baostock 获取全量沪深 A 股代码列表（排除北交所 8/4 开头）。
    约 5000+ 只，包含主板/创业板/科创板。
    """
    import baostock as bs
    _bs_login()
    rs = bs.query_stock_basic(code_name='')
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    df = pd.DataFrame(rows, columns=rs.fields)
    # type='1'：上市A股；排除北交所（8/4开头）
    df = df[df['type'] == '1']
    codes = df['code'].apply(lambda x: x.split('.')[-1]).tolist()
    codes = [c for c in codes
             if not c.startswith('8') and not c.startswith('4')
             and (c.startswith('6') or c.startswith('0') or c.startswith('3'))]
    print(f'  baostock 全量A股: {len(codes)} 只')
    return sorted(codes)


# ── 主目录路径 ────────────────────────────────────────────────────────────────
def _main_path(code: str) -> str:
    sub = SH_DIR if code.startswith('6') else SZ_DIR
    return os.path.join(sub, f'price_{code}.csv')

def _patch_path(code: str) -> str:
    sub = PATCH_SH_DIR if code.startswith('6') else PATCH_SZ_DIR
    return os.path.join(sub, f'price_{code}.csv')


# ── 步骤1：下载到临时目录 ───────────────────────────────────────────────────────
def step1_download(codes: list, end_date: str):
    os.makedirs(PATCH_SH_DIR, exist_ok=True)
    os.makedirs(PATCH_SZ_DIR, exist_ok=True)
    total   = len(codes)
    ok      = 0
    skipped = 0
    failed  = 0

    print(f'\n[步骤1] 下载 {total} 只股票的日线数据 ({PATCH_START} ~ {end_date})')
    print(f'  临时目录: {PATCH_DIR}')

    for i, code in enumerate(codes, 1):
        if i % 200 == 0 or i == 1 or i == total:
            print(f'  进度: {i}/{total}  ok={ok} skip={skipped} fail={failed}')

        out = _patch_path(code)
        # 已下载则跳过（支持断点续传）
        if os.path.exists(out) and os.path.getsize(out) > 100:
            skipped += 1
            time.sleep(0.01)
            continue

        df = fetch_bs(code, PATCH_START, end_date)
        if df.empty:
            skipped += 1
        else:
            df.to_csv(out, index=False)
            ok += 1

        time.sleep(DELAY_MIN + random.random() * (DELAY_MAX - DELAY_MIN))

    print(f'  下载完成: ok={ok}, 跳过(已存在/无数据)={skipped}, 失败={failed}')


# ── 步骤2：应用补丁到主目录 ────────────────────────────────────────────────────
def step2_apply(codes: list):
    PATCH_START_INT = int(PATCH_START)
    total   = len(codes)
    ok      = 0
    skipped = 0
    failed  = 0

    print(f'\n[步骤2] 将补丁应用到主目录 {MAIN_DIR}')
    print(f'  规则：截断主目录中 timetag >= {PATCH_START} 的行，追加新数据')

    for i, code in enumerate(codes, 1):
        if i % 500 == 0 or i == 1 or i == total:
            print(f'  进度: {i}/{total}  ok={ok} skip={skipped} fail={failed}')

        patch_f = _patch_path(code)
        main_f  = _main_path(code)

        if not os.path.exists(patch_f):
            skipped += 1
            continue

        try:
            new_df = pd.read_csv(patch_f)
            if new_df.empty:
                skipped += 1
                continue

            if os.path.exists(main_f):
                old_df = pd.read_csv(main_f)
                # 截断：只保留 timetag < 20251101 的行
                old_df = old_df[pd.to_numeric(old_df['timetag'], errors='coerce') < PATCH_START_INT]
                merged = pd.concat([old_df, new_df], ignore_index=True)
            else:
                merged = new_df

            merged = merged.drop_duplicates(subset=['timetag']).sort_values('timetag').reset_index(drop=True)
            merged.to_csv(main_f, index=False)
            ok += 1

        except Exception as e:
            print(f'  [{code}] 应用失败: {e}')
            failed += 1

    print(f'  应用完成: ok={ok}, 跳过={skipped}, 失败={failed}')


# ── 入口 ──────────────────────────────────────────────────────────────────────
def main():
    global PATCH_START, PATCH_DIR, PATCH_SH_DIR, PATCH_SZ_DIR  # 允许动态覆盖

    ap = argparse.ArgumentParser(description='日线数据补丁：下载指定区间的前复权日线数据，覆盖主目录对应区间')
    ap.add_argument('--patch-start',  default=PATCH_START,  help=f'补丁起始日 YYYYMMDD（默认{PATCH_START}）')
    ap.add_argument('--dry-run',      action='store_true',  help='只下载到临时目录，不写入主目录')
    ap.add_argument('--apply-only',   action='store_true',  help='跳过下载，直接应用临时目录中已有数据')
    ap.add_argument('--full-market',  action='store_true',  help='从baostock拉全量A股列表（约5000+只），而非只更新本地已有股票')
    ap.add_argument('--end-date',     default=TODAY_STR,    help=f'补丁截止日 YYYYMMDD（默认今天 {TODAY_STR}）')
    args = ap.parse_args()

    # 动态覆盖全局配置（支持 --patch-start）
    PATCH_START  = args.patch_start
    PATCH_DIR    = f'D:/daily_data_patch_{PATCH_START}_{args.end_date}'
    PATCH_SH_DIR = os.path.join(PATCH_DIR, 'SH')
    PATCH_SZ_DIR = os.path.join(PATCH_DIR, 'SZ')

    print('=' * 60)
    print(f'  日线数据补丁脚本')
    print(f'  补丁区间  : {PATCH_START} ~ {args.end_date}')
    print(f'  临时目录  : {PATCH_DIR}')
    print(f'  主目录    : {MAIN_DIR}')
    mode_str = "dry-run（仅下载）" if args.dry_run else "apply-only（仅应用）" if args.apply_only else "下载 + 应用"
    src_str  = "全量A股（baostock）" if args.full_market else "本地已有股票（2658只）"
    print(f'  模式      : {mode_str}')
    print(f'  股票来源  : {src_str}')
    print('=' * 60)

    # 确定股票列表
    if args.apply_only:
        # apply-only 模式：从临时目录读取已有文件列表
        codes = []
        for sub in [PATCH_SH_DIR, PATCH_SZ_DIR]:
            if not os.path.exists(sub):
                continue
            for fname in os.listdir(sub):
                if fname.startswith('price_') and fname.endswith('.csv'):
                    codes.append(fname[len('price_'):-len('.csv')])
        codes = sorted(codes)
        print(f'临时目录中已有: {len(codes)} 只')
    elif args.full_market:
        print('正在从 baostock 获取全量A股列表...')
        codes = get_all_market_codes()
    else:
        codes = get_all_local_codes()
        print(f'本地股票总数: {len(codes)} 只')

    t0 = time.time()

    if not args.apply_only:
        step1_download(codes, args.end_date)
        _bs_logout()

    if not args.dry_run:
        # apply 时用临时目录中实际存在的文件
        if not args.apply_only:
            apply_codes = codes
        else:
            apply_codes = codes
        step2_apply(apply_codes)

    elapsed = (time.time() - t0) / 60
    print(f'\n全部完成，耗时 {elapsed:.1f} 分钟')
    if not args.dry_run:
        print(f'已更新主目录: {MAIN_DIR}（{PATCH_START} ~ {args.end_date} 区间数据已覆盖）')
    else:
        print(f'dry-run 模式：临时目录已就绪，如需应用请运行:')
        print(f'  python patch_daily_data_from_20251101.py --apply-only')


if __name__ == '__main__':
    main()
