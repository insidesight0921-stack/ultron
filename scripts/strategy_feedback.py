"""strategy_feedback.py — 성과→전략(슬롯 비중) 피드백 루프 골격 (v0, 추천 전용)

paper 성과(performance_stats)를 슬롯 자본 비중에 **보수적으로** 되먹이는 제안을 만든다.
⚠️ v0는 **추천만** 한다 — 어떤 비중도 자동 적용하지 않는다. 표본이 쌓이고 검증된 뒤
telegram 리밸런싱 잡에 연결할 수 있도록 순수·결정론 함수로 분리했다.

가드(과적합·소표본 방어):
  - MIN_SAMPLE 미만 완결 슬롯은 **고정**(중립) — 신호로 안 봄.
  - 조정폭은 슬롯당 ±MAX_TILT(기본 5%p)로 캡.
  - 델타 합 = 0 (총비중 보존). better 슬롯↑ / worse 슬롯↓.
  - 정성적 점수 = 수익률 + 샤프(위험조정) 가벼운 가중.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("strategy_feedback")

MIN_SAMPLE = 10          # 완결 거래 이 수 미만이면 조정 안 함(고정)
MAX_TILT = 0.05          # 슬롯당 최대 ±5%p
SHARPE_WEIGHT = 1.0      # 점수 = 수익률(%) + SHARPE_WEIGHT * 샤프
SENSITIVITY = 0.004      # 점수 1점차 → 비중 델타(상한 MAX_TILT로 재캡)
EWMA_HALFLIFE = 4.0      # 평활 반감기(스냅샷 개수). 최근 스냅샷에 더 큰 가중
MIN_SNAPSHOTS = 3        # 이 개수 이상 history가 있어야 EWMA 평활 사용(아니면 최신 스냅샷)
APPLY_MIN_DELTA = 0.5    # 실제 적용 시 이 %p 미만 변화는 무시(노이즈)


# ─── 순수 계산 ───────────────────────────────────────


def perf_score(stat: dict) -> float:
    """슬롯 성과 점수 (수익률 + 위험조정). 높을수록 좋음."""
    ret = stat.get("total_return_pct") or 0.0
    sharpe = stat.get("sharpe") or 0.0
    return ret + SHARPE_WEIGHT * sharpe


def ewma_score(series: list[float], halflife: float = EWMA_HALFLIFE) -> float:
    """오래된→최신 순 점수 series의 지수가중평균(최신 가중↑). series 비면 0."""
    if not series:
        return 0.0
    n = len(series)
    num = den = 0.0
    for idx, val in enumerate(series):
        age = (n - 1) - idx  # 최신=0
        w = 0.5 ** (age / halflife) if halflife > 0 else (1.0 if age == 0 else 0.0)
        num += w * val
        den += w
    return num / den if den else 0.0


def scores_from_history(history: list[dict], halflife: float = EWMA_HALFLIFE) -> dict[str, float]:
    """성과 history(오래된→최신) → 슬롯별 EWMA 평활 점수.

    각 스냅샷 슬롯 dict(total_return_pct·sharpe)를 perf_score로 환산해 시계열을 만든 뒤 평활.
    """
    series: dict[str, list[float]] = {}
    for rec in history:
        for name, sd in (rec.get("slots") or {}).items():
            series.setdefault(name, []).append(perf_score(sd))
    return {name: ewma_score(vals, halflife) for name, vals in series.items()}


def _neutral(name: str, cur: float, reason: str) -> dict:
    pct = round(cur * 100, 1)
    return {"slot_name": name, "current_pct": pct, "suggested_pct": pct,
            "delta_pct": 0.0, "reason": reason}


def compute_tilts(stats: list[dict], allocations: dict[str, float],
                  max_tilt: float = MAX_TILT, min_sample: int = MIN_SAMPLE,
                  sensitivity: float = SENSITIVITY,
                  scores: Optional[dict] = None) -> list[dict]:
    """성과 stats + 현재 비중 → 비중 조정 제안(순수, 총비중 보존, ±max_tilt 캡).

    allocations: {slot_name: 0..1}. 반환: 슬롯별 current/suggested/delta(%)+reason.
    표본 부족·자격 슬롯 2개 미만이면 전부 중립(조정 없음).
    """
    qual = [s for s in stats
            if (s.get("n_closed") or 0) >= min_sample and s.get("slot_name") in allocations]

    # 조정 대상이 2개 미만이면 의미 있는 재배분 불가 → 전부 중립
    if len(qual) < 2:
        out = []
        for s in stats:
            name = s.get("slot_name")
            if name not in allocations:
                continue
            reason = (f"표본 부족(<{min_sample}건) — 고정"
                      if (s.get("n_closed") or 0) < min_sample else "조정 대상 부족 — 고정")
            out.append(_neutral(name, allocations[name], reason))
        return out

    # scores 주입(EWMA 평활) 있으면 사용, 없으면 최신 스냅샷 perf_score
    qscores = {s["slot_name"]: (scores.get(s["slot_name"], perf_score(s))
                                if scores else perf_score(s)) for s in qual}
    mean = sum(qscores.values()) / len(qscores)
    z = {n: sc - mean for n, sc in qscores.items()}
    maxabs = max((abs(v) for v in z.values()), default=0.0)
    # factor가 sum-zero를 보존하면서 최대 델타를 max_tilt 이내로 만든다
    factor = 0.0 if maxabs == 0 else min(sensitivity, max_tilt / maxabs)
    deltas = {n: z[n] * factor for n in z}

    out = []
    for s in stats:
        name = s.get("slot_name")
        if name not in allocations:
            continue
        if name in deltas:
            cur = allocations[name]
            sug = max(0.01, cur + deltas[name])
            out.append({
                "slot_name": name,
                "current_pct": round(cur * 100, 1),
                "suggested_pct": round(sug * 100, 1),
                "delta_pct": round((sug - cur) * 100, 1),
                "reason": (f"완결 {s.get('n_closed')}건 · 수익률 {s.get('total_return_pct', 0):+.2f}% · "
                           f"샤프 {('%.2f' % s['sharpe']) if s.get('sharpe') is not None else '-'}"),
            })
        else:
            out.append(_neutral(name, allocations[name], f"표본 부족(<{min_sample}건) — 고정"))
    return out


def format_suggestions(suggestions: list[dict]) -> str:
    if not suggestions:
        return "🧭 비중 제안: 데이터가 없습니다."
    any_change = any(abs(x["delta_pct"]) >= 0.05 for x in suggestions)
    lines = ["🧭 성과 기반 비중 제안 (추천 전용 — 자동 적용 안 함)"]
    for x in suggestions:
        d = x["delta_pct"]
        arrow = "▲" if d > 0 else ("▼" if d < 0 else "−")
        lines.append(
            f"\n• {x['slot_name']}: {x['current_pct']}% → {x['suggested_pct']}% "
            f"({arrow}{abs(d)}%p)\n  {x['reason']}"
        )
    if not any_change:
        lines.append("\n\n표본이 더 쌓일 때까지 변경 제안 없음(보수적 가드).")
    return "\n".join(lines)


# ─── DB 연동(지연 import) ────────────────────────────


def current_suggestions(db_path=None) -> list[dict]:
    """실제 paper.db 성과 + 현재 슬롯 비중 → compute_tilts 제안 리스트(원자료).

    history가 충분하면 EWMA 평활 점수를 사용. 실패 시 빈 리스트.
    """
    try:
        import paper_db
        stats = paper_db.performance_stats(db_path) if db_path else paper_db.performance_stats()
        slots = paper_db.list_slots(db_path) if db_path else paper_db.list_slots()
    except Exception:
        log.exception("current_suggestions 데이터 조회 실패")
        return []
    allocations = {s["name"]: float(s.get("allocation_pct") or 0) for s in slots}
    scores = None
    try:
        import paper_analytics
        hist = paper_analytics.load_history()
        if len(hist) >= MIN_SNAPSHOTS:
            scores = scores_from_history(hist)
    except Exception as e:
        log.debug(f"history 로드 실패 — 최신 스냅샷 사용: {e}")
    return compute_tilts(stats, allocations, scores=scores)


def proposal(db_path=None) -> str:
    """실제 paper.db 성과 + 현재 슬롯 비중 → 비중 제안 문자열(추천 전용)."""
    sugs = current_suggestions(db_path)
    if not sugs:
        return "🧭 비중 제안 조회 실패 또는 데이터 없음."
    return format_suggestions(sugs)


# ─── 승인형 적용 골격 (v0.1, 아직 자동 실행/텔레그램 연결 안 함) ─────
# 순수 계획/적용 함수만. 실제 슬롯 비중 쓰기는 setter 주입(승인 시 telegram이 연결).


def build_apply_plan(suggestions: list[dict],
                     min_delta_pct: float = APPLY_MIN_DELTA) -> list[dict]:
    """제안(compute_tilts 출력) → 적용 계획. |delta| >= min_delta_pct 슬롯만(노이즈 컷).

    제안이 sum-보존이므로 계획 delta 합도 ~0(총비중 보존). 중립/미미 변화면 빈 계획.
    """
    plan = []
    for x in suggestions:
        if abs(x.get("delta_pct", 0.0)) >= min_delta_pct:
            plan.append({
                "slot_name": x["slot_name"],
                "from_pct": x["current_pct"],
                "to_pct": x["suggested_pct"],
                "delta_pct": x["delta_pct"],
            })
    return plan


def apply_plan(plan: list[dict], setter, dry_run: bool = True) -> dict:
    """계획을 setter(slot_name, new_fraction)로 적용. dry_run이면 호출 안 하고 미리보기만.

    ⚠️ 기본 dry_run=True — 명시적으로 dry_run=False(사용자 승인)일 때만 실제 쓰기.
    setter: (slot_name:str, new_pct_fraction:float 0..1) → None. telegram이 paper_db 쓰기를 주입.
    반환: {"applied": n, "dry_run": bool, "changes": [...]}.
    """
    if not plan:
        return {"applied": 0, "dry_run": dry_run, "changes": []}
    changes = []
    for p in plan:
        frac = round(p["to_pct"] / 100.0, 4)
        if not dry_run:
            setter(p["slot_name"], frac)
        changes.append({"slot_name": p["slot_name"], "to_pct": p["to_pct"]})
    return {"applied": (0 if dry_run else len(changes)),
            "dry_run": dry_run, "changes": changes}


def format_apply_plan(plan: list[dict]) -> str:
    if not plan:
        return "🧭 적용할 비중 변경이 없습니다(제안이 중립이거나 변화 미미)."
    lines = ["🧭 비중 적용 계획 (승인 대기 — 아직 적용 안 함)"]
    for p in plan:
        arrow = "▲" if p["delta_pct"] > 0 else "▼"
        lines.append(f"• {p['slot_name']}: {p['from_pct']}% → {p['to_pct']}% "
                     f"({arrow}{abs(p['delta_pct'])}%p)")
    lines.append("\n(승인 시에만 slot allocation_pct에 반영됩니다.)")
    return "\n".join(lines)


def run(**kwargs) -> tuple[str, list]:
    return proposal(), []


if __name__ == "__main__":
    print(proposal())
