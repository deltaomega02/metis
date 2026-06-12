# config/logging_config.py
# 구조화된 로그 설정
# 파일 + 콘솔 출력, 일별 로테이션

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """
    로깅 설정 초기화
    
    Args:
        level: 로그 레벨
    
    Returns:
        루트 로거
    """
    logger = logging.getLogger("metis_f")
    logger.setLevel(level)
    
    # 핸들러 중복 방지
    if logger.handlers:
        return logger
    
    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)
    
    # 콘솔 핸들러
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # 파일 핸들러 (일별 로테이션, 7일 보관)
    file_handler = TimedRotatingFileHandler(
        LOG_DIR / "app.log",
        when="midnight",
        interval=1,
        backupCount=7,
        encoding="utf-8"
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    return logger


def get_logger(name: str) -> logging.Logger:
    """
    모듈별 로거 반환
    
    Args:
        name: 모듈명
    
    Returns:
        로거 인스턴스
    """
    return logging.getLogger(f"metis_f.{name}")
