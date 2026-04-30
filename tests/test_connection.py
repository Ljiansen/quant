# -*- coding: utf-8 -*-
"""
miniQMT 连接测试脚本
功能：连接 miniQMT、查询账户状态、资产信息、持仓信息
"""

import os
import sys
import time

from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockAccount

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config


class MyCallback(XtQuantTraderCallback):
    """交易回调类，用于接收异步推送的数据"""

    def on_disconnected(self):
        """连接断开回调"""
        print("[回调] 与 miniQMT 的连接已断开")

    def on_stock_asset(self, asset):
        """资产信息异步推送回调"""
        print("[回调] 收到资产信息异步推送")

    def on_stock_order(self, order):
        """订单信息异步推送回调"""
        print("[回调] 收到订单信息异步推送")

    def on_stock_trade(self, trade):
        """成交信息异步推送回调"""
        print("[回调] 收到成交信息异步推送")

    def on_order_error(self, order_error):
        """委托失败回调"""
        print(f"[回调] 委托失败: {order_error}")

    def on_cancel_error(self, cancel_error):
        """撤单失败回调"""
        print(f"[回调] 撤单失败: {cancel_error}")

    def on_order_stock_async_response(self, response):
        """异步下单响应回调"""
        print(f"[回调] 异步下单响应: {response}")


def test_connection():
    """测试连接 miniQMT 并查询相关信息"""
    print("=" * 60)
    print("miniQMT 连接测试开始")
    print("=" * 60)

    # 1. 创建交易接口实例
    print(f"\n[1/5] 正在创建 XtQuantTrader 实例...")
    print(f"      miniQMT 路径: {config.MINIQMT_PATH}")
    print(f"      Session ID: {config.SESSION_ID}")
    trader = XtQuantTrader(config.MINIQMT_PATH, config.SESSION_ID)

    # 2. 注册回调
    print("\n[2/5] 正在注册回调...")
    callback = MyCallback()
    trader.register_callback(callback)

    # 3. 启动交易接口
    print("\n[3/5] 正在启动交易接口...")
    try:
        trader.start()
        print("      交易接口启动成功")
    except Exception as e:
        print(f"[错误] 启动交易接口异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # 4. 建立连接
    print("\n[4/5] 正在连接 miniQMT...")
    try:
        connect_result = trader.connect()
        if connect_result != 0:
            print(f"[错误] 连接 miniQMT 失败，错误码: {connect_result}")
            sys.exit(1)
        print("      连接 miniQMT 成功")
    except Exception as e:
        print(f"[错误] 连接 miniQMT 异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # 5. 创建资金账号对象并订阅
    print("\n[5/5] 正在创建资金账号对象并订阅...")
    account = StockAccount(config.ACCOUNT_ID, config.ACCOUNT_TYPE)
    print(f"      账号: {config.ACCOUNT_ID}, 类型: {config.ACCOUNT_TYPE}")
    try:
        subscribe_result = trader.subscribe(account)
        print(f"      账号订阅结果: {'成功' if subscribe_result else '失败'}")
    except Exception as e:
        print(f"      订阅异常: {e}")

    # 等待连接完全就绪
    time.sleep(1)

    # 查询账户状态
    print("\n" + "=" * 60)
    print("账户状态查询")
    print("=" * 60)
    try:
        is_connected = trader.connected
        print(f"连接状态: {'已连接' if is_connected else '未连接'}")
    except Exception as e:
        print(f"连接状态查询异常: {e}")
        is_connected = False

    # 查询客户端所有账号状态
    print("\n" + "-" * 60)
    print("客户端账号状态")
    print("-" * 60)
    try:
        account_status_list = trader.query_account_status()
        if account_status_list:
            for acc in account_status_list:
                print(f"  账号: {acc.account_id}, 类型: {acc.account_type}, 状态: {acc.status}")
        else:
            print("  未查询到账号状态信息")
    except Exception as e:
        print(f"[错误] 查询账号状态异常: {e}")

    # 查询资产信息
    print("\n" + "-" * 60)
    print("资产信息查询")
    print("-" * 60)
    try:
        asset = trader.query_stock_asset(account)
        if asset:
            print(f"账号类型: {asset.account_type}")
            print(f"资金账号: {asset.account_id}")
            print(f"可用资金: {asset.cash:.2f}")
            print(f"冻结资金: {asset.frozen_cash:.2f}")
            print(f"持仓市值: {asset.market_value:.2f}")
            print(f"总资产:   {asset.total_asset:.2f}")
        else:
            print("[警告] 未查询到资产信息，返回 None")
    except Exception as e:
        print(f"[错误] 查询资产信息异常: {e}")

    # 查询持仓信息
    print("\n" + "-" * 60)
    print("持仓信息查询")
    print("-" * 60)
    try:
        positions = trader.query_stock_positions(account)
        if positions and len(positions) > 0:
            print(f"当前持仓数量: {len(positions)}")
            for idx, pos in enumerate(positions, 1):
                print(f"\n  [{idx}] 股票代码: {pos.stock_code}")
                print(f"      持仓数量: {pos.volume}")
                print(f"      可用数量: {pos.can_use_volume}")
                print(f"      开仓价:   {pos.open_price:.3f}")
                print(f"      市值:     {pos.market_value:.2f}")
        else:
            print("当前无持仓")
    except Exception as e:
        print(f"[错误] 查询持仓信息异常: {e}")

    # 查询当日订单
    print("\n" + "-" * 60)
    print("当日订单查询")
    print("-" * 60)
    try:
        orders = trader.query_stock_orders(account)
        if orders and len(orders) > 0:
            print(f"当日订单数量: {len(orders)}")
            for idx, order in enumerate(orders, 1):
                print(f"\n  [{idx}] 订单ID:   {order.order_id}")
                print(f"      股票代码: {order.stock_code}")
                print(f"      委托类型: {order.order_type}")
                print(f"      委托数量: {order.order_volume}")
                print(f"      委托价格: {order.price:.3f}")
                print(f"      成交数量: {order.traded_volume}")
                print(f"      订单备注: {order.order_remark}")
        else:
            print("当日无订单")
    except Exception as e:
        print(f"[错误] 查询当日订单异常: {e}")

    # 断开连接
    print("\n" + "=" * 60)
    print("正在断开连接...")
    print("=" * 60)
    trader.stop()
    print("连接已断开")

    print("\n" + "=" * 60)
    print("miniQMT 连接测试完成")
    print("=" * 60)


if __name__ == "__main__":
    test_connection()
