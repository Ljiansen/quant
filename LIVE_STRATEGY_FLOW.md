# V3 策略实盘流程文档

> **维护规范**：每次修改实盘策略逻辑后，必须同步更新本文档对应章节。  
> 最后更新：2026-05-05

---

## 目录

1. [系统架构总览](#1-系统架构总览)
2. [每日完整流程](#2-每日完整流程)
3. [启动与恢复阶段](#3-启动与恢复阶段)
4. [集合竞价阶段 9:15~9:30](#4-集合竞价阶段-915930)
5. [盘中主循环 9:30~15:00](#5-盘中主循环-930~1500)
6. [买入信号判断](#6-买入信号判断)
7. [卖出执行三段式](#7-卖出执行三段式)
8. [收盘前检查 14:55](#8-收盘前检查-1455)
9. [收盘后自动化 15:00+](#9-收盘后自动化-1500)
10. [状态持久化](#10-状态持久化)
11. [回测脚本逻辑说明](#11-回测脚本逻辑说明)
12. [实盘与回测策略差异对照表](#12-实盘与回测策略差异对照表)

---

## 1. 系统架构总览

```
run_live_v3.py                  ← 每日启动入口（手动 / 定时）
    └── LiveEngineV3.run()      ← 实盘引擎主循环（engine/live_engine_v3.py）
            ↓
        TradeExecutor           ← miniQMT 下单接口（trade/executor.py）
            ↓
        xtquant / miniQMT       ← 实时行情 + 下单

收盘后（run_live_v3.py engine.run() 返回后）：
    ├── track_pool_performance.track()       ← 追踪今日调仓池表现
    ├── init_rebalance_pool.main(strategy)   ← 重新建池（写 state_v3_rebalance.json）
    └── update_5min_incremental.run_incremental() ← 增量更新5分钟线数据
```

### 关键文件

| 文件 | 作用 |
|------|------|
| `run_live_v3.py` | 每日启动脚本，调用引擎+收盘后自动化 |
| `engine/live_engine_v3.py` | 实盘引擎核心（LiveEngineV3 + SimulationEngineV3） |
| `engine/offline_sim_engine_v3.py` | 离线测试引擎（单元测试用，不影响实盘） |
| `config.py` | 全局策略参数（止损、止盈、选股等） |
| `state_v3.json` | 实盘持仓/资金状态持久化文件 |
| `state_v3_rebalance.json` | 调仓池文件（每日收盘后刷新） |
| `trades_v3.json` | 交易日志 |
| `failed_orders_v3.json` | 买卖失败复盘日志 |
| `params_v3.json` | 策略参数热重载文件（可选，覆盖 config.py） |

---

## 2. 每日完整流程

```
[启动] python run_live_v3.py
    │
    ▼
[初始化] LiveEngineV3(mode='live', capital_limit=200000)
    │  ─ 从 config.py / params_v3.json 加载策略参数
    │  ─ 初始化 TradeExecutor（连接 miniQMT）
    │
    ▼
[启动恢复] _recover()
    │  ─ 读 state_v3.json → 恢复持仓/现金/pending_sells
    │  ─ 与 miniQMT 真实持仓核对 (_reconcile_with_broker)
    │  ─ 批量重建止损条件单 (_setup_all_condition_orders)
    │
    ▼
[加载调仓池] _load_rebalance_pool()
    │  ─ 读 state_v3_rebalance.json
    │
    ▼
[主循环] while _market_is_open():  ← 9:00~15:01
    │
    ├─ [9:15~9:25] 集合竞价挂 pending 卖单
    ├─ [9:25~9:30] 检查竞价成交，未成交→9:30重挂
    ├─ [9:30~15:00] 每分钟：止损监控 → 扫描买入
    ├─ [14:55] 收盘信号检查（阴跌/移动止盈/时间止损→pending）
    └─ [每5分钟心跳] 保存状态 / 热重载调仓池 / 检测条件单成交
    │
    ▼
[收盘] _save_state()
    │
    ▼
[收盘后自动化]
    ├─ 追踪调仓池表现
    ├─ 重建调仓池 init_rebalance_pool.main()
    └─ 增量更新5分钟线
```

---

## 3. 启动与恢复阶段

### 3.1 策略参数加载顺序

```
config.py（基础值）
    ↓ 被 params_v3.json 覆盖（如果文件存在）
```

`_reload_params()` 在 `__init__` 最后执行，支持不重启引擎热更新参数。

### 3.2 持仓恢复 `_recover()`

```
读 state_v3.json
    ↓
恢复 positions / cash / pending_sells / days_held
    ↓
[实盘模式] _reconcile_with_broker()
    ├─ 查询 miniQMT 真实持仓
    ├─ 策略有但券商无 → 判为条件单已成交 → 自动清仓
    └─ 券商有但策略无 → 打印警告（手动买入）
    ↓
_setup_all_condition_orders()
    └─ 为所有 T+1 以上持仓批量重挂服务器端止损条件单
```

**安全兜底**：若 miniQMT 返回空持仓但策略有持仓，疑似 API 异常，跳过核对防止误清仓。

### 3.3 条件单机制

每只持仓在服务器端挂一个止损条件单（触发价 = 硬止损价），作为程序崩溃时的兜底保护。
- 移动止盈已激活时，条件单触发价更新为回撤线（更严的保护）
- 次日开盘时全部撤销重建（条件单仅当日有效）
- 主动卖出前必须先撤销条件单防止双重卖出

---

## 4. 集合竞价阶段 9:15~9:30

```
[9:15~9:25] _execute_pending_sells_auction()
    │
    ├─ 获取 pending_sells 列表中各股的昨收价
    ├─ 挂限价卖单：昨收 × 0.99
    ├─ 撤销对应条件单（防止双重卖出）
    └─ 记录 order_id → _auction_sell_orders

[9:25~9:30] _check_auction_sell_results()
    │
    ├─ 查询每笔竞价单状态
    ├─ 已成交(status=56) → 更新持仓/资金，清理 pending_sells
    └─ 未成交 → 调用 _resubmit_sells_at_930()
                    └─ 等待 9:30 → 按买一价重挂 → 等待5分钟
                         ├─ 成交 → 更新持仓/资金
                         └─ 超时未成交 → 撤单，记录 failed_orders
```

**pending_sells 来源**：14:55 收盘信号触发（阴跌止损 / 移动止盈 / 时间止损），次日集合竞价执行。

---

## 5. 盘中主循环 9:30~15:00

```
每分钟循环：
    │
    ├─ [跨日检查] days_held 递增（每天第一次进入时执行）
    │
    ├─ _monitor_positions()  ── 持仓止损/止盈监控
    │       ├─ get_full_tick 批量获取持仓实时快照
    │       ├─ 更新 highest_price（实时追踪最高价）
    │       │       └─ 最高价上升且移动止盈已激活 → 更新条件单触发价
    │       ├─ T+0 当天买入不执行卖出（days_held == 0 跳过）
    │       ├─ 触发硬止损 → _execute_sell_with_fallback('hard_stop')
    │       └─ 触发移动止盈 → _execute_sell_with_fallback('trailing_stop')
    │
    ├─ [持仓 < 3 时] _scan_and_buy()  ── 每分钟扫描一次
    │       ├─ _get_available_cash() 检查可用资金
    │       ├─ _get_tradable_pool() 过滤候选（排除已持仓）
    │       ├─ get_full_tick 批量获取行情
    │       ├─ [可选] prev_bar_up 过滤：检查前一根5分钟K线非阴线
    │       ├─ _check_buy_signal() 检查买入条件
    │       ├─ 按卖一价下限价单 → _wait_fill_result(timeout=300s)
    │       │       └─ 等待期间持续执行 _monitor_positions()
    │       ├─ 全量成交 → 记录持仓
    │       ├─ 部分成交 → 按实际数量记录，标记 intended_qty
    │       └─ 超时未成交 → 撤单，当日不再重试此股
    │
    ├─ [14:55] _check_close_signals()  ── 仅执行一次
    │
    └─ [每5分钟心跳]
            ├─ _save_state()
            ├─ _maybe_reload_rebalance_pool()  ← 热重载调仓池文件
            └─ _check_condition_order_fills()  ← 检测条件单是否已触发
```

### 硬止损触发逻辑

```
实时价 lastPrice ≤ 买入价 × (1 - hard_stop_loss)
    → 以止损价挂限价卖单
```

### 移动止盈触发逻辑

```
最高价 highest_price ≥ 买入价 × (1 + trail_activate)  ← 激活条件
    AND 实时价 ≤ 最高价 × (1 - trail_stop)              ← 触发条件
        → 以回撤触发价挂限价卖单
```

---

## 6. 买入信号判断

`_check_buy_signal(code, bar, pre_close)` 必须**全部满足**：

| # | 条件 | 主板 | 科创/创业板 |
|---|------|------|-----------|
| 1 | 涨幅 > 最低阈值 | > 1% | > 2% |
| 2 | 涨幅 < 防追高上限 | < 7% | < 8% |
| 3 | 收阳线 | close > open | close > open |
| 4 | 未涨停 | < 9.8% | < 19.8% |

**前K线过滤**（`prev_bar_up=True` 时额外检查）：
- 从 xtdata 获取最近2根5分钟K线
- 上一根K线 close < open（阴线）→ 跳过不买

**注意**：当前实盘 `prev_bar_up` 由 `config.V3_PREV_BAR_UP = True` 控制。

### 买入数量计算

```python
空仓位数 = max_positions - _count_effective_positions()
单位分配 = available_cash / 空仓位数
买入股数 = floor(单位分配 / 卖一价 / 100) * 100
```

`_count_effective_positions()`：部分成交且成本 < 槽预算 50% 的持仓不计为完整槽位。

---

## 7. 卖出执行三段式

`_execute_sell_with_fallback(code, sell_price, quantity, sell_type, ...)`:

```
第1轮（3分钟）：以止损价/止盈价挂限价单
    ├─ 全量成交 → 记录，结束
    ├─ 部分成交 → 记录已成交部分，remaining 进入第2轮
    └─ 超时 → 撤单，remaining 进入第2轮

第2轮（2分钟）：以买一价（对手价）重挂
    ├─ 全量成交 → 记录，结束
    ├─ 部分成交 → 记录已成交部分，remaining 进入第3轮
    └─ 超时 → 撤单，remaining 进入第3轮

第3轮（兜底）：加入 pending_sells → 次日集合竞价执行
    └─ 记录 failed_orders（供复盘）
```

---

## 8. 收盘前检查 14:55

`_check_close_signals()` 按优先级顺序检查，**三者互斥（第一个触发则不检查后续）**：

```
遍历所有持仓（跳过 T+0 当天买入、已在 pending_sells 中的）：
    │
    ├─ 1. 阴跌止损
    │       条件：收盘价 < 买入价 × (1 - soft_stop_loss)
    │             AND 收盘价 < 当日开盘价（收阴线）
    │       → 加入 pending_sells (sell_type='soft_stop')
    │
    ├─ 2. 移动止盈 pending（对齐实盘盘中逻辑）
    │       条件：最高价已激活（highest ≥ 买入价 × (1+trail_activate)）
    │             AND 收盘价 ≤ 最高价 × (1 - trail_stop)
    │       → 加入 pending_sells (sell_type='trailing_stop')
    │
    └─ 3. 时间止损
            条件：持仓天数 ≥ time_stop_days
                  AND 收盘价 ≤ 买入价
            → 加入 pending_sells (sell_type='time_stop')
```

**说明**：14:55 后撤销已加入 pending 股票的条件单，防止 14:55~15:00 间双重触发。

---

## 9. 收盘后自动化 15:00+

`engine.run()` 返回后，`run_live_v3.py` 自动执行：

```python
# 1. 追踪今日调仓池涨跌表现（必须在刷新前执行）
track_pool_performance.track()

# 2. 重建调仓池（每日自动刷新）
init_rebalance_pool.main(strategy='ba')   # 读 state_v3_rebalance.json 中的 strategy_key
# 输出写入 state_v3_rebalance.json，第二天启动时加载

# 3. 增量更新5分钟线数据
update_5min_incremental.run_incremental(force_full=False)
```

**选股池建池逻辑（B+A策略）**：
- B层：MA20 趋势过滤（最近20日均线，收盘价必须在 MA20 之上）
- A层：信号质量评分（过去120交易日中，涨幅1%~7%且收阳线的历史天数）
- 取评分 Top 50 写入调仓池

---

## 10. 状态持久化

### state_v3.json 字段说明

| 字段 | 含义 |
|------|------|
| `cash` | 策略可用现金（受 capital_limit 限制） |
| `positions` | 持仓列表（含 buy_price/buy_date/days_held/highest_price） |
| `pending_sells` | 次日竞价卖出队列 |
| `total_value` | 总资产（cash + 持仓按买入价估值） |
| `_last_increment_date` | 上次递增 days_held 的日期（防止重复计数） |
| `_daily_filter_date` | 日均成交额过滤缓存日期（暂停用） |
| `_daily_filter_cache` | 日均成交额过滤缓存列表（暂停用） |

### 持仓字段说明

| 字段 | 含义 |
|------|------|
| `code` | 股票代码（纯数字） |
| `buy_price` | 买入价（卖一价） |
| `buy_date` | 买入日期 |
| `days_held` | 持仓交易日数（T+0 时为 0） |
| `quantity` | 持仓股数 |
| `highest_price` | 持仓期间最高价（移动止盈基准） |
| `sell_type` | pending 卖出类型（soft_stop/trailing_stop/time_stop） |

---

## 11. 回测脚本逻辑说明

**唯一回测脚本**：`run_backtest_5min_live_sim.py`（多年批量封装：`run_v3_multiyear_backtest.py`）

### 回测整体流程

```
1. 加载日线数据（全市场，用于 prev_close/day_open 缓存 + B+A 选股）
2. 加载5分钟K线数据（指定年份目录）
3. 构建交易日历
4. 预计算每日选股池（基于前一日日线，支持磁盘缓存）
5. 逐日逐根5分钟K线模拟：
   ├─ 开盘前：执行 pending 卖出（次日第一根K线开盘价）
   ├─ days_held 递增
   ├─ 逐根K线：
   │   ├─ 持仓止损/止盈（K线 low 触及止损价）
   │   └─ 买入扫描（K线 close 价判断信号）
   └─ 14:55：阴跌止损 / 移动止盈 / 时间止损 → pending
6. 末日强平（按末日收盘价）
7. 输出统计报告
```

### 买入价格计算（close 模式，默认）

```python
buy_px = bar['close']   # 5分钟K线收盘价
chg = (buy_px - prev_close) / prev_close
# 条件判断 & 成交均用同一价格，无前视偏差
```

**合理性**：实盘采用实时价（卖一价限价单），5分钟bar收盘价是合理近似。

---

## 12. 实盘与回测策略差异对照表

| 差异点 | 实盘 | 回测 | 影响程度 |
|--------|------|------|---------|
| **买入成交价** | 卖一价（askPrice[0]） | 5分钟K线 close 价 | 低（价差极小） |
| **选股池** | 每日收盘后重建，第二天使用 | 每日基于前一日数据实时计算 | **已对齐**（机制一致） |
| **止损触发依据** | 实时价 lastPrice | 5分钟K线 low 价 | 低（low < 实时价，实盘略宽松） |
| **止损卖出成交** | 三段式（限价→买一价→pending） | max(止损价, 当根K线 open)，一次性 | 中（回测略乐观） |
| **pending 卖出成交价** | 9:15 集合竞价，昨收×0.99 | 次日第一根K线开盘价 | 低 |
| **14:55 移动止盈 pending** | ✅ 有 | ✅ 有（已对齐） | 无（盘中已大量触发） |
| **ST 股票过滤** | ~~有~~ → **已注释** | 无 | 无（进池概率极低） |
| **10天日均5亿过滤** | ~~有~~ → **已注释** | 无 | 已三端统一 |
| **选股池建池 ST/流动性过滤** | 无 | 无 | 无（B+A评分天然过滤低质股） |
| **前K线非阴线过滤** | ✅ 有（xtdata 历史K线） | ✅ 有（bars_idx 前一根K线） | 已对齐 |

### 已知合理偏差（不影响策略有效性评估）

1. **止损成交价**：回测用 `max(止损价, K线open)` 属于保守估计；实盘三段式可能在更差价格成交。回测此处略乐观，但差异可控。
2. **买入价**：close vs 卖一价，差距通常 < 0.1%，可忽略。

---

*文档维护：每次修改以下内容需同步更新 → 止损/止盈逻辑、买入条件、选股池建池规则、卖出执行流程、收盘后自动化流程。*
