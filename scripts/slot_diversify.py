"""slot_diversify.py — 슬롯 내 집중 위험 진단·상한 필터 (v1, 순수)

배경: 슬롯을 콴텍/키움/IPO/마이퀀트로 나눈 것은 **전략 간 분산**이다.
한 슬롯 **안에서** 종목이 같은 위험에 몰리는 것은 그것만으로 막지 못한다.
2026-08-25에 키움 슬롯이 SK하이닉스·SK스퀘어·삼성전자·LG이노텍으로 4종목 동시 손절된 것이
그 사례다(진입액 2,208만원 = 슬롯 자본의 88%).

모멘텀 전략은 구조적으로 같은 주도 섹터를 뽑는다. 그래서 스코어링을 고치는 대신
**선정 이후·매수 직전에 상한을 적용**한다. 이 모듈은 그 판정만 담당한다(순수).

⚠️ 이 모듈은 매매하지 않는다. 상한에 걸린 종목은 **조용히 사라지지 않고** 사유와 함께 반환된다.
"""
from __future__ import annotations

from typing import Iterable, Optional

# 계열(그룹) 추정 — 종목명 접두사. 섹터 정보가 없을 때의 2차 방어선이다.
# 지주·계열사는 같은 뉴스에 같이 움직이므로 분산으로 치지 않는다.
GROUP_PREFIXES: tuple[str, ...] = (
    "HD현대", "포스코", "POSCO", "삼성", "SK", "LG", "현대", "한화", "롯데",
    "GS", "CJ", "KT", "두산", "효성", "LS", "신세계", "미래에셋", "한진",
    "아모레", "NAVER", "카카오", "농심", "CS", "DL", "HL", "OCI",
)

SECTOR_PREFIX = "섹터:"
GROUP_PREFIX = "계열:"
SOLO_PREFIX = "단독:"


def _sector_map(mapping: Optional[dict]) -> dict:
    if mapping is not None:
        return mapping
    try:  # 기존 수동 맵 재사용 (trade_analytics.SECTOR_BY_TICKER)
        import trade_analytics as ta
        return dict(ta.SECTOR_BY_TICKER)
    except Exception:  # noqa: BLE001
        return {}


def group_of(ticker: str, name: str = "", sector_map: Optional[dict] = None) -> str:
    """종목 → 집중도 판정 단위.

    우선순위: 섹터(알려진 경우) > 계열(이름 접두사) > 단독(그 종목 하나).
    단독은 상한 판정에서 서로 겹치지 않으므로 사실상 제한 없음을 뜻한다.
    """
    sector = _sector_map(sector_map).get(ticker)
    if sector:
        return f"{SECTOR_PREFIX}{sector}"
    text = (name or "").strip()
    for prefix in GROUP_PREFIXES:
        if text.upper().startswith(prefix.upper()):
            return f"{GROUP_PREFIX}{prefix}"
    return f"{SOLO_PREFIX}{ticker}"


def annotate(rows: Iterable[dict], sector_map: Optional[dict] = None) -> list[dict]:
    """각 행에 group을 붙인다(원본 비파괴)."""
    return [{**r, "group": group_of(r.get("ticker", ""), r.get("name", ""), sector_map)}
            for r in rows]


def apply_caps(
    rows: Iterable[dict],
    *,
    max_per_group: int = 2,
    max_groups: Optional[int] = None,
    sector_map: Optional[dict] = None,
) -> tuple[list[dict], list[dict]]:
    """상한을 적용해 (통과, 제외) 반환. 입력 순서를 우선순위로 본다(점수 내림차순 가정).

    max_per_group: 한 섹터·계열에서 최대 몇 종목까지 허용할지
    max_groups: 서로 다른 그룹 수 상한(None이면 무제한)

    **단독(섹터·계열 미상)은 그룹 상한에서 제외**한다. 서로 다른 종목이므로
    같은 위험이라고 볼 근거가 없고, 미분류를 이유로 걸러내면 정보 부족이
    투자 판단을 대신하게 된다.
    """
    kept: list[dict] = []
    dropped: list[dict] = []
    counts: dict[str, int] = {}
    seen_groups: list[str] = []

    for row in annotate(rows, sector_map):
        group = row["group"]
        if group.startswith(SOLO_PREFIX):
            kept.append(row)
            continue
        if max_groups is not None and group not in seen_groups and len(seen_groups) >= max_groups:
            dropped.append({**row, "drop_reason": f"그룹 수 상한 {max_groups} 초과"})
            continue
        used = counts.get(group, 0)
        if used >= max_per_group:
            dropped.append({**row, "drop_reason":
                            f"{group} 이미 {used}종목 (상한 {max_per_group})"})
            continue
        counts[group] = used + 1
        if group not in seen_groups:
            seen_groups.append(group)
        kept.append(row)
    return kept, dropped


