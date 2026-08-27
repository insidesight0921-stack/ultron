"""principle_review.py — 원칙 갱신 제안 초안 생성 (회고 루프 3단계, v1)

주간 성과 JSON(`paper_weekly_report.py` 산출물)을 여러 주차 모아서 원칙별 성과를 집계하고,
**강화 / 수정·폐기 / 관망 / 보류** 초안을 만든다.

지켜야 할 것 (설계도 6장 회고봇):
  - **자동 반영 금지.** 이 모듈은 wiki를 건드리지 않는다. 초안만 만든다.
    사람이 읽고 승인해야 원칙이 바뀐다(HITL).
  - **반례를 적극적으로 찾는다.** 제안이 좋아 보일수록 반대 증거를 먼저 캔다.
    이익이 한 건에서 나왔거나 한 종목에 쏠렸으면 '강화'를 '보류'로 되돌린다.
  - **표본 부족은 결론 없음.** 최소 표본 미달이면 아무 제안도 하지 않는다.
  - 주간 JSON의 충분통계는 더할 수 있게 설계돼 있으므로 여러 주차를 그대로 합산한다.

초안은 private 상태 디렉터리에 쓴다. **vault에 쓰지 않는다** — 승인 전 초안이
RAG에 들어가면 봇이 확정된 원칙처럼 인용하게 된다.
"""
from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger("principle_review")

MIN_SAMPLE = 20            # 원칙을 건드리려면 이 정도는 쌓여야 한다(주간 노트의 † 기준보다 높다)
STRONG_PF = 1.3            # 이 이상이면 '강화' 후보
WEAK_PF = 0.8              # 이 이하면 '수정·폐기 검토' 후보
STRONG_WIN_RATE = 45.0     # 손익비가 좋아도 승률이 이보다 낮으면 강화하지 않는다
CONCENTRATION_LIMIT = 0.6  # 이익·손실·종목 쏠림이 이 비율을 넘으면 반례로 본다

VERDICT_HOLD = "보류"
VERDICT_STRENGTHEN = "강화"
VERDICT_REVISE = "수정·폐기 검토"
VERDICT_WATCH = "관망"


# ─── 입력 ────────────────────────────────────────────


