# V3 实盘交易流程文档

> **维护说明**：每次策略逻辑发生变更（止损条件、买入条件、调仓机制等），必须同步更新本文档对应章节。
> 上次更新：2026-04-30

---

## 目录

1. [整体架构概览](#1-整体架构概览)
2. [启动前准备](#2-启动前准备)
3. [每日完整流程（时间轴）](#3-每日完整流程时间轴)
4. [各阶段详细流程](#4-各阶段详细流程)
   - 4.1 [启动恢复（_recover）](#41-启动恢复_recover)
   - 4.2 [9:15 竞价卖出](#42-915-竞价卖出)
   - 4.3 [9:25 检查竞价成交](#43-925-检查竞价成交)
   - 4.4 [9:30~15:00 盘中主循环](#44-930~1500-盘中主循环)
   - 4.5 [持仓监控止损止盈](#45-持仓监控止损止盈)
   - 4.6 [扫描买入](#46-扫描买入)
   - 4.7 [14:55 收盘信号检查](#47-1455-收盘信号检查)
   - 4.8 [15:00+ 收盘后自动任务](#48-1500-收盘后自动任务)
5. [选股池机制](#5-选股池机制)
6. [条件单止损兜底机制](#6-条件单止损兜底机制)
7. [资金管理](#7-资金管理)
8. [买入信号逻辑](#8-买入信号逻辑)
9. [卖出逻辑汇总](#9-卖出逻辑汇总)
10. [状态文件说明](#10-状态文件说明)
11. [策略参数完整表](#11-策略参数完整表)
12. [回测脚本逻辑 & 与实盘的差异](#12-回测脚本逻辑--与实盘的差异)

---

## 1. 整体架构概览

```
run_live_v3.py
    └── LiveEngineV3(mode='live', capital_limit=200000)
            │
            ├── TradeExecutor (miniQMT 连接)
            ├── state_v3.json (持仓/资金持久化)
            ├── state_v3_rebalance.json (选股池)
            └── params_v3.json (可选：热重载参数覆盖 config.py)

收盘后（engine.run() 返回）
    ├── track_pool_performance.track()  → 追踪调仓池涨跌
    ├── init_rebalance_pool.main()      → 自动更新选股池
    └── update_5min_incremental.run()   → 增量更新5分钟线
```

**关键文件**

| 文件 | 作用 |
|------|------|
| `run_live_v3.py` | 实盘启动入口 |
| `engine/live_engine_v3.py` | 核心引擎（`LiveEngineV3` 类） |
| `init_rebalance_pool.py` | 选股池构建（B+A策略） |
| `state_v3.json` | 当日持仓/资金状态 |
| `state_v3_rebalance.json` | 选股池（Top 50） |
| `config.py` | 策略参数 |
| `params_v3.json` | （可选）热重载参数，优先级高于 config.py |
| `trades_v3.json` | 成交记录日志 |
| `failed_orders_v3.json` | 未成交/失败订单复盘日志 |

---

## 2. 启动前准备

```
python run_live_v3.py
```

引擎启动时依次执行：
1. 连接 miniQMT（`TradeExecutor.connect()`）
2. 加载 `params_v3.json` 热重载参数（文件不存在则用 `config.py` 默认值）
3. 调用 `_recover()` 恢复上次状态
4. 调用 `_load_rebalance_pool()` 加载选股池

> 若需查看当前持仓而不运行主循环：`python run_live_v3.py --status`

---

## 3. 每日完整流程（时间轴）

```
graph TD
    A[程序启动] --> B[连接 miniQMT]
    B --> C[_recover: 读 state + 券商持仓核对]
    C --> D[_load_rebalance_pool: 加载选股池]
    D --> E[进入主循环 while market_is_open]
    E --> F{时间判断}

    F -->|9:15~9:25| G[_execute_pending_sells_auction\n挂竞价限价卖单 昨收×0.99]
    F -->|9:25~9:30| H[_check_auction_sell_results\n检查成交 未成交→9:30重挂买一价]
    F -->|9:30~15:00| I[_monitor_positions\n硬止损 / 移动止盈]
    I --> J{持仓 < 3?}
    J -->|是| K[_scan_and_buy\n扫描候选池买入]
    J -->|否| L[等待下一分钟]
    F -->|14:55| M[_check_close_signals\n阴跌/移动止盈/时间止损 → pending]
    F -->|15:01| N[退出主循环]

    N --> O[_save_state]
    O --> P[track_pool_performance.track]
    P --> Q[init_rebalance_pool.main\n更新选股池]
    Q --> R[update_5min_incremental.run\n增量更新5分钟线]

    E -->|每5分钟心跳| S[_save_state\n_maybe_reload_rebalance_pool\n_check_condition_order_fills]
```

---

## 4. 各阶段详细流程

### 4.1 启动恢复（_recover）

```
graph TD
    A[_recover] --> B[_load_state: 读 state_v3.json]
    B --> C[恢复 positions / cash / pending_sells]
    C --> D[恢复 days_held 兼容处理\n旧数据用自然日差初始化]
    D --> E{实盘模式?}
    E -->|是| F[_reconcile_with_broker\n查券商真实持仓]
    F --> G{对比策略持仓}
    G -->|券商多出 extra| H[打印警告 不干预\n可能为手动买入]
    G -->|券商少了 missing| I[判定条件单已成交\n自动清理持仓 同步资金]
    E -->|否| J[跳过核对]
    J --> K[_setup_all_condition_orders\n批量重建当日条件单]
    I --> K
```

**代码位置**：`live_engine_v3.py` L414-L516

**关键逻辑**：
- 券商返回空持仓但策略有持仓 → **保守跳过**，防止 API 异常导致误清仓
- `missing_in_broker` 判定为条件单已成交，自动从策略记录移除并对齐资金
- 条件单每日有效期，启动时必须重建

---

### 4.2 9:15 竞价卖出

```
graph TD
    A[_execute_pending_sells_auction] --> B{pending_sells 为空?}
    B -->|是| C[打印无任务 返回]
    B -->|否| D[get_full_tick 获取待卖股票行情]
    D --> E[逐只处理 pending_sells]
    E --> F[撤销该股条件单\n防止双重卖出]
    F --> G[挂限价卖单\n价格 = 昨收 × 0.99]
    G --> H{下单成功?}
    H -->|是| I[记录 _auction_sell_orders\norder_id → pos]
    H -->|否| J[记录 failed_orders_v3.json]
```

**代码位置**：`live_engine_v3.py` L722-L783

**要点**：
- 价格 = 昨收 × 0.99（集合竞价中略低于昨收，优先成交）
- 竞价前必须撤销条件单（防双重卖出）
- 昨收价获取优先级：`lastClose` → `preClose` → `lastPrice` → 买入价兜底

---

### 4.3 9:25 检查竞价成交

```
graph TD
    A[_check_auction_sell_results] --> B[_query_orders 获取当日订单]
    B --> C[遍历 _auction_sell_orders]
    C --> D{订单状态 = 已成交 56?}
    D -->|是| E[更新资金 cash += net_income\n记录交易日志\n移除持仓 & pending_sells]
    D -->|否| F[加入 unfilled_pos]
    F --> G[_resubmit_sells_at_930\n等待至 9:30 按买一价重挂]
    G --> H[_wait_fill 等待最多5分钟]
    H --> I{成交?}
    I -->|是| J[更新资金 记录交易]
    I -->|否| K[撤单 记录 failed_orders]
```

**代码位置**：`live_engine_v3.py` L787-L893

---

### 4.4 9:30~15:00 盘中主循环

每分钟执行一轮：

```python
# 主循环核心逻辑（run() 方法，L364-L402）
while _market_is_open():
    h, m = now.hour, now.minute

    if (h == 9 and m >= 30) or (10 <= h <= 14) or (h == 15 and m == 0):
        self._monitor_positions()            # 止损/止盈检查

        if self._count_effective_positions() < self.max_positions:
            self._scan_and_buy()             # 扫描买入（每1分钟）

        if h == 14 and m >= 55 and not self._close_check_done:
            self._check_close_signals()      # 14:55 收盘信号

    # 心跳（每5分钟）
    if _heartbeat_counter >= 5:
        self._save_state()
        self._maybe_reload_rebalance_pool()  # 热重载选股池
        self._check_condition_order_fills()  # 检测条件单成交

    time.sleep(60)
```

**每日 days_held 递增**：主循环第一次进入时检查日期，若跨日则对所有非今日买入的持仓 `days_held += 1`，并重建所有条件单。

---

### 4.5 持仓监控止损止盈

```
graph TD
    A[_monitor_positions] --> B[get_full_tick 批量获取持仓行情]
    B --> C[逐只持仓检查]
    C --> D{days_held == 0?}
    D -->|是| E[T+1限制 跳过]
    D -->|否| F[更新 highest_price\n若最高价刷新且移动止盈已激活\n→ 更新条件单触发价]
    F --> G{last_price <= hard_stop_price?}
    G -->|是| H[触发硬止损\nsell_type = hard_stop]
    G -->|否| I{highest >= buy×1+trail_act\nAND last <= highest×1-trail_pct?}
    I -->|是| J[触发移动止盈\nsell_type = trailing_stop]
    I -->|否| K[不触发 继续下一只]
    H --> L[撤销条件单]
    J --> L
    L --> M[_execute_sell_with_fallback\n三段式卖出]
```

**代码位置**：`live_engine_v3.py` L898-L990

**注意**：阴跌止损（`soft_stop`）和时间止损仅在 **14:55** 检查，不在盘中实时监控。

---

### 4.6 扫描买入

```
graph TD
    A[_scan_and_buy] --> B{调仓池为空?}
    B -->|是| C[跳过]
    B -->|否| D[_get_available_cash\n计算可用资金]
    D --> E{可用资金 < 单槕预算×50%?}
    E -->|是| F[资金不足 跳过]
    E -->|否| G[构建 held_codes\n策略持仓 + 券商持仓]
    G --> H[_get_tradable_pool\n排除已持仓]
    H --> I[get_full_tick 批量获取行情]
    I --> J[按排名顺序遍历候选股]
    J --> K{停牌 / 数据异常?}
    K -->|是| L[跳过]
    K -->|否| M{prev_bar_up=True?\n检查前5分钟K线非阴线}
    M -->|阴线| N[跳过]
    M -->|非阴| O[_check_buy_signal\n买入信号检查]
    O -->|不满足| P[打印原因 跳过]
    O -->|满足| Q[计算买入量\n按卖一价下单]
    Q --> R[_wait_fill_result\n等待最多5分钟\n等待期间持续监控止损]
    R --> S{成交量 > 0?}
    S -->|全量/部分| T[记录持仓 扣除资金\n记录交易日志 钉钉通知]
    S -->|完全未成交| U[记入 failed_buys_today\n当日不再重试]
```

**代码位置**：`live_engine_v3.py` L995-L1229

**关键细节**：
- 按排名顺序处理候选股，满足条件后立即下单，买入等待期间同步运行止损监控
- `_count_effective_positions()`：单槕成本 < 槕预算×50% 时不计为完整仓位，允许继续买入
- 买入当天（T+0）不挂条件单；次日启动时统一批量重建

---

### 4.7 14:55 收盘信号检查

```
graph TD
    A[_check_close_signals] --> B[get_full_tick 获取所有持仓行情]
    B --> C[逐只持仓检查]
    C --> D{days_held == 0? T+1限制}
    D -->|是| E[跳过]
    D -->|否| F{阴跌止损?\nlast < buy×1-soft_sl\nAND last < open}
    F -->|是| G[sell_type = soft_stop]
    F -->|否| H{移动止盈已激活?\nhighest >= buy×1+trail_act\nAND last <= highest×1-trail_pct}
    H -->|是| I[sell_type = trailing_stop]
    H -->|否| J{时间止损?\ndays_held >= time_stop\nAND last <= buy_price}
    J -->|是| K[sell_type = time_stop]
    J -->|否| L[不触发]
    G --> M[加入 pending_sells\n撤销条件单\n钉钉通知]
    I --> M
    K --> M
    M --> N[_save_state 保存]
```

**代码位置**：`live_engine_v3.py` L1234-L1323

**优先级顺序**：阴跌止损 → 移动止盈 → 时间止损（互斥，优先级从高到低）

---

### 4.8 15:00+ 收盘后自动任务

`engine.run()` 返回后，`run_live_v3.py` 顺序执行：

| 步骤 | 脚本 | 说明 |
|------|------|------|
| 1 | `track_pool_performance.track()` | 追踪今日调仓池涨跌表现（必须在刷新前执行） |
| 2 | `init_rebalance_pool.main(strategy)` | 用今日收盘数据重建选股池，写入 `state_v3_rebalance.json` |
| 3 | `update_5min_incremental.run()` | 增量更新5分钟线数据 |

任一步骤失败不影响其他步骤，均有独立的异常捕获。

---

## 5. 选股池机制

### 构建逻辑（init_rebalance_pool.py）

```
graph TD
    A[main strategy=ba] --> B[构建交易日历\nD:/daily_data]
    B --> C[确定调仓日 = 最近交易日]
    C --> D[回看 120 个交易日]
    D --> E[获取全市场股票代码\n过滤板块: 60/00/30/688]
    E --> F[逐只计算指标]
    F --> G{use_ma20=True ba/b策略?}
    G -->|是| H[计算MA20\n收盘价 <= MA20 → 排除]
    G -->|否| I[跳过MA20过滤]
    H --> J[计算 quality_score\n过去120日中满足条件的天数\n涨幅1%~7% AND 收阳线]
    I --> J
    J --> K[按 quality_score 降序]
    K --> L[取 Top 50]
    L --> M[写入 state_v3_rebalance.json]
```

**策略类型**

| key | 名称 | MA20过滤 | 信号要求 |
|-----|------|----------|---------|
| `ba` | B+A（默认） | ✅ | 涨幅1%~7% + 收阳线 |
| `a` | 纯信号质量 | ❌ | 涨幅1%~7% + 收阳线 |
| `b` | 纯趋势 | ✅ | 涨幅1%~7%（不要求阳线） |

**选股池参数**
- 回看：120 个交易日（`V3_REBALANCE_LOOKBACK`）
- 池大小：Top 50（`V3_TOP_N`）
- 数据源：`D:/daily_data`（`V3_LOCAL_DATA_DIR`）
- 排除：上市不足60个交易日的新股、北交所(8开头)、B股(4开头)

### 盘中二次过滤（_get_tradable_pool）

```python
# 当前逻辑（live_engine_v3.py L1702-1725）
def _get_tradable_pool(self, held_codes: set) -> list:
    candidates = [c for c in self.rebalance_pool if c not in held_codes]
    # ST过滤：已注释（建池阶段未过滤，盘中暂停执行）
    # 日均成交额5亿过滤：已注释（建池阶段未过滤，盘中暂停执行）
    return candidates
```

> **注意**：ST 过滤和日均 5 亿过滤目前已注释，如需启用需同步在 `init_rebalance_pool.py` 建池阶段也加过滤，保持一致性。

### 选股池热重载

主循环每5分钟心跳中调用 `_maybe_reload_rebalance_pool()`，检测 `state_v3_rebalance.json` 文件 mtime 是否变更，如有变更则热重载，无需重启引擎。

---

## 6. 条件单止损兜底机制

条件单是服务器端（券商侧）的止损单，引擎崩溃后仍有效（当日有效期）。

```
流程：
买入成交 → T+1 次日启动时重建条件单
                │
                ├── 触发价 = buy_price × (1 - hard_stop_loss)
                │   或 highest_price × (1 - trailing_stop)（移动止盈已激活时）
                └── 委托价 = 触发价 × 0.995（略低，尽量成交）

引擎运行中：
    _monitor_positions → 最高价刷新且移动止盈已激活
                       → _update_condition_order（撤旧单 + 按新触发价重挂）

程序主动卖出前：
    _cancel_condition_order_for_code → 撤销条件单（防双重卖出）

每5分钟心跳：
    _check_condition_order_fills → 对比券商真实持仓
                                 → 策略记录有但券商已无 → 判定条件单已成交 → 清理持仓
```

**代码位置**：`live_engine_v3.py` L520-L696

---

## 7. 资金管理

### 可用资金计算（_get_available_cash）

```
实盘模式:
    real_cash  = 券商真实可用资金（executor.query_asset）
    used       = Σ(buy_price × quantity) for pos in positions
    available  = min(real_cash, capital_limit - used)

模拟模式:
    available  = min(self.cash, capital_limit - used)
```

### 买入量计算（_calculate_buy_volume）

```
empty_slots = max_positions - _count_effective_positions()
alloc       = available_cash / empty_slots
volume      = floor(alloc / ask_price / 100) * 100   # 100股整数倍
```

### 有效持仓计数（_count_effective_positions）

单槕成本 = `buy_price × quantity`  
若某持仓成本 < `capital_limit / max_positions × 50%`（部分成交导致），**不计为完整槕位**，允许继续用剩余资金买入其他股票。

### 卖出净收入（_calc_sell_income）

```
净收入 = 卖出金额 - max(卖出金额×0.025%, 5元) - 卖出金额×0.05%
         佣金最低5元       印花税
```

---

## 8. 买入信号逻辑

**函数**：`_check_buy_signal(code, bar, pre_close)`（L1831-L1873）

| 条件 | 主板/创业板 | 科创板 |
|------|------------|--------|
| 1. 涨幅 > 阈值 | `> 1%` | `> 2%` |
| 2. 涨幅 < 防追高 | `< 7%` | `< 8%` |
| 3. 收阳线 | `close > open` | `close > open` |
| 4. 未涨停 | `< 9.8%` | `< 19.8%` |

**额外过滤（扫描前）**：
- 停牌/无效数据：`last_price <= 0 or pre_close <= 0 or volume == 0`
- 前5分钟K线非阴线（`V3_PREV_BAR_UP = True`）：调用 `xtdata.get_market_data` 取前一根5分钟K线，若 `close < open` 则跳过

**科创板/创业板判断**：`_is_star(code)`，688开头或30开头

---

## 9. 卖出逻辑汇总

### 盘中实时（_monitor_positions，9:30~15:00）

| 触发类型 | 条件 | 卖出方式 |
|---------|------|---------|
| 硬止损 | `last_price <= buy_price × (1-3%)` | `_execute_sell_with_fallback`（三段式） |
| 移动止盈 | `highest >= buy×1.02` 且 `last <= highest×0.99` | `_execute_sell_with_fallback`（三段式） |

### 三段式卖出（_execute_sell_with_fallback）

```
第1轮（3分钟）：按止损价/止盈价限价单
    ↓ 部分/未成交
第2轮（2分钟）：按买一价（对手价）重挂
    ↓ 部分/未成交
第3轮：加入 pending_sells → 次日 9:15 竞价执行
```

### 14:55 收盘前（_check_close_signals）

| 触发类型 | 条件 | 卖出方式 |
|---------|------|---------|
| 阴跌止损 | `last < buy×(1-2%)` 且 `last < open`（收阴线） | 加入 `pending_sells` |
| 移动止盈 | `highest >= buy×1.02` 且 `last <= highest×0.99` | 加入 `pending_sells` |
| 时间止损 | `days_held >= 5` 且 `last <= buy_price` | 加入 `pending_sells` |

### 9:15 竞价卖出（_execute_pending_sells_auction）

对 `pending_sells` 中的股票，在集合竞价阶段以 **昨收×0.99** 挂限价卖单。

---

## 10. 状态文件说明

### state_v3.json（每5分钟心跳更新）

```json
{
  "initial_capital": 200000,
  "cash": 66666.67,
  "positions": [
    {
      "code": "600000",
      "symbol": "600000.SH",
      "buy_price": 8.50,
      "buy_date": "2026-04-30",
      "quantity": 4500,
      "days_held": 0,
      "sell_type": null,
      "highest_price": 8.50
    }
  ],
  "pending_sells": [],
  "total_value": 204916.67,
  "last_update": "2026-04-30 14:30:05",
  "_last_increment_date": "2026-04-30",
  "_daily_filter_date": null,
  "_daily_filter_cache": []
}
```

### state_v3_rebalance.json（收盘后自动更新）

```json
{
  "pool": ["600000", "000001", ...],
  "rebalance_date": "2026-04-30",
  "strategy_key": "ba",
  "strategy": "B+A（最优组合）",
  "min_chg": 0.01,
  "max_chg": 0.07
}
```

---

## 11. 策略参数完整表

**来源**：`config.py`（可被 `params_v3.json` 热重载覆盖）

| 参数 | 值 | 说明 |
|------|-----|------|
| `V3_MAX_POSITIONS` | 3 | 最大持仓数 |
| `capital_limit` | **200,000** | 实盘资金上限（run_live_v3.py 中设置） |
| `V3_PREV_BAR_UP` | True | 前5分钟K线非阴线过滤 |
| **主板/创业板** | | |
| `V3_MIN_CHANGE_PCT` | 1% | 最低涨幅买入阈值 |
| `V3_MAX_CHANGE_PCT` | 7% | 防追高上限 |
| `V3_HARD_STOP_LOSS` | 3% | 硬止损 |
| `V3_SOFT_STOP_LOSS` | 2% | 阴跌止损 |
| `V3_TRAILING_ACTIVATE` | 2% | 移动止盈激活浮盈阈值 |
| `V3_TRAILING_STOP` | 1% | 移动止盈回撤触发幅度 |
| `V3_TIME_STOP_DAYS` | 5天 | 时间止损天数 |
| **科创板** | | |
| `V3_STAR_MIN_CHANGE_PCT` | 2% | 最低涨幅 |
| `V3_STAR_MAX_CHANGE_PCT` | 8% | 防追高上限 |
| `V3_STAR_HARD_STOP_LOSS` | 3% | 硬止损 |
| `V3_STAR_SOFT_STOP_LOSS` | 2% | 阴跌止损 |
| `V3_STAR_TRAILING_ACTIVATE` | 8% | 移动止盈激活 |
| `V3_STAR_TRAILING_STOP` | 5% | 移动止盈回撤 |
| `V3_STAR_TIME_STOP_DAYS` | 5天 | 时间止损 |
| `V3_STAR_LIMIT_UP` | 19.8% | 涨停保护 |
| **选股池** | | |
| `V3_TOP_N` | 50 | 调仓池大小 |
| `V3_REBALANCE_LOOKBACK` | 120 | 回看交易日数 |
| `V3_LOCAL_DATA_DIR` | `D:/daily_data` | 日线数据目录 |
| **费率** | | |
| `V3_COMMISSION_RATE` | 0.025% | 佣金率 |
| `V3_MIN_COMMISSION` | 5元 | 最低佣金 |
| `V3_STAMP_TAX_RATE` | 0.05% | 印花税 |

---

## 12. 回测脚本逻辑 & 与实盘的差异

### 回测执行

```bash
# 唯一有效回测脚本
python run_v3_multiyear_backtest.py --years "2022,2025~2026"

# 买入价模式（默认 close = 5分钟K线收盘价，等价于实盘实时价）
python run_v3_multiyear_backtest.py --years "2022" --buy-price close
```

**核心回测引擎**：`run_backtest_5min_live_sim.py`（按5分钟K线逐bar模拟）

### 回测 vs 实盘差异对比

| 维度 | 回测 | 实盘 | 影响程度 |
|------|------|------|---------|
| **选股池更新** | 每日用前一日数据自动重算 | 当日收盘后自动刷新（`init_rebalance_pool.main()`） | ⚠️ 差异已基本对齐（实盘收盘后自动更新） |
| **买入价** | 5分钟K线 close 价（`close` 模式） | 卖一价（盘口最优卖价） | ⚠️ 实盘按卖一价，通常略高于 close；模拟上认为等价 |
| **买入时间** | 全天扫描（逐根5分钟K线检查信号，满足即成交） | 持仓 < 3 时每分钟扫描 tick，满足信号即按卖一价下单 | 低（逻辑完全对齐，仅行情粒度不同） |
| **14:55 移动止盈** | ✅ 已添加（14:55 bar检查） | ✅ `_check_close_signals` 中检查 | 无影响（回测验证结果无变化，盘中已触发） |
| **阴跌止损** | ✅ 14:55 bar close 检查 | ✅ 14:55 tick 实时价检查 | 低 |
| **时间止损** | ✅ 14:55 检查 | ✅ 14:55 检查 | 无差异 |
| **硬止损** | ✅ 每5分钟bar close价实时检查 | ✅ 每分钟tick实时价检查 | 低 |
| **条件单** | ❌ 无（不需要） | ✅ 有，服务器端兜底止损 | 仅实盘安全性保障，不影响策略逻辑 |
| **T+1限制** | ✅ 买入当日不卖出 | ✅ `days_held == 0` 跳过 | 无差异 |
| **部分成交** | ❌ 假设全量成交 | ✅ 支持部分成交 | 低（流动性好的股票几乎全量） |
| **滑点** | 可配置（默认0） | 卖一价买入含隐式滑点 | 低 |
| **ST/日均5亿过滤** | ❌ 无 | ❌ 已注释（暂停执行） | 对齐 |
| **费率** | ✅ 佣金0.025% + 印花税0.05% | ✅ 相同 | 无差异 |

### 回测历史基准结果（close模式，55c091a代码基础+14:55移动止盈补丁）

| 年份 | 总收益 | 年化 | 最大回撤 | 夏普比率 | 交易笔数 |
|------|--------|------|---------|---------|---------|
| 2022（熊市） | +217.45% | +229.81% | 14.87% | 3.439 | 561 |
| 2025~2026 | +2050.21% | +999.01% | 12.50% | 6.059 | 606 |

> **注**：以上结果用于对比基准，若策略参数变更后需重新回测更新此表。

---

## 13. 测试与覆盖率检查

### 13.1 测试文件说明

| 文件 | 类型 | 说明 |
|------|------|------|
| `run_coverage_checks.py` | HC 健康检查 | HC-01~HC-39，覆盖引擎全路径；主要测试目标，每次改引擎后必跑 |
| `tests/test_live_engine_v3.py` | pytest 单元测试 | 补充细节断言，与 HC 互补 |
| `tests/test_executor.py` | pytest 单元测试 | 覆盖 `trade/executor.py` 下单/撤单逻辑 |
| `tests/test_offline_sim.py` | pytest 单元测试 | 覆盖 `engine/offline_sim_engine_v3.py` 回测引擎 |

### 13.2 HC 健康检查（最常用）

> **用途**：每次修改 `engine/live_engine_v3.py` 或 `trade/executor.py` 后，快速验证没有引入回归。

#### 仅跑 HC（不统计覆盖率，速度最快）

```powershell
cd d:\miniqmt_quant
python run_coverage_checks.py
```

成功标志：输出最后一行为 `[覆盖率检查] 全部完成！`，无 `Traceback` 或 `AssertionError`。

#### 跑 HC + 生成覆盖率报告（推荐，改代码后用这个）

```powershell
cd d:\miniqmt_quant
python -m coverage run --include=engine/live_engine_v3.py,engine/offline_sim_engine_v3.py,trade/executor.py run_coverage_checks.py
python -m coverage report --include=engine/live_engine_v3.py,engine/offline_sim_engine_v3.py,trade/executor.py -m
```

#### 查看带缺失行号的详细报告

```powershell
python -m coverage report --include=engine/live_engine_v3.py,engine/offline_sim_engine_v3.py,trade/executor.py -m
```

输出示例（当前基准）：

```
Name                              Stmts   Miss  Cover
-----------------------------------------------------------
engine\live_engine_v3.py           1330     30    98%
engine\offline_sim_engine_v3.py     290      1    99%
trade\executor.py                   311     13    96%
-----------------------------------------------------------
TOTAL                              1931     44    98%
```

#### 生成 HTML 报告（可浏览器查看哪些行未覆盖）

```powershell
python -m coverage html --include=engine/live_engine_v3.py,engine/offline_sim_engine_v3.py,trade/executor.py
# 然后用浏览器打开 htmlcov/index.html
Start-Process htmlcov\index.html
```

### 13.3 pytest 单元测试

#### 跑所有 pytest 用例

```powershell
cd d:\miniqmt_quant
python -m pytest tests/ -v
```

#### 跑特定测试文件

```powershell
python -m pytest tests/test_live_engine_v3.py -v
```

#### 同时统计 pytest 覆盖率

```powershell
python -m pytest tests/ --cov=engine --cov=trade --cov-report=term-missing
```

### 13.4 完整测试流程（改代码后标准步骤）

```
1. 改完代码
2. python run_coverage_checks.py           ← 快速验证无回归
3. python -m coverage run ... ; coverage report  ← 确认覆盖率 ≥ 95%
4. python -m pytest tests/ -v             ← 跑 pytest 补充断言
5. 若覆盖率下降，在 run_coverage_checks.py 末尾补 HC 用例
```

### 13.5 覆盖率目标与历史记录

| 日期 | live_engine_v3 | offline_sim | executor | 总计 | 备注 |
|------|---------------|-------------|---------|------|------|
| 2026-04-xx | 94% | 99% | 96% | ~94% | v3 引擎 1677 行基础版 |
| 2026-04-xx | 53% | 99% | 96% | ~53% | v3 扩展至 2414 行后新增代码未覆盖 |
| 2026-04-30 | **98%** | **99%** | **96%** | **98%** | 补 HC-29~HC-39 后 |

**目标**：`live_engine_v3.py` 覆盖率 ≥ 95%，总覆盖率 ≥ 95%。

### 13.6 HC 用例编号速查

| HC | 测试内容 |
|----|----------|
| HC-01~HC-10 | 基础初始化、集合竞价、状态持久化 |
| HC-11~HC-20 | 扫描买入、止损止盈、pending 卖出 |
| HC-21~HC-28 | SimulationEngineV3、rebalance_pool、异常路径 |
| HC-29 | `_check_close_signals`（14:55 阴跌/移动止盈/时间止损） |
| HC-30 | `_execute_sell_with_fallback`（三段式卖出各路径） |
| HC-31 | `_try_local_5min_fallback`（CSV 本地兜底） |
| HC-32 | `_get_position_5m_bars` + `_subscribe_5m_pool` |
| HC-33 | `_check_buy_signal` + `_is_star`（所有买入信号分支） |
| HC-34 | `get_status_report`（状态报告） |
| HC-35 | `_log_failed_order` + `_get_actual_fill_price` |
| HC-36 | `_load_state` + `_save_state`（异常路径） |
| HC-37 | `_filter_by_avg_amount`（日均成交额过滤） |
| HC-38 | `_scan_and_buy` 股票处理循环（DataFrame xtdata mock） |
| HC-39 | `_monitor_positions`（止损/止盈/T+1/条件单各分支） |
