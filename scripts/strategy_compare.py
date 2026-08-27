"""strategy_compare.py — 전략별 성과 비교 (탭 A 데이터 소스, v1)

계획서 "탭 A: 전략별 성과 비교 차트"에 해당한다.

**먼저 확인한 사실**: 2026-08-27 기준 완결 라운드트립 78건 중 진입 전략 태그가 붙은 것은
0건이다. 마이퀀트 진입 조건 4종(추세위 눌림 / 정배열 / 신고가 돌파 / 과낙폭 반등)은
`entry_backtest.py`에만 있고 실제 매수 기록(`trades.notes`)에 남지 않는다.
그래서 지금 "전략별"로 나눌 수 있는 축은 진입 조건이 아니라 다음 넷뿐이다.

  - 봇(슬롯)      — 키움 / 콴텍 / IPO / 마이퀀트
  - 청산 사유      — 손절 / 익절 / 리밸런싱 / 수동
  - 보유 기간      — 당일 / 2~5일 / 6~20일 / 21일+
  - 규칙 변경 전후  — `RULES_SINCE` 기준 분리

이 모듈은 그 한계를 숨기지 않는다. `attribution()`이 태그 커버리지를 그대로 보고하고,
UI는 그것을 화면에 띄운다. 커버리지가 낮은 동안 "전략 A가 낫다"는 결론은 낼 수 없다.

설계:
  - 집계는 `paper_weekly_report.group_stats`를 그대로 쓴다. 승률·손익비를 두 곳에서
    따로 계산하면 언젠가 서로 다른 값을 말하게 된다.
  - 각 축의 행마다 **전체 평균 대비 차이(edge_ret)** 를 함께 낸다. 어떤 묶음이든
    전체보다 나은지가 판단 기준이지, 절대 수익률이 아니다.
  - 표본 5건 미만은 `small=True`로 표시한다. 판정하지 않는다.
  - 순수 함수만 둔다. DB 조회는 `load_roundtrips()` 하나에 모은다.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date
from typing import Callable, Iterable, Optional

from paper_weekly_report import MIN_SAMPLE_N, RULES_SINCE, group_stats

TAG_AXIS = "tag"
UNTAGGED = "미태깅"


# ─── 축 라벨 (순수) ──────────────────────────────────


def slot_label(rt: dict) -> str:
    return rt.get("slot") or "?"


def reason_label(rt: dict) -> str:
    return rt.get("reason") or "수동/기타"


def hold_label(rt: dict) -> str:
    d = rt.get("hold_days")
    if d is None:
        return "미상"
    if d <= 1:
        return "당일~1일"
    if d <= 5:
        return "2~5일"
    if d <= 20:
        return "6~20일"
    return "21일+"


def era_label(rt: dict, rules_since: date = RULES_SINCE) -> str:
    """규칙 변경 전후. **청산일** 기준으로 가른다.

    2026-07-23 변경은 청산 쪽 규칙이었다(장중 감시 주기 30분 → 5분, 손절 실행 지연 개선).
    청산 규칙의 효과는 그 이후에 판 거래에 나타나므로, 언제 샀는지가 아니라
    언제 팔았는지로 갈라야 한다. 매수일로 가르면 새 규칙이 실제로 구해 준 거래
    (구 규칙 때 사서 새 규칙으로 빨리 잘라낸 건)가 구 규칙 성적으로 들어간다.

    `paper_weekly_report.build_report`의 `cumulative_since`와 같은 기준이다 —
    같은 것을 두 화면이 다르게 말하면 어느 쪽도 믿을 수 없게 된다.
    """
    sold = str(rt.get("sell_at") or "")[:10]
    if not sold:
        return "미상"
    return "현 규칙" if sold >= rules_since.isoformat() else "구 규칙"


def tag_labels(rt: dict, extract: Optional[Callable[[Optional[str]], list]] = None) -> list[str]:
    """진입 태그(복수 가능). 없으면 ['미태깅'] — 조용히 빠지면 커버리지를 못 본다."""
    if extract is None:
        from paper_weekly_report import extract_tags as extract
    tags = extract(rt.get("buy_notes"))
    return list(tags) if tags else [UNTAGGED]


DEFAULT_CAVEAT = ""

# 축마다 오독하기 쉬운 지점을 함께 들고 다닌다. 숫자만 띄우면 반드시 인과로 읽힌다.
AXES: dict[str, dict] = {
    "slot": {
        "title": "봇(슬롯)", "label": slot_label, "multi": False,
        "caveat": "슬롯마다 자본과 회전율이 달라 총액은 규모를 함께 반영한다. "
                  "평균 수익률과 나란히 볼 것.",
    },
    "reason": {
        "title": "청산 사유", "label": reason_label, "multi": False,
        "caveat": "손절은 정의상 손실, 익절은 정의상 이익이다. 사유끼리 비교하는 칸이 아니라 "
                  "**각 사유 안에서 크기가 어떤가**(평균·PF)를 보는 칸이다.",
    },
    "hold": {
        "title": "보유 기간", "label": hold_label, "multi": False,
        "caveat": "청산 규칙과 얽혀 있다 — 손절선(−7%)에 먼저 걸리는 거래는 짧게 끝나고 "
                  "트레일링이 붙은 거래는 길게 간다. '오래 들면 좋다'가 아니라 "
                  "'좋았기에 오래 갔다'일 수 있다. 인과로 읽지 말 것.",
    },
    "era": {
        "title": "규칙 변경 전후", "label": era_label, "multi": False,
        "caveat": "청산일 기준 귀속(2026-07-23 변경이 청산 규칙이었기 때문). "
                  "진입 규칙을 바꾼 뒤에는 기준을 매수일로 옮겨야 한다.",
    },
    TAG_AXIS: {
        "title": "진입 태그", "label": tag_labels, "multi": True,
        "caveat": "커버리지가 낮으면 표본이 아니라 흔적일 뿐이다. "
                  "한 거래에 태그가 여럿이면 각 태그에 중복 계상된다.",
    },
}


# ─── 집계 (순수) ─────────────────────────────────────


def avg_ret(rts: list[dict]) -> Optional[float]:
    """평균 수익률(**%**). 금액이 아니라 비율이라야 규모가 다른 묶음을 비교할 수 있다.

    `compute_roundtrips`의 `ret`은 비율(pnl/cost, 0.05 = 5%)이다. 여기서 100을 곱해
    퍼센트로 바꾼다 — 화면에 %로 쓰면서 소수를 그대로 내보내면 −14%가 −0.14%로 보인다.
    """
    vals = [r["ret"] for r in rts if r.get("ret") is not None]
    return round(sum(vals) / len(vals) * 100, 2) if vals else None


def bucketize(rts: Iterable[dict], axis: str, *,
              rules_since: date = RULES_SINCE) -> dict[str, list[dict]]:
    """축 이름 → {라벨: 라운드트립 목록}(순수). 복수 라벨 축은 중복 계상된다."""
    spec = AXES[axis]
    out: dict[str, list[dict]] = defaultdict(list)
    for rt in rts:
        if axis == "era":
            labels = [spec["label"](rt, rules_since)]
        elif spec["multi"]:
            labels = spec["label"](rt)
        else:
            labels = [spec["label"](rt)]
        for lb in labels:
            out[lb].append(rt)
    return dict(out)


def compare(rts: list[dict], axis: str = "slot", *,
            rules_since: date = RULES_SINCE) -> dict:
    """한 축의 비교표(순수).

    각 행에 전체 평균 대비 `edge_ret`을 붙인다 — 절대 수익률만 보면
    장이 좋았던 구간의 묶음이 항상 이긴다.
    """
    if axis not in AXES:
        raise ValueError(f"알 수 없는 축: {axis}")
    rts = list(rts)
    total = group_stats(rts)
    total["avg_ret"] = avg_ret(rts)

    rows = []
    for label, group in bucketize(rts, axis, rules_since=rules_since).items():
        st = group_stats(group)
        st["label"] = label
        st["avg_ret"] = avg_ret(group)
        st["edge_ret"] = (None if (st["avg_ret"] is None or total["avg_ret"] is None)
                          else round(st["avg_ret"] - total["avg_ret"], 2))
        st["share_n"] = round(len(group) / len(rts) * 100, 1) if rts else None
        st["small"] = st["n"] < MIN_SAMPLE_N
        rows.append(st)
    rows.sort(key=lambda r: -r["pnl"])
    return {
        "axis": axis,
        "title": AXES[axis]["title"],
        "caveat": AXES[axis].get("caveat", DEFAULT_CAVEAT),
        "multi": AXES[axis]["multi"],
        "rows": rows,
        "total": total,
        "min_sample": MIN_SAMPLE_N,
        "rules_since": rules_since.isoformat(),
    }


def attribution(rts: list[dict],
                extract: Optional[Callable[[Optional[str]], list]] = None) -> dict:
    """진입 전략 귀속 커버리지(순수).

    커버리지가 0이면 "전략별 비교"라는 말 자체가 성립하지 않는다.
    이 값을 화면에 띄우는 것이 이 모듈에서 가장 중요한 일이다.
    """
    rts = list(rts)
    tagged = [r for r in rts if tag_labels(r, extract) != [UNTAGGED]]
    n = len(rts)
    return {
        "total": n,
        "tagged": len(tagged),
        "coverage": round(len(tagged) / n * 100, 1) if n else None,
        "usable": len(tagged) >= MIN_SAMPLE_N,
    }


OUTLIER_RET = 1.0   # |수익률| 100% 이상 — 스윙 매매에서 나올 수 없는 값


def outliers(rts: list[dict], threshold: float = OUTLIER_RET) -> list[dict]:
    """수익률이 현실적으로 불가능한 라운드트립(순수).

    2026-05-10 콴텍 슬롯의 삼성전자 10건이 이 경우다. UI를 시험하며 매수가를
    80,000원으로 임의 입력한 뒤 279,000원(당시 실제가)에 판 기록이라 +247.7%,
    +198만원이 잡힌다. **성적이 아니라 시험 잔재다.**

    자동으로 빼지 않는다 — 원본 기록을 조용히 지우면 나중에 왜 숫자가 달라졌는지
    아무도 모른다. 표시만 하고, 뺄지 말지는 사람이 정한다.
    """
    out = []
    for r in rts:
        ret = r.get("ret")
        if ret is None or abs(ret) < threshold:
            continue
        out.append({"slot": r.get("slot"), "ticker": r.get("ticker"),
                    "name": r.get("name"), "ret": round(ret * 100, 1),
                    "pnl": round(r.get("pnl") or 0), "buy_at": r.get("buy_at"),
                    "sell_at": r.get("sell_at")})
    return sorted(out, key=lambda r: -abs(r["ret"]))


def overview(rts: list[dict], *, rules_since: date = RULES_SINCE,
             excluded: Optional[list] = None) -> dict:
    """모든 축 + 귀속 커버리지 + 데이터 품질 경고(순수). 탭 A가 한 번에 받는 형태."""
    odd = outliers(rts)
    try:
        import data_quality
        exc_summary = data_quality.summary(list(excluded or []))
        flagged = data_quality.summary(
            [dict(r, excluded_kind=r.get("flag_kind"),
                  excluded_reason=r.get("flag_reason")) for r in data_quality.flagged(rts)])
    except Exception:  # noqa: BLE001
        exc_summary = flagged = {"n": 0, "pnl": 0, "kinds": [], "rows": []}
    return {
        "excluded": exc_summary,
        "flagged": flagged,
        "axes": {a: compare(rts, a, rules_since=rules_since) for a in AXES},
        "attribution": attribution(rts),
        "outliers": odd,
        "outlier_pnl": round(sum(o["pnl"] for o in odd)),
        "n": len(rts),
    }


def format_overview(ov: dict) -> str:
    """텍스트 요약(순수). 텔레그램·CLI 공용."""
    att = ov["attribution"]
    if not ov["n"]:
        return "📊 전략 비교: 완결된 거래가 아직 없습니다."
    lines = [f"📊 전략별 성과 비교 (완결 {ov['n']}건)"]
    exc = ov.get("excluded") or {}
    if exc.get("n"):
        lines.append(f"🧹 데이터 품질 제외 {exc['n']}건({exc['pnl']:,}원) — 기록은 DB에 남아 있습니다.")
    flg = ov.get("flagged") or {}
    if flg.get("n"):
        lines.append(f"🏷 체결 가정 편향 표시 {flg['n']}건 — 집계에는 포함되어 있습니다.")
    if ov.get("outliers"):
        lines.append(
            f"⚠️ 수익률 이상치 {len(ov['outliers'])}건({ov['outlier_pnl']:,}원)이 "
            f"집계에 섞여 있습니다 — 초기 시험 입력으로 보입니다. 아래 수치는 그대로 포함한 값입니다.")
    if not att["usable"]:
        lines.append(
            f"⚠️ 진입 전략 태그 {att['tagged']}/{att['total']}건 "
            f"({att['coverage']}%) — 진입 조건별 비교는 아직 불가능합니다. "
            f"매수 시 태그를 남겨야 쌓입니다.")
    for axis in ("slot", "reason", "era"):
        c = ov["axes"][axis]
        lines.append("")
        lines.append(f"■ {c['title']}")
        for r in c["rows"]:
            mark = "†" if r["small"] else ""
            edge = "—" if r["edge_ret"] is None else f"{r['edge_ret']:+.2f}%p"
            lines.append(f"• {r['label']}{mark}: {r['n']}건 {r['pnl']:,}원 "
                         f"승률 {r['win_rate']}% · 전체 대비 {edge}")
    lines.append("")
    lines.append(f"† 표본 {ov['axes']['slot']['min_sample']}건 미만 — 참고용, 판정 근거 아님.")
    return "\n".join(lines)


# ─── 경계 (I-O) ──────────────────────────────────────


def load_roundtrips(db_path=None) -> list[dict]:
    """운영 DB → 완결 라운드트립."""
    import paper_db
    import trade_analytics as ta

    kwargs = {"db_path": db_path} if db_path else {}
    kept, _ = ta.roundtrips_for_analysis(paper_db.list_trades(limit=100000, **kwargs))
    return kept


def load_roundtrips_with_exclusions(db_path=None) -> tuple[list[dict], list[dict]]:
    """(집계 대상, 제외분). 화면이 '무엇을 뺐는지'를 말할 수 있어야 한다."""
    import paper_db
    import trade_analytics as ta

    kwargs = {"db_path": db_path} if db_path else {}
    return ta.roundtrips_for_analysis(paper_db.list_trades(limit=100000, **kwargs))


def _cli() -> int:
    ap = argparse.ArgumentParser(description="전략별 성과 비교 (탭 A 데이터)")
    ap.add_argument("--db")
    ap.add_argument("--axis", choices=sorted(AXES), help="한 축만 자세히 보기")
    args = ap.parse_args()

    rts, excluded = load_roundtrips_with_exclusions(args.db)
    if args.axis:
        c = compare(rts, args.axis)
        print(f"■ {c['title']} (전체 {c['total']['n']}건)")
        for r in c["rows"]:
            mark = "†" if r["small"] else ""
            print(f"  {r['label']}{mark}: {r['n']}건 · {r['pnl']:,}원 · "
                  f"승률 {r['win_rate']}% · 평균 {r['avg_ret']}% · "
                  f"PF {r['profit_factor']} · 전체 대비 {r['edge_ret']}%p")
        return 0
    print(format_overview(overview(rts, excluded=excluded)))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
