# -*- coding: utf-8 -*-
"""
miniQMT 下单测试脚本
功能：连接 miniQMT、下单测试、查询订单状态、撤单
"""

import os
import sys
import time

from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockAccount
from xtquant import xtconstant

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
        print(f"[回调] 订单状态更新: {order.stock_code}, 订单ID={order.order_id}")

    def on_stock_trade(self, trade):
        """成交信息异步推送回调"""
        print(f"[回调] 成交推送: {trade.stock_code}, 成交数量={trade.traded_volume}")

    def on_order_error(self, order_error):
        """委托失败回调"""
        print(f"[回调] 委托失败: 订单ID={order_error.order_id}, 错误码={order_error.error_id}, 错误信息={order_error.error_msg}")

    def on_cancel_error(self, cancel_error):
        """撤单失败回调"""
        print(f"[回调] 撤单失败: 订单ID={cancel_error.order_id}, 错误码={cancel_error.error_id}, 错误信息={cancel_error.error_msg}")

    def on_order_stock_async_response(self, response):
        """异步下单响应回调"""
        print(f"[回调] 异步下单响应: 订单ID={response.order_id}, 状态={response.async_status}")


def query_and_print_order(trader, account, order_id):
    """根据订单ID查询并打印订单详情"""
    try:
        orders = trader.query_stock_orders(account)
        if orders:
            for order in orders:
                if str(order.order_id) == str(order_id):
                    print(f"      订单ID:   {order.order_id}")
                    print(f"      股票代码: {order.stock_code}")
                    print(f"      委托类型: {order.order_type} (23=买入, 24=卖出)")
                    print(f"      委托数量: {order.order_volume}")
                    print(f"      委托价格: {order.price:.3f}")
                    print(f"      成交数量: {order.traded_volume}")
                    print(f"      订单备注: {order.order_remark}")
                    return order
        print("      [警告] 未找到该订单信息")
    except Exception as e:
        print(f"      [错误] 查询订单异常: {e}")
    return None


# 测试专用 session ID，避免与实盘进程冲突（实盘用 654321）
_TEST_SESSION_ID = 654399


def test_order():
    """测试下单流程"""
    print("=" * 60)
    print("miniQMT 下单测试开始")
    print("=" * 60)

    # 1. 创建交易接口实例（使用测试专用 session，不占用实盘 session）
    print(f"\n[1/6] 正在创建 XtQuantTrader 实例...")
    print(f"      miniQMT 路径: {config.MINIQMT_PATH}")
    print(f"      Session ID: {_TEST_SESSION_ID}（测试专用，不影响实盘）")
    trader = XtQuantTrader(config.MINIQMT_PATH, _TEST_SESSION_ID)

    # 2. 注册回调
    print("\n[2/6] 正在注册回调...")
    callback = MyCallback()
    trader.register_callback(callback)

    # 3. 启动交易接口
    print("\n[3/6] 正在启动交易接口...")
    try:
        trader.start()
        print("      交易接口启动成功")
    except Exception as e:
        print(f"[错误] 启动交易接口异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # 4. 建立连接
    print("\n[4/6] 正在连接 miniQMT...")
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
    print("\n[5/6] 正在创建资金账号对象并订阅...")
    account = StockAccount(config.ACCOUNT_ID, config.ACCOUNT_TYPE)
    print(f"      账号: {config.ACCOUNT_ID}, 类型: {config.ACCOUNT_TYPE}")
    try:
        subscribe_result = trader.subscribe(account)
        print(f"      账号订阅结果: {'成功' if subscribe_result else '失败'}")
    except Exception as e:
        print(f"      订阅异常: {e}")

    # 等待连接完全就绪
    time.sleep(1)

    # 查询初始资产
    print("\n" + "-" * 60)
    print("初始资产信息")
    print("-" * 60)
    try:
        asset = trader.query_stock_asset(account)
        if asset:
            print(f"可用资金: {asset.cash:.2f}")
            print(f"总资产:   {asset.total_asset:.2f}")
        else:
            print("[警告] 未查询到资产信息")
    except Exception as e:
        print(f"[错误] 查询资产异常: {e}")

    # 6. 下单
    print("\n" + "=" * 60)
    print("下单操作")
    print("=" * 60)
    print(f"股票代码: {config.TEST_STOCK_CODE}")
    print(f"委托类型: 买入 (STOCK_BUY={xtconstant.STOCK_BUY})")
    print(f"委托数量: {config.TEST_ORDER_VOLUME} 股")
    print(f"价格类型: 限价 (FIX_PRICE={xtconstant.FIX_PRICE})")
    print(f"委托价格: {config.TEST_ORDER_PRICE} 元")
    print("\n【重要】委托价格远低于市价，该订单不会实际成交，仅用于测试下单通路")
    print("-" * 60)

    order_id = None
    try:
        order_id = trader.order_stock(
            account=account,
            stock_code=config.TEST_STOCK_CODE,
            order_type=xtconstant.STOCK_BUY,
            order_volume=config.TEST_ORDER_VOLUME,
            price_type=xtconstant.FIX_PRICE,
            price=config.TEST_ORDER_PRICE,
            strategy_name="test_strategy",
            order_remark="下单测试-不成交"
        )
        print(f"下单成功，订单ID: {order_id}")
    except Exception as e:
        print(f"[错误] 下单异常: {e}")
        trader.stop()
        sys.exit(1)

    # 等待订单状态同步
    time.sleep(4)

    # 查询订单状态
    print("\n" + "-" * 60)
    print("订单状态查询")
    print("-" * 60)
    if order_id:
        query_and_print_order(trader, account, order_id)

    # 等待一会观察状态
    time.sleep(4)

    # 再次查询订单状态
    print("\n" + "-" * 60)
    print("订单状态二次查询（撤单前）")
    print("-" * 60)
    if order_id:
        query_and_print_order(trader, account, order_id)

    # 7. 撤单
    print("\n" + "=" * 60)
    print("撤单操作")
    print("=" * 60)
    if order_id:
        print(f"正在对订单 {order_id} 执行撤单...")
        try:
            cancel_result = trader.cancel_order_stock(account, order_id)
            if cancel_result == 0:
                print("撤单请求发送成功")
            else:
                print(f"撤单请求发送失败，返回码: {cancel_result}")
        except Exception as e:
            print(f"[错误] 撤单异常: {e}")
    else:
        print("[警告] 无有效订单ID，跳过撤单")

    # 等待撤单结果同步
    time.sleep(4)

    # 查询撤单后的订单状态
    print("\n" + "-" * 60)
    print("撤单后订单状态查询")
    print("-" * 60)
    if order_id:
        query_and_print_order(trader, account, order_id)

    # 查询最终资产
    print("\n" + "-" * 60)
    print("最终资产信息")
    print("-" * 60)
    try:
        asset = trader.query_stock_asset(account)
        if asset:
            print(f"可用资金: {asset.cash:.2f}")
            print(f"总资产:   {asset.total_asset:.2f}")
        else:
            print("[警告] 未查询到资产信息")
    except Exception as e:
        print(f"[错误] 查询资产异常: {e}")

    # 断开连接
    print("\n" + "=" * 60)
    print("正在断开连接...")
    print("=" * 60)
    trader.stop()
    print("连接已断开")

    print("\n" + "=" * 60)
    print("miniQMT 下单测试完成")
    print("=" * 60)


if __name__ == "__main__":
    test_order()
