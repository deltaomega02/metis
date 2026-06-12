# core/scheduler.py
# 중간 점검 스케줄링
# Phase 4 포지션 보유 중 주기적 AI 재평가

import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Optional, Dict, Any

from config import SCHEDULER, get_logger

logger = get_logger("scheduler")


class PositionRecheckScheduler:
    """
    포지션 중간 점검 스케줄러
    
    포지션 진입 후 설정된 시간에 AI 재평가 실행
    """
    
    def __init__(
        self,
        on_recheck_callback: Callable[[], None]
    ):
        """
        Args:
            on_recheck_callback: 중간 점검 시 호출할 콜백
        """
        self.on_recheck = on_recheck_callback
        self._timer: Optional[threading.Timer] = None
        self._running = False
        self._next_recheck_time: Optional[datetime] = None
        self._lock = threading.RLock()
    
    def schedule_recheck(self, hours: float):
        """
        중간 점검 예약
        
        Args:
            hours: 다음 점검까지
        """
        with self._lock:
            self.cancel()
            
            self._running = True
            self._next_recheck_time = datetime.now() + timedelta(hours=hours)
            
            seconds = hours * 3600
            self._timer = threading.Timer(seconds, self._execute_recheck)
            self._timer.daemon = True
            self._timer.start()
            
            logger.info(f"중간 점검 예약: {hours}시간 후 ({self._next_recheck_time.strftime('%H:%M')})")
    
    def cancel(self):
        """예약 취소"""
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
            self._running = False
            self._next_recheck_time = None
    
    def _execute_recheck(self):
        """점검 실행"""
        with self._lock:
            if not self._running:
                return
        
        logger.info("중간 점검 실행")
        
        try:
            self.on_recheck()
        except Exception as e:
            logger.error(f"중간 점검 콜백 오류: {e}")
    
    def get_next_recheck_time(self) -> Optional[datetime]:
        """다음 점검 시각 반환"""
        with self._lock:
            return self._next_recheck_time
    
    def is_scheduled(self) -> bool:
        """예약 상태 확인"""
        with self._lock:
            return self._running and self._timer is not None


class DailyReportScheduler:
    """
    일일 리포트 스케줄러
    
    매일 09:00 KST에 리포트 발송
    """
    
    def __init__(
        self,
        on_report_callback: Callable[[], None],
        hour: int = 9,
        minute: int = 0
    ):
        """
        Args:
            on_report_callback: 리포트 발송 콜백
            hour: 발송 시각 (시)
            minute: 발송 시각 (분)
        """
        self.on_report = on_report_callback
        self.hour = hour
        self.minute = minute
        self._timer: Optional[threading.Timer] = None
        self._running = False
    
    def start(self):
        """스케줄러 시작"""
        self._running = True
        self._schedule_next()
        logger.info(f"일일 리포트 스케줄러 시작: 매일 {self.hour:02d}:{self.minute:02d}")
    
    def stop(self):
        """스케줄러 중지"""
        self._running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None
    
    def _schedule_next(self):
        """다음 리포트 시각 계산 및 예약"""
        if not self._running:
            return
        
        now = datetime.now()
        target = now.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
        
        # 오늘 시각이 지났으면 내일로
        if target <= now:
            target += timedelta(days=1)
        
        seconds = (target - now).total_seconds()
        
        self._timer = threading.Timer(seconds, self._execute_report)
        self._timer.daemon = True
        self._timer.start()
        
        logger.info(f"다음 일일 리포트: {target.strftime('%Y-%m-%d %H:%M')}")
    
    def _execute_report(self):
        """리포트 실행"""
        if not self._running:
            return
        
        logger.info("일일 리포트 생성")
        
        try:
            self.on_report()
        except Exception as e:
            logger.error(f"일일 리포트 오류: {e}")
        finally:
            # 다음 날 예약
            self._schedule_next()