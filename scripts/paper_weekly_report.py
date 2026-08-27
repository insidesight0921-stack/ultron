"""paper_weekly_report.py — 페이퍼 주간 성과 → Obsidian 노트 + 분석용 JSON (v2)

설계 원칙:
  - **Gemma 정제를 태우지 않는다.** 숫자를 LLM에 통과시키면 왜곡 위험이 있어
    raw→wiki 파이프라인을 쓰지 않고 wiki/투자/성과/에 결정론적으로 직접 쓴다.
  - 계산(순수) / 렌더(순수) / I-O(경계)를 분리한다. 순수 함수는 DB·파일을 모른다.
  - look-ahead 차단: frontmatter에 판단시점(주 시작)과 평가시점(집계 실행일)을
    분리 기록한다. 라운드트립은 **청산일(sell_at)** 기준으로 주간에 귀속한다.
  - 표본 5건 미만 라벨은 †(참고용). 적은 표본으로 원칙을 고치면 과적합이다.

출력이 둘인 이유 (v2):
  ① 노트(vault)  — 사람이 읽고 RAG가 참조한다. 표와 백링크 중심, frontmatter는
     Dataview로 걸 수 있는 **평평한 스칼라**만 담는다. 큰 데이터 덩어리를 노트에
     넣으면 임베딩 품질만 떨어진다.
  ② JSON(private state) — 나중에 분석·백테스트가 읽는다. 승률·손익비 같은 **파생값이
     아니라 충분통계**(건수·승/패·이익합·손실합·비용합)와 라운드트립 원본을 담아,
     주차를 가로질러 재집계·재계산할 수 있게 한다. 파생값만 저장하면 나중에
     "표본 가중 평균"조차 복원하지 못한다.

이 스크립트는 기록만 한다. 원칙 갱신은 사람 승인(HITL)을 거친다.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger("paper_weekly_report")

SCHEMA_VERSION = 2  # frontmatter·JSON 구조 버전. 필드가 바뀌면 올린다

# 청산 규칙이 바뀐 날. 이 앞뒤 성과를 한 덩어리로 집계하면 "지금 규칙이 통하는지"를
# 알 수 없다 — 옛 규칙의 손실이 현재 성적을 덮어쓴다. exit_rules(v3.44) 도입 이후
# 기록에 [손절선]·[트레일링] 같은 태그가 붙기 시작한 시점을 경계로 쓴다.
RULES_SINCE = date(2026, 7, 23)
MIN_SAMPLE_N = 5    # 이 미만 라벨은 참고용(†)
TREND_WEEKS = 4     # 노트에 함께 싣는 최근 주차 수
MYQUANT_SLOT = "마이퀀트"

# 슬롯 → 그 슬롯의 판단 근거가 되는 원칙 노트(백링크용)
SLOT_PRINCIPLES: dict[str, tuple[str, ...]] = {
    "콴텍": ("국면별_팩터_가중", "퀀트_팩터_가설"),
    "키움": ("모멘텀_전략_원칙", "수급_판단_기준"),
    "IPO": ("IPO_매력지수_기준", "IPO_매도전략"),
    MYQUANT_SLOT: ("마이퀀트_진입조건",),
}
COMMON_PRINCIPLES: tuple[str, ...] = ("매매_청산_조건", "리스크_관리_원칙")


# ─── 기간 (순수) ─────────────────────────────────────


def iso_week_label(d: date) -> str:
    """날짜 → ISO 주차 라벨. 예) 2026-08-26 → '2026-W35'."""
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def week_bounds(d: date) -> tuple[date, date]:
    """날짜가 속한 ISO 주의 (월요일, 일요일)."""
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=6)


def _date_of(value: Optional[str]) -> Optional[date]:
    """'2026-08-26 14:22' / '2026-08-26' → date. 파싱 실패 시 None."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def in_period(rt: dict, start: date, end: date) -> bool:
    """라운드트립이 해당 구간에 귀속되는지 — **청산일 기준**.

    진입일이 아니라 청산일로 귀속한다. 성과가 확정되는 시점이 청산이기 때문이며,
    아직 안 팔린 포지션은 어떤 주간에도 들어가지 않는다(평가손익 미반영).
    """
    sold = _date_of(rt.get("sell_at"))
    return bool(sold and start <= sold <= end)


# ─── 집계 (순수) ─────────────────────────────────────


