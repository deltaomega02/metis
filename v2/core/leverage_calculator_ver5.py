# core/leverage_calculator.py
# 청산가 계산, 포지션 사이즈, AI 전략 검증
# AI가 결정한 레버리지/SL/TP를 안전성 검증만 수행

from typing import Dict, Any, Tuple

from config import TRADING, get_logger

logger = get_logger("leverage_calculator")


class InvalidStopLossError(Exception):
    """손절가가 청산가에 너무 가까움"""
    pass


def calculate_liquidation_price(
    entry_price: float,
    leverage: int,
    direction: str
) -> float:
    """
    청산가 계산
    
    Bybit Cross Margin 기준, 유지마진율 0.4% 적용
    
    Args:
        entry_price: 진입가
        leverage: 레버리지 배수
        direction: 포지션 방향 (LONG | SHORT)
    
    Returns:
        청산가
    """
    mmr = TRADING.MAINTENANCE_MARGIN_RATE  # 0.004
    
    if direction == "LONG":
        liquidation = entry_price * (1 - 1/leverage + mmr)
    else:
        liquidation = entry_price * (1 + 1/leverage - mmr)
    
    return round(liquidation, 2)


def validate_stop_loss_margin(
    stop_loss: float,
    liquidation: float,
    direction: str
) -> bool:
    """
    손절가가 청산가보다 안전한지 검증
    
    손절가는 청산가보다 최소 2% 마진 확보 필요
    
    Args:
        stop_loss: 손절가
        liquidation: 청산가
        direction: 포지션 방향
    
    Returns:
        검증 통과 여부
    """
    margin_pct = TRADING.STOP_LOSS_MARGIN_PCT  # 0.02
    
    if direction == "LONG":
        safe_stop = liquidation * (1 + margin_pct)
        return stop_loss >= safe_stop
    else:
        safe_stop = liquidation * (1 - margin_pct)
        return stop_loss <= safe_stop


def calculate_position_size(
    balance: float,
    leverage: int,
    entry_price: float
) -> Tuple[float, float]:
    """
    포지션 사이즈 계산
    
    Args:
        balance: 사용 가능 잔고 (USDT)
        leverage: 레버리지 배수
        entry_price: 진입 예정가
    
    Returns:
        (position_value_usdt, quantity_btc)
    """
    margin = balance
    position_value = margin * leverage
    quantity = position_value / entry_price
    
    quantity = round(quantity, TRADING.QTY_PRECISION)
    quantity = max(TRADING.MIN_ORDER_QTY, quantity)
    
    actual_position_value = quantity * entry_price
    
    return round(actual_position_value, 2), quantity


def calculate_pnl(
    entry_price: float,
    exit_price: float,
    quantity: float,
    direction: str,
    leverage: int
) -> Tuple[float, float]:
    """
    손익 계산
    
    Args:
        entry_price: 진입가
        exit_price: 청산가
        quantity: 수량 (BTC)
        direction: 포지션 방향
        leverage: 레버리지
    
    Returns:
        (pnl_usdt, pnl_percentage)
    """
    if direction == "LONG":
        price_diff = exit_price - entry_price
    else:
        price_diff = entry_price - exit_price
    
    pnl_usdt = price_diff * quantity
    pnl_pct = (price_diff / entry_price) * leverage * 100
    
    return round(pnl_usdt, 2), round(pnl_pct, 2)


def calculate_fee(
    position_value: float,
    is_maker: bool = False
) -> float:
    """
    수수료 계산
    
    Args:
        position_value: 포지션 가치 (USDT)
        is_maker: 메이커 여부
    
    Returns:
        수수료 (USDT)
    """
    fee_rate = TRADING.MAKER_FEE_PCT if is_maker else TRADING.TAKER_FEE_PCT
    return round(position_value * fee_rate, 4)


