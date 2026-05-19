# V3 策略每日操作清单

> **用途**：每天开盘前、盘中、收盘后的标准操作流程（手动执行版）。
> 最后更新：2026-05-11

---

## 目录

1. [盘前准备（8:30~9:10）](#1-盘前准备)
2. [盘中监控（9:30~15:00）](#2-盘中监控)
3. [收盘后必做事项（15:00~19:30）](#3-收盘后必做事项)
4. [脚本功能速查表](#4-脚本功能速查表)
5. [关键文件速查](#5-关键文件速查)
6. [常见问题排查](#6-常见问题排查)

---

## 1. 盘前准备

> 时间：**8:30 ~ 9:10**

### Step 1｜启动 QMT 客户端

手动启动 XtMiniQmt 并完成登录。  
⚠️ **必须先启动 QMT 再运行任何 python 脚本**，否则实盘引擎连接失败。

---

### Step 2｜一键开盘前诊断

```powershell
cd d:\miniqmt_quant
python market_open_check.py
```

**期望结果：**

| 项目 | 期望 |
|------|------|
| 持仓数量 | 与昨日收盘一致 |
| rebalance_date | 等于昨日交易日 |
| 5min池最新文件日期 | 今日日期（如 `_20260511.csv`） |
| pending_sells | 若非空，今日 9:15 自动竞价卖出 |

---

### Step 3｜启动实盘引擎

```powershell
cd d:\miniqmt_quant
python run_live_v3.py
```

> 程序会自动恢复持仓、加载调仓池，无需手动干预。  
> 保持终端窗口不关闭。

---

### Step 4｜启动 Dashboard（可选，用于盘中监控）

```powershell
cd d:\miniqmt_quant
python run_dashboard.py
# 浏览器访问 http://localhost:8088
```

---

## 2. 盘中监控

> 时间：**9:30 ~ 15:00**

### 正常运行日志标志

| 时间 | 日志关键词 | 说明 |
|------|-----------|------|
| 9:15~9:25 | `[竞价卖出]` | 有 pending_sells 时自动挂单 |
| 9:30~ | `[监控]` / `[扫描]` | 每分钟轮询一次 |
| 随时 | `触发硬止损` / `触发移动止盈` | 止损止盈卖出 |
| 随时 | `买入全量成交` | 买入成功 |
| 14:55 | `[收盘信号]` | 阴跌/移动止盈/时间止损检查 |
| 15:01 | `退出主循环` | 正常收盘 |

### 需要人工干预的场景

| 场景 | 操作 |
|------|------|
| 日志停止输出 / 连接失败 | 重启 QMT 客户端 → 重启 `run_live_v3.py` |
| 启动时打印 `持仓核对警告` | 查看日志，通常自动同步，观察即可 |

---

## 3. 收盘后必做事项

> 时间：**15:05 ~ 19:30**，建议 **19:00 之后执行**（baostock 约 18:00 后数据完整）

---

### Step 1｜增量更新日线数据（每日必做）

**脚本**：`update_daily_data.py`  
**功能**：自动检测本地 `D:/daily_data` 最新日期，只拉取缺失区间，增量写入当日最新日线数据。

```powershell
cd d:\miniqmt_quant
python update_daily_data.py
```

**预期输出**：打印增量区间（如 `20260508~20260511`），完成后打印更新股票数量。

> ⚠️ 如果增量更新失败（baostock 网络异常），可使用区间补丁脚本：
> ```powershell
> python patch_daily_data_from_20251101.py --full-market --patch-start 20260501 --end-date 20260511
> ```

---

### Step 2｜刷新调仓池 + 生成次日 5min 预缓存（每日必做）

**脚本**：`init_rebalance_pool.py`  
**功能**：
- 基于最新日线数据，用 B+A 策略（MA20 过滤 + 信号质量排名）计算次日调仓池 Top50
- 生成次日所有候选股的 5 分钟 K 线预缓存，存入 `5min_next_pool/`，文件名带次日交易日日期（如 `_20260512.csv`）
- 更新 `state_v3_rebalance.json`

```powershell
cd d:\miniqmt_quant
python init_rebalance_pool.py
```

> 如果日线数据已是最新（Step 1 刚跑完），也可加 `--skip-update` 跳过内部更新步骤：
> ```powershell
> python init_rebalance_pool.py --skip-update
> ```

**预期输出**：调仓池 xx 只（正常约 40~50 只），5min 预缓存 `成功=xx 无数据=x 失败=0`。

---

### Step 3｜增量更新 5 分钟 K 线（每日建议做）

**脚本**：`update_5min_incremental.py`  
**功能**：增量拉取 `D:/5min_data` 中所有股票最新一天的 5 分钟 K 线，追加写入。

```powershell
cd d:\miniqmt_quant
python update_5min_incremental.py
```

---

### Step 4｜收盘后验证

```powershell
cd d:\miniqmt_quant
python -c "
import json, os, glob
from datetime import date

with open('state_v3_rebalance.json') as f:
    p = json.load(f)
print('调仓池数量:', len(p.get('pool',[])))
print('rebalance_date:', p.get('rebalance_date'))

files = glob.glob('5min_next_pool/*.csv')
if files:
    latest = max(files, key=os.path.getmtime)
    print('5min_next_pool 最新文件:', os.path.basename(latest))
else:
    print('5min_next_pool: 无文件！')
"
```

---

## 4. 脚本功能速查表

| 脚本 | 功能说明 | 执行时机 |
|------|---------|---------|
| `run_live_v3.py` | **实盘引擎主程序**，负责买卖信号执行、止损止盈、条件单管理，收盘后自动触发调仓池刷新 | 每日 9:10 前启动，15:00 自动退出 |
| `run_dashboard.py` | **可视化监控面板**，浏览器访问 http://localhost:8088，查看持仓/候选池/成交记录 | 随时启动，可不开 |
| `update_daily_data.py` | **日线增量更新**，自动检测缺口，拉取 baostock 前复权日线数据写入 `D:/daily_data` | 每日收盘后 19:00+ |
| `patch_daily_data_from_20251101.py` | **日线区间补丁**，支持 `--patch-start` 指定起始日，`--full-market` 全量 A 股（5500+），用于修复历史数据 | 按需手动执行 |
| `init_rebalance_pool.py` | **调仓池计算 + 5min 预缓存生成**，B+A 选股，输出次日候选池，自动下载候选股的 5min K 线到 `5min_next_pool/` | 每日收盘后 19:00+ |
| `update_5min_incremental.py` | **5 分钟 K 线增量更新**，追加当日最新 5min 数据到 `D:/5min_data` | 每日收盘后 |
| `download_5min_data.py` | **5 分钟 K 线全量下载**，支持 `--all`（全量股票）和 `--start YYYY-MM-DD`（指定起始日），用于初始化或重建 5min 数据 | 按需手动执行 |

---

## 5. 关键文件速查

| 文件 | 作用 | 更新时机 |
|------|------|---------|
| `state_v3.json` | 实盘持仓/资金/状态 | 每5分钟心跳 + 收盘15:00 |
| `state_v3_rebalance.json` | 调仓池（Top 50）+ rebalance_date | 每日收盘后 19:00+ |
| `5min_next_pool/*.csv` | 次日买入候选股的 5min K 线缓存 | 随调仓池一起生成，文件名含次日交易日日期 |
| `trades_v3.json` | 历史成交记录 | 每次成交时追加 |
| `failed_orders_v3.json` | 失败/超时订单，用于复盘 | 每次失败时追加 |
| `params_v3.json` | 热重载策略参数（优先级高于 config.py） | 手动修改 |
| `config.py` | 基础策略参数（兜底） | 代码修改时更新 |
| `D:/daily_data/SH|SZ/price_{code}.csv` | 全量 A 股日线数据（前复权） | 每日 update_daily_data.py 增量更新 |
| `D:/5min_data/SH|SZ/{code}.csv` | 全量股票 5 分钟 K 线 | 每日 update_5min_incremental.py 增量更新 |

---

## 6. 常见问题排查

### 6.1 miniQMT 连接失败

```
[错误] 连接 miniQMT 失败 / TradeExecutor connect 超时
```

**原因**：QMT 客户端未启动或未登录。  
**处理**：先启动 QMT 客户端完成登录，再重启 `run_live_v3.py`。  
⚠️ 禁止通过脚本 Kill XtMiniQmt 进程！会导致登录状态丢失。

---

### 6.2 调仓池只有几只（远少于正常的 40~50 只）

**原因**：`D:/daily_data` 数据不完整，MA20 过滤所需的历史数据缺失。  
**处理**：

1. 检查数据范围：
```powershell
python -c "import pandas as pd; df=pd.read_csv('D:/daily_data/SH/price_600000.csv'); print(df['timetag'].min(), df['timetag'].max())"
```
2. 若最早日期晚于 `2025-11-01`（不足 120 交易日），需补历史数据：
```powershell
cd d:\miniqmt_quant
python patch_daily_data_from_20251101.py --full-market --patch-start 20251101
```
3. 补完后重跑调仓池：
```powershell
python init_rebalance_pool.py --skip-update
```

---

### 6.3 5min_next_pool 无今日日期文件

**原因**：`init_rebalance_pool.py` 未在今日执行，或 baostock 当日数据未发布。  
**处理**：

```powershell
cd d:\miniqmt_quant
# 19:00 后执行
python init_rebalance_pool.py
```

---

### 6.4 日线增量更新失败（baostock 限速或网络异常）

**处理**：等 10 分钟后重跑，或改用区间补丁（从上次成功的日期开始）：

```powershell
cd d:\miniqmt_quant
python patch_daily_data_from_20251101.py --full-market --patch-start 20260501
```

---

### 6.5 收盘后脚本没自动跑（run_live_v3.py 中途退出）

手动补全收盘后任务（19:00 后执行）：

```powershell
cd d:\miniqmt_quant
python update_daily_data.py
python init_rebalance_pool.py
python update_5min_incremental.py
```

---

### 6.6 state_v3.json 损坏

```powershell
cd d:\miniqmt_quant
# 备份损坏文件
Copy-Item state_v3.json state_v3.json.bak
# 联系排查，或从最近的备份恢复
```

---

*文档结束*
