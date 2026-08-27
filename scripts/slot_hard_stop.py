"""slot_hard_stop.py — 슬롯별 일일 손실 한도 (v1, 순수 + 얇은 경계)

계획서가 "KIS 연동 전 필수"로 지정한 항목. 종목 단위 손절(`exit_rules`)은 있지만
**슬롯 전체가 하루에 얼마까지 잃을 수 있는지**를 정한 규칙이 없었다.
2026-08-25 키움 슬롯에서 4종목이 개장 6분 만에 동시 손절된 사례가 그 공백을 드러냈다.
종목별로는 모두 규칙대로였지만 슬롯 단위로는 아무도 멈추지 않았다.

설계 원칙:
  - **자동 청산하지 않는다.** 한도에 걸리면 그 슬롯의 *신규 매수만* 막고 알린다.
    보유 종목을 강제로 파는 것은 되돌릴 수 없는 행동이라 사람 판단에 맡긴다.
    (기존 종목별 손절·트레일링은 그대로 계속 작동한다.)
  - 하루 단위로 자동 해제된다. 날짜가 바뀌면 상태를 새로 만든다.
  - 판정은 순수 함수, 저장·조회만 경계에 둔다.
  - 실현손익만 본다. 평가손익은 시세 조회에 의존해 값이 흔들리고, 그 흔들림으로
    매수를 막으면 원인을 추적하기 어렵다.

한도 기본값 -3.0%는 **가설이다.** 종목 손절선 -7%와 슬롯당 8종목 전후 운용을 감안한
값이며, 표본이 쌓이면 재조정한다(`SLOT_DAILY_LOSS_LIMIT_PCT`로 덮어쓸 수 있음).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger("slot_hard_stop")

# 슬롯 자본 대비 하루 실현손실 한도(%). 음수로 지정한다.
LIMIT_PCT = float(os.getenv("SLOT_DAILY_LOSS_LIMIT_PCT", "-3.0"))
STATE_NAME = "slot_hard_stop.json"


# ─── 판정 (순수) ─────────────────────────────────────


def _day_of(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return str(value).strip()[:10] or None


def realized_by_slot(roundtrips: Iterable[dict], day: str) -> dict[str, float]:
    """해당 날짜에 **청산된** 라운드트립의 슬롯별 실현손익 합계."""
    out: dict[str, float] = {}
    for rt in roundtrips:
        if _day_of(rt.get("sell_at")) != day:
            continue
        slot = rt.get("slot") or "?"
        out[slot] = out.get(slot, 0.0) + float(rt.get("pnl") or 0.0)
    return out


def evaluate_slot(slot: str, realized: float, capital: float,
                  *, limit_pct: float = LIMIT_PCT) -> dict:
    """슬롯 하나의 한도 판정(순수).

    capital이 0 이하이면 비율을 계산할 수 없으므로 발동하지 않는다 —
    분모를 모르는 상태에서 매수를 막으면 원인을 알 수 없는 차단이 된다.
    """
    limit = -abs(limit_pct)
    if capital <= 0:
        return {"slot": slot, "realized": round(realized), "capital": round(capital),
                "loss_pct": None, "limit_pct": limit, "tripped": False,
                "reason": "슬롯 자본을 알 수 없어 판정하지 않음"}
    loss_pct = round(realized / capital * 100, 2)
    tripped = loss_pct <= limit
    return {
        "slot": slot,
        "realized": round(realized),
        "capital": round(capital),
        "loss_pct": loss_pct,
        "limit_pct": limit,
        "tripped": tripped,
        "reason": (f"오늘 실현손실 {loss_pct}% (한도 {limit}%)" if tripped
                   else f"오늘 {loss_pct}% · 한도 {limit}%"),
    }


def evaluate_all(realized: dict[str, float], capitals: dict[str, float],
                 *, limit_pct: float = LIMIT_PCT) -> list[dict]:
    """모든 슬롯 판정. 손실이 큰 순으로 정렬."""
    slots = set(realized) | set(capitals)
    out = [evaluate_slot(s, realized.get(s, 0.0), capitals.get(s, 0.0), limit_pct=limit_pct)
           for s in slots]
    return sorted(out, key=lambda r: (r["loss_pct"] is None, r["loss_pct"] or 0))


# ─── 상태 (순수 변환) ────────────────────────────────


def empty_state(day: str) -> dict:
    return {"date": day, "tripped": {}}


def normalize_state(state: Optional[dict], day: str) -> dict:
    """날짜가 다르면 새 상태로 — 한도는 하루 단위로 자동 해제된다."""
    if not isinstance(state, dict) or state.get("date") != day:
        return empty_state(day)
    tripped = state.get("tripped")
    return {"date": day, "tripped": tripped if isinstance(tripped, dict) else {}}


def apply_verdicts(state: dict, verdicts: Iterable[dict], *, at: str) -> tuple[dict, list[dict]]:
    """판정 결과를 상태에 반영. (새 상태, 이번에 새로 발동한 슬롯들) 반환.

    이미 발동한 슬롯은 다시 알리지 않는다(멱등) — 5분마다 같은 경보가 오면 무시하게 된다.
    """
    new_state = {"date": state["date"], "tripped": dict(state["tripped"])}
    fresh = []
    for v in verdicts:
        if not v["tripped"]:
            continue
        if v["slot"] in new_state["tripped"]:
            continue
        new_state["tripped"][v["slot"]] = {
            "at": at, "loss_pct": v["loss_pct"], "realized": v["realized"],
            "limit_pct": v["limit_pct"],
        }
        fresh.append(v)
    return new_state, fresh


def is_blocked(state: Optional[dict], slot: str, day: str) -> bool:
    """해당 슬롯이 오늘 매수 차단 상태인지."""
    return slot in normalize_state(state, day)["tripped"]


def block_message(state: dict, slot: str) -> str:
    info = state["tripped"].get(slot) or {}
    return (f"🛑 {slot} 슬롯은 오늘 신규 매수가 막혀 있습니다 — "
            f"일일 손실 한도 {info.get('limit_pct')}% 도달 "
            f"(실현 {info.get('loss_pct')}%, {info.get('at', '')}). "
            f"보유 종목의 손절·트레일링은 계속 작동합니다. 내일 자동 해제됩니다.")


def format_alert(fresh: list[dict]) -> str:
    """새로 발동한 슬롯 경보(순수). 없으면 빈 문자열."""
    if not fresh:
        return ""
    lines = ["🛑 *일일 손실 한도 도달*"]
    for v in fresh:
        lines.append(f"• {v['slot']}: 실현 {v['realized']:,}원 ({v['loss_pct']}%) "
                     f"· 한도 {v['limit_pct']}%")
    lines.append("")
    lines.append("_해당 슬롯 신규 매수를 오늘 하루 막습니다. 보유 종목 손절·트레일링은 유지됩니다._")
    lines.append("_자동 청산은 하지 않습니다 — 정리 여부는 직접 판단하세요._")
    return "\n".join(lines)


# ─── 경계 (I-O) ──────────────────────────────────────


def default_state_path() -> Path:
    try:
        from storage_paths import PATHS
        return Path(PATHS.private_state_file(STATE_NAME))
    except Exception:  # noqa: BLE001
        return Path(__file__).resolve().parent.parent / "data" / "private" / "state" / STATE_NAME


def load_state(path=None, *, day: Optional[str] = None) -> dict:
    day = day or date.today().isoformat()
    p = Path(path) if path else default_state_path()
    try:
        return normalize_state(json.loads(p.read_text(encoding="utf-8")), day)
    except Exception:  # noqa: BLE001
        return empty_state(day)


def save_state(state: dict, path=None) -> None:
    p = Path(path) if path else default_state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.warning("일일 한도 상태 저장 실패: %s", e)


def current_inputs(db_path=None, *, day: Optional[str] = None) -> tuple[dict, dict, str]:
    """운영 DB에서 (슬롯별 오늘 실현손익, 슬롯별 자본, 기준일)."""
    import paper_db
    import trade_analytics as ta

    day = day or date.today().isoformat()
    kwargs = {"db_path": db_path} if db_path else {}
    trades = paper_db.list_trades(limit=100000, **kwargs)
    slots = paper_db.list_slots(**kwargs)
    realized = realized_by_slot(ta.compute_roundtrips(trades), day)
    capitals = {s["name"]: float(s.get("current_capital") or 0) for s in slots}
    return realized, capitals, day


def check_and_record(db_path=None, state_path=None, *,
                     limit_pct: float = LIMIT_PCT,
                     now: Optional[str] = None) -> tuple[dict, list[dict], list[dict]]:
    """판정 → 상태 반영 → 저장. (상태, 전체 판정, 새로 발동분) 반환."""
    realized, capitals, day = current_inputs(db_path)
    verdicts = evaluate_all(realized, capitals, limit_pct=limit_pct)
    state = load_state(state_path, day=day)
    state, fresh = apply_verdicts(
        state, verdicts, at=now or datetime.now().strftime("%H:%M"))
    if fresh:
        save_state(state, state_path)
    return state, verdicts, fresh


def guard_buy(slot: str, db_path=None, state_path=None) -> Optional[str]:
    """매수 직전 관문. 막혀 있으면 사유 문자열, 아니면 None.

    상태 파일만 읽는다(가볍고 결정론적). 판정 갱신은 장중 모니터가 맡는다.
    """
    day = date.today().isoformat()
    state = load_state(state_path, day=day)
    if is_blocked(state, slot, day):
        return block_message(state, slot)
    return None


def _cli() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="슬롯별 일일 손실 한도 점검")
    ap.add_argument("--limit", type=float, default=LIMIT_PCT, help="한도 %% (기본 %.1f)" % LIMIT_PCT)
    ap.add_argument("--db"), ap.add_argument("--state")
    ap.add_argument("--reset", metavar="SLOT", help="해당 슬롯 차단 해제 (all이면 전체)")
    args = ap.parse_args()

    if args.reset:
        day = date.today().isoformat()
        state = load_state(args.state, day=day)
        if args.reset == "all":
            state["tripped"] = {}
        else:
            state["tripped"].pop(args.reset, None)
        save_state(state, args.state)
        print(f"✅ 해제: {args.reset}")
        return 0

    state, verdicts, fresh = check_and_record(args.db, args.state, limit_pct=args.limit)
    print(f"기준일 {state['date']} · 한도 {-abs(args.limit)}%")
    for v in verdicts:
        mark = "🛑" if v["tripped"] else ("🟡" if (v["loss_pct"] or 0) < 0 else "🟢")
        print(f"  {mark} {v['slot']}: {v['reason']}")
    blocked = list(state["tripped"])
    print("차단 중:", ", ".join(blocked) if blocked else "없음")
    if fresh:
        print()
        print(format_alert(fresh))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
