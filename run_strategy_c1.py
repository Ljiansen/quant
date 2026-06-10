# -*- coding: utf-8 -*-
"""C1策略独立回测入口"""
import sys
import json
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine.strategy_c1_engine import StrategyC1Engine


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else '2026-01-01'
    end = sys.argv[2] if len(sys.argv) > 2 else '2026-04-30'
    capital = float(sys.argv[3]) if len(sys.argv) > 3 else 90_000.0

    engine = StrategyC1Engine(capital=capital)
    result = engine.run_backtest(start, end)

    out_path = f'c1_backtest_{start}_{end}.json'
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n[C1] 结果已保存: {out_path}")


if __name__ == '__main__':
    main()
