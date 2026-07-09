"""system_info.py — 봇 자기/시스템 인식 결정론 응답기 (v1)

봇이 "자기 자신·자기 상태"에 대한 질문에 wiki RAG/웹으로 새지 않고 **실제 시스템
상태에서 결정론적으로** 답하게 한다.

예) "paper 주소 뭐야", "리밸런싱 됐어?", "무슨 봇 돌고 있어?", "마지막 신호 언제?"

설계:
  - 순수/저의존: 표준 라이브러리 + (자동작업은) action_scheduler만 사용. 망·LLM 불필요.
  - 모든 함수는 cache_dir / sched_path 주입을 받아 테스트에서 tmp 경로로 격리 가능.
  - 라우터가 topic을 정해 answer(topic)을 호출. topic이 없으면 요약(status).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger("system_info")

PROJECT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT / "data" / "cache"
LOG_DIR = PROJECT / "data" / "logs"

# 접속 정보 (web_ui.py / paper_ui.py 기본 포트와 동기화)
WEB_UI_PORT = 8080
PAPER_PORT = 8081

# launchd 서비스 4종 (agent_services.sh LABEL_* 와 동기화)
SERVICES = [
    ("web-ui", f"http://localhost:{WEB_UI_PORT}", "채팅 UI"),
    ("watch-raw", "-", "raw/ 폴더 감시 → 정제 → 인덱싱"),
    ("telegram", "-", "텔레그램 봇 (라우터 → 도구)"),
    ("paper", f"http://localhost:{PAPER_PORT}", "paper trading 사이트"),
]

# 등록된 봇/도구 (라우터 KNOWN_TOOLS + 에이전트)
REGISTERED_BOTS = [
    ("knowledge", "wiki RAG 검색 + 답변"),
    ("schedule", "개인 일정 등록/조회/알림"),
    ("finance", "경제지표(ECOS/FRED) + 원칙 대조"),
    ("invest", "한국 단일종목 차트·기술 분석"),
    ("kium", "모멘텀 Top N 스캔 + 크래시 감지"),
    ("quant", "거시 국면(MSCI 4분면) + 종목 추천"),
    ("ipo", "공모주 일정 + 매력지수 5요소"),
    ("news", "IT/AI 뉴스 다이제스트"),
    ("action_schedule", "자연어 반복작업 예약(뉴스·신호·공모주·국면)"),
    ("agent", "로컬 Gemma 범용 오케스트레이션"),
]

# 플래그 파일 (telegram_bot 상수와 동일 경로)
_FLAG_FILES = {
    "rebalance": "quant_rebalance_last.json",  # {"YYYY-MM": [chat_ids]}
    "kium": "kium_weekly_last.json",           # {"YYYY-Www": [chat_ids]}
    "ipo": "ipo_weekly_last.json",             # {"YYYY-Www": [chat_ids]}
    "signal": "signal_last.json",              # {"date": "YYYY-MM-DD", "keys": [...]}
}


# ─── 내부 헬퍼 ───────────────────────────────────────


def _read_json(path: Path) -> Optional[dict]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.debug(f"플래그 읽기 실패 {path}: {e}")
    return None


def _flag_path(name: str, cache_dir: Optional[Path]) -> Path:
    return (cache_dir or CACHE_DIR) / _FLAG_FILES[name]


def _latest_period_key(data: dict) -> Optional[str]:
    """{"YYYY-MM"|"YYYY-Www": [...]} 중 비어있지 않은 가장 최근 키."""
    if not isinstance(data, dict):
        return None
    keys = [k for k, v in data.items() if v]
    return max(keys) if keys else None


# ─── topic별 응답 ────────────────────────────────────


def access_info() -> str:
    return (
        "🔌 접속 정보\n"
        f"• 채팅 웹 UI: http://localhost:{WEB_UI_PORT}\n"
        f"• Paper 트레이딩: http://localhost:{PAPER_PORT}\n"
        f"• 로그 경로: {LOG_DIR}\n"
        f"• watch_raw 로그: {PROJECT / 'data' / 'watch_raw.log'}"
    )


def service_info() -> str:
    lines = ["🖥 서비스(launchd 4종) — 상태는 `bash agent_services.sh status`로 확인"]
    for name, url, desc in SERVICES:
        tail = f" · {url}" if url and url != "-" else ""
        lines.append(f"• {name}: {desc}{tail}")
    return "\n".join(lines)


def bots_info() -> str:
    lines = ["🤖 등록된 봇/도구"]
    for name, desc in REGISTERED_BOTS:
        lines.append(f"• {name}: {desc}")
    return "\n".join(lines)


def automation_info(cache_dir: Optional[Path] = None,
                    sched_path: Optional[Path] = None) -> str:
    """자연어 예약 작업(action_scheduler) + 시스템 고정작업 마지막 실행."""
    lines = ["🗓 자동 작업 상태"]
    try:
        import action_scheduler as _asch
        scheds = _asch.load_schedules(sched_path)
        if scheds:
            for s in scheds:
                lf = f" · 마지막 실행 {s.last_fired}" if s.last_fired else " · 아직 실행 전"
                lines.append(f"• {s.describe()}{lf}")
        else:
            lines.append("• 등록된 자연어 예약 작업 없음")
    except Exception as e:
        log.debug(f"action_scheduler 로드 실패: {e}")
        lines.append("• (예약 목록 로드 실패)")

    lines.append("\n⚙️ 시스템 고정작업 마지막 실행(플래그 기준)")
    lines.append("• " + _signal_line(cache_dir))
    lines.append("• " + _rebalance_line(cache_dir))
    lines.append("• " + _scan_line(cache_dir))
    return "\n".join(lines)


def _signal_line(cache_dir: Optional[Path]) -> str:
    data = _read_json(_flag_path("signal", cache_dir))
    if not data or not data.get("date"):
        return "기술적 신호: 아직 전송 기록 없음"
    n = len(data.get("keys") or [])
    return f"기술적 신호: 마지막 {data['date']} ({n}건 전송)"


def _rebalance_line(cache_dir: Optional[Path]) -> str:
    data = _read_json(_flag_path("rebalance", cache_dir))
    key = _latest_period_key(data) if data else None
    if not key:
        return "월간 리밸런싱: 아직 추천 푸시 기록 없음"
    return f"월간 리밸런싱: {key} 추천 푸시됨"


def _scan_line(cache_dir: Optional[Path]) -> str:
    kium = _latest_period_key(_read_json(_flag_path("kium", cache_dir)) or {})
    ipo = _latest_period_key(_read_json(_flag_path("ipo", cache_dir)) or {})
    k = kium or "기록 없음"
    i = ipo or "기록 없음"
    return f"주간 스캔: 키움 {k} / IPO {i}"


def signal_info(cache_dir: Optional[Path] = None) -> str:
    data = _read_json(_flag_path("signal", cache_dir))
    if not data or not data.get("date"):
        return "📡 기술적 신호: 아직 전송된 신호가 없습니다."
    keys = data.get("keys") or []
    head = "📡 기술적 신호\n" + f"• 마지막 전송일: {data['date']} ({len(keys)}건)"
    if keys:
        head += "\n• 신호: " + ", ".join(str(k) for k in keys[:8])
    return head


def rebalance_info(cache_dir: Optional[Path] = None) -> str:
    data = _read_json(_flag_path("rebalance", cache_dir))
    key = _latest_period_key(data) if data else None
    if not key:
        return ("🔁 월간 콴텍 리밸런싱: 아직 추천이 푸시된 기록이 없습니다.\n"
                "(매월 첫 영업일 09:30 자동 추천. 아직 실행 전이거나 봇 미가동)")
    return f"🔁 월간 콴텍 리밸런싱: {key} 분 추천이 푸시되었습니다."


def scan_info(cache_dir: Optional[Path] = None) -> str:
    return "🔎 주간 스캔 상태\n• " + _scan_line(cache_dir)


def analysis_info() -> str:
    """트레이드 단위 심화 분석(trade_analytics 위임, LLM 총평 포함). 지연 import."""
    try:
        import trade_analytics
        return trade_analytics.deep_report()
    except Exception as e:
        log.debug(f"심화 분석 실패: {e}")
        return f"🔬 매매 분석 조회 실패: {e}"


def feedback_info() -> str:
    """성과 기반 슬롯 비중 제안(strategy_feedback 위임, 추천 전용). 지연 import."""
    try:
        import strategy_feedback
        return strategy_feedback.proposal()
    except Exception as e:
        log.debug(f"비중 제안 실패: {e}")
        return f"🧭 비중 제안 조회 실패: {e}"


def performance_info() -> str:
    """paper trading 성과 리포트(paper_analytics 위임). 지연 import로 의존 격리."""
    try:
        import paper_analytics
        return paper_analytics.report()
    except Exception as e:
        log.debug(f"성과 조회 실패: {e}")
        return f"📊 paper 성과 조회 실패: {e}"


def status_summary(cache_dir: Optional[Path] = None,
                   sched_path: Optional[Path] = None) -> str:
    return "\n\n".join([
        access_info(),
        bots_info(),
        automation_info(cache_dir=cache_dir, sched_path=sched_path),
    ])


# ─── 진입점 ──────────────────────────────────────────

_DISPATCH = {
    "access": lambda c, s: access_info(),
    "service": lambda c, s: service_info(),
    "bots": lambda c, s: bots_info(),
    "automation": lambda c, s: automation_info(cache_dir=c, sched_path=s),
    "signal": lambda c, s: signal_info(cache_dir=c),
    "rebalance": lambda c, s: rebalance_info(cache_dir=c),
    "scan": lambda c, s: scan_info(cache_dir=c),
    "performance": lambda c, s: performance_info(),
    "feedback": lambda c, s: feedback_info(),
    "analysis": lambda c, s: analysis_info(),
    "status": lambda c, s: status_summary(cache_dir=c, sched_path=s),
}


def answer(topic: Optional[str] = None,
           cache_dir: Optional[Path] = None,
           sched_path: Optional[Path] = None) -> str:
    """topic에 맞는 시스템 상태 문자열. 알 수 없으면 status 요약."""
    fn = _DISPATCH.get((topic or "status").strip().lower(), _DISPATCH["status"])
    return fn(cache_dir, sched_path)


def run(topic: Optional[str] = None, **kwargs) -> tuple[str, list]:
    """telegram_bot 분기 호환 — (answer, chunks) 형태."""
    return answer(topic), []


if __name__ == "__main__":
    import sys
    t = sys.argv[1] if len(sys.argv) > 1 else None
    print(answer(t))
