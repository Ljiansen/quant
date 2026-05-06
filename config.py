# -*- coding: utf-8 -*-
"""
miniQMT 配置文件
集中管理路径、账号等配置信息
"""

# miniQMT 客户端的 userdata_mini 路径
MINIQMT_PATH = r"D:\迅投QMT交易终端浙商证券金桥版\userdata_mini"

# 证券账号
ACCOUNT_ID = "1520023160"

# 账号类型
ACCOUNT_TYPE = "STOCK"

# session_id，用于区分不同的交易会话（任意正整数）
SESSION_ID = 654321

# 下单测试用的股票代码
TEST_STOCK_CODE = "516630.SH"

# 测试下单价格（接近市价）
TEST_ORDER_PRICE = 1.730

# 测试下单数量（股）
TEST_ORDER_VOLUME = 100

# ===== 数据源配置 =====
DATA_SOURCE = 'baostock'       # 'akshare' 或 'baostock'
DATA_CACHE_DIR = 'd:/miniqmt_quant/data_cache'

# ===== 回测配置 =====
BACKTEST_INITIAL_CAPITAL = 500000   # 初始资金
BACKTEST_COMMISSION_RATE = 0.0003   # 佣金费率
BACKTEST_STAMP_TAX_RATE = 0.001     # 印花税率

# ===== 报告配置 =====
REPORT_OUTPUT_DIR = 'd:/miniqmt_quant/reports'

# ===== 日志配置 =====
LOG_DIR = 'd:/miniqmt_quant/logs'
LOG_LEVEL = 'INFO'

# ===== V3策略参数 =====
V3_TOP_N = 50                    # 选股池大小（综合排名前N）
V3_MAX_POSITIONS = 3             # 最大持仓数
V3_MIN_CHANGE_PCT = 0.01        # 主板/创业板最低涨幅1%
V3_HARD_STOP_LOSS = 0.03        # 硬止损3%（网格优化最优参数）
V3_SOFT_STOP_LOSS = 0.02        # 阴跌止损2%（网格优化最优参数）
V3_TAKE_PROFIT = 0.05           # 主板/创业板固定止盈5%（保留兼容，已被移动止盈替代）
V3_TRAILING_ACTIVATE = 0.005    # 主板/创业板：浮盈超0.5%后激活移动止盈（多周期验证最优参数）
V3_TRAILING_STOP = 0.01         # 主板/创业板：从最高价回撤1%触发卖出（网格优化最优参数）
V3_TIME_STOP_DAYS = 5           # 时间止损天数
V3_COMMISSION_RATE = 0.00025    # 佣金0.025%
V3_MIN_COMMISSION = 5           # 最低佣金5元
V3_STAMP_TAX_RATE = 0.0005      # 印花税0.05%
V3_INITIAL_CAPITAL = 300000     # 初始资金
V3_DATA_SOURCE = 'local'        # V3数据源: 'akshare' / 'baostock' / 'local'
V3_LOCAL_DATA_DIR = 'D:/daily_data'  # 本地数据目录

# 调仓参数
V3_REBALANCE_LOOKBACK = 120     # 回看交易日数（约6个月）

# 每日二次过滤参数
V3_DAILY_MIN_AMOUNT = 500000000 # 近N天日均成交额最低5亿
V3_DAILY_AMOUNT_DAYS = 10       # 日均成交额回看天数

# ===== 科创板独立参数 =====
V3_STAR_MIN_CHANGE_PCT = 0.02   # 科创板买入最低涨幅2%
V3_STAR_TAKE_PROFIT = 0.15      # 科创板止盈15%（保留兼容，已被移动止盈替代）
V3_STAR_TRAILING_ACTIVATE = 0.08  # 科创板：浮盈超8%后激活移动止盈
V3_STAR_TRAILING_STOP = 0.05    # 科创板：从最高价回撤5%触发卖出（网格优化最优参数）
V3_STAR_HARD_STOP_LOSS = 0.03   # 科创板硬止损3%（网格优化最优参数）
V3_STAR_SOFT_STOP_LOSS = 0.02   # 科创板阴跌止损2%（网格优化最优参数）
V3_STAR_TIME_STOP_DAYS = 5      # 科创板时间止损5天
V3_STAR_LIMIT_UP = 0.198        # 科创板涨停保护19.8%
V3_MAX_CHANGE_PCT = 0.07        # 主板最大涨幅（超过此値视为追高，不买），回测最佳参数
V3_STAR_MAX_CHANGE_PCT = 0.08   # 科创/创业板最大涨幅（超过此值视为追高，不买）
V3_PREV_BAR_UP = True              # 买入时要求上一根5分钟K线非阴线（close >= open）

# 明日调仓池5分钟K线预缓存目录（每日收盘后建池时下载，供次日实盘引擎兜底使用）
V3_NEXT_POOL_5MIN_DIR = 'd:/miniqmt_quant/5min_next_pool'

# 实盘下单实时价智能路由参数
V3_LIVE_BUY_SLIP_MAX  = 0.003   # 买入：实时卖一价超过bar_c的最大可接受溢价（超出则挂bar_c等回落）
V3_LIVE_SELL_SLIP_MAX = 0.003   # 卖出：实时买一价低于止损价的最大可接受折价（超出时记警告，仍用买一价优先成交）
