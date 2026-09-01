"""vkospi_threshold_review.py — VKOSPI 임계값 정기 재측정과 **승인형** 갱신.

**왜 자동 갱신이 아닌가.** 2026-09-01 실측에서 롤링 백분위 자동 갱신을 재봤고,
더 나빴다. 임계값이 값을 따라 올라가면 "평소보다 불안하다"는 판정 자체가
성립하지 않는다.

    407일 기준        상단 발동   하단 발동   밴드 전환
    롤링 250일 자동     90.4%      0.0%        1회   ← 상단이 상수, 하단은 죽음
    롤링 120일 자동     53.3%      5.2%       25회
    고정 60.6/20.7      19.9%     20.1%       32회   ← 채택

자동 갱신에는 두 번째 문제도 있다. 임계값이 조용히 바뀌면 **과거 판단을
재현할 수 없다** — "그때 왜 60%로 내렸지?"에 답할 방법이 없어진다. 슬롯 비중
110% 사고(2026-08-29)와 같은 유형이다.

**그래서 이 모듈은 세 가지만 한다.**

1. 분기마다 다시 재고, 지금 임계값이 분포에서 얼마나 벗어났는지 말한다.
2. 제안값을 **검증한 뒤에만** 내놓는다 — 죽은 가지를 만드는 값은 제안 자체를
   거부한다. 사람이 승인 버튼을 눌러도 통과하지 못한다.
3. 승인된 값을 **추가만 되는 원장**에 적는다. 코드 상수는 초기값(폴백)일 뿐,
   실제로 도는 값은 원장의 마지막 승인 항목이다. 원장에는 그날의 표본 수·분포·
   발동 비율이 함께 남아 **몇 달 뒤에도 그때의 판단을 재현할 수 있다.**
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger("vkospi_threshold_review")

# 재측정 주기. 분기 1회. 더 자주 재면 표본이 거의 그대로라 제안이 흔들리기만 한다.
REVIEW_EVERY_DAYS = 90
# 제안이 이만큼 안 움직이면 알리지 않는다. 소음이 되면 안 본다.
MIN_SHIFT_PCT = 10.0
# 승인 대기가 이만큼 지나면 다시 알린다(월 MAX_RENOTIFY회까지).
RENOTIFY_AFTER_DAYS = 7
MAX_RENOTIFY = 3

# 검증 한계. 어느 한쪽이 이 밖으로 나가면 규칙이 아니라 상수에 가깝다.
MIN_SIDE_PCT = 5.0
MAX_SIDE_PCT = 40.0
MIN_SAMPLE = 120


# ─── 검증 (순수) ─────────────────────────────────────


def validate(values: Iterable[float], high: float, low: float) -> dict:
    """이 임계값 쌍을 **써도 되는가**. 승인 전후로 같은 함수가 판정한다.

    이 프로젝트가 실제로 겪은 실패를 그대로 막는다: `<15`는 407일 중 0일
    걸렸다. 사람이 승인해도 통과시키지 않는다 — 사람은 분포를 안 본다.
    """
    vals = [float(v) for v in (values or []) if v is not None]
    n = len(vals)
    if n < MIN_SAMPLE:
        return {"ok": False, "n": n,
                "reason": f"표본 {n}일 — {MIN_SAMPLE}일 미만이면 판정하지 않는다"}
    if not (low < high):
        return {"ok": False, "n": n,
                "reason": f"하단({low})이 상단({high})보다 작아야 한다"}
    above = sum(1 for v in vals if v > high)
    below = sum(1 for v in vals if v < low)
    a_pct, b_pct = above / n * 100, below / n * 100
    if above == 0 or below == 0:
        dead = "상단" if above == 0 else "하단"
        return {"ok": False, "n": n, "above_pct": round(a_pct, 1),
                "below_pct": round(b_pct, 1),
                "reason": f"{dead} 분기가 한 번도 걸리지 않는다 — 죽은 가지다"}
    if a_pct > MAX_SIDE_PCT or b_pct > MAX_SIDE_PCT:
        side = "상단" if a_pct > MAX_SIDE_PCT else "하단"
        return {"ok": False, "n": n, "above_pct": round(a_pct, 1),
                "below_pct": round(b_pct, 1),
                "reason": f"{side}이 {max(a_pct, b_pct):.1f}% 걸린다 — 규칙이 아니라 상수에 가깝다"}
    if a_pct < MIN_SIDE_PCT or b_pct < MIN_SIDE_PCT:
        side = "상단" if a_pct < MIN_SIDE_PCT else "하단"
        return {"ok": False, "n": n, "above_pct": round(a_pct, 1),
                "below_pct": round(b_pct, 1),
                "reason": f"{side}이 {min(a_pct, b_pct):.1f}%만 걸린다 — 너무 드물어 반응이 늦는다"}
    return {"ok": True, "n": n,
            "above_pct": round(a_pct, 1), "below_pct": round(b_pct, 1)}


# ─── 재측정 (순수) ───────────────────────────────────


def review(values: Iterable[float], *, current_high: float, current_low: float,
           high_pctl: float = 80.0, low_pctl: float = 20.0) -> dict:
    """지금 임계값이 이 분포에서 어떤가 + 제안값.

    verdict는 셋 중 하나다.
      keep     — 지금 값이 멀쩡하고 제안도 크게 다르지 않다
      propose  — 지금 값도 유효하지만 분포가 움직여 조정을 권한다
      broken   — 지금 값이 검증을 통과하지 못한다(죽은 가지 등). 급한 건 이것뿐이다
    """
    import vkospi_calibrate as vc

    vals = [float(v) for v in (values or []) if v is not None]
    cur = validate(vals, current_high, current_low)
    if len(vals) < MIN_SAMPLE:
        return {"verdict": "keep", "n": len(vals), "current": cur,
                "suggested": None,
                "note": f"표본 {len(vals)}일 — 아직 재측정하지 않는다"}

    sug = vc.suggest(vals, high_pctl=high_pctl, low_pctl=low_pctl)
    proposed = None
    if sug.get("ok"):
        check = validate(vals, sug["high"], sug["low"])
        proposed = {"high": sug["high"], "low": sug["low"], "check": check}

    if not cur.get("ok"):
        verdict = "broken"
    elif proposed and proposed["check"].get("ok") and _shifted(
            current_high, current_low, proposed["high"], proposed["low"]):
        verdict = "propose"
    else:
        verdict = "keep"

    return {"verdict": verdict, "n": len(vals), "current": cur,
            "suggested": proposed, "describe": vc.describe(vals)}


def _shifted(cur_hi: float, cur_lo: float, new_hi: float, new_lo: float,
             *, min_pct: float = MIN_SHIFT_PCT) -> bool:
    """제안이 의미 있게 움직였는가. 1~2%는 재측정 잡음이다."""
    def moved(a: float, b: float) -> float:
        return abs(b - a) / a * 100 if a else 100.0
    return max(moved(cur_hi, new_hi), moved(cur_lo, new_lo)) >= min_pct


# ─── 원장 (얇은 I/O) ─────────────────────────────────


def load_ledger(path: Path) -> dict:
    """없으면 빈 원장. 깨졌으면 **빈 원장으로 되돌리지 않고 예외를 남긴다.**

    깨진 원장을 조용히 빈 것으로 취급하면 승인 이력이 사라지고 코드 상수로
    말없이 되돌아간다 — 정확히 이 모듈이 막으려는 일이다.
    """
    p = Path(path)
    if not p.exists():
        return {"active": None, "history": []}
    raw = json.loads(p.read_text(encoding="utf-8"))
    # **아는 키만 골라 담지 않는다.** 처음에 active/history/pending만 옮겼더니
    # `last_reviewed_at`이 왕복마다 사라져 분기 재측정이 **매일** 돌았다
    # (2026-09-01 리뷰에서 발견). 원장은 통째로 보존하고 필수 키만 보정한다.
    out = dict(raw)
    out["active"] = raw.get("active")
    out["history"] = list(raw.get("history") or [])
    out["pending"] = raw.get("pending")
    return out


def active_thresholds(ledger: dict, *, fallback_high: float,
                      fallback_low: float) -> tuple[float, float, str]:
    """지금 유효한 (상단, 하단, 출처). 원장이 비면 코드 상수로 폴백."""
    act = (ledger or {}).get("active") or {}
    hi, lo = act.get("high"), act.get("low")
    if hi is None or lo is None:
        return fallback_high, fallback_low, "코드 초기값"
    try:
        hi, lo = float(hi), float(lo)
    except (TypeError, ValueError):
        log.warning("원장의 임계값을 읽을 수 없다 — 코드 초기값으로 돈다")
        return fallback_high, fallback_low, "코드 초기값(원장 손상)"
    if not (lo < hi):
        log.warning("원장의 임계값 순서가 뒤집혀 있다 — 코드 초기값으로 돈다")
        return fallback_high, fallback_low, "코드 초기값(원장 이상)"
    return hi, lo, f"승인 {act.get('approved_at', '?')}"


def approve(ledger: dict, *, high: float, low: float, values: Iterable[float],
            approved_at: Optional[str] = None, by: str = "user",
            note: str = "") -> dict:
    """승인된 임계값을 원장에 **추가**한다. 기존 항목은 지우지 않는다.

    **검증을 다시 통과해야 한다.** 제안할 때 한 번, 승인할 때 한 번 — 사이에
    분포가 바뀌었을 수 있고, 무엇보다 사람이 누른 버튼은 근거가 아니다.
    """
    check = validate(values, high, low)
    if not check.get("ok"):
        raise ValueError(f"승인 거부: {check.get('reason')}")
    import vkospi_calibrate as vc

    when = approved_at or date.today().strftime("%Y-%m-%d")
    prev = (ledger or {}).get("active")
    entry = {
        "high": float(high), "low": float(low),
        "approved_at": when, "by": by, "note": note,
        "n": check["n"],
        "above_pct": check["above_pct"], "below_pct": check["below_pct"],
        "describe": vc.describe(values),
        "previous": {"high": prev.get("high"), "low": prev.get("low")} if prev else None,
    }
    out = {"active": entry,
           "history": list((ledger or {}).get("history") or []) + [entry],
           "pending": None}
    return out


def save_ledger(path: Path, ledger: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(p)


# ─── 주기·재알림 (순수) ──────────────────────────────


def due_for_review(ledger: dict, *, today: Optional[str] = None,
                   every_days: int = REVIEW_EVERY_DAYS) -> bool:
    """마지막 승인(또는 마지막 검토)에서 every_days 지났는가."""
    ref = _parse(today or date.today().strftime("%Y-%m-%d"))
    if ref is None:
        return False
    last = ((ledger or {}).get("active") or {}).get("approved_at")
    last_seen = (ledger or {}).get("last_reviewed_at") or last
    if not last_seen:
        return True
    d = _parse(str(last_seen))
    if d is None:
        return True
    return (ref - d).days >= every_days


def needs_push(pending: Optional[dict], *, today: Optional[str] = None) -> bool:
    """승인 대기 중인 제안을 (다시) 알려야 하는가.

    한 번 밀어놓고 잊지 않는다 — 2026-07 리밸런싱이 그렇게 사라졌다. 다만
    매일 조르지 않는다.
    """
    if not pending:
        return False
    pushes = list(pending.get("pushes") or [])
    if not pushes:
        return True
    if len(pushes) >= MAX_RENOTIFY:
        return False
    ref = _parse(today or date.today().strftime("%Y-%m-%d"))
    last = _parse(str(pushes[-1]))
    if ref is None or last is None:
        return False
    return (ref - last).days >= RENOTIFY_AFTER_DAYS


def _parse(value: str):
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(str(value), fmt).date()
        except ValueError:
            continue
    return None


# ─── 사람이 읽는 문구 ────────────────────────────────


def format_proposal(rev: dict, *, current_high: float, current_low: float) -> str:
    """제안 알림. **지금 값이 왜 문제인지를 먼저 말한다.**"""
    cur = rev.get("current") or {}
    d = rev.get("describe") or {}
    lines = []
    if rev["verdict"] == "broken":
        lines.append("🚨 VKOSPI 임계값이 더는 규칙이 아닙니다")
    elif rev["verdict"] == "propose":
        lines.append("📉 VKOSPI 임계값 재측정 — 조정을 제안합니다")
    else:
        lines.append("📉 VKOSPI 임계값 재측정 — 지금 값을 유지합니다")
    lines.append("")
    if d.get("n"):
        lines.append(f"표본 {d['n']}일 · 중앙 {d['median']:.2f} · "
                     f"p20 {d['p20']:.2f} · p80 {d['p80']:.2f}")
    lines.append(f"현재 >{current_high} / <{current_low} — "
                 f"상단 {cur.get('above_pct', '?')}% · 하단 {cur.get('below_pct', '?')}%")
    if not cur.get("ok"):
        lines.append(f"  ⚠️ {cur.get('reason')}")
    sug = rev.get("suggested")
    if sug:
        chk = sug.get("check") or {}
        lines.append("")
        lines.append(f"제안 >{sug['high']} / <{sug['low']} — "
                     f"상단 {chk.get('above_pct', '?')}% · 하단 {chk.get('below_pct', '?')}%")
        if not chk.get("ok"):
            lines.append(f"  ⛔ 제안값도 통과하지 못했습니다: {chk.get('reason')}")
            lines.append("  → 승인해도 반영되지 않습니다. 규칙 형태를 다시 봐야 합니다.")
    if rev["verdict"] == "propose":
        lines.append("")
        lines.append("승인하면 원장에 기록되고 그때부터 적용됩니다.")
        lines.append("승인 전까지는 지금 값이 그대로 돕니다.")
    return "\n".join(lines)
