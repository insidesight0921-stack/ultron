"""paper_analytics.py — paper trading 성과 분석·리포트 (v1)

paper_db.performance_stats()(슬롯별 FIFO 실현손익·승률·MDD·샤프) 위에 얹는 분석 계층.
봇(슬롯)별 비교, 포트폴리오 집계, 정체(stale) 슬롯 진단, 텔레그램용 리포트 포매팅.

설계:
  - 분석/포매팅 함수는 stats 리스트를 받는 **순수 함수** → DB 없이 hermetic 테스트.
  - report()만 paper_db를 호출(지연 import)해 실제 stats를 가져와 포맷.
  - "성과 어때?"(on-demand) + 주간 성과 리포트(action_scheduler) 양쪽에서 재사용.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from storage_paths import PATHS

log = logging.getLogger("paper_analytics")

_PROJECT = Path(__file__).resolve().parent.parent
HISTORY_PATH = PATHS.private_state_file("perf_history.json")
# 스냅샷에 보존할 슬롯 지표(EWMA 평활·추세용)
_SNAP_FIELDS = ("n_closed", "total_return_pct", "sharpe", "total_pnl", "win_rate")


def record_snapshot(stats: list[dict], now: Optional[datetime] = None,
                    path: Optional[Path] = None) -> dict:
    """현재 성과 stats를 타임스탬프와 함께 history(JSON list)에 append. 추가한 record 반환.

    EWMA 평활·성과 추세의 입력이 된다. 외부 의존 없음(JSON 파일만) → tmp 격리 테스트.
    """
    now = now or datetime.now()
    rec = {
        "ts": now.strftime("%Y-%m-%d %H:%M"),
        "slots": {
            s.get("slot_name", "?"): {k: s.get(k) for k in _SNAP_FIELDS}
            for s in stats
        },
    }
    hist = load_history(path)
    hist.append(rec)
    p = path or HISTORY_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        log.warning(f"성과 스냅샷 저장 실패: {e}")
    return rec


def load_history(path: Optional[Path] = None) -> list[dict]:
    """성과 스냅샷 history(오래된→최신). 없으면 빈 리스트."""
    p = path or HISTORY_PATH
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
    except Exception as e:
        log.warning(f"성과 history 로드 실패: {e}")
    return []


# ─── 순수 분석 ───────────────────────────────────────


def summarize(stats: list[dict]) -> dict:
    """슬롯별 stats → 포트폴리오 집계 dict (순수).

    반환: total_realized_pnl, total_closed, overall_win_rate,
          total_open_cost, best/worst(슬롯명·수익률), active_slots, stale_slots(이름)
    """
    total_pnl = sum(s.get("total_pnl", 0) or 0 for s in stats)
    total_closed = sum(s.get("n_closed", 0) or 0 for s in stats)
    total_wins = sum(
        round((s["win_rate"] / 100.0) * s["n_closed"])
        for s in stats
        if s.get("win_rate") is not None and s.get("n_closed")
    )
    total_open_cost = sum(s.get("open_cost", 0) or 0 for s in stats)

    traded = [s for s in stats if (s.get("n_closed") or 0) > 0]
    best = max(traded, key=lambda s: s["total_return_pct"], default=None)
    worst = min(traded, key=lambda s: s["total_return_pct"], default=None)

    # 정체: 실현거래도 없고 보유 포지션도 없는 슬롯
    stale = [s["slot_name"] for s in stats
             if (s.get("n_closed") or 0) == 0 and (s.get("n_open_positions") or 0) == 0]

    return {
        "total_realized_pnl": round(total_pnl),
        "total_closed": total_closed,
        "overall_win_rate": (round(total_wins / total_closed * 100, 1)
                             if total_closed else None),
        "total_open_cost": round(total_open_cost),
        "active_slots": [s["slot_name"] for s in stats
                         if (s.get("n_closed") or 0) > 0 or (s.get("n_open_positions") or 0) > 0],
        "stale_slots": stale,
        "best": ({"name": best["slot_name"], "return_pct": best["total_return_pct"]}
                 if best else None),
        "worst": ({"name": worst["slot_name"], "return_pct": worst["total_return_pct"]}
                  if worst else None),
    }


def _fmt_won(n: int | float) -> str:
    n = int(round(n or 0))
    sign = "+" if n > 0 else ""
    return f"{sign}{n:,}원"


def format_report(stats: list[dict], summ: Optional[dict] = None) -> str:
    """슬롯별 stats + 집계 → 텔레그램용 성과 리포트 문자열 (순수)."""
    if not stats:
        return "📊 paper 성과: 데이터가 없습니다."
    summ = summ or summarize(stats)
    lines = ["📊 Paper Trading 성과"]

    for s in stats:
        name = s.get("slot_name", "?")
        nc = s.get("n_closed", 0) or 0
        if nc == 0 and (s.get("n_open_positions") or 0) == 0:
            lines.append(f"\n• {name}: 거래 없음 (정체)")
            continue
        wr = s.get("win_rate")
        wr_s = f"{wr:.0f}%" if wr is not None else "-"
        sharpe = s.get("sharpe")
        sh_s = f"{sharpe:.2f}" if sharpe is not None else "-"
        lines.append(
            f"\n• {name}: 실현 {_fmt_won(s.get('total_pnl'))} "
            f"({s.get('total_return_pct', 0):+.2f}%)\n"
            f"  완결 {nc}건 · 승률 {wr_s} · MDD {s.get('max_drawdown_pct', 0):.1f}% · "
            f"샤프 {sh_s} · 보유 {s.get('n_open_positions', 0)}종목"
        )

    lines.append("\n──────────")
    lines.append(
        f"합계: 실현 {_fmt_won(summ['total_realized_pnl'])} · "
        f"완결 {summ['total_closed']}건 · "
        f"전체승률 {('%.0f%%' % summ['overall_win_rate']) if summ['overall_win_rate'] is not None else '-'}"
    )
    if summ.get("best"):
        lines.append(f"🏆 최고: {summ['best']['name']} ({summ['best']['return_pct']:+.2f}%)")
    if summ.get("worst") and summ["worst"]["name"] != (summ.get("best") or {}).get("name"):
        lines.append(f"📉 최저: {summ['worst']['name']} ({summ['worst']['return_pct']:+.2f}%)")
    if summ.get("stale_slots"):
        lines.append(f"⚠️ 정체 슬롯(거래·보유 없음): {', '.join(summ['stale_slots'])}")
    return "\n".join(lines)


# ─── DB 연동(지연 import) ────────────────────────────


def report(db_path=None) -> str:
    """실제 paper.db 성과를 가져와 리포트 문자열로 반환."""
    try:
        import paper_db
        stats = (paper_db.performance_stats(db_path) if db_path
                 else paper_db.performance_stats())
    except Exception as e:
        log.exception("performance_stats 실패")
        return f"📊 paper 성과 조회 실패: {e}"
    return format_report(stats)


def run(**kwargs) -> tuple[str, list]:
    """telegram 분기/예약작업 호환 — (report, chunks)."""
    return report(), []


if __name__ == "__main__":
    print(report())