def load_weekly_payloads(state_dir) -> list[dict]:
    """주간 JSON 전부 읽기(오래된 → 최신). 깨진 파일은 건너뛴다."""
    out = []
    for path in sorted(Path(state_dir).glob("*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception as e:  # noqa: BLE001
            log.warning(f"주간 JSON 읽기 실패 {path.name}: {e}")
    return out


def collect_rows(payloads: Iterable[dict]) -> list[dict]:
    """주차별 라운드트립을 하나로. 주차는 서로 겹치지 않으므로 그냥 이어 붙인다."""
    rows: list[dict] = []
    for payload in payloads:
        period = payload.get("period")
        for row in payload.get("roundtrips") or []:
            rows.append({**row, "period": period})
    return rows


# ─── 집계 (순수) ─────────────────────────────────────


def _stats(rows: list[dict]) -> dict:
    from paper_weekly_report import group_stats
    return group_stats(rows)


def by_tag(rows: Iterable[dict]) -> dict[str, list[dict]]:
    """마이퀀트 조건 태그별 라운드트립. 한 거래가 여러 태그면 각 태그에 계상."""
    out: dict[str, list[dict]] = {}
    for row in rows:
        for tag in row.get("tags") or []:
            out.setdefault(tag, []).append(row)
    return out


def by_slot(rows: Iterable[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for row in rows:
        out.setdefault(row.get("slot") or "?", []).append(row)
    return out


def counterevidence(rows: list[dict]) -> dict:
    """이 결론이 틀릴 수 있는 이유를 먼저 캔다(순수).

    확증 편향 방어 — 제안이 좋아 보일 때 반대 증거를 찾는 것이 회고의 핵심이다.
    """
    n = len(rows)
    if not n:
        return {"n": 0, "notes": []}
    wins = [r for r in rows if (r.get("pnl") or 0) > 0]
    losses = [r for r in rows if (r.get("pnl") or 0) < 0]
    sum_win = sum(r["pnl"] for r in wins) or 0.0
    sum_loss = abs(sum(r["pnl"] for r in losses)) or 0.0

    top_win_share = (max((r["pnl"] for r in wins), default=0.0) / sum_win) if sum_win else 0.0
    top_loss_share = (abs(min((r["pnl"] for r in losses), default=0.0)) / sum_loss) if sum_loss else 0.0
    ticker_counts = Counter(r.get("ticker") for r in rows)
    top_ticker, top_ticker_n = ticker_counts.most_common(1)[0]
    periods = sorted({r.get("period") for r in rows if r.get("period")})

    notes: list[str] = []
    if top_win_share >= CONCENTRATION_LIMIT:
        notes.append(f"이익의 {top_win_share*100:.0f}%가 단 한 건에서 나왔다")
    if top_loss_share >= CONCENTRATION_LIMIT:
        notes.append(f"손실의 {top_loss_share*100:.0f}%가 단 한 건에 몰렸다")
    if top_ticker_n / n >= CONCENTRATION_LIMIT:
        notes.append(f"{top_ticker} 한 종목이 표본의 {top_ticker_n/n*100:.0f}%")
    if len(periods) <= 1:
        notes.append("한 주차 안에서만 나온 표본 — 특정 시장 국면에 갇혔을 수 있다")

    return {
        "n": n,
        "top_win_share": round(top_win_share, 3),
        "top_loss_share": round(top_loss_share, 3),
        "top_ticker": top_ticker,
        "top_ticker_share": round(top_ticker_n / n, 3),
        "periods": periods,
        "notes": notes,
    }


def judge(stats: dict, counter: dict, *, min_sample: int = MIN_SAMPLE) -> tuple[str, str]:
    """(판정, 사유). 반례가 있으면 긍정 판정을 보류로 되돌린다."""
    n = stats.get("n") or 0
    if n < min_sample:
        return VERDICT_HOLD, f"표본 {n}건 — 최소 {min_sample}건 미달, 결론 없음"

    pf = stats.get("profit_factor")
    win = stats.get("win_rate") or 0.0
    if pf is None:
        return VERDICT_WATCH, "손실 표본이 없어 Profit Factor를 계산할 수 없다"

    if pf >= STRONG_PF and win >= STRONG_WIN_RATE:
        if counter.get("notes"):
            return VERDICT_HOLD, ("성과는 좋지만 반례가 있어 보류 — "
                                  + "; ".join(counter["notes"]))
        return VERDICT_STRENGTHEN, f"PF {pf} · 승률 {win}% — 유지·확대 검토"
    if pf <= WEAK_PF:
        return VERDICT_REVISE, f"PF {pf} · 승률 {win}% — 조건 재검토 또는 폐기"
    return VERDICT_WATCH, f"PF {pf} · 승률 {win}% — 경계 구간, 표본 추가 관찰"


# ─── 제안 (순수) ─────────────────────────────────────


def _principles_for_slot(slot: str) -> list[str]:
    from paper_weekly_report import SLOT_PRINCIPLES
    return list(SLOT_PRINCIPLES.get(slot, ()))


def build_proposals(rows: list[dict], *, min_sample: int = MIN_SAMPLE) -> list[dict]:
    """태그·슬롯별 제안 초안. 판정이 나쁜 순(수정 우선)으로 정렬."""
    proposals: list[dict] = []

    for tag, group in by_tag(rows).items():
        stats = _stats(group)
        counter = counterevidence(group)
        verdict, reason = judge(stats, counter, min_sample=min_sample)
        proposals.append({
            "target": f"조건 {tag}", "note": "마이퀀트_진입조건",
            "kind": "tag", "key": tag,
            "stats": stats, "counter": counter,
            "verdict": verdict, "reason": reason,
        })

    for slot, group in by_slot(rows).items():
        stats = _stats(group)
        counter = counterevidence(group)
        verdict, reason = judge(stats, counter, min_sample=min_sample)
        notes = _principles_for_slot(slot)
        proposals.append({
            "target": f"슬롯 {slot}", "note": notes[0] if notes else None,
            "kind": "slot", "key": slot,
            "stats": stats, "counter": counter,
            "verdict": verdict, "reason": reason,
        })

    order = {VERDICT_REVISE: 0, VERDICT_STRENGTHEN: 1, VERDICT_WATCH: 2, VERDICT_HOLD: 3}
    return sorted(proposals, key=lambda p: (order.get(p["verdict"], 9), -(p["stats"]["n"])))


def render_review(proposals: list[dict], *, as_of: date, periods: list[str]) -> str:
    """승인용 초안(순수). 사람이 읽고 판단하는 문서이지 확정 원칙이 아니다."""
    span = f"{periods[0]} ~ {periods[-1]}" if periods else "기간 없음"
    lines = [
        f"# 원칙 갱신 제안 초안 ({as_of.isoformat()})",
        "",
        f"집계 구간 {span} · 주간 노트 {len(periods)}개",
        "",
        "> ⚠️ **초안이다. 승인 전에는 어떤 원칙도 바뀌지 않는다.**",
        "> 반영하려면 해당 원칙 노트를 직접 고친다. 이 파일은 vault에 넣지 않는다.",
        "",
    ]
    if not proposals:
        lines += ["제안할 것이 없다. 완결 거래가 없거나 표본이 전혀 없다.", ""]
        return "\n".join(lines)

    for p in proposals:
        s, c = p["stats"], p["counter"]
        note = f" → [[{p['note']}]]" if p.get("note") else ""
        lines += [
            f"## [{p['verdict']}] {p['target']}{note}",
            "",
            f"- 표본 {s['n']}건 (승 {s['wins']} / 패 {s['losses']}) · "
            f"실현손익 {s['pnl']:,}원 · 승률 {s['win_rate']}% · PF {s['profit_factor']}",
            f"- 판단 근거: {p['reason']}",
        ]
        if c.get("notes"):
            lines.append("- 반례: " + "; ".join(c["notes"]))
        else:
            lines.append("- 반례: 눈에 띄는 쏠림 없음")
        lines.append("")

    lines += [
        "---",
        "",
        "## 승인 절차",
        "",
        "1. 위 제안 중 반영할 것을 고른다.",
        "2. 해당 원칙 노트를 고치고, **무엇을 왜 바꿨는지 날짜와 함께** 남긴다.",
        "3. 규칙이 바뀐 날을 `paper_weekly_report.py`의 `RULES_SINCE`에 반영한다.",
        "   그래야 변경 전후 성과가 섞이지 않는다.",
        "",
    ]
    return "\n".join(lines)


# ─── I-O 경계 ────────────────────────────────────────


def default_state_dir() -> Path:
    try:
        from storage_paths import PATHS
        return Path(PATHS.private_state_dir) / "paper_weekly"
    except Exception:  # noqa: BLE001
        return Path(__file__).resolve().parent.parent / "data" / "private" / "state" / "paper_weekly"


def _cli() -> int:
    ap = argparse.ArgumentParser(description="원칙 갱신 제안 초안 생성 (승인 전 반영 없음)")
    ap.add_argument("--state", help="주간 JSON 디렉터리")
    ap.add_argument("--out", help="초안 저장 경로 (미지정 시 화면 출력만)")
    ap.add_argument("--min-sample", type=int, default=MIN_SAMPLE)
    args = ap.parse_args()

    state_dir = Path(args.state) if args.state else default_state_dir()
    payloads = load_weekly_payloads(state_dir)
    if not payloads:
        print(f"주간 JSON이 없습니다: {state_dir}")
        print("먼저 paper_weekly_report.py를 몇 주 돌려 데이터를 쌓아야 합니다.")
        return 1

    rows = collect_rows(payloads)
    proposals = build_proposals(rows, min_sample=args.min_sample)
    periods = [p.get("period") for p in payloads if p.get("period")]
    text = render_review(proposals, as_of=date.today(), periods=periods)

    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        print(f"✅ 초안 저장: {target}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
