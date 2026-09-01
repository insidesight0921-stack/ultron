"""param_store.py — 실측으로 정해진 파라미터만, 표본 없이는 못 바꾼다.

**왜 이 파일이 생겼나.** 2026-09-01 하루에 교과서에서 온 상수 다섯 개를 재봤고
결말이 전부 달랐다.

    VKOSPI <15        407일 중 0일 — 죽은 가지          → 교체
    VKOSPI 20/30(긴급) 48.6%의 날에 발동 — 상시 조치     → 교체
    VIX ≤18/≥28       58.1% / 3.4% — 치우쳤지만 살아 있음 → 유지
    환율 ±2%          25.9% / 18.7% — 멀쩡              → 유지
    200일선 기울기 ±0.5% 부호가 안 바뀜 — 구간 문제       → 유지(보류)

바꿔야 할 값과 두어야 할 값을 **가려낸 것은 분포였지 판단이 아니었다.** 그래서
파라미터 조정 창구를 열되, 조정의 조건을 하나로 못 박는다.

    **표본이 없으면 바꿀 수 없다.**

값을 바꾸려면 그 파라미터가 어느 계열에서 나왔는지, 새 값이 그 계열에서 몇 %
걸리는지가 함께 있어야 한다. 사람이 승인해도 검증을 통과하지 못하면 반영되지
않는다 — 버튼은 근거가 아니다(`vkospi_threshold_review`와 같은 원칙).

**여기서 다루지 않는 것.** 알림 주기·슬롯 비중·종목 수처럼 '맞다/틀리다'가
없는 취향 값은 이 창구에 넣지 않는다. 검증할 분포가 없어서 위 규율이 그냥
장식이 되고, 장식이 된 규율은 다음 값에도 안 지켜진다.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("param_store")

LEDGER_NAME = "params.json"

# 밴드형 파라미터의 허용 발동 범위(%). `vkospi_threshold_review`와 같은 값.
MIN_SIDE_PCT = 5.0
MAX_SIDE_PCT = 40.0
MIN_SAMPLE = 120

# 드문 사건형(긴급)의 허용 발동 범위 — 연 환산 횟수.
MIN_PER_YEAR = 0.5
MAX_PER_YEAR = 6.0
TRADING_DAYS = 252


@dataclass(frozen=True)
class Param:
    """조정 가능한 파라미터 하나.

    `sample()`은 이 값을 판정할 **원자료**를 돌려준다. 없으면 `[]` — 그러면
    이 파라미터는 바꿀 수 없다. 그게 이 모듈의 핵심 규칙이다.
    """
    key: str
    label: str
    module: str
    attr: str
    kind: str                      # "band_low" | "band_high" | "rare_low" | "rare_high"
    sample: Callable[[], list]
    unit: str = ""
    note: str = ""
    partner: Optional[str] = None  # 같이 봐야 하는 반대쪽 임계값의 key
    # ±T처럼 **한 숫자가 양쪽을 동시에 정하는** 파라미터. 절대값 분포로 한 번에
    # 세면 두 쪽이 합산되어 실제보다 두 배로 걸리는 것처럼 보인다 —
    # 2026-09-01에 환율 ±2%가 그렇게 44.6%로 나와 검증에 걸릴 뻔했다
    # (실제는 약세 25.9% / 강세 18.7%로 양쪽 다 정상).
    two_sided: bool = False


# ─── 판정 (순수) ─────────────────────────────────────


def coverage_pct(values: list, threshold: float, *, above: bool) -> Optional[float]:
    """임계값이 이 표본에서 몇 % 걸리는가(순수)."""
    vals = [float(v) for v in (values or []) if v is not None]
    if not vals:
        return None
    hit = sum(1 for v in vals if (v >= threshold if above else v <= threshold))
    return round(hit / len(vals) * 100, 1)


def entries_per_year(values: list, threshold: float, *, above: bool) -> Optional[float]:
    """**진입 횟수**를 연으로 환산한다(순수).

    해당 일수가 아니라 진입 횟수를 센다. 긴급 경보는 실측 평균 13.7일 이어져서
    일수로 세면 '자주 걸린다'로 보이지만 사람이 받는 알림은 **진입할 때 한 번**
    이다. 세는 단위가 다르면 판단도 달라진다.
    """
    vals = [float(v) for v in (values or []) if v is not None]
    if not vals:
        return None
    inside = [(v >= threshold if above else v <= threshold) for v in vals]
    entries = sum(1 for i in range(1, len(inside)) if inside[i] and not inside[i - 1])
    entries += 1 if inside[0] else 0
    return round(entries / len(vals) * TRADING_DAYS, 1)


def validate(param: Param, value: float, values: list) -> dict:
    """이 값을 써도 되는가(순수). **표본이 없으면 판정 자체를 거부한다.**"""
    vals = [float(v) for v in (values or []) if v is not None]
    if len(vals) < MIN_SAMPLE:
        return {"ok": False, "n": len(vals),
                "reason": f"표본 {len(vals)}일 — {MIN_SAMPLE}일 미만이면 바꾸지 않는다"}
    above = param.kind.endswith("high")
    if param.two_sided:
        # 부호 있는 분포에서 양쪽을 따로 센다. 각각이 범위 안이어야 한다.
        up = coverage_pct(vals, abs(value), above=True)
        down = coverage_pct(vals, -abs(value), above=False)
        sides = (("상단", up), ("하단", down))
        # **죽은 가지를 먼저 본다.** 한쪽이 아예 안 걸리는 것은 '자주 걸린다'
        # 보다 근본적인 문제인데, 검사 순서를 그냥 두면 다른 쪽의 빈도 문제에
        # 가려 진짜 진단이 안 나온다.
        for side, pct in sides:
            if pct == 0:
                return {"ok": False, "n": len(vals), "up": up, "down": down,
                        "reason": f"{side}이 한 번도 걸리지 않는다 — 죽은 가지다"}
        for side, pct in sides:
            if pct > MAX_SIDE_PCT:
                return {"ok": False, "n": len(vals), "up": up, "down": down,
                        "reason": f"{side}이 {pct}% 걸린다 — 상시 조치에 가깝다"}
            if pct < MIN_SIDE_PCT:
                return {"ok": False, "n": len(vals), "up": up, "down": down,
                        "reason": f"{side}이 {pct}%만 걸린다 — 너무 드물다"}
        return {"ok": True, "n": len(vals), "up": up, "down": down,
                "coverage": round(up + down, 1)}
    if param.kind.startswith("band"):
        pct = coverage_pct(vals, value, above=above)
        if pct == 0:
            return {"ok": False, "n": len(vals), "coverage": pct,
                    "reason": "이 표본에서 한 번도 걸리지 않는다 — 죽은 가지다"}
        if pct >= 100:
            return {"ok": False, "n": len(vals), "coverage": pct,
                    "reason": "늘 걸린다 — 규칙이 아니라 상수다"}
        if pct > MAX_SIDE_PCT:
            return {"ok": False, "n": len(vals), "coverage": pct,
                    "reason": f"{pct}% 걸린다 — 상시 조치에 가깝다(한도 {MAX_SIDE_PCT}%)"}
        if pct < MIN_SIDE_PCT:
            return {"ok": False, "n": len(vals), "coverage": pct,
                    "reason": f"{pct}%만 걸린다 — 너무 드물어 반응이 늦는다(하한 {MIN_SIDE_PCT}%)"}
        return {"ok": True, "n": len(vals), "coverage": pct}

    per_year = entries_per_year(vals, value, above=above)
    if not per_year:
        return {"ok": False, "n": len(vals), "per_year": per_year,
                "reason": "이 표본에서 한 번도 진입하지 않는다 — 죽은 가지다"}
    if per_year > MAX_PER_YEAR:
        return {"ok": False, "n": len(vals), "per_year": per_year,
                "reason": f"연 {per_year}회 진입 — 긴급이라기엔 잦다(한도 {MAX_PER_YEAR}회)"}
    if per_year < MIN_PER_YEAR:
        return {"ok": False, "n": len(vals), "per_year": per_year,
                "reason": f"연 {per_year}회 — 너무 드물어 검증할 수 없다(하한 {MIN_PER_YEAR}회)"}
    return {"ok": True, "n": len(vals), "per_year": per_year}


# ─── 원장 (얇은 I/O) ─────────────────────────────────


def load(path: Path) -> dict:
    """없으면 빈 원장. **깨졌으면 예외를 올린다** — 조용히 비우지 않는다."""
    p = Path(path)
    if not p.exists():
        return {"active": {}, "history": []}
    raw = json.loads(p.read_text(encoding="utf-8"))
    return {"active": dict(raw.get("active") or {}),
            "history": list(raw.get("history") or [])}


def save(path: Path, ledger: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(p)


def active_value(ledger: dict, param: Param, fallback: float) -> tuple[float, str]:
    """지금 유효한 값과 출처. 원장에 없거나 이상하면 코드 초기값."""
    entry = ((ledger or {}).get("active") or {}).get(param.key)
    if not entry:
        return fallback, "코드 초기값"
    try:
        return float(entry["value"]), f"승인 {entry.get('approved_at', '?')}"
    except (KeyError, TypeError, ValueError):
        log.warning("%s 원장 값을 읽을 수 없다 — 코드 초기값으로 돈다", param.key)
        return fallback, "코드 초기값(원장 손상)"


def approve(ledger: dict, param: Param, value: float, values: list, *,
            approved_at: Optional[str] = None, by: str = "user",
            note: str = "", previous: Optional[float] = None,
            previous_source: str = "") -> dict:
    """검증을 통과한 값만 원장에 **추가**한다. 기존 기록은 지우지 않는다.

    **무엇을 대체했는지 반드시 남는다.** 첫 승인은 원장에 이전 항목이 없어
    `previous`가 `None`이 되는데, 그러면 기록만 보고는 "18.0에서 15.7로
    바꿨다"를 재현할 수 없다 — 코드 초기값이 나중에 바뀌면 영영 모른다.
    (2026-09-01 첫 승인이 `None → 15.7`로 남아 이 인자가 생겼다.)
    """
    check = validate(param, value, values)
    if not check.get("ok"):
        raise ValueError(f"{param.label}: {check.get('reason')}")
    when = approved_at or date.today().strftime("%Y-%m-%d")
    prev = ((ledger or {}).get("active") or {}).get(param.key)
    prev_value = (prev or {}).get("value")
    if prev_value is None:
        prev_value = previous
    entry = {"key": param.key, "value": float(value), "approved_at": when,
             "by": by, "note": note, "n": check["n"],
             "coverage": check.get("coverage"),
             "per_year": check.get("per_year"),
             "previous": prev_value,
             "previous_source": (prev and f"승인 {prev.get('approved_at')}")
                                or previous_source or "코드 초기값"}
    act = dict((ledger or {}).get("active") or {})
    act[param.key] = entry
    return {"active": act,
            "history": list((ledger or {}).get("history") or []) + [entry]}


# ─── 표시 ────────────────────────────────────────────


def describe(param: Param, current: float, source: str,
             values: list) -> str:
    """한 줄 요약(순수). **표본이 없으면 그 사실을 먼저 말한다.**"""
    vals = [v for v in (values or []) if v is not None]
    if len(vals) < MIN_SAMPLE:
        return (f"{param.label} = {current}{param.unit} ({source}) · "
                f"표본 {len(vals)}일 — **바꿀 수 없다**(재측정 필요)")
    if param.two_sided:
        up = coverage_pct(vals, abs(current), above=True)
        down = coverage_pct(vals, -abs(current), above=False)
        got = f"상단 {up}% · 하단 {down}%"
    elif param.kind.startswith("band"):
        pct = coverage_pct(vals, current, above=param.kind.endswith("high"))
        got = f"발동 {pct}%"
    else:
        got = f"진입 연 {entries_per_year(vals, current, above=param.kind.endswith('high'))}회"
    return (f"{param.label} = {current}{param.unit} ({source}) · "
            f"표본 {len(vals)}일 · {got}")
