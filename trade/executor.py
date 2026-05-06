# -*- coding: utf-8 -*-
"""
miniQMT 交易执行器模块
封装 xtquant 交易接口，提供统一、易用的交易操作封装。
包含实盘执行器 TradeExecutor 和模拟执行器 SimulatedExecutor，
两者接口完全一致，便于上层引擎无缝切换。
"""

import sys
import time
from datetime import datetime

from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockAccount
from xtquant import xtconstant

sys.path.insert(0, r"d:\miniqmt_quant")
import config


# ---------------------------------------------------------------------------
# 价格类型映射
# ---------------------------------------------------------------------------
_PRICE_TYPE_MAP = {
    "limit": xtconstant.FIX_PRICE,      # 11  限价
    "market": xtconstant.LATEST_PRICE,  # 5   市价/最新价
}

_ORDER_TYPE_MAP = {
    "buy": xtconstant.STOCK_BUY,   # 23
    "sell": xtconstant.STOCK_SELL, # 24
}


def _now_str() -> str:
    """返回当前时间的字符串"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ===========================================================================
# 回调类（miniQMT 要求注册，即使当前暂不处理异步推送）
# ===========================================================================
class _DefaultTradeCallback(XtQuantTraderCallback):
    """默认空回调，仅打印关键事件以便调试"""

    def on_disconnected(self):
        print(f"[{_now_str()}] [交易回调] miniQMT 连接已断开")

    def on_stock_trade(self, trade):
        print(f"[{_now_str()}] [交易回调] 成交通知: {trade.stock_code} "
              f"方向={'买' if trade.order_type == 23 else '卖'} "
              f"成交量={trade.traded_volume} 成交价={trade.traded_price}")

    def on_order_event(self, order):
        # 仅对非完全成交/已撤的单子做简要打印，避免日志刷屏
        if order.order_status not in (50, 53, 54, 55, 56, 57):
            print(f"[{_now_str()}] [交易回调] 订单状态变化: {order.stock_code} "
                  f"order_id={order.order_id} status={order.order_status}")

    def on_query_asset(self, asset):
        # 异步查询资产回调，通常无需处理
        pass

    def on_query_positions(self, positions):
        pass

    def on_query_orders(self, orders):
        pass


# ===========================================================================
# TradeExecutor —— 实盘交易执行器
# ===========================================================================
class TradeExecutor:
    """miniQMT 实盘交易执行器

    封装了连接、下单、撤单、查询资产/持仓/订单等常用操作，
    所有方法均包含异常捕获与中文日志输出。
    """

    def __init__(self, mini_qmt_path=None, account_id=None, session_id=None):
        """初始化交易执行器

        参数为空时自动从 config.py 读取默认值。
        """
        self.mini_qmt_path = mini_qmt_path or config.MINIQMT_PATH
        self.account_id = account_id or config.ACCOUNT_ID
        self.session_id = session_id or config.SESSION_ID
        self.account_type = getattr(config, "ACCOUNT_TYPE", "STOCK")

        self._trader: XtQuantTrader | None = None
        self._account: StockAccount | None = None
        self._connected = False

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------
    def connect(self) -> bool:
        """连接 miniQMT，返回是否成功"""
        try:
            print(f"[{_now_str()}] [TradeExecutor] 正在连接 miniQMT ...")
            print(f"  路径: {self.mini_qmt_path}")
            print(f"  账号: {self.account_id}  类型: {self.account_type}")
            print(f"  session_id: {self.session_id}")

            self._trader = XtQuantTrader(self.mini_qmt_path, self.session_id)
            self._trader.register_callback(_DefaultTradeCallback())
            self._trader.start()

            # connect() 返回 0 表示成功
            result = self._trader.connect()
            if result != 0:
                print(f"[{_now_str()}] [TradeExecutor] 连接失败，返回码: {result}")
                self._connected = False
                return False

            # 创建账号对象
            self._account = StockAccount(self.account_id, self.account_type)

            # 订阅账号（部分版本需要显式 subscribe）
            self._trader.subscribe(self._account)

            # 稍等片刻确保连接稳定
            time.sleep(0.5)

            self._connected = True
            print(f"[{_now_str()}] [TradeExecutor] 连接成功！")
            return True

        except Exception as e:
            print(f"[{_now_str()}] [TradeExecutor] 连接异常: {e}")
            self._connected = False
            return False

    def disconnect(self):
        """断开与 miniQMT 的连接"""
        try:
            if self._trader is not None:
                self._trader.stop()
                print(f"[{_now_str()}] [TradeExecutor] 已断开连接")
        except Exception as e:
            print(f"[{_now_str()}] [TradeExecutor] 断开连接时异常: {e}")
        finally:
            self._connected = False
            self._trader = None
            self._account = None

    @property
    def is_connected(self) -> bool:
        """是否已连接到 miniQMT"""
        return self._connected and (self._trader is not None)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    def _check_ready(self) -> bool:
        """检查是否已连接并初始化账号"""
        if not self.is_connected:
            print(f"[{_now_str()}] [TradeExecutor] 错误：尚未连接 miniQMT")
            return False
        if self._account is None:
            print(f"[{_now_str()}] [TradeExecutor] 错误：账号对象未初始化")
            return False
        return True

    @staticmethod
    def _resolve_price_type(price_type: str) -> int:
        """将字符串价格类型解析为 xtconstant"""
        pt = price_type.lower().strip()
        if pt not in _PRICE_TYPE_MAP:
            print(f"[{_now_str()}] [TradeExecutor] 警告：未知价格类型 '{price_type}'，默认使用限价")
            return _PRICE_TYPE_MAP["limit"]
        return _PRICE_TYPE_MAP[pt]

    # ------------------------------------------------------------------
    # 下单 / 撤单
    # ------------------------------------------------------------------
    def buy(self, symbol: str, price: float, volume: int,
            price_type: str = "limit", order_remark: str = "") -> int:
        """买入股票

        Args:
            symbol: 股票代码，如 '516630.SH'
            price: 委托价格
            volume: 委托数量（股）
            price_type: 'limit'(限价) 或 'market'(市价/最新价)
            order_remark: 订单备注

        Returns:
            order_id，失败返回 -1
        """
        if not self._check_ready():
            return -1

        try:
            pt = self._resolve_price_type(price_type)
            print(f"[{_now_str()}] [TradeExecutor] 买入 {symbol} 价格={price} 数量={volume} "
                  f"price_type={price_type} remark={order_remark}")

            order_id = self._trader.order_stock(
                account=self._account,
                stock_code=symbol,
                order_type=_ORDER_TYPE_MAP["buy"],
                order_volume=int(volume),
                price_type=pt,
                price=float(price),
                strategy_name="TradeExecutor",
                order_remark=order_remark,
            )

            if order_id == -1 or order_id == 0:
                print(f"[{_now_str()}] [TradeExecutor] 买入失败，返回 order_id={order_id}")
                return -1

            print(f"[{_now_str()}] [TradeExecutor] 买入委托成功，order_id={order_id}")
            return order_id

        except Exception as e:
            print(f"[{_now_str()}] [TradeExecutor] 买入异常: {e}")
            return -1

    def sell(self, symbol: str, price: float, volume: int,
             price_type: str = "limit", order_remark: str = "") -> int:
        """卖出股票

        Args:
            symbol: 股票代码，如 '516630.SH'
            price: 委托价格
            volume: 委托数量（股）
            price_type: 'limit'(限价) 或 'market'(市价/最新价)
            order_remark: 订单备注

        Returns:
            order_id，失败返回 -1
        """
        if not self._check_ready():
            return -1

        try:
            pt = self._resolve_price_type(price_type)
            print(f"[{_now_str()}] [TradeExecutor] 卖出 {symbol} 价格={price} 数量={volume} "
                  f"price_type={price_type} remark={order_remark}")

            order_id = self._trader.order_stock(
                account=self._account,
                stock_code=symbol,
                order_type=_ORDER_TYPE_MAP["sell"],
                order_volume=int(volume),
                price_type=pt,
                price=float(price),
                strategy_name="TradeExecutor",
                order_remark=order_remark,
            )

            if order_id == -1 or order_id == 0:
                print(f"[{_now_str()}] [TradeExecutor] 卖出失败，返回 order_id={order_id}")
                return -1

            print(f"[{_now_str()}] [TradeExecutor] 卖出委托成功，order_id={order_id}")
            return order_id

        except Exception as e:
            print(f"[{_now_str()}] [TradeExecutor] 卖出异常: {e}")
            return -1

    def cancel(self, order_id: int) -> bool:
        """撤单

        Args:
            order_id: 要撤销的订单号

        Returns:
            是否成功
        """
        if not self._check_ready():
            return False

        try:
            print(f"[{_now_str()}] [TradeExecutor] 撤单 order_id={order_id}")
            result = self._trader.cancel_order_stock(self._account, int(order_id))
            # 部分版本返回 0 表示成功，部分返回布尔值；统一按 0 / True 处理
            if result == 0 or result is True:
                print(f"[{_now_str()}] [TradeExecutor] 撤单成功")
                return True
            else:
                print(f"[{_now_str()}] [TradeExecutor] 撤单失败，返回码: {result}")
                return False
        except Exception as e:
            print(f"[{_now_str()}] [TradeExecutor] 撤单异常: {e}")
            return False

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def query_asset(self) -> dict:
        """查询账户资产

        Returns:
            dict，键包含 'cash', 'total_asset', 'market_value' 等
        """
        if not self._check_ready():
            return {}

        try:
            asset = self._trader.query_stock_asset(self._account)
            if asset is None:
                print(f"[{_now_str()}] [TradeExecutor] 查询资产返回 None")
                return {}

            result = {
                "cash": getattr(asset, "cash", 0.0),
                "total_asset": getattr(asset, "total_asset", 0.0),
                "market_value": getattr(asset, "market_value", 0.0),
                "frozen_cash": getattr(asset, "frozen_cash", 0.0),
                "fetch_balance": getattr(asset, "fetch_balance", 0.0),
            }
            print(f"[{_now_str()}] [TradeExecutor] 查询资产: {result}")
            return result
        except Exception as e:
            print(f"[{_now_str()}] [TradeExecutor] 查询资产异常: {e}")
            return {}

    def query_positions(self) -> list:
        """查询当前持仓

        Returns:
            list[dict]，每个元素包含:
                symbol, volume, available, cost, market_value
        """
        if not self._check_ready():
            return []

        try:
            positions = self._trader.query_stock_positions(self._account)
            if positions is None:
                print(f"[{_now_str()}] [TradeExecutor] 查询持仓返回 None")
                return []

            result = []
            for pos in positions:
                item = {
                    "symbol": getattr(pos, "stock_code", ""),
                    "volume": getattr(pos, "volume", 0),
                    "available": getattr(pos, "can_use_volume", 0),
                    "cost": getattr(pos, "open_price", 0.0),
                    "market_value": getattr(pos, "market_value", 0.0),
                }
                result.append(item)

            print(f"[{_now_str()}] [TradeExecutor] 查询持仓: 共 {len(result)} 条")
            return result
        except Exception as e:
            print(f"[{_now_str()}] [TradeExecutor] 查询持仓异常: {e}")
            return []

    def query_orders(self) -> list:
        """查询当日所有订单

        Returns:
            list[dict]，每个元素包含:
                order_id, symbol, order_type, price, volume, traded_volume, status
        """
        if not self._check_ready():
            return []

        try:
            orders = self._trader.query_stock_orders(self._account)
            if orders is None:
                print(f"[{_now_str()}] [TradeExecutor] 查询订单返回 None")
                return []

            result = []
            for o in orders:
                item = {
                    "order_id": getattr(o, "order_id", -1),
                    "symbol": getattr(o, "stock_code", ""),
                    "order_type": getattr(o, "order_type", -1),
                    "price": getattr(o, "price", 0.0),
                    "volume": getattr(o, "order_volume", 0),
                    "traded_volume": getattr(o, "traded_volume", 0),
                    "status": getattr(o, "order_status", -1),
                    "remark": getattr(o, "order_remark", ""),
                }
                result.append(item)

            print(f"[{_now_str()}] [TradeExecutor] 查询订单: 共 {len(result)} 条")
            return result
        except Exception as e:
            print(f"[{_now_str()}] [TradeExecutor] 查询订单异常: {e}")
            return []

    def query_trades(self) -> list:
        """查询当日成交明细（含实际成交价）

        Returns:
            list[dict]，每个元素包含:
                order_id, symbol, traded_volume, traded_price
        """
        if not self._check_ready():
            return []
        try:
            trades = self._trader.query_stock_trades(self._account)
            if trades is None:
                return []
            result = []
            for t in trades:
                result.append({
                    "order_id":      getattr(t, "order_id",      -1),
                    "symbol":        getattr(t, "stock_code",    ""),
                    "traded_volume": getattr(t, "traded_volume", 0),
                    "traded_price":  getattr(t, "traded_price",  0.0),
                })
            return result
        except Exception as e:
            print(f"[{_now_str()}] [TradeExecutor] 查询成交明细异常: {e}")
            return []

    # ------------------------------------------------------------------
    # 条件单（服务器端止损兜底）
    # ------------------------------------------------------------------
    def place_condition_order(self, symbol: str, trigger_price: float, sell_price: float,
                              volume: int, order_remark: str = '') -> int:
        """挂止损条件单（服务器端执行，进程崩溃后仍有效）

        当最新价 <= trigger_price 时，以 sell_price 限价卖出 volume 股。
        条件单运行在券商服务器，miniQMT 进程崩溃后依然有效（当日有效期）。

        Args:
            symbol: 股票代码，如 '600000.SH'
            trigger_price: 触发价格（最新价跌至此价时触发）
            sell_price: 委托卖出限价
            volume: 委托数量（股）
            order_remark: 备注

        Returns:
            condition_order_id（整数），API 不支持或失败返回 -1
        """
        if not self._check_ready():
            return -1

        try:
            # order_stock_condition 参数：account, stock_code, order_type, order_volume,
            #   price_type, price, strategy_name, order_remark,
            #   condition_type（1=价格条件）, condition_param
            # condition_type=1 时 condition_param 格式：[触发价, 方向(0=<=, 1=>=)]
            cond_id = self._trader.order_stock_condition(
                account=self._account,
                stock_code=symbol,
                order_type=_ORDER_TYPE_MAP["sell"],
                order_volume=int(volume),
                price_type=_PRICE_TYPE_MAP["limit"],
                price=float(sell_price),
                strategy_name="TradeExecutor_Condition",
                order_remark=order_remark,
                condition_type=1,
                condition_param=[float(trigger_price), 0],  # 0 = 价格 <= 触发价
            )
            if cond_id is None or cond_id == -1 or cond_id == 0:
                print(f"[{_now_str()}] [TradeExecutor] 条件单下单失败: {symbol} "
                      f"触发价={trigger_price} 委托价={sell_price} 返回={cond_id}")
                return -1
            print(f"[{_now_str()}] [TradeExecutor] 条件单已挂: {symbol} "
                  f"触发价={trigger_price:.3f} 委托价={sell_price:.3f} 数量={volume} "
                  f"cond_id={cond_id} remark={order_remark}")
            return int(cond_id)
        except AttributeError:
            # 部分版本 xtquant 不支持 order_stock_condition
            print(f"[{_now_str()}] [TradeExecutor] 警告：当前 xtquant 版本不支持条件单，跳过")
            return -1
        except Exception as e:
            print(f"[{_now_str()}] [TradeExecutor] 条件单下单异常: {e}")
            return -1

    def cancel_condition_order(self, condition_order_id: int) -> bool:
        """撤销条件单

        Args:
            condition_order_id: 条件单ID（由 place_condition_order 返回）

        Returns:
            是否成功
        """
        if not self._check_ready():
            return False
        if condition_order_id == -1:
            return True  # 没有有效条件单，视为成功

        try:
            result = self._trader.cancel_order_stock_condition(
                self._account, int(condition_order_id)
            )
            if result == 0 or result is True:
                print(f"[{_now_str()}] [TradeExecutor] 条件单撤销成功: cond_id={condition_order_id}")
                return True
            else:
                print(f"[{_now_str()}] [TradeExecutor] 条件单撤销失败: cond_id={condition_order_id} 返回={result}")
                return False
        except AttributeError:
            print(f"[{_now_str()}] [TradeExecutor] 警告：当前 xtquant 版本不支持撤销条件单")
            return False
        except Exception as e:
            print(f"[{_now_str()}] [TradeExecutor] 条件单撤销异常: {e}")
            return False


# ===========================================================================
# SimulatedExecutor —— 模拟交易执行器（接口与 TradeExecutor 完全一致）
# ===========================================================================
class SimulatedExecutor:
    """模拟交易执行器

    所有操作仅记录日志，不实际向券商发送委托。
    接口与 TradeExecutor 保持 100% 一致，便于策略引擎在实盘/模拟模式间无缝切换。
    """

    def __init__(self):
        self.order_log = []      # 记录所有模拟订单
        self._order_id_counter = 100000  # 模拟订单号从 100000 起
        self._connected = False
        self._virtual_cash = 1000000.0   # 默认模拟资金 100 万
        self._virtual_positions = {}     # symbol -> {volume, available, cost, market_value}

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------
    def connect(self) -> bool:
        """模拟连接"""
        self._connected = True
        print(f"[{_now_str()}] [SimulatedExecutor] [模拟] 连接成功")
        return True

    def disconnect(self):
        """模拟断开"""
        self._connected = False
        print(f"[{_now_str()}] [SimulatedExecutor] [模拟] 已断开连接")

    @property
    def is_connected(self) -> bool:
        """是否已连接"""
        return self._connected

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    def _next_order_id(self) -> int:
        self._order_id_counter += 1
        return self._order_id_counter

    def _log_order(self, action: str, symbol: str, price: float, volume: int,
                   price_type: str, order_remark: str, order_id: int):
        """记录模拟订单"""
        record = {
            "time": _now_str(),
            "action": action,
            "symbol": symbol,
            "price": price,
            "volume": volume,
            "price_type": price_type,
            "remark": order_remark,
            "order_id": order_id,
        }
        self.order_log.append(record)

    # ------------------------------------------------------------------
    # 下单 / 撤单
    # ------------------------------------------------------------------
    def buy(self, symbol: str, price: float, volume: int,
            price_type: str = "limit", order_remark: str = "") -> int:
        """模拟买入"""
        order_id = self._next_order_id()
        self._log_order("buy", symbol, price, volume, price_type, order_remark, order_id)
        print(f"[{_now_str()}] [SimulatedExecutor] [模拟买入] {symbol} "
              f"价格={price} 数量={volume} price_type={price_type} "
              f"remark={order_remark} order_id={order_id}")

        # 更新虚拟持仓（简化处理：立即成交）
        total_cost = price * volume
        if total_cost <= self._virtual_cash:
            self._virtual_cash -= total_cost
            if symbol in self._virtual_positions:
                pos = self._virtual_positions[symbol]
                old_cost = pos["cost"] * pos["volume"]
                pos["volume"] += volume
                pos["available"] += volume
                pos["cost"] = (old_cost + total_cost) / pos["volume"]
                pos["market_value"] = pos["volume"] * price
            else:
                self._virtual_positions[symbol] = {
                    "volume": volume,
                    "available": volume,
                    "cost": price,
                    "market_value": price * volume,
                }
            print(f"  -> 模拟成交，剩余资金: {self._virtual_cash:.2f}")
        else:
            print(f"  -> 模拟资金不足，当前资金: {self._virtual_cash:.2f}")

        return order_id

    def sell(self, symbol: str, price: float, volume: int,
             price_type: str = "limit", order_remark: str = "") -> int:
        """模拟卖出"""
        order_id = self._next_order_id()
        self._log_order("sell", symbol, price, volume, price_type, order_remark, order_id)
        print(f"[{_now_str()}] [SimulatedExecutor] [模拟卖出] {symbol} "
              f"价格={price} 数量={volume} price_type={price_type} "
              f"remark={order_remark} order_id={order_id}")

        # 更新虚拟持仓（简化处理：立即成交）
        if symbol in self._virtual_positions:
            pos = self._virtual_positions[symbol]
            sell_vol = min(volume, pos["volume"])
            proceeds = price * sell_vol
            self._virtual_cash += proceeds
            pos["volume"] -= sell_vol
            pos["available"] -= sell_vol
            pos["market_value"] = pos["volume"] * price
            if pos["volume"] <= 0:
                del self._virtual_positions[symbol]
            print(f"  -> 模拟成交，获得资金: {proceeds:.2f} 剩余资金: {self._virtual_cash:.2f}")
        else:
            print(f"  -> 模拟持仓中没有 {symbol}")

        return order_id

    def cancel(self, order_id: int) -> bool:
        """模拟撤单"""
        print(f"[{_now_str()}] [SimulatedExecutor] [模拟撤单] order_id={order_id}")
        # 模拟撤单总是成功（实际实现可遍历 order_log 做状态更新）
        return True

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def query_asset(self) -> dict:
        """模拟查询资产"""
        total_market_value = sum(
            p.get("market_value", 0.0) for p in self._virtual_positions.values()
        )
        result = {
            "cash": round(self._virtual_cash, 2),
            "total_asset": round(self._virtual_cash + total_market_value, 2),
            "market_value": round(total_market_value, 2),
            "frozen_cash": 0.0,
            "fetch_balance": 0.0,
        }
        print(f"[{_now_str()}] [SimulatedExecutor] [模拟查询资产] {result}")
        return result

    def query_positions(self) -> list:
        """模拟查询持仓"""
        result = []
        for symbol, pos in self._virtual_positions.items():
            result.append({
                "symbol": symbol,
                "volume": pos["volume"],
                "available": pos["available"],
                "cost": pos["cost"],
                "market_value": pos["market_value"],
            })
        print(f"[{_now_str()}] [SimulatedExecutor] [模拟查询持仓] 共 {len(result)} 条")
        return result

    def query_orders(self) -> list:
        """模拟查询当日订单"""
        result = []
        for record in self.order_log:
            # 只返回当天的记录（简化：返回全部模拟记录）
            result.append({
                "order_id": record["order_id"],
                "symbol": record["symbol"],
                "order_type": 23 if record["action"] == "buy" else 24,
                "price": record["price"],
                "volume": record["volume"],
                "traded_volume": record["volume"],  # 模拟全部成交
                "status": 56,  # ORDER_STATUS_FILLED=56 已成交
                "remark": record["remark"],
            })
        print(f"[{_now_str()}] [SimulatedExecutor] [模拟查询订单] 共 {len(result)} 条")
        return result

    # ------------------------------------------------------------------
    # 条件单（模拟 stub，不实际挂单）
    # ------------------------------------------------------------------
    def place_condition_order(self, symbol: str, trigger_price: float, sell_price: float,
                              volume: int, order_remark: str = '') -> int:
        """模拟条件单（仅打印日志，返回 -1 表示模拟模式不实际挂单）"""
        print(f"[{_now_str()}] [SimulatedExecutor] [模拟条件单] 跳过挂单: {symbol} "
              f"触发价={trigger_price:.3f} 委托价={sell_price:.3f} 数量={volume}")
        return -1  # 模拟模式不挂真实条件单

    def cancel_condition_order(self, condition_order_id: int) -> bool:
        """模拟撤销条件单（直接返回 True）"""
        print(f"[{_now_str()}] [SimulatedExecutor] [模拟条件单] 跳过撤销: cond_id={condition_order_id}")
        return True
