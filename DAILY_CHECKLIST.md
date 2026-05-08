# V3 策略每日操作清单

> **用途**：每天开盘前、盘中、收盘后的标准操作流程。  
> 下次可以直接说"按照 DAILY_CHECKLIST.md 流程检查开盘前准备"。  
> 上次更新：2026-05-06

---

## 目录

1. [盘前准备（8:30~9:10）](#1-盘前准备830910)
2. [盘中监控（9:30~15:00）](#2-盘中监控9301500)
3. [收盘后必做事项（15:00~19:30）](#3-收盘后必做事项150019:30)
4. [开盘前快速检查命令](#4-开盘前快速检查命令)
5. [常见问题排查](#5-常见问题排查)
6. [状态快照：今日（2026-05-06 收盘后）](#6-状态快照今日2026-05-06-收盘后)

---

## 1. 盘前准备（8:30~9:10）

### 1.1 检查清单（逐项确认）

| # | 检查项 | 命令 / 方法 | 期望结果 |
|---|--------|------------|---------|
| □ | QMT 客户端已启动并登录 | 手动查看桌面 | XtMiniQmt 进程运行中 |
| □ | state_v3.json 正常 | `python run_live_v3.py --status` | 打印持仓/资金信息无报错 |
| □ | 调仓池已更新到昨日 | 检查 `state_v3_rebalance.json` 的 `rebalance_date` | 等于昨日交易日日期 |
| □ | 5分钟线数据已就绪 | 检查 `5min_next_pool/` 文件日期 | 最新文件日期=昨日交易日 |
| □ | pending_sells 是否有任务 | 查看 `state_v3.json` 中 `pending_sells` 字段 | 若非空则今日 9:15 将自动挂卖单 |
| □ | 无旧进程残留 | `Get-Process python -ErrorAction SilentlyContinue` | 无残留 python 进程 |

### 1.2 盘前快速状态命令

```powershell
cd d:\miniqmt_quant

# 查看持仓/资金/调仓池概览
python run_live_v3.py --status

# 查看 state_v3.json 关键字段
python -c "
import json
with open('state_v3.json') as f:
    s = json.load(f)
print('持仓:', len(s.get('positions',[])), '只')
print('现金:', s.get('cash'))
print('pending_sells:', s.get('pending_sells', []))
print('last_update:', s.get('last_update'))
"

# 查看调仓池更新日期
python -c "
import json
with open('state_v3_rebalance.json') as f:
    p = json.load(f)
print('调仓池数量:', len(p.get('pool',[])))
print('rebalance_date:', p.get('rebalance_date'))
print('策略:', p.get('strategy'))
"
```

### 1.3 启动实盘

```powershell
cd d:\miniqmt_quant
python run_live_v3.py
```

> **注意**：启动前务必确认 QMT 客户端已登录，否则 miniQMT 连接失败。  
> 程序启动后自动执行恢复、加载调仓池，不需要手动干预。

---

## 2. 盘中监控（9:30~15:00）

### 2.1 程序正常运行时的日志标志

| 时间 | 正常日志关键词 | 说明 |
|------|--------------|------|
| 9:15~9:25 | `[竞价卖出]` | 若有 pending_sells 则自动挂单 |
| 9:25~9:30 | `[竞价检查]` | 检查竞价成交结果 |
| 9:30~ | `[监控]` / `[扫描]` | 每分钟打印一轮 |
| 随时 | `触发硬止损` / `触发移动止盈` | 止损止盈卖出 |
| 随时 | `买入全量成交` / `买入部分成交` | 买入成功 |
| 14:55 | `[收盘信号]` | 阴跌/移动止盈/时间止损检查 |
| 15:01 | `退出主循环` | 正常收盘 |

### 2.2 需要人工干预的场景

| 场景 | 判断方法 | 操作 |
|------|---------|------|
| 连接断开 | 日志停止输出 / `连接失败` | 重启 QMT 客户端，再重启 `run_live_v3.py` |
| 手动买入了额外股票 | 券商持仓 > 策略持仓 | 日志打印警告，无需干预，策略不管理手动买入的持仓 |
| 异常持仓差异 | 启动时日志 `持仓核对警告` | 查看日志，判断是否需要手动同步 |

### 2.3 Dashboard 查看（可选）

```powershell
cd d:\miniqmt_quant
python run_dashboard.py
# 浏览器打开 http://localhost:5000
```

---

## 3. 收盘后必做事项（15:00~19:30）

> **好消息**：`run_live_v3.py` 收盘后自动执行以下所有步骤，通常**无需手动操作**。  
> 但若程序中途退出，需要手动补跑。

### 3.1 自动流程（由 run_live_v3.py 驱动）

```
15:01  _save_state               → 保存 state_v3.json
15:01  track_pool_performance    → 追踪今日调仓池涨跌
       等待至 19:00              → baostock 日线数据约 18:00 后完整
19:00+ init_rebalance_pool.main → 刷新调仓池（含增量更新日线数据）
       update_5min_incremental  → 增量更新5分钟线（写哨兵文件）
```

### 3.2 手动补跑命令（若自动流程失败）

```powershell
cd d:\miniqmt_quant

# 1. 手动刷新调仓池（须在 19:00 后执行，等 baostock 数据出来）
python init_rebalance_pool.py

# 2. 手动增量更新5分钟线
python update_5min_incremental.py

# 3. 验证调仓池是否更新
python -c "
import json
with open('state_v3_rebalance.json') as f:
    p = json.load(f)
print('rebalance_date:', p.get('rebalance_date'))
print('pool size:', len(p.get('pool',[])))
"
```

### 3.3 收盘后检查清单

| # | 检查项 | 期望结果 |
|---|--------|---------|
| □ | state_v3.json last_update 是今日 | 格式 `今日日期 15:0x:xx` |
| □ | state_v3_rebalance.json rebalance_date 是今日 | 等于今日交易日 |
| □ | 5min_next_pool/ 最新文件日期是今日 | 50个CSV，日期=今日 |
| □ | 5min哨兵文件存在 | `.5min_incremental_done_YYYYMMDD` 存在 |
| □ | trades_v3.json 有今日成交记录（若有成交） | `buy_date` / `sell_date` 为今日 |

---

## 4. 开盘前快速检查命令

> 把这段作为每次开盘前的"一键诊断"，让 AI 按此命令执行并报告结果。

```powershell
cd d:\miniqmt_quant

# ── 一键开盘前诊断 ──────────────────────────────
python -c "
import json, os, glob
from datetime import datetime, date

print('='*55)
print('开盘前诊断报告', datetime.now().strftime('%Y-%m-%d %H:%M'))
print('='*55)

# 1. state_v3.json
with open('state_v3.json') as f:
    s = json.load(f)
print(f'[持仓] {len(s[\"positions\"])} 只 | 现金 {s[\"cash\"]:,.2f}')
print(f'[状态更新] {s[\"last_update\"]}')
ps = s.get('pending_sells', [])
if ps:
    print(f'[待卖] {len(ps)} 只 -> {[p[\"code\"] for p in ps]}  ← 今日9:15自动挂竞价卖单')
else:
    print('[待卖] 无 pending_sells')

# 2. 调仓池
with open('state_v3_rebalance.json') as f:
    pool = json.load(f)
print(f'[调仓池] {len(pool[\"pool\"])} 只 | rebalance_date={pool.get(\"rebalance_date\")}')

# 3. 5min_next_pool
files = glob.glob('5min_next_pool/*.csv')
if files:
    latest = max(files, key=os.path.getmtime)
    mtime = datetime.fromtimestamp(os.path.getmtime(latest)).strftime('%Y-%m-%d %H:%M')
    print(f'[5min池] {len(files)} 只 | 最新={os.path.basename(latest)} ({mtime})')
else:
    print('[5min池] !! 无文件，需补跑 init_rebalance_pool.py !!')

# 4. 哨兵文件
today_str = date.today().strftime('%Y%m%d')
sentinel = f'd:/miniqmt_quant/.5min_incremental_done_{today_str}'
print(f'[5min增量] 哨兵文件 {\"OK\" if os.path.exists(sentinel) else \"缺失（可补跑）\"}')

# 5. 持仓 T+1 提示
for pos in s.get('positions', []):
    if pos.get('days_held', 1) == 0:
        print(f'[T+1] {pos[\"code\"]} 今日买入，明日才能卖出')

print('='*55)
"
```

---

## 5. 常见问题排查

### 5.1 miniQMT 连接失败

```
[错误] 连接 miniQMT 失败 / TradeExecutor connect 超时
```

**原因**：QMT 客户端未启动或未登录。  
**处理**：先启动 QMT 客户端并完成登录，再重启 `run_live_v3.py`。

> ⚠️ 禁止通过脚本 Kill XtMiniQmt 进程！会导致登录状态丢失。

### 5.2 调仓池未更新（rebalance_date 不是今日）

```powershell
cd d:\miniqmt_quant
# 等 19:00 后执行（baostock 数据约 18:00 后发布）
python init_rebalance_pool.py
```

### 5.3 5min_next_pool 数据过期

```powershell
# 手动重新生成 5min 候选池
python init_rebalance_pool.py
# 会同时更新 state_v3_rebalance.json 和 5min_next_pool/*.csv
```

### 5.4 持仓与券商不一致（启动时警告）

| 情况 | 说明 | 处理 |
|------|------|------|
| 券商持仓 > 策略持仓 | 可能有手动买入 | 无需处理，策略只管理自己的持仓 |
| 券商持仓 < 策略持仓 | 条件单已成交/手动卖出 | 引擎自动清理，关注日志确认同步正确 |
| 券商返回空持仓 | API 异常 | 引擎保守跳过，不清仓，重新查询 |

### 5.5 state_v3.json 损坏

```powershell
# 备份损坏文件
Copy-Item state_v3.json state_v3.json.bak

# 从 trades_v3.json 手动重建（紧急情况）
# 或使用上次备份
```

### 5.6 程序未到 19:00 就退出（收盘后自动任务未完成）

```powershell
# 手动补全收盘后任务
cd d:\miniqmt_quant
python init_rebalance_pool.py        # 更新调仓池
python update_5min_incremental.py    # 更新5min线（如有此脚本）
```

---

## 6. 状态快照：今日（2026-05-06 收盘后）

> 每次收盘后更新此节。

| 项目 | 状态 | 详情 |
|------|------|------|
| 持仓 | 3/3 满仓 | 600331 (3500股)、600773 (2400股)、603124 (500股) |
| 现金 | 4,161.05 | 基本满仓 |
| pending_sells | 无 | 今日 9:15 无竞价卖单任务 |
| 调仓池 | ✅ 已更新 | 50只，rebalance_date=2026-05-06 |
| 5min_next_pool | ✅ 已生成 | 50个CSV，19:03 更新 |
| 5min增量哨兵 | ⚠️ 缺失 | 不影响开盘，可手动补：`python update_5min_incremental.py` |
| 明日注意 | T+1 限制 | 3支均为今日买入，明日 days_held=1，**不触发 T+1 限制**（T+1 仅限 days_held=0 当日不卖） |

### 明日（2026-05-07）预期行为

- **9:15**：无 pending_sells，竞价阶段静默
- **9:30~**：监控3支持仓股止损止盈（持仓满，不扫描买入）
- **9:30~**：若有止损/止盈触发 → 卖出后持仓<3 → 自动扫描买入
- **14:55**：检查阴跌/时间止损（3支均 days_held=1，时间止损不触发）

---

## 附录：关键文件速查

| 文件 | 作用 | 更新时机 |
|------|------|---------|
| `state_v3.json` | 持仓/资金状态 | 每5分钟心跳 + 收盘15:00 |
| `state_v3_rebalance.json` | 调仓池（Top 50） | 每日收盘后 19:00+ |
| `5min_next_pool/*.csv` | 次日买入候选5min线 | 随调仓池一起生成 |
| `trades_v3.json` | 历史成交记录 | 每次成交时追加 |
| `failed_orders_v3.json` | 失败/超时订单复盘 | 每次失败时追加 |
| `params_v3.json` | 热重载参数（优先级高于 config.py） | 手动修改 |
| `config.py` | 基础参数（兜底） | 代码提交时修改 |
| `.5min_incremental_done_YYYYMMDD` | 5min增量更新完成哨兵 | 增量更新完成后写入 |
