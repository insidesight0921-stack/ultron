"""exit_rules.py — 장중 손절·익절 판정 (순수, v1.0)

trade_analytics 정밀 진단 후속: 손절 30건 평균 -14.2%(손절선 -7%) → 실행 지연 수정.
telegram_bot.intraday_monitor_job 에서 판정 로직만 분리한 순수 모듈(hermetic 테스트 대상).

규칙(우선순위 순):
  1. 급락/갭 하드스톱: pnl ≤ hard_stop(-12%) → 손절(급락)
  2. 손절선:           pnl ≤ stop(-7%)       → 손절
  3. 익절선:           pnl ≥ take(+20%)      → 익절
  4. 트레일링:         peak ≥ trail_arm(+10%) 도달 후 peak 대비 trail_drop(7%p) 반납 → 청산
     (pnl>0 익절 / pnl≤0 손절 라벨 — trade_analytics._classify_reason 호환)

상수는 환경변수로 조정 가능(손절선 재검토는 실행 지연 수정 후 재측정하여 결정):
  EXIT_STOP_PCT, EXIT_TAKE_PCT, EXIT_HARD_STOP_PCT, EXIT_TRAIL_ARM_PCT, EXIT_TRAIL_DROP_PCT
"""
from __future__ import annotations

import os
from typing import Optional

STOP_PCT = float(os.getenv("EXIT_STOP_PCT", "-0.07"))
TAKE_PCT = float(os.getenv("EXIT_TAKE_PCT", "0.20"))
HARD_STOP_PCT = float(os.getenv("EXIT_HARD_STOP_PCT", "-0.12"))
TRAIL_ARM_PCT = float(os.getenv("EXIT_TRAIL_ARM_PCT", "0.10"))
TRAIL_DROP_PCT = float(os.getenv("EXIT_TRAIL_DROP_PCT", "0.07"))


def should_exit(pnl_pct: float, peak_pnl_pct: Optional[float] = None, *,
                stop: float = STOP_PCT, take: float = TAKE_PCT,
                hard_stop: float = HARD_STOP_PCT,
                trail_arm: float = TRAIL_ARM_PCT,
                trail_drop: float = TRAIL_DROP_PCT) -> Optional[tuple[str, str]]:
    """수익률 → (액션, 사유) or None(보유 유지). 순수.

    반환 액션 ∈ {"손절","익절"} — paper_db notes 및 trade_analytics 사유 분류와 호환.
    사유 ∈ {"급락","손절선","익절선","트레일링"}.
    """
    if pnl_pct <= hard_stop:
        return ("손절", "급락")
    if pnl_pct <= stop:
        return ("손절", "손절선")
    if pnl_pct >= take:
        return ("익절", "익절선")
    if (peak_pnl_pct is not None and peak_pnl_pct >= trail_arm
            and peak_pnl_pct - pnl_pct >= trail_drop):
        return (("익절" if pnl_pct > 0 else "손절"), "트레일링")
    return None


def peak_key(slot_id, ticker: str, avg_price: float) -> str:
    """포지션 식별 키. avg_price 포함 → 재진입/물타기 시 피크 자동 리셋."""
    return f"{slot_id}:{ticker}:{round(float(avg_price), 2)}"


def update_peak(peaks: dict, key: str, pnl_pct: float) -> float:
    """peaks[key]를 max(기존, 현재)로 갱신하고 반환(제자리 수정). 순수(입력 dict 기준)."""
    prev = peaks.get(key)
    cur = pnl_pct if prev is None else max(prev, pnl_pct)
    peaks[key] = cur
    return cur


def prune_peaks(peaks: dict, live_keys: set) -> dict:
    """청산·소멸된 포지션의 피크 제거(신규 dict 반환)."""
    return {k: v for k, v in peaks.items() if k in live_keys}


NAVER_WARN_STREAK = int(os.getenv("EXIT_NAVER_WARN_STREAK", "3"))  # 전멸 연속 경고 임계


def naver_health(n_ok: int, n_total: int, streak: int,
                 warn_streak: int = NAVER_WARN_STREAK) -> tuple[int, bool]:
    """실시간 시세원 건강 판정(순수).

    사이클마다 (성공 수, 시도 수, 직전 연속 전멸 수) → (새 연속 전멸 수, 경고 여부).
    전멸(성공 0)이 warn_streak 연속 도달하는 '순간'에만 True(1회 경고).
    회복하면 streak 리셋. n_total==0(포지션 없음)은 판정 보류.
    """
    if n_total <= 0:
        return streak, False
    streak = streak + 1 if n_ok == 0 else 0
    return streak, streak == warn_streak


MAX_PRICE_JUMP = float(os.getenv("EXIT_MAX_PRICE_JUMP", "0.15"))   # 직전 폴링 대비 의심 임계
PRICE_CONFIRM_TOL = float(os.getenv("EXIT_PRICE_CONFIRM_TOL", "0.05"))  # 2차 소스 일치 허용


def confirm_price(primary: Optional[float], reference: Optional[float],
                  fallback_fn=None, *,
                  max_jump: float = MAX_PRICE_JUMP,
                  tol: float = PRICE_CONFIRM_TOL) -> Optional[float]:
    """오호가 방어 — 직전가(reference) 대비 급변한 1차 시세를 2차 소스로 교차확인.

    - primary 없음 → None
    - reference 없거나 변동 ≤ max_jump → primary 채택
    - 급변 시 fallback_fn() 2차 시세가 primary와 tol 이내로 일치 → primary 채택(실제 급락/급등)
      아니면 None(의심 틱 — 이번 사이클 스킵). 순수(fallback_fn 주입).
    """
    if not primary or primary <= 0:
        return None
    if not reference or reference <= 0:
        return primary
    if abs(primary / reference - 1.0) <= max_jump:
        return primary
    second = None
    if fallback_fn is not None:
        try:
            second = fallback_fn()
        except Exception:
            second = None
    if second and second > 0 and abs(second / primary - 1.0) <= tol:
        return primary
    return None


def parse_naver_price(data) -> Optional[float]:
    """네이버 금융 polling JSON → 현재가(float). 실패 시 None. 순수.

    지원 형태:
      신형: {"datas": [{"closePrice": "71,900", ...}]}
      구형: {"result": {"areas": [{"datas": [{"nv": 71900, ...}]}]}}
    """
    try:
        if not isinstance(data, dict):
            return None
        if "datas" in data and data["datas"]:
            v = data["datas"][0].get("closePrice")
            if v is not None:
                f = float(str(v).replace(",", ""))
                return f if f > 0 else None
        areas = (data.get("result") or {}).get("areas") or []
        if areas and areas[0].get("datas"):
            v = areas[0]["datas"][0].get("nv")
            if v is not None:
                f = float(v)
                return f if f > 0 else None
    except (ValueError, TypeError, AttributeError, IndexError, KeyError):
        pass
    return None
