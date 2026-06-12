# ai/gemini_client.py
# Gemini API 래퍼
# 텍스트 데이터 기반 분석 (이미지 제거 - Ver5.1)

import gc
import json
import time
from typing import Dict, Any, Optional, List

from google import genai
from google.genai import types

from config import GEMINI, get_logger
from ai.prompts import (
    create_phase4_recheck_prompt,
    create_entry_filter_prompt
)

logger = get_logger("gemini_client")


# 재점검 시간 범위 상수
MIN_WAIT_HOURS = 0.1        # 6분 (Phase 2 WAIT)
MAX_WAIT_HOURS = 24.0       # 24시간
MIN_RECHECK_HOURS = 0.05    # 3분 (Phase 4 긴급)
MAX_RECHECK_HOURS = 12.0    # 12시간
DEFAULT_WAIT_HOURS = 4.0    # 기본 대기
DEFAULT_RECHECK_HOURS = 1.0 # 기본 재점검


def clamp_hours(value: float, min_val: float, max_val: float, default: float) -> float:
    """시간 값을 유효 범위로 클램핑"""
    if value is None or not isinstance(value, (int, float)):
        return default
    return max(min_val, min(max_val, float(value)))


class GeminiClient:
    """
    Gemini API 클라이언트
    
    Phase 2, 3, 4에서 AI 분석 수행
    google.genai SDK 사용
    """
    
    MAX_RETRIES = 3
    RETRY_DELAY = 5  # 초
    
    def __init__(self):
        self.client = genai.Client(api_key=GEMINI.API_KEY)
        self.model_id = GEMINI.MODEL_ID
        
        self.common_config = {
            "temperature": 0.7,
            "top_p": 0.95,
            "top_k": 40,
            "max_output_tokens": 65536,
            "response_mime_type": "application/json",
            
            "thinking_config": {
                "include_thoughts": True,
                "thinking_level": "HIGH"
            },
            
            "safety_settings": [
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
            ]
        }
        
        logger.info(f"Gemini 클라이언트 초기화: {self.model_id}, Thinking: HIGH")
    
    def _call_with_retry(self, prompt: str) -> Optional[Dict[str, Any]]:
        """
        재시도 로직이 포함된 API 호출 (텍스트 전용)
        
        Args:
            prompt: 텍스트 프롬프트
        
        Returns:
            파싱된 JSON 딕셔너리 또는 None
        """
        for attempt in range(self.MAX_RETRIES):
            try:
                logger.debug(f"Gemini API 호출 시도 {attempt + 1}/{self.MAX_RETRIES}")
                
                # Content 구조 생성 (텍스트만)
                contents = []
                parts = [types.Part.from_text(text=prompt)]
                contents.append(types.Content(role="user", parts=parts))
                
                # API 호출
                response = self.client.models.generate_content(
                    model=self.model_id,
                    contents=contents,
                    config=self.common_config
                )
                
                if not response or not response.text:
                    logger.warning(f"빈 응답 수신 - 시도 {attempt + 1}")
                    time.sleep(2 ** attempt)
                    continue
                
                logger.debug("=" * 60)
                logger.debug("Gemini Raw Response:")
                logger.debug(response.text[:500] + "..." if len(response.text) > 500 else response.text)
                logger.debug("=" * 60)
                
                # JSON 파싱
                parsed_result = self._parse_json_response(response.text)
                
                # 메모리 정리
                del contents
                del response
                gc.collect()
                
                if parsed_result:
                    return parsed_result
                else:
                    logger.warning(f"JSON 파싱 실패 - 시도 {attempt + 1}")
                    time.sleep(2 ** attempt)
                    continue
                
            except Exception as e:
                logger.error(f"Gemini API 오류 - 시도 {attempt + 1}: {e}")
                
                # 메모리 정리
                if 'contents' in locals():
                    del contents
                if 'response' in locals():
                    del response
                gc.collect()
                
                if attempt < self.MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)
                else:
                    return None
        
        return None
    
    def _parse_json_response(self, response_text: str) -> Optional[Dict[str, Any]]:
        """
        Thinking Process 분리 및 JSON 추출
        """
        try:
            cleaned_text = response_text.strip()
            
            # Case 1: ```json 블록이 있는 경우
            if "```json" in cleaned_text:
                parts = cleaned_text.split("```json")
                
                # Thought process 로깅
                if len(parts) > 1:
                    thought_process = parts[0].strip()
                    if thought_process:
                        logger.info("[AI Thought Process]")
                        logger.info(thought_process[:300] + "..." if len(thought_process) > 300 else thought_process)
                
                json_part = parts[-1].split("```")[0].strip()
                return json.loads(json_part)
            
            # Case 2: ``` 블록만 있는 경우
            elif "```" in cleaned_text:
                parts = cleaned_text.split("```")
                
                if len(parts) > 1:
                    thought_process = parts[0].strip()
                    if thought_process:
                        logger.info("[AI Thought Process]")
                        logger.info(thought_process[:300] + "..." if len(thought_process) > 300 else thought_process)
                
                # 역순으로 JSON 찾기
                for part in reversed(parts):
                    part = part.strip()
                    if part.startswith("{") and part.endswith("}"):
                        return json.loads(part)
            
            # Case 3: 순수 JSON 응답
            if cleaned_text.startswith("{") and cleaned_text.endswith("}"):
                return json.loads(cleaned_text)
            
            # Case 4: 텍스트 중간에 JSON이 있는 경우
            start_idx = cleaned_text.find('{')
            end_idx = cleaned_text.rfind('}')
            
            if start_idx != -1 and end_idx != -1:
                if start_idx > 0:
                    thought = cleaned_text[:start_idx].strip()
                    if thought:
                        logger.info("[AI Thought Process - Mixed]")
                        logger.info(thought[:300] + "..." if len(thought) > 300 else thought)
                
                json_str = cleaned_text[start_idx:end_idx + 1]
                return json.loads(json_str)
            
            logger.warning("응답에서 유효한 JSON을 찾을 수 없음")
            return None
            
        except json.JSONDecodeError as e:
            logger.error(f"JSON 디코드 오류: {e}")
            return None
        except Exception as e:
            logger.error(f"응답 파싱 오류: {e}")
            return None
    
    # Ver X: analyze_direction, plan_strategy 제거
    # Phase 2/3은 regime_engine.py가 담당
    # AI는 filter_entry (진입 필터) + recheck_position (중간 점검)만 수행
    
    def recheck_position(
        self,
        market_data: Dict[str, Any],
        position_info: Dict[str, Any],
        elapsed_hours: float,
        unrealized_pnl_pct: float,
        prev_pnl_pct: Optional[float] = None,
        peak_pnl_pct: float = 0.0,
        prev_decision: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Phase 4: 중간 점검 (텍스트 데이터 전용)
        
        Args:
            market_data: 시장 데이터
            position_info: 포지션 정보
            elapsed_hours: 경과 시간
            unrealized_pnl_pct: 미실현 손익 %
            prev_pnl_pct: 직전 점검 시 PnL % (첫 점검이면 None)
            peak_pnl_pct: 점검 이력 중 최고 PnL %
            prev_decision: 직전 점검 AI 결정
        
        Returns:
            {decision, new_stop_loss, new_take_profit, next_recheck_hours, reason}
        """
        logger.info(
            f"Phase 4 중간점검: 경과={elapsed_hours:.1f}h PnL={unrealized_pnl_pct:+.2f}% "
            f"(직전={prev_pnl_pct:+.2f}% 피크={peak_pnl_pct:+.2f}%)" if prev_pnl_pct is not None
            else f"Phase 4 중간점검: 경과={elapsed_hours:.1f}h PnL={unrealized_pnl_pct:+.2f}% (첫 점검)"
        )
        
        prompt = create_phase4_recheck_prompt(
            market_data, position_info, elapsed_hours, unrealized_pnl_pct,
            prev_pnl_pct=prev_pnl_pct,
            peak_pnl_pct=peak_pnl_pct,
            prev_decision=prev_decision
        )
        result = self._call_with_retry(prompt)
        
        if not result:
            logger.error("Phase 4 AI 응답 실패")
            return {
                "decision": "HOLD",
                "reason": "AI 응답 실패",
                "next_recheck_hours": DEFAULT_RECHECK_HOURS
            }
        
        # 응답 검증 및 정규화
        result = self._validate_and_normalize_phase4(result)
        
        logger.info(
            f"Phase 4 중간점검 결과: {result.get('decision')} "
            f"다음점검={result.get('next_recheck_hours')}h"
        )
        
        return result
    
    def filter_entry(
        self,
        market_data: Dict[str, Any],
        regime: str,
        direction: str,
        signal_reason: str,
        signal_score: int
    ) -> Dict[str, Any]:
        """
        Ver X: AI 진입 필터 (PASS/REJECT만)
        
        코드가 이미 결정한 진입을 AI가 최종 검토.
        PASS면 진입, REJECT면 WAIT.
        """
        logger.info(f"AI 진입 필터: {regime} {direction} (점수={signal_score})")
        
        prompt = create_entry_filter_prompt(
            market_data, regime, direction, signal_reason, signal_score
        )
        result = self._call_with_retry(prompt)
        
        if not result:
            logger.warning("AI 필터 응답 실패 → 안전상 REJECT")
            return {
                "decision": "REJECT",
                "reason": "AI 응답 실패",
                "review": "AI 응답 실패로 안전상 거부",
                "risk_note": None
            }
        
        decision = result.get("decision", "REJECT")
        if decision not in ["PASS", "REJECT"]:
            logger.warning(f"AI 필터 비정상 응답: {decision} → REJECT")
            result["decision"] = "REJECT"
        
        logger.info(f"AI 필터 결과: {result.get('decision')} - {result.get('reason', '')}")
        return result

    # ========== 응답 검증 및 정규화 메서드 ==========
    # Ver X: Phase 2/3 validation 제거 (regime_engine이 대체)
    # Phase 4 validation만 유지
    
    def _validate_and_normalize_phase4(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """Phase 4 응답 검증 및 정규화"""
        default_response = {
            "decision": "HOLD",
            "reason": "응답 검증 실패",
            "next_recheck_hours": DEFAULT_RECHECK_HOURS
        }
        
        if not response:
            return default_response
        
        decision = response.get("decision")
        
        # next_recheck_hours 클램핑 (HOLD, MODIFY 공통)
        if decision in ["HOLD", "MODIFY"]:
            next_recheck = response.get("next_recheck_hours")
            response["next_recheck_hours"] = clamp_hours(
                next_recheck, MIN_RECHECK_HOURS, MAX_RECHECK_HOURS, DEFAULT_RECHECK_HOURS
            )
        
        if decision == "HOLD":
            if "reason" not in response:
                response["reason"] = "유지"
            return response
        
        elif decision == "MODIFY":
            if "reason" not in response:
                response["reason"] = "전략 수정"
            
            # 수정 사항 존재 여부 확인
            has_modification = "new_stop_loss" in response or "new_take_profit" in response
            if not has_modification:
                logger.warning("MODIFY 결정이지만 수정 사항 없음, HOLD 반환")
                return {
                    "decision": "HOLD",
                    "reason": "수정 사항 없음",
                    "next_recheck_hours": response.get("next_recheck_hours", DEFAULT_RECHECK_HOURS)
                }
            return response
        
        elif decision == "EXIT":
            if "reason" not in response:
                response["reason"] = "청산"
            return response
        
        # 알 수 없는 decision
        logger.warning(f"Unknown Phase 4 decision: {decision}, HOLD 반환")
        return default_response


# 싱글톤 인스턴스
gemini_client = GeminiClient()