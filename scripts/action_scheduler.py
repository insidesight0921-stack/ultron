"""action_scheduler.py — 자연어로 등록하는 '봇 작업' 스케줄러 (v1)

기존 schedule_bot은 사용자에게 보내는 *리마인더(텍스트)* 였다. 이 모듈은
지정 시각에 **실제 봇 동작(news/signal/ipo/quant)** 을 실행·전송하는 반복 작업을
관리한다. "매일 8시 IT 뉴스 보내" 같은 자연어 → 라우터가 파싱 → 여기 등록 →
telegram 디스패처(60초)가 시각 맞으면 해당 봇 실행.

설계:
  - 순수 로직(CRUD·is_due) — 외부 의존 없음, 단위 테스트 용이.
  - 실제 봇 실행 콜러블은 telegram_bot에서 ACTION_RUNNERS로 주입(여기선 정의만).
  - 영속: ``storage_paths``의 Private state. 멱등: last_fired(YYYY-MM-DD) 동일 occurrence 1회.
  - freq: "daily" | "weekly"(weekday 0=월~6=일). time "HH:MM". until "YYYY-MM-DD"|None.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from storage_paths import PATHS

log = logging.getLogger("action_scheduler")

_DATA_DIR = PATHS.private_root
_STORE_PATH = PATHS.action_schedules

# 등록 가능한 봇 액션(키) → 사람이 읽는 라벨. 실제 실행 함수는 telegram에서 주입.
ACTION_LABELS = {
    "news": "IT/AI 뉴스 다이제스트",
    "signal": "기술적 신호 스캔",
    "ipo": "공모주 스캔",
    "quant": "거시 국면 진단",
    "performance": "주간 성과 리포트",
}

WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]


@dataclass
class ActionSchedule:
    id: int
    action: str            # ACTION_LABELS 키
    freq: str              # "daily" | "weekly"
    time: str              # "HH:MM" (24h)
    weekday: Optional[int] = None   # weekly일 때 0=월~6=일
    until: Optional[str] = None     # "YYYY-MM-DD" 포함, None=무기한
    enabled: bool = True
    last_fired: Optional[str] = None  # "YYYY-MM-DD"
    created: str = ""

    def describe(self) -> str:
        label = ACTION_LABELS.get(self.action, self.action)
        when = (f"매주 {WEEKDAY_KO[self.weekday]} {self.time}"
                if self.freq == "weekly" and self.weekday is not None
                else f"매일 {self.time}")
        tail = f" (~{self.until})" if self.until else ""
        state = "" if self.enabled else " [중지]"
        return f"#{self.id} {label} · {when}{tail}{state}"


# ─── 영속 ───────────────────────────────────────────


def _load_raw(path: Path = None) -> list[dict]:
    p = path or _STORE_PATH
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")).get("schedules", [])
    except Exception as e:
        log.warning(f"스케줄 로드 실패: {e}")
    return []


def _save_raw(items: list[dict], path: Path = None) -> None:
    p = path or _STORE_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"schedules": items}, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    except Exception as e:
        log.warning(f"스케줄 저장 실패: {e}")


def load_schedules(path: Path = None) -> list[ActionSchedule]:
    return [ActionSchedule(**d) for d in _load_raw(path)]


def save_schedules(scheds: list[ActionSchedule], path: Path = None) -> None:
    _save_raw([asdict(s) for s in scheds], path)


# ─── CRUD ───────────────────────────────────────────


def add_schedule(action: str, freq: str, time_str: str,
                 weekday: Optional[int] = None, until: Optional[str] = None,
                 path: Path = None) -> ActionSchedule:
    if action not in ACTION_LABELS:
        raise ValueError(f"알 수 없는 액션: {action}")
    scheds = load_schedules(path)
    new_id = (max((s.id for s in scheds), default=0) + 1)
    sched = ActionSchedule(
        id=new_id, action=action, freq=freq, time=time_str,
        weekday=weekday, until=until, enabled=True,
        created=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    scheds.append(sched)
    save_schedules(scheds, path)
    return sched


def delete_schedule(sched_id: int, path: Path = None) -> bool:
    scheds = load_schedules(path)
    new = [s for s in scheds if s.id != sched_id]
    if len(new) == len(scheds):
        return False
    save_schedules(new, path)
    return True


def set_enabled(sched_id: int, enabled: bool, path: Path = None) -> bool:
    scheds = load_schedules(path)
    found = False
    for s in scheds:
        if s.id == sched_id:
            s.enabled = enabled
            found = True
    if found:
        save_schedules(scheds, path)
    return found


def format_list(path: Path = None) -> str:
    scheds = load_schedules(path)
    if not scheds:
        return "등록된 자동 작업이 없습니다."
    return "🗓 자동 작업 목록\n" + "\n".join(s.describe() for s in scheds)


# ─── 발화 판정 (순수) ────────────────────────────────


def is_due(sched: ActionSchedule, now: datetime) -> bool:
    """now 시점에 이 스케줄을 실행해야 하는가 (멱등: 같은 날 1회)."""
    if not sched.enabled:
        return False
    today = now.strftime("%Y-%m-%d")
    if sched.until and today > sched.until:
        return False
    if now.strftime("%H:%M") != sched.time:
        return False
    if sched.freq == "weekly" and sched.weekday is not None and now.weekday() != sched.weekday:
        return False
    if sched.last_fired == today:
        return False
    return True


def monthly_rebalance_due(now: datetime, already_pushed: bool,
                          is_first_biz: bool, first_biz_passed: bool,
                          hour: int = 9, minute: int = 30) -> bool:
    """월간 리밸런싱을 지금 발화해야 하는가 (순수, catch-up 포함).

    기존 버그: '첫 영업일 당일 09:30'에 24h 잡 tick이 정확히 맞아야만 발화 →
    봇 재시작으로 tick 위상이 어긋나면 영영 안 떴음(콴텍 슬롯 5/18 이후 정지).

    수정: 이번 달 미발송이고 (당일이면 09:30 이후, 첫 영업일이 이미 지났으면
    봇이 켜진 즉시) 발화 → 재시작/오프라인이어도 다음 tick에 catch-up.
    """
    if already_pushed:
        return False
    if is_first_biz:
        return now.hour > hour or (now.hour == hour and now.minute >= minute)
    return first_biz_passed


def due_now(now: datetime, path: Path = None) -> list[ActionSchedule]:
    return [s for s in load_schedules(path) if is_due(s, now)]


def mark_fired(sched_id: int, now: datetime, path: Path = None) -> None:
    scheds = load_schedules(path)
    for s in scheds:
        if s.id == sched_id:
            s.last_fired = now.strftime("%Y-%m-%d")
    save_schedules(scheds, path)
