"""entry_tags.py — 진입 근거 태그 (탭 A 귀속용, v1)

**왜 필요한가**: 2026-08-27 기준 완결 라운드트립 78건 중 진입 근거가 기록된 것은 0건이었다.
어떤 조건으로 샀는지가 `trades.notes`에 남지 않아, 탭 A의 "전략별 성과 비교"가
봇·청산 사유·보유 기간까지만 나눌 수 있고 **진입 전략별로는 아무것도 말하지 못했다.**
이 모듈은 매수 시점에 이미 손에 있는 값만으로 태그를 만들어 메모에 붙인다.

설계 원칙:
  - **매수 시점에 아는 것만 쓴다.** 나중에 알게 된 값으로 태그를 붙이면 look-ahead다.
  - **태그 어휘를 미리 고정한다.** 사후에 자유롭게 쪼개면 우연히 좋아 보이는 조합이
    반드시 나온다. 봇마다 볼 축을 두세 개로 못 박아 둔다.
  - **기존 메모를 덮지 않는다.** `[AUTO] 2026-W34 키움봇` 뒤에 ` MQ[...]`를 덧붙일 뿐이라
    기존 파서(`_classify_reason`, 주차 추출)가 그대로 동작한다.
  - 값이 없으면 태그를 만들지 않는다. 모르는 것을 "미상"으로 채우면 그 "미상"이
    나중에 하나의 전략처럼 집계된다.

태그 형식은 `MQ[태그1,태그2]` — `trade_analytics.extract_tags` /
`paper_weekly_report.extract_tags` / `strategy_compare` 가 이미 읽는 형식이다.
"""
from __future__ import annotations

from typing import Any, Optional

PREFIX = "MQ["
MAX_TAGS = 3          # 조합을 늘릴수록 우연한 승자가 나온다. 봇당 최대 3개.


# ─── 값 꺼내기 (dict·dataclass 양쪽 지원) ────────────


def attr(obj: Any, key: str, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


# ─── 키움봇 (주간 모멘텀) ────────────────────────────


def rank_bucket(rank: Optional[int]) -> Optional[str]:
    """스캔 순위 구간. 상위와 하위를 나눠 두면 '몇 등까지 살 것인가'를 나중에 검증할 수 있다."""
    if rank is None or rank < 1:
        return None
    return "랭크1-4" if rank <= 4 else "랭크5-8" if rank <= 8 else "랭크9+"


def momentum_bucket(ret_12m: Optional[float]) -> Optional[str]:
    """12개월 수익률 구간. 모멘텀 크기별로 결과가 갈리는지 보려는 축."""
    if ret_12m is None:
        return None
    pct = float(ret_12m) * 100
    if pct >= 60:
        return "12M강함"
    if pct >= 20:
        return "12M보통"
    return "12M약함"


def kium_tags(rec: Any, rank: Optional[int] = None) -> list[str]:
    """키움봇 매수 1건의 진입 태그(순수)."""
    tags = ["키움모멘텀"]
    for t in (rank_bucket(rank), momentum_bucket(attr(rec, "return_12m"))):
        if t:
            tags.append(t)
    return tags[:MAX_TAGS]


# ─── 콴텍봇 (국면 + 팩터) ────────────────────────────


def dominant_factor(z_factors: Any) -> Optional[str]:
    """복합 점수를 가장 크게 끌어올린 팩터(순수).

    콴텍봇은 국면에 따라 팩터 가중치를 바꾼다. 어떤 팩터가 그 종목을 뽑았는지가
    사실상의 진입 전략이므로, 그것을 남겨야 나중에 팩터별로 성과를 가를 수 있다.
    """
    if not isinstance(z_factors, dict) or not z_factors:
        return None
    usable = {k: v for k, v in z_factors.items() if isinstance(v, (int, float))}
    if not usable:
        return None
    return f"주도-{max(usable, key=usable.get)}"


def quant_tags(rec: Any, phase: Optional[str] = None) -> list[str]:
    """콴텍봇 매수 1건의 진입 태그(순수)."""
    tags = ["콴텍팩터"]
    if phase:
        tags.append(f"국면-{phase}")
    top = dominant_factor(attr(rec, "z_factors"))
    if top:
        tags.append(top)
    return tags[:MAX_TAGS]


# ─── 메모 합치기 ─────────────────────────────────────


def clean(tags) -> list[str]:
    """빈 값·중복 제거, 순서 유지. 쉼표·대괄호는 파서를 깨뜨리므로 뺀다."""
    out: list[str] = []
    for t in tags or []:
        if t is None or t is False:
            continue                     # str(None) == "None" — 빈 값이 태그가 되면 안 된다
        t = str(t).replace(",", " ").replace("[", "").replace("]", "").strip()
        if t and t not in out:
            out.append(t)
    return out


def format_note(base: str, tags) -> str:
    """기존 메모 뒤에 `MQ[...]`를 덧붙인다(순수). 태그가 없으면 원본 그대로."""
    tags = clean(tags)[:MAX_TAGS]
    if not tags:
        return base
    if PREFIX in (base or ""):
        return base                      # 이미 태그가 있으면 건드리지 않는다
    joined = ",".join(tags)
    return f"{base} {PREFIX}{joined}]" if base else f"{PREFIX}{joined}]"