def group_stats(rts: list[dict]) -> dict:
    """라운드트립 묶음 → 충분통계 + 파생값.

    충분통계(n, wins, losses, sum_win, sum_loss, cost)를 함께 남기는 것이 핵심이다.
    나중에 여러 주차를 합칠 때 승률·손익비는 평균 낼 수 없지만 충분통계는 더할 수 있다.
    """
    n = len(rts)
    if not n:
        return {"n": 0, "wins": 0, "losses": 0, "flats": 0, "sum_win": 0, "sum_loss": 0,
                "cost": 0, "pnl": 0, "win_rate": None, "payoff": None,
                "profit_factor": None, "avg_hold_days": None}

    wins = [r for r in rts if (r.get("pnl") or 0) > 0]
    losses = [r for r in rts if (r.get("pnl") or 0) < 0]
    sum_win = sum(r["pnl"] for r in wins)
    sum_loss = abs(sum(r["pnl"] for r in losses))
    holds = [r["hold_days"] for r in rts if r.get("hold_days") is not None]

    avg_win = sum_win / len(wins) if wins else None
    avg_loss = sum_loss / len(losses) if losses else None
    return {
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "flats": n - len(wins) - len(losses),
        "sum_win": round(sum_win),
        "sum_loss": round(sum_loss),
        "cost": round(sum((r.get("cost") or 0) for r in rts)),
        "pnl": round(sum_win - sum_loss),
        "win_rate": round(len(wins) / n * 100, 1),
        "payoff": round(avg_win / avg_loss, 2) if (avg_win and avg_loss) else None,
        "profit_factor": round(sum_win / sum_loss, 2) if sum_loss else None,
        "avg_hold_days": round(sum(holds) / len(holds), 1) if holds else None,
    }


def extract_tags(notes: Optional[str]) -> list[str]:
    """매수 메모의 'MQ[태그1,태그2]' → 태그 리스트. 없으면 []."""
    text = notes or ""
    start = text.find("MQ[")
    if start < 0:
        return []
    end = text.find("]", start)
    if end < 0:
        return []
    return [t.strip() for t in text[start + 3:end].split(",") if t.strip()]


def _row(rt: dict) -> dict:
    """JSON에 남길 라운드트립 한 줄 — 재집계에 필요한 것만."""
    return {
        "slot": rt.get("slot"),
        "ticker": rt.get("ticker"),
        "name": rt.get("name"),
        "qty": rt.get("qty"),
        "pnl": round(rt.get("pnl") or 0, 2),
        "cost": round(rt.get("cost") or 0, 2),
        "ret": round(rt.get("ret") or 0, 4),
        "buy_at": rt.get("buy_at"),
        "sell_at": rt.get("sell_at"),
        "hold_days": rt.get("hold_days"),
        "reason": rt.get("reason"),
        "tags": extract_tags(rt.get("buy_notes")),
    }


def build_report(
    roundtrips: Iterable[dict],
    *,
    as_of: date,
    slot_order: Optional[list[str]] = None,
    trend_weeks: int = TREND_WEEKS,
    rules_since: Optional[date] = RULES_SINCE,
) -> dict:
    """라운드트립 → 주간 리포트 데이터(순수). 파일·DB를 모른다.

    as_of: 집계 실행일(평가시점). 이 날짜가 속한 ISO 주가 대상 구간이다.
    """
    all_rts = list(roundtrips)
    start, end = week_bounds(as_of)
    period = [r for r in all_rts if in_period(r, start, end)]

    by_slot: dict[str, list[dict]] = {}
    for rt in period:
        by_slot.setdefault(rt.get("slot") or "?", []).append(rt)

    # slot_order는 표시 순서만 정한다. 이번 주 완결 거래가 없는 슬롯은 표에서 빼고
    # 이름만 따로 알린다 — 0건 행은 표만 지저분하게 만든다.
    order = [name for name in (slot_order or []) if name in by_slot]
    order += [name for name in sorted(by_slot) if name not in order]
    slots = [{"slot": name, **group_stats(by_slot[name])} for name in order]
    idle = [name for name in (slot_order or []) if name not in by_slot]

    by_tag: dict[str, list[dict]] = {}
    for rt in period:
        if (rt.get("slot") or "") != MYQUANT_SLOT:
            continue
        for tag in extract_tags(rt.get("buy_notes")):
            by_tag.setdefault(tag, []).append(rt)
    tags = sorted(
        ({"tag": tag, **group_stats(rows)} for tag, rows in by_tag.items()),
        key=lambda row: -row["pnl"],
    )

    # 누적: 이 주 끝까지 청산된 전부. 같은 as_of면 항상 같은 값(결정론).
    cumulative_rts = [r for r in all_rts
                      if (sold := _date_of(r.get("sell_at"))) and sold <= end]
    # 현 규칙 이후만 따로 — 규칙 변경 전후를 섞으면 전방 검증이 안 된다
    since_rts = ([r for r in cumulative_rts
                  if (sold := _date_of(r.get("sell_at"))) and sold >= rules_since]
                 if rules_since else [])

    return {
        "schema": SCHEMA_VERSION,
        "week": iso_week_label(as_of),
        "start": start,
        "end": end,
        "as_of": as_of,
        "total": group_stats(period),
        "slots": slots,
        "idle_slots": idle,
        "tags": tags,
        "cumulative": group_stats(cumulative_rts),
        "rules_since": rules_since,
        "cumulative_since": group_stats(since_rts) if rules_since else None,
        "trend": _trend(all_rts, as_of, trend_weeks),
        "rows": [_row(r) for r in sorted(period, key=lambda r: (r.get("sell_at") or ""))],
        "principles": _principles_for([s["slot"] for s in slots], bool(tags)),
    }


