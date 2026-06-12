# -*- coding: utf-8 -*-
"""
独立子进程下单工作器
由 live_engine_v4.py 通过 subprocess 调用，确保 TradeExecutor 在无 xtdata 的干净环境中运行。
输入：sys.argv[1] = JSON字符串，含 {xt_path, account_id, session_id, symbol, price, volume, remark, action}
输出：最后一行打印 JSON 结果 {ok, oid, n_orders, found_ids}
"""
import sys
import json
import time

sys.path.insert(0, r'd:\miniqmt_quant')


def main():
    if len(sys.argv) < 2:
        print(json.dumps({'ok': False, 'oid': -1, 'error': 'no args'}))
        return

    try:
        params = json.loads(sys.argv[1])
    except Exception as e:
        print(json.dumps({'ok': False, 'oid': -1, 'error': f'json parse: {e}'}))
        return

    from trade.executor import TradeExecutor

    ex = TradeExecutor(
        mini_qmt_path=params['xt_path'],
        account_id=params['account_id'],
        session_id=int(params.get('session_id', 654321)),
    )

    ok = ex.connect()
    if not ok:
        print(json.dumps({'ok': False, 'oid': -1, 'error': 'connect failed'}))
        return

    time.sleep(1.0)  # 等待连接注册

    action = params.get('action', 'buy')
    symbol = params['symbol']
    # A股价格最小变动单位 0.01 元，必须 round 到 2 位小数，否则 miniQMT 会静默拒绝
    price  = round(float(params['price']), 2)
    volume = int(params['volume'])
    remark = params.get('remark', '')

    if action == 'buy':
        oid = ex.buy(symbol=symbol, price=price, volume=volume,
                     price_type='limit', order_remark=remark)
    else:
        oid = ex.sell(symbol=symbol, price=price, volume=volume,
                      price_type='limit', order_remark=remark)

    if oid == -1:
        print(json.dumps({'ok': False, 'oid': -1, 'error': 'order_stock returned -1'}))
        ex.disconnect()
        return

    # 验证：等待 4s 查询委托（miniQMT 委托同步约需 2-3 秒）
    time.sleep(4.0)
    try:
        orders = ex._trader.query_stock_orders(ex._account, cancelable_only=False)
        found_ids = [getattr(o, 'order_id', None) for o in (orders or [])]
        found = oid in found_ids
        result = {
            'ok': found,
            'oid': oid,
            'n_orders': len(found_ids),
            'found_ids': found_ids,
            'found': found,
        }
    except Exception as e:
        # 查询异常时保守认为成功（order_stock已返回正ID）
        result = {'ok': True, 'oid': oid, 'error': f'query failed: {e}'}

    # 卖出时查询真实成交价（加权均价）
    if action == 'sell':
        try:
            trades = ex._trader.query_stock_trades(ex._account)
            if trades:
                # 筛选当前 order_id 的成交记录
                my_trades = [t for t in trades if getattr(t, 'order_id', -1) == oid]
                if my_trades:
                    total_vol = sum(getattr(t, 'traded_volume', 0) for t in my_trades)
                    total_amt = sum(getattr(t, 'traded_volume', 0) * getattr(t, 'traded_price', 0)
                                    for t in my_trades)
                    if total_vol > 0:
                        fill_price = round(total_amt / total_vol, 4)
                        result['fill_price'] = fill_price
                        result['fill_volume'] = total_vol
                        fills = [(getattr(t, 'traded_volume', 0), getattr(t, 'traded_price', 0))
                                 for t in my_trades]
                        result['fill_details'] = fills
                        print(f'[worker] 真实成交: {total_vol}股 @ 均价{fill_price}', file=sys.stderr)
        except Exception as e:
            print(f'[worker] 查询成交明细异常: {e}', file=sys.stderr)

    print(json.dumps(result, ensure_ascii=False))
    ex.disconnect()


if __name__ == '__main__':
    main()