def validate_ai_strategy(
    current_price: float,
    balance: float,
    direction: str,
    leverage: int,
    stop_loss_price: float,
    take_profit_price: float
) -> Dict[str, Any]:
    """
    AI가 결정한 전략을 안전성 검증만 수행
    
    AI Phase 3 출력값(leverage, stop_loss_price, take_profit_price)을
    그대로 사용하되, 시스템 안전장치만 적용.
    
    Args:
        current_price: 현재가
        balance: 사용 가능 잔고
        direction: 포지션 방향
        leverage: AI가 결정한 레버리지
        stop_loss_price: AI가 결정한 손절가 (절대가)
        take_profit_price: AI가 결정한 익절가 (절대가)
    
    Returns:
        검증된 전략 딕셔너리
    """
    # 1. 레버리지 클램핑 (1~10 범위)
    leverage = max(TRADING.MIN_LEVERAGE, min(TRADING.MAX_LEVERAGE, int(leverage)))
    
    # 2. 청산가 계산
    liquidation = calculate_liquidation_price(current_price, leverage, direction)
    
    # 3. 손절가-청산가 마진 검증
    if not validate_stop_loss_margin(stop_loss_price, liquidation, direction):
        return {
            "valid": False,
            "reason": f"손절가({stop_loss_price:.0f})가 청산가({liquidation:.0f})에 너무 가깝습니다. "
                      f"레버리지를 낮추거나 손절가를 조정하세요."
        }
    
    # 4. SL/TP 방향 검증
    if direction == "LONG":
        if stop_loss_price >= current_price:
            return {"valid": False, "reason": f"LONG 손절가({stop_loss_price:.0f})가 현재가({current_price:.0f}) 이상"}
        if take_profit_price <= current_price:
            return {"valid": False, "reason": f"LONG 익절가({take_profit_price:.0f})가 현재가({current_price:.0f}) 이하"}
    else:
        if stop_loss_price <= current_price:
            return {"valid": False, "reason": f"SHORT 손절가({stop_loss_price:.0f})가 현재가({current_price:.0f}) 이하"}
        if take_profit_price >= current_price:
            return {"valid": False, "reason": f"SHORT 익절가({take_profit_price:.0f})가 현재가({current_price:.0f}) 이상"}
    
    # 5. 포지션 사이즈
    position_value, quantity = calculate_position_size(balance, leverage, current_price)
    
    if quantity < TRADING.MIN_ORDER_QTY:
        return {
            "valid": False,
            "reason": f"주문 수량({quantity} BTC)이 최소 단위({TRADING.MIN_ORDER_QTY} BTC) 미만"
        }
    
    # 6. 예상 수수료
    fee_entry = calculate_fee(position_value)
    fee_exit = calculate_fee(position_value)
    total_fee = fee_entry + fee_exit
    
    # 7. R:R 비율 (정보 제공용, 거부하지 않음)
    risk = abs(current_price - stop_loss_price)
    reward = abs(take_profit_price - current_price)
    rr_ratio = reward / risk if risk > 0 else 0
    
    # 8. 손절/익절 비율 역산
    stop_loss_pct = abs(current_price - stop_loss_price) / current_price * 100
    take_profit_pct = abs(take_profit_price - current_price) / current_price * 100
    
    # 9. 실제 사용 마진
    actual_margin = position_value / leverage
    
    return {
        "valid": True,
        "direction": direction,
        "leverage": leverage,
        "entry_price": round(current_price, 2),
        "stop_loss_price": round(stop_loss_price, 2),
        "take_profit_price": round(take_profit_price, 2),
        "liquidation_price": round(liquidation, 2),
        "position_size_usdt": position_value,
        "quantity_btc": quantity,
        "margin_used": round(actual_margin, 2),
        "estimated_fee": total_fee,
        "risk_reward_ratio": round(rr_ratio, 2),
        "stop_loss_pct": round(stop_loss_pct, 2),
        "take_profit_pct": round(take_profit_pct, 2)
    }


def is_near_liquidation(
    current_price: float,
    liquidation_price: float,
    direction: str
) -> bool:
    """
    청산가 근접 여부 확인
    
    3% 이내 접근 시 True
    """
    if liquidation_price <= 0 or current_price <= 0:
        logger.warning(f"청산가 무효: current={current_price}, liq={liquidation_price}")
        return False
    
    if direction == "LONG":
        distance_pct = (current_price - liquidation_price) / current_price
    else:
        distance_pct = (liquidation_price - current_price) / current_price
    
    return distance_pct <= TRADING.LIQUIDATION_WARN_PCT


def get_liquidation_distance(
    current_price: float,
    liquidation_price: float,
    direction: str
) -> float:
    """
    청산가까지 거리 (%) 반환
    """
    if liquidation_price <= 0 or current_price <= 0:
        logger.warning(f"청산가 거리 계산 무효: current={current_price}, liq={liquidation_price}")
        return 100.0
    
    if direction == "LONG":
        return ((current_price - liquidation_price) / current_price) * 100
    else:
        return ((liquidation_price - current_price) / current_price) * 100