def _trend(all_rts: list[dict], as_of: date, weeks: int) -> list[dict]:
    """최근 N개 ISO 주차의 요약(오래된 → 최신). 노트 한 장에서 흐름이 보이게 한다."""
    out = []
    for offset in range(weeks - 1, -1, -1):
        day = as_of - timedelta(weeks=offset)
        start, end = week_bounds(day)
        rows = [r for r in all_rts if in_period(r, start, end)]
        stats = group_stats(rows)
        out.append({"week": iso_week_label(day), "n": stats["n"],
                    "pnl": stats["pnl"], "win_rate": stats["win_rate"]})
    return out


def _principles_for(slot_names: Iterable[str], has_tags: bool) -> list[str]:
    """이번 주 거래가 있었던 슬롯의 원칙 노트 + 공통 원칙(중복 제거, 순서 유지)."""
    out: list[str] = []
    for name in slot_names:
        for note in SLOT_PRINCIPLES.get(name, ()):
            if note not in out:
                out.append(note)
    if has_tags and "마이퀀트_진입조건" not in out:
        out.append("마이퀀트_진입조건")
    for note in COMMON_PRINCIPLES:
        if note not in out:
            out.append(note)
    return out


# ─── 렌더 (순수) ─────────────────────────────────────


def _won(value) -> str:
    number = int(round(value or 0))
    return f"{'+' if number > 0 else ''}{number:,}"


def _pct(value) -> str:
    return "—" if value is None else f"{value}%"


def _num(value) -> str:
    return "—" if value is None else f"{value}"


def _mark(n: int) -> str:
    return "†" if n < MIN_SAMPLE_N else ""


def _yaml_list(values: Iterable[str]) -> str:
    items = [str(v).replace('"', "'") for v in values]
    return "[" + ", ".join(f'"{v}"' for v in items) + "]"


def frontmatter(report: dict, data_file: Optional[str] = None) -> str:
    """Dataview로 걸 수 있는 평평한 스칼라만. 큰 데이터는 JSON 쪽에 둔다."""
    total, cum = report["total"], report["cumulative"]
    lines = [
        "---",
        "type: paper-weekly",
        f"schema: {report['schema']}",
        f"period: {report['week']}",
        f"period_start: {report['start'].isoformat()}",
        f"period_end: {report['end'].isoformat()}",
        f"판단시점: {report['start'].isoformat()}",
        f"평가시점: {report['as_of'].isoformat()}",
        f"closed_n: {total['n']}",
        f"pnl: {total['pnl']}",
        f"win_rate: {_num(total['win_rate'])}",
        f"payoff: {_num(total['payoff'])}",
        f"profit_factor: {_num(total['profit_factor'])}",
        f"cum_closed_n: {cum['n']}",
        f"cum_pnl: {cum['pnl']}",
        f"cum_win_rate: {_num(cum['win_rate'])}",
        f"slots: {_yaml_list(s['slot'] for s in report['slots'])}",
        f"myquant_tags: {_yaml_list(t['tag'] for t in report['tags'])}",
    ]
    since = report.get("cumulative_since")
    if since is not None and report.get("rules_since"):
        lines += [
            f"rules_since: {report['rules_since'].isoformat()}",
            f"cum_since_n: {since['n']}",
            f"cum_since_pnl: {since['pnl']}",
            f"cum_since_win_rate: {_num(since['win_rate'])}",
            f"cum_since_profit_factor: {_num(since['profit_factor'])}",
        ]
    if data_file:
        lines.append(f"data_file: {data_file}")
    lines.append("---")
    return "\n".join(lines)


