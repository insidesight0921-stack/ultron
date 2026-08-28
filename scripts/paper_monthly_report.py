"""paper_monthly_report.py — 페이퍼 월간 성과 노트 + 분석 JSON (v1)

주간 리포트(`paper_weekly_report`)는 그 주에 청산된 거래만 센다. 월간은 그 위에
**주간에는 실을 수 없던 것**을 얹는다 — 일간 마크투마켓 자산곡선에서 나오는
샤프·MDD·베타·알파, 그리고 실전 전환 기준 대조다. 이 지표들은 20거래일쯤은 있어야
의미가 생겨서 주 단위로는 낼 수 없다.

집계 기준은 주간과 같다.
  - 라운드트립 귀속은 **청산일** 기준(성과가 확정되는 시점).
  - 승률·손익비 대신 **충분통계**(건수·승패·이익합·손실합·비용)를 함께 저장한다.
    나중에 여러 달을 합칠 때 비율은 평균 낼 수 없지만 충분통계는 더할 수 있다.
  - 데이터 품질 규칙에 걸린 거래는 집계에서 빠지고, 무엇이 빠졌는지 노트에 남는다.

두 구간을 나눠 싣는다.
  - **이번 달**: 그 달에 청산된 거래 + 그 달 구간의 자산곡선 수익률·MDD.
  - **누적**: 첫 거래일부터의 자산곡선. 실전 전환 기준(샤프 1.0 / MDD −15% /
    초과수익 +3%p / 젠센 알파 양수)은 여기서만 판정한다. 한 달로 6개월 기준을
    판정하면 표본이 모자라 아무 의미가 없다.

이 스크립트는 **기록만 한다.** 원칙 갱신이나 파라미터 변경은 사람 승인을 거친다.
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Optional

import paper_weekly_report as wk

log = logging.getLogger("paper_monthly_report")

SCHEMA_VERSION = 1
TREND_MONTHS = 6      # 노트에 함께 싣는 최근 개월 수


# ─── 기간 (순수) ─────────────────────────────────────


def month_label(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def month_bounds(d: date) -> tuple[date, date]:
    """그 날짜가 속한 달의 (1일, 말일)."""
    start = d.replace(day=1)
    nxt = (start + timedelta(days=32)).replace(day=1)
    return start, nxt - timedelta(days=1)


def prev_month(d: date) -> date:
    """직전 달의 아무 날(말일). 월초 실행 시 '지난달'을 집계하려고 쓴다."""
    return d.replace(day=1) - timedelta(days=1)


def slice_curve(curve: dict, start: date, end: date) -> dict:
    """자산곡선을 한 달 구간으로 자른다(순수).

    커버리지는 자른 구간 기준으로 다시 센다 — 전체 커버리지를 그대로 쓰면
    그 달에 결측이 몰려 있어도 드러나지 않는다.
    """
    lo, hi = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    days = [d for d in curve.get("days", []) if lo <= d["date"] <= hi]
    missing = [d for d in curve.get("missing_days", []) if lo <= d <= hi]
    total = len(days) + len(missing)
    return {"days": days, "n_days": len(days),
            "coverage": round(len(days) / total, 3) if total else None,
            "missing_days": missing,
            "missing_tickers": curve.get("missing_tickers", {})}


# ─── 집계 (순수) ─────────────────────────────────────


def _by(rts: list[dict], key) -> list[dict]:
    """키별 충분통계 — 손익 큰 순."""
    groups: dict[str, list[dict]] = {}
    for r in rts:
        groups.setdefault(key(r) or "?", []).append(r)
    rows = [dict(wk.group_stats(v), label=k) for k, v in groups.items()]
    return sorted(rows, key=lambda r: -r["pnl"])


def _tag_rows(rts: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for r in rts:
        for tag in wk.extract_tags(r.get("buy_notes")):
            groups.setdefault(tag, []).append(r)
    rows = [dict(wk.group_stats(v), tag=k) for k, v in groups.items()]
    return sorted(rows, key=lambda r: -r["pnl"])


def _trend(all_rts: list[dict], as_of: date, months: int) -> list[dict]:
    """최근 N개월 요약(오래된 → 최신). 한 달만 보면 흐름이 안 보인다."""
    out, cursor = [], as_of
    for _ in range(months):
        start, end = month_bounds(cursor)
        rows = [r for r in all_rts if wk.in_period(r, start, end)]
        out.append(dict(wk.group_stats(rows), month=month_label(cursor)))
        cursor = prev_month(cursor)
    return list(reversed(out))


def build_report(roundtrips: Iterable[dict], *, as_of: date,
                 curve: Optional[dict] = None, bench: Optional[dict] = None,
                 excluded: Optional[list] = None,
                 slot_order: Optional[list[str]] = None,
                 trend_months: int = TREND_MONTHS,
                 rf_annual: float = 0.0) -> dict:
    """라운드트립 + 자산곡선 → 월간 리포트 데이터(순수). 파일·DB를 모른다."""
    all_rts = list(roundtrips)
    start, end = month_bounds(as_of)
    period = [r for r in all_rts if wk.in_period(r, start, end)]

    slots = _by(period, lambda r: r.get("slot"))
    if slot_order:
        rank = {name: i for i, name in enumerate(slot_order)}
        slots.sort(key=lambda r: (rank.get(r["label"], 99), -r["pnl"]))
    idle = [s for s in (slot_order or []) if s not in {r["label"] for r in slots}]

    month_metrics = cumulative_metrics = None
    if curve is not None and bench is not None:
        import equity_curve as ec

        month_metrics = ec.evaluate(slice_curve(curve, start, end), bench,
                                    rf_annual=rf_annual)
        cumulative_metrics = ec.evaluate(curve, bench, rf_annual=rf_annual)
        cumulative_metrics["verdict"] = ec.verdict(cumulative_metrics)

    ex = list(excluded or [])
    try:
        import data_quality
        ex_summary = data_quality.summary(ex)
    except Exception:  # noqa: BLE001
        ex_summary = {"n": len(ex), "pnl": 0, "kinds": [], "rows": []}

    return {
        "schema": SCHEMA_VERSION,
        "month": month_label(as_of),
        "start": start,
        "end": end,
        "as_of": as_of,
        "total": wk.group_stats(period),
        "slots": slots,
        "idle_slots": idle,
        "reasons": _by(period, lambda r: r.get("reason")),
        "tags": _tag_rows(period),
        "cumulative": wk.group_stats(all_rts),
        "trend": _trend(all_rts, as_of, trend_months),
        "month_metrics": month_metrics,
        "cumulative_metrics": cumulative_metrics,
        "excluded": ex_summary,
        "rows": [wk._row(r) for r in sorted(period, key=lambda r: (r.get("sell_at") or ""))],
        "principles": wk._principles_for([s["label"] for s in slots], bool(_tag_rows(period))),
    }


# ─── 렌더 (순수) ─────────────────────────────────────


def _unit(value, unit: str) -> str:
    """값이 없으면 단위를 붙이지 않는다 — '—%'는 0%처럼 읽힌다."""
    return "—" if value is None else f"{value}{unit}"


def _metric_line(m: Optional[dict], label: str) -> list[str]:
    if not m or not m.get("n_days"):
        return [f"- {label}: 평가할 거래일이 없습니다."]
    n = wk._num
    lines = [f"- {label} ({m['start']}~{m['end']}, {m['n_days']}거래일 · "
             f"커버리지 {m['coverage']:.0%})" if m.get("coverage") is not None
             else f"- {label} ({m['n_days']}거래일)"]
    lines.append(f"    - 수익률 {_unit(m['port_return'], '%')} "
                 f"(코스피 {_unit(m['bench_return'], '%')}, "
                 f"초과 {_unit(m['excess_return'], '%p')})")
    lines.append(f"    - MDD {_unit(m['mdd'], '%')} (코스피 {_unit(m['bench_mdd'], '%')}) · "
                 f"샤프 {n(m['sharpe'])} · 베타 {n(m['beta'])} · "
                 f"알파 {_unit(m['alpha'], '%')}")
    if not m.get("usable"):
        lines.append(f"    - ⚠️ 판정 보류: {' / '.join(m.get('reasons') or [])}")
    if m.get("missing_days"):
        lines.append(f"    - 가격 결측으로 건너뛴 날 {m['missing_days']}일 "
                     f"(직전 값으로 메우지 않음)")
    return lines


def render_note(report: dict, data_file: Optional[str] = None) -> str:
    n, won, pct = wk._num, wk._won, wk._pct
    t = report["total"]
    out = [f"# 페이퍼 월간 성과 — {report['month']}", "",
           f"> {report['start']} ~ {report['end']} · 집계 {report['as_of']}",
           "> 모의투자(페이퍼) 기록이며 실계좌가 아닙니다.", ""]

    out += ["## 이번 달 완결 거래", "",
            f"- {t['n']}건 · {won(t['pnl'])} · 승률 {pct(t['win_rate'])} · "
            f"손익비 {n(t['payoff'])} · PF {n(t['profit_factor'])}",
            f"- 평균 보유 {n(t['avg_hold_days'])}일 · 진입 원가 "
            f"{int(round(t['cost'] or 0)):,}원", ""]

    if report["slots"]:
        out += ["### 슬롯별", "",
                "| 슬롯 | 건수 | 승률 | 실현 손익 | PF |", "|---|---:|---:|---:|---:|"]
        for s in report["slots"]:
            out.append(f"| {s['label']}{wk._mark(s['n'])} | {s['n']} | {pct(s['win_rate'])} "
                       f"| {won(s['pnl'])} | {n(s['profit_factor'])} |")
        out.append("")
    if report["idle_slots"]:
        out += [f"거래 없음: {', '.join(report['idle_slots'])}", ""]

    if report["reasons"]:
        out += ["### 청산 사유별", "",
                "| 사유 | 건수 | 실현 손익 | 평균 보유 |", "|---|---:|---:|---:|"]
        for r in report["reasons"]:
            out.append(f"| {r['label']}{wk._mark(r['n'])} | {r['n']} | {won(r['pnl'])} "
                       f"| {n(r['avg_hold_days'])}일 |")
        out.append("")

    if report["tags"]:
        out += ["### 진입 태그별", "",
                "| 태그 | 건수 | 승률 | 실현 손익 |", "|---|---:|---:|---:|"]
        for r in report["tags"]:
            out.append(f"| {r['tag']}{wk._mark(r['n'])} | {r['n']} | {pct(r['win_rate'])} "
                       f"| {won(r['pnl'])} |")
        out.append("")

    out += ["## 자산곡선 (일간 마크투마켓)", ""]
    out += _metric_line(report.get("month_metrics"), "이번 달")
    out += _metric_line(report.get("cumulative_metrics"), "누적")
    out.append("")

    cm = report.get("cumulative_metrics") or {}
    if cm.get("verdict"):
        out += ["### 실전 전환 기준 (누적 기준)", "",
                "| 기준 | 현재 | 기준값 | 판정 |", "|---|---:|---|---|"]
        for c in cm["verdict"]:
            mark = ("판정 불가" if c["passed"] is None
                    else ("✅ 충족" if c["passed"] else "❌ 미충족"))
            out.append(f"| {c['name']} | {n(c['value'])} | {c['threshold']} {c['direction']} "
                       f"| {mark} |")
        out += ["", "> 표본이 6개월에 못 미치는 동안에는 충족해도 전환 근거가 아닙니다.", ""]

    ex = report.get("excluded") or {}
    if ex.get("n"):
        out += ["## 집계에서 제외한 기록", "",
                f"- {ex['n']}건 · {won(ex['pnl'])} — 진입가가 과거 종가와 일치한 건입니다.",
                "- 기록은 DB에 그대로 남아 있고, 집계에서만 뺐습니다.", ""]

    if report["trend"]:
        out += ["## 최근 추이", "", "| 월 | 건수 | 승률 | 실현 손익 |", "|---|---:|---:|---:|"]
        for m in report["trend"]:
            out.append(f"| {m['month']} | {m['n']} | {pct(m['win_rate'])} | {won(m['pnl'])} |")
        out.append("")

    if any(r["n"] < wk.MIN_SAMPLE_N for r in report["slots"] + report["reasons"]):
        out += [f"† 표본 {wk.MIN_SAMPLE_N}건 미만, 참고용. 이 수치로 원칙을 고치지 않습니다.", ""]

    if report["principles"]:
        out += ["## 연결된 원칙 노트", ""]
        out += [f"- [[{p}]]" for p in report["principles"]]
        out.append("")
    if data_file:
        out += [f"분석용 원자료: `{data_file}`", ""]
    return "\n".join(out)


def format_telegram(report: dict) -> str:
    """텔레그램용 요약(순수). 평문 — 서식 문자를 쓰지 않는다.

    노트를 그대로 보내지 않는다. 표는 폰에서 읽히지 않고, 길면 잘려서 결론이
    맨 뒤로 밀린다. **판정과 그 근거만** 담고 자세한 내용은 노트로 넘긴다.
    """
    n, won, pct = wk._num, wk._won, wk._pct
    t = report["total"]
    out = [f"📅 페이퍼 월간 성과 {report['month']}",
           f"({report['start']} ~ {report['end']} · 모의투자, 실계좌 아님)", "",
           f"완결 {t['n']}건 · {won(t['pnl'])}원 · 승률 {pct(t['win_rate'])} "
           f"· PF {n(t['profit_factor'])}"]

    for row in report["slots"]:
        out.append(f"  {row['label']}{wk._mark(row['n'])} {row['n']}건 "
                   f"{won(row['pnl'])}원 승률 {pct(row['win_rate'])}")

    m = report.get("month_metrics")
    if m and m.get("n_days"):
        out += ["", f"이번 달 자산 {_unit(m['port_return'], '%')} "
                    f"(코스피 {_unit(m['bench_return'], '%')}, "
                    f"초과 {_unit(m['excess_return'], '%p')})"]
        if not m.get("usable"):
            out.append(f"  ※ {' / '.join(m.get('reasons') or [])}")

    c = report.get("cumulative_metrics")
    if c and c.get("n_days"):
        out += ["", f"누적 {c['n_days']}거래일 · 자산 {_unit(c['port_return'], '%')} "
                    f"· 샤프 {n(c['sharpe'])} · MDD {_unit(c['mdd'], '%')}"]
        for check in c.get("verdict") or []:
            mark = ("· 판정 불가" if check["passed"] is None
                    else ("✅" if check["passed"] else "❌"))
            out.append(f"  {mark} {check['name']} {n(check['value'])} "
                       f"(기준 {check['threshold']} {check['direction']})")
        out.append("  ※ 6개월 표본 전에는 충족해도 전환 근거가 아닙니다.")

    ex = report.get("excluded") or {}
    if ex.get("n"):
        out += ["", f"집계 제외 {ex['n']}건 ({won(ex['pnl'])}원) — 진입가 오류. "
                    f"기록은 DB에 남아 있습니다."]
    out += ["", "자세한 내용은 옵시디언 월간 노트를 보세요."]
    return "\n".join(out)


def build_data_payload(report: dict) -> dict:
    """재집계용 JSON. 비율이 아니라 충분통계와 원자료를 남긴다."""
    def _d(v):
        return v.isoformat() if isinstance(v, date) else v
    return {
        "schema": SCHEMA_VERSION,
        "month": report["month"],
        "start": _d(report["start"]), "end": _d(report["end"]),
        "as_of": _d(report["as_of"]),
        "total": report["total"], "slots": report["slots"],
        "reasons": report["reasons"], "tags": report["tags"],
        "cumulative": report["cumulative"], "trend": report["trend"],
        "month_metrics": _strip_series(report.get("month_metrics")),
        "cumulative_metrics": _strip_series(report.get("cumulative_metrics")),
        "excluded": report.get("excluded"),
        "rows": report["rows"],
    }


def _strip_series(m: Optional[dict]) -> Optional[dict]:
    """지표에서 일별 시계열은 뺀다 — 노트 옆 JSON이 수 MB가 되면 아무도 안 읽는다."""
    if not m:
        return m
    return {k: v for k, v in m.items() if k != "series"}


# ─── 경계 (I-O) ──────────────────────────────────────


def note_path(vault_root: Path, month: str) -> Path:
    return Path(vault_root) / "wiki" / "투자" / "성과" / f"월간_{month}.md"


def data_path(state_root: Path, month: str) -> Path:
    return Path(state_root) / "paper_monthly" / f"{month}.json"


def load_inputs(db_path=None):
    """(집계용 라운드트립, 제외분, 자산곡선, 벤치마크)."""
    import equity_curve as ec
    import paper_db
    import trade_analytics as ta

    kwargs = {"db_path": db_path} if db_path else {}
    kept, dropped = ta.roundtrips_for_analysis(paper_db.list_trades(limit=100000, **kwargs))
    trades, calendar, seeds, series, bench = ec.load_inputs(db_path)
    curve = ec.build(trades, calendar, seeds, series)
    return kept, dropped, curve, bench


def write_report(report: dict, *, vault_root: Path, state_root: Path) -> tuple[Path, Path]:
    json_target = data_path(state_root, report["month"])
    note_target = note_path(vault_root, report["month"])
    wk.write_atomic(json_target,
                    json.dumps(build_data_payload(report), ensure_ascii=False, indent=2) + "\n")
    wk.write_atomic(note_target, render_note(report, data_file=str(json_target)))
    return note_target, json_target


def _notify(text: str) -> int:
    """텔레그램 발송. 실패해도 예외를 올리지 않는다 — 알림이 안 갔다고 리포트 생성까지
    실패시킬 이유가 없다. 노트는 이미 파일로 남아 있다."""
    try:
        import telegram_notify
        return telegram_notify.send(text)
    except Exception as exc:  # noqa: BLE001
        log.warning("텔레그램 발송 실패: %s", exc)
        return 0


def _cli() -> int:
    ap = argparse.ArgumentParser(description="페이퍼 월간 성과 노트 + 분석 JSON 생성")
    ap.add_argument("--date", help="집계 기준일 YYYY-MM-DD (기본: 오늘)")
    ap.add_argument("--last-month", action="store_true",
                    help="직전 달을 집계 (월초 스케줄 실행용)")
    ap.add_argument("--vault"), ap.add_argument("--state"), ap.add_argument("--db")
    ap.add_argument("--rf", type=float, default=0.0, help="무위험수익률(연, 0.03 = 3%%)")
    ap.add_argument("--stdout", action="store_true", help="파일로 쓰지 않고 노트만 출력")
    ap.add_argument("--notify", action="store_true", help="텔레그램으로 요약 발송")
    ap.add_argument("--notify-only", action="store_true",
                    help="파일을 쓰지 않고 텔레그램 요약만 발송(점검용)")
    args = ap.parse_args()

    as_of = wk._date_of(args.date) or date.today()
    if args.last_month:
        as_of = prev_month(as_of)

    kept, dropped, curve, bench = load_inputs(args.db)
    report = build_report(kept, as_of=as_of, curve=curve, bench=bench, excluded=dropped,
                          slot_order=["콴텍", "키움", "IPO", wk.MYQUANT_SLOT],
                          rf_annual=args.rf)
    if args.notify_only:
        summary = format_telegram(report)
        print(summary)
        _notify(summary)
        return 0
    if args.stdout:
        print(render_note(report))
        return 0
    note, data = write_report(
        report,
        vault_root=Path(args.vault) if args.vault else wk.default_vault_root(),
        state_root=Path(args.state) if args.state else wk.default_state_root())
    print(f"✅ 월간 노트: {note}")
    print(f"   분석 JSON: {data}")
    if args.notify:
        sent = _notify(format_telegram(report))
        print(f"   텔레그램 발송: {sent}건" if sent
              else "   텔레그램 발송 안 됨(환경변수 없음)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