def concentration(rows: Iterable[dict], *, value_key: Optional[str] = None,
                  sector_map: Optional[dict] = None) -> list[dict]:
    """그룹별 집중도. value_key를 주면 금액 비중, 없으면 종목 수 비중(내림차순)."""
    annotated = annotate(rows, sector_map)
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for row in annotated:
        group = row["group"]
        counts[group] = counts.get(group, 0) + 1
        totals[group] = totals.get(group, 0.0) + (
            float(row.get(value_key) or 0) if value_key else 1.0)
    grand = sum(totals.values())
    out = [{"group": g, "n": counts[g], "value": round(totals[g], 2),
            "share_pct": round(totals[g] / grand * 100, 1) if grand else 0.0}
           for g in totals]
    return sorted(out, key=lambda r: (-r["share_pct"], r["group"]))


def format_concentration(rows: list[dict], *, top_share_warn: float = 40.0) -> str:
    """집중도 리포트(순수). 상위 그룹이 기준을 넘으면 경고 한 줄을 덧붙인다."""
    if not rows:
        return "보유 종목이 없습니다."
    lines = ["🧭 그룹별 집중도"]
    for row in rows:
        lines.append(f"  • {row['group']}: {row['n']}종목 · {row['share_pct']}%")
    top = rows[0]
    if top["share_pct"] >= top_share_warn:
        lines.append(f"  ⚠️ {top['group']}에 {top['share_pct']}%가 몰려 있습니다 "
                     f"(경고 기준 {top_share_warn}%). 같은 뉴스에 함께 움직입니다.")
    unknown = sum(r["n"] for r in rows if r["group"].startswith(SOLO_PREFIX))
    if unknown:
        lines.append(f"  ℹ️ 섹터·계열 미분류 {unknown}종목 — "
                     f"trade_analytics.SECTOR_BY_TICKER에 추가하면 판정이 정확해집니다.")
    return "\n".join(lines)


def format_caps(kept: list[dict], dropped: list[dict]) -> str:
    """상한 적용 결과(순수). 제외 종목을 반드시 보여준다 — 조용한 탈락 금지."""
    lines = [f"통과 {len(kept)}종목 / 제외 {len(dropped)}종목"]
    for row in kept:
        lines.append(f"  ✅ {row.get('name') or row.get('ticker')} [{row['group']}]")
    for row in dropped:
        lines.append(f"  ⛔ {row.get('name') or row.get('ticker')} — {row['drop_reason']}")
    return "\n".join(lines)


def load_position_rows(*, slot: str | None = None, private_client=None) -> list[dict]:
    """Private API 보유내역을 집중도 입력 행으로 변환한다.

    진단 소비자는 paper.db를 직접 열거나 API 실패 시 SQLite로 폴백하지 않는다.
    ``private_client`` 주입은 hermetic 테스트용이다.
    """
    if private_client is None:
        from private_data_api_client import PrivateDataClient
        private_client = PrivateDataClient()
    positions = private_client.list_paper_positions(slot)
    return [
        {
            "ticker": str(row["ticker"]),
            "name": row.get("name") or row["ticker"],
            "slot": str(row["slot_name"]),
            "amount": int(row["quantity"]) * float(row["avg_price"]),
        }
        for row in positions
        if int(row["quantity"]) > 0
    ]


def _cli() -> None:
    import argparse
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except ImportError:
        pass
    ap = argparse.ArgumentParser(description="슬롯 내 집중 위험 진단")
    ap.add_argument("--slot", help="특정 슬롯만 (예: 키움)")
    ap.add_argument("--warn", type=float, default=40.0, help="경고 기준 비중 %% (기본 40)")
    args = ap.parse_args()

    rows = load_position_rows(slot=args.slot)

    if not rows:
        print("보유 종목이 없습니다.")
        return
    by_slot: dict[str, list[dict]] = {}
    for row in rows:
        by_slot.setdefault(row["slot"], []).append(row)
    for slot, items in by_slot.items():
        total = sum(r["amount"] for r in items)
        print(f"\n[{slot}] 보유 {len(items)}종목 · 평가 {total:,.0f}원")
        print(format_concentration(concentration(items, value_key="amount"),
                                   top_share_warn=args.warn))


if __name__ == "__main__":
    _cli()