def render_note(report: dict, data_file: Optional[str] = None) -> str:
    """리포트 데이터 → 노트 본문(순수). 같은 입력이면 같은 출력."""
    total = report["total"]
    lines = [
        frontmatter(report, data_file),
        f"# 페이퍼 주간 성과 {report['week']}",
        "",
        f"집계 구간 {report['start'].isoformat()} ~ {report['end'].isoformat()} "
        f"· 청산일 기준 · 완결 {total['n']}건",
        "",
    ]

    if not total["n"]:
        lines += ["이번 주 완결 거래가 없다. 보유 중인 포지션의 평가손익은 집계하지 않는다.", ""]
    else:
        lines += [
            "## 슬롯별",
            "",
            "| 슬롯 | 실현손익 | 승/패 | 승률 | 손익비 | 평균보유 | 표본 |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for row in report["slots"]:
            hold = "—" if row["avg_hold_days"] is None else f"{row['avg_hold_days']}일"
            lines.append(
                f"| {row['slot']}{_mark(row['n'])} | {_won(row['pnl'])} | "
                f"{row['wins']}/{row['losses']} | {_pct(row['win_rate'])} | "
                f"{_num(row['payoff'])} | {hold} | {row['n']} |"
            )
        lines.append(
            f"| **합계** | **{_won(total['pnl'])}** | {total['wins']}/{total['losses']} | "
            f"{_pct(total['win_rate'])} | {_num(total['payoff'])} | — | {total['n']} |"
        )
        lines.append("")
        if report.get("idle_slots"):
            lines += [f"이번 주 완결 거래 없음: {', '.join(report['idle_slots'])}", ""]

    if report["tags"]:
        lines += [
            "## 조건별 (마이퀀트)",
            "",
            "| 태그 | 실현손익 | 승/패 | 승률 | 손익비 | 표본 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for row in report["tags"]:
            lines.append(
                f"| {row['tag']}{_mark(row['n'])} | {_won(row['pnl'])} | "
                f"{row['wins']}/{row['losses']} | {_pct(row['win_rate'])} | "
                f"{_num(row['payoff'])} | {row['n']} |"
            )
        lines.append("")

    trend = report.get("trend") or []
    if len(trend) > 1:
        lines += [
            f"## 최근 {len(trend)}주 추이",
            "",
            "| 주차 | 완결 | 실현손익 | 승률 |",
            "|---|---:|---:|---:|",
        ]
        for row in trend:
            lines.append(
                f"| {row['week']} | {row['n']} | {_won(row['pnl'])} | {_pct(row['win_rate'])} |"
            )
        lines.append("")

    cum = report["cumulative"]
    lines += [
        "## 누적 (개시 ~ 이번 주)",
        "",
        f"- **전체**: 완결 {cum['n']}건 · 실현손익 {_won(cum['pnl'])} · 승률 {_pct(cum['win_rate'])} "
        f"· 손익비 {_num(cum['payoff'])} · PF {_num(cum['profit_factor'])}",
    ]
    since = report.get("cumulative_since")
    if since and report.get("rules_since"):
        lines += [
            f"- **현 규칙 이후** ({report['rules_since'].isoformat()}~): 완결 {since['n']}건 "
            f"· 실현손익 {_won(since['pnl'])} · 승률 {_pct(since['win_rate'])} "
            f"· 손익비 {_num(since['payoff'])} · PF {_num(since['profit_factor'])}",
            "",
            "> 청산 규칙이 바뀐 뒤 기록만 따로 본 것이 **전방 검증**이다. 규칙 변경 전후를",
            "> 한 덩어리로 보면 옛 규칙의 손실이 지금 성적을 덮어쓴다.",
        ]
    lines.append("")
    # † 안내는 실제로 †가 붙은 행이 있을 때만. 없는 각주는 읽는 사람을 헷갈리게 한다.
    if any(row["n"] < MIN_SAMPLE_N for row in report["slots"] + report["tags"]):
        lines += [f"† 표본 {MIN_SAMPLE_N}건 미만, 참고용. 이 수치로 원칙을 고치지 않는다.", ""]
    lines += [
        "## 적용 원칙",
        "",
    ]
    lines += [f"- [[{note}]]" for note in report["principles"]]
    lines += [
        "",
        "> 이 노트는 `scripts/paper_weekly_report.py`가 자동 생성한다. 직접 고친 내용은",
        "> 다음 실행 때 덮어쓰인다. 회고 메모는 별도 노트에 남길 것.",
        "> 주차를 가로지르는 분석은 노트가 아니라 frontmatter의 `data_file`(충분통계 +",
        "> 라운드트립 원본)을 읽는다.",
        "",
    ]
    return "\n".join(lines)


def build_data_payload(report: dict) -> dict:
    """분석용 JSON — 파생값이 아니라 **재집계 가능한 형태**로 남긴다."""
    return {
        "schema": report["schema"],
        "period": report["week"],
        "period_start": report["start"].isoformat(),
        "period_end": report["end"].isoformat(),
        "판단시점": report["start"].isoformat(),
        "평가시점": report["as_of"].isoformat(),
        "total": report["total"],
        "slots": report["slots"],
        "myquant_tags": report["tags"],
        "cumulative": report["cumulative"],
        "rules_since": report["rules_since"].isoformat() if report.get("rules_since") else None,
        "cumulative_since": report.get("cumulative_since"),
        "trend": report["trend"],
        "roundtrips": report["rows"],
    }


# ─── I-O 경계 ────────────────────────────────────────


def load_roundtrips(db_path=None) -> list[dict]:
    """paper.db → FIFO 완결 라운드트립. 지연 임포트(순수 함수 테스트와 분리)."""
    import paper_db
    import trade_analytics as ta

    trades = (paper_db.list_trades(limit=100000) if db_path is None
              else paper_db.list_trades(limit=100000, db_path=db_path))
    return ta.compute_roundtrips(trades)


def note_path(vault_root: Path, week: str) -> Path:
    return Path(vault_root) / "wiki" / "투자" / "성과" / f"주간_{week}.md"


def data_path(state_root: Path, week: str) -> Path:
    return Path(state_root) / "paper_weekly" / f"{week}.json"


def write_atomic(path: Path, text: str) -> Path:
    """같은 디렉터리에 임시 파일로 쓴 뒤 교체(원자적). 같은 주차 재실행은 덮어쓴다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def default_vault_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "obsidian-vault"


def default_state_root() -> Path:
    try:
        from storage_paths import PATHS
        return Path(PATHS.private_state_dir)
    except Exception:  # storage_paths 없이도 동작 (테스트·오프라인)
        return Path(__file__).resolve().parent.parent / "data" / "private" / "state"


def write_report(report: dict, *, vault_root: Path, state_root: Path) -> tuple[Path, Path]:
    """노트 + 분석용 JSON을 함께 쓴다. 노트가 JSON 경로를 frontmatter로 가리킨다."""
    json_target = data_path(state_root, report["week"])
    note_target = note_path(vault_root, report["week"])
    write_atomic(json_target, json.dumps(build_data_payload(report),
                                         ensure_ascii=False, indent=2) + "\n")
    write_atomic(note_target, render_note(report, data_file=str(json_target)))
    return note_target, json_target


def _cli() -> None:
    parser = argparse.ArgumentParser(description="페이퍼 주간 성과 노트 + 분석 JSON 생성")
    parser.add_argument("--date", help="집계 기준일 YYYY-MM-DD (기본: 오늘)")
    parser.add_argument("--vault", help="obsidian-vault 경로")
    parser.add_argument("--state", help="분석용 JSON 저장 루트 (기본: data/private/state)")
    parser.add_argument("--db", help="paper.db 경로 (기본: 운영 경로)")
    parser.add_argument("--since", help=f"현 규칙 시작일 YYYY-MM-DD (기본: {RULES_SINCE})")
    parser.add_argument("--stdout", action="store_true", help="파일로 쓰지 않고 노트만 출력")
    args = parser.parse_args()

    as_of = _date_of(args.date) or date.today()
    report = build_report(
        load_roundtrips(args.db),
        as_of=as_of,
        slot_order=["콴텍", "키움", "IPO", MYQUANT_SLOT],
        rules_since=_date_of(args.since) or RULES_SINCE,
    )

    if args.stdout:
        print(render_note(report))
        return

    note, data = write_report(
        report,
        vault_root=Path(args.vault) if args.vault else default_vault_root(),
        state_root=Path(args.state) if args.state else default_state_root(),
    )
    print(f"✅ 노트: {note}")
    print(f"✅ 데이터: {data}  (완결 {report['total']['n']}건 / 누적 {report['cumulative']['n']}건)")


if __name__ == "__main__":
    _cli()
