# -*- coding: utf-8 -*-
"""
miniQMT 交易通道诊断脚本
用法: python diagnose_trade.py
功能:
  1. 连接 TradeExecutor
  2. 查询账户资产（确认账号是否正确）
  3. 查询当前所有委托
  4. 尝试挂一笔 @1 元的测试委托（不会成交）
  5. 等待3秒后再次查询委托，比对是否出现新委托
  6. 输出诊断结论
"""
import sys
import time

sys.path.insert(0, r'd:\miniqmt_quant')

from trade.executor import TradeExecutor
import config

print("=" * 60)
print("miniQMT 交易通道诊断")
print(f"  账号: {config.ACCOUNT_ID}")
print(f"  路径: {config.MINIQMT_PATH}")
print(f"  session_id: {config.SESSION_ID}")
print("=" * 60)

ex = TradeExecutor()
ok = ex.connect()
print(f"\n[1] 连接结果: {'✅ 成功' if ok else '❌ 失败'}")
if not ok:
    print("连接失败，请检查 miniQMT 是否已启动并登录")
    sys.exit(1)

time.sleep(1)

# ── 查询账户资产 ──
print("\n[2] 查询账户资产:")
try:
    asset = ex._trader.query_stock_asset(ex._account)
    if asset:
        print(f"  总资产: {getattr(asset, 'total_asset', 'N/A')}")
        print(f"  可用资金: {getattr(asset, 'cash', 'N/A')}")
        print(f"  市值: {getattr(asset, 'market_value', 'N/A')}")
    else:
        print("  ❌ 返回 None (账号可能未在 miniQMT 中登录)")
except Exception as e:
    print(f"  查询资产异常: {e}")

# ── 查询当前委托 ──
print("\n[3] 查询现有委托:")
try:
    orders_before = ex._trader.query_stock_orders(ex._account, cancelable_only=False)
    ids_before = [getattr(o, 'order_id', -1) for o in (orders_before or [])]
    print(f"  当前委托数: {len(ids_before)}")
    print(f"  委托ID列表: {ids_before}")
except Exception as e:
    print(f"  查询委托异常: {e}")
    ids_before = []

# ── 挂测试委托 ──
print("\n[4] 挂测试委托 (300786.SZ @1.0 100股 — 不会成交):")
oid = ex.buy(
    symbol='300786.SZ',
    price=1.0,
    volume=100,
    price_type='limit',
    order_remark='DIAG_TEST',
)
print(f"  order_stock 返回: order_id={oid}")

if oid == -1:
    print("  ❌ order_stock 返回 -1，委托直接失败")
    sys.exit(1)

# ── 等待3秒 ──
print("\n[5] 等待3秒后查询委托...")
time.sleep(3)

try:
    orders_after = ex._trader.query_stock_orders(ex._account, cancelable_only=False)
    ids_after = [getattr(o, 'order_id', -1) for o in (orders_after or [])]
    new_ids = [i for i in ids_after if i not in ids_before]
    print(f"  等待后委托数: {len(ids_after)}")
    print(f"  委托ID列表: {ids_after}")
    print(f"  新增委托ID: {new_ids}")

    if oid in ids_after:
        print(f"\n✅ 诊断结论: 委托 {oid} 已进入QMT委托列表 — 交易通道正常!")
        print("   请同时在QMT界面'委托'页签确认是否显示该委托。")
    else:
        print(f"\n❌ 诊断结论: 委托 {oid} 未出现在QMT委托列表!")
        print("   order_stock()返回了ID，但3秒后查询不到。")
        print("   可能原因:")
        print("   1. miniQMT 未登录券商账号（处于离线/模拟状态）")
        print("   2. 账号 ID 不匹配 (config.ACCOUNT_ID 与 QMT 登录账号不一致)")
        print("   3. miniQMT 连接断开（需重启 QMT 客户端）")
        print("   建议: 打开 QMT 客户端，查看登录状态和委托页面")
except Exception as e:
    print(f"  查询委托异常: {e}")

ex.disconnect()
print("\n[6] 已断开连接，诊断完成。")
print("=" * 60)
print("请检查 QMT 客户端的【委托】页签，")
print(f"看是否有 '300786' 的委托出现。")
print("=" * 60)
