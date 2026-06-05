# 代码更新说明 (2026-06-05)

## 本次变更

### 引擎修复: `engine/live_engine_v4.py`
- **卖出成交价修正**: `_execute_sell` 现在使用实际路由成交价（买一价）计算 cash、PnL、price，不再使用信号价
- **trade log 增强**: 卖出记录自动写入 `snapshot_hs`/`snapshot_ta`/`snapshot_ts`/`snapshot_max_pos`
- **日志格式**: 卖出日志改为 `成交价=xx.xxx(信号xx.xxx)` 格式，便于排查

### 日线更新: `update_daily_data.py`
- 除权检测前置到增量更新之前
- 除权股票跳过增量更新，最后全量重下
- 检测范围: 持仓 + 近30天交易过的股票

### 盘前处理: `engine/live_engine_v4.py`
- 盘前处理时间从 09:00 调整为 **08:30**

---

## Pull 后需执行的操作

### 1. 修复历史交易数据 (必做)
```bash
python fix_sell_price.py
```
**作用**:
- 修正所有卖出交易的 `price` 字段: 从信号价改为实际成交价 (order_px)
- 修正 300319 的 snapshot 参数 (chop_else → chop_init)
- 从 300,000 重建完整 cash 链
- 更新 state_v4.json 的 cash 和 total_value
- 自动备份 `.bak_sellfix` 文件

### 2. 验证修复结果 (建议)
```bash
python _check.py
```
**作用**: 验证 cash 链一致性 + 当前持仓止盈止损线

---

## 已知遗留问题 (暂不处理)

- 买入成交价精度: 引擎用委托价 (2位小数) 记录，QMT 实际成交价可能 3 位小数 (如 44.745 vs 44.74)，差异极小 (~11元)
- 需要后续调用 `query_stock_trades` 获取真实成交价才能彻底修复
