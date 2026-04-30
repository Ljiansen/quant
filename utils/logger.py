# -*- coding: utf-8 -*-
"""
日志工具
提供统一的日志配置，同时输出到控制台和文件
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config


def get_logger(name, log_dir=None, level=None):
    """
    获取 logger，同时输出到控制台和文件

    Args:
        name: logger 名称，也用于生成日志文件名
        log_dir: 日志目录，默认从 config.LOG_DIR 读取
        level: 日志级别，默认从 config.LOG_LEVEL 读取

    Returns:
        logging.Logger 实例
    """
    log_dir = log_dir or getattr(config, "LOG_DIR", "d:/miniqmt_quant/logs")
    level = level or getattr(config, "LOG_LEVEL", "INFO")

    os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if not logger.handlers:
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        # 控制台输出
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        # 文件输出
        log_file = os.path.join(log_dir, f"{name}.log")
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger
