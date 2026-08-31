"""failure_analysis.py — 실패 원인 분석 (탭 D, 순수 코어)

계획서 3단계 "탭 D: 실패 원인 분석 (FastAPI → Ollama → Gemma 4)".

**이 화면의 실패 모드는 '이야기가 그럴듯한 것'이다.** 63건짜리 표본에서 축을
넷만 잡아도 "어떤 국면에서 크게 잃었다", "어떤 섹터가 문제다" 같은 문장은 반드시
나온다. 그리고 그 문장은 대개 사실이 아니다 — 무작위로 갈라도 같은 크기의 차이가
절반의 확률로 나오기 때문이다. 2026-08-29 마이퀀트 분석에서 실측으로 확인했다.

    무작위 4분할 20,000회 → '가장 좋은 그룹'의 승률 중앙값 50.0%, 평균 +3.90%
    (전체 승률 36.5%, 평균 −0.09%인 표본에서)

그래서 이 모듈은 **원인을 찾아 주지 않는다.** 축별로 갈라 본 뒤, 각 칸이
**라벨을 무작위로 섞었을 때와 구분되는지**를 함께 낸다. 구분되지 않으면
`우연 범위`로 표시하고 그 사실이 화면과 LLM 프롬프트에 그대로 간다.

세 가지를 지킨다.
  1) **품질 규칙을 적용한 표본**을 쓴다. 가격 오류로 강제 청산된 건을 놓고
     "전략이 실패했다"고 진단하면 원인이 통째로 어긋난다.
  2) **모르는 것은 모른다고 낸다.** 국면 라벨이 없는 구간, 섹터 미분류는
     별도로 세어 표시한다.
  3) 숫자는 여기서 만들고, LLM은 **그 숫자만** 문장으로 옮긴다.
"""
from __future__ import annotations

import random
import statistics
from typing import Callable, Iterable, Optional

# 라벨 유의성 판정에 쓰는 무작위 재배치 횟수. 결정적(시드 고정).
TRIALS = 2000
SEED = 20260831

# 이보다 작은 칸은 판정하지 않는다. 3~4건짜리 칸의 평균은 무엇이든 나온다.
MIN_CELL = 5

# 백분위가 이 밖으로 나가야 "우연으로 설명되지 않는다"고 말한다(양측 5%).
LOW, HIGH = 2.5, 97.5


def _rets(rows: Iterable[dict]) -> list[float]:
    return [float(r.get("ret") or 0.0) for r in rows]


def label_cells(rts: list[dict], label_fn: Callable[[dict], str],
                *, trials: int = TRIALS, seed: int = SEED,
                min_cell: int = MIN_CELL) -> list[dict]:
    """축 하나로 갈라 칸별 손익과 **우연 여부**를 함께 낸다(순수).

    판정 방법: 라벨만 무작위로 섞고(칸 크기는 그대로) 각 칸의 평균 수익률을 다시
    잰다. 실제 값이 그 분포의 어디에 있는지가 백분위다. **칸 크기를 보존하는 것이
    핵심이다** — 작은 칸은 원래 평균이 크게 흔들리므로, 크기를 무시하고 비교하면
    작은 칸이 항상 '특별해' 보인다.
    """
    rows = [r for r in rts if r.get("ret") is not None]
    if not rows:
        return []
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(str(label_fn(r) or "미분류"), []).append(r)

    all_rets = _rets(rows)
    rng = random.Random(seed)
    sims: dict[str, list[float]] = {k: [] for k in groups}
    sizes = {k: len(v) for k, v in groups.items()}
    for _ in range(trials):
        pool = all_rets[:]
        rng.shuffle(pool)
        i = 0
        for k, n in sizes.items():
            chunk = pool[i:i + n]
            i += n
            sims[k].append(sum(chunk) / n if chunk else 0.0)

    out = []
    for k, rows_k in groups.items():
        rr = _rets(rows_k)
        mean = sum(rr) / len(rr)
        dist = sorted(sims[k])
        below = sum(1 for x in dist if x < mean)
        ties = sum(1 for x in dist if x == mean)
        pct = (below + ties / 2) / len(dist) * 100 if dist else 50.0
        judged = len(rr) >= min_cell
        out.append({
            "label": k,
            "n": len(rr),
            "pnl": round(sum(float(r.get("pnl") or 0.0) for r in rows_k)),
            "mean_ret": round(mean * 100, 2),
            "win_rate": round(sum(1 for x in rr if x > 0) / len(rr) * 100, 1),
            "percentile": round(pct, 1) if judged else None,
            "verdict": ("표본 부족" if not judged else
                        ("우연 범위" if LOW <= pct <= HIGH else
                         ("유의하게 나쁨" if pct < LOW else "유의하게 좋음"))),
        })
    return sorted(out, key=lambda c: c["pnl"])


# 축의 성격. **모든 축에 같은 검정을 대면 안 된다.**
#
#   "cross"   종목 성질로 가르는 축(섹터·슬롯). 라벨 섞기가 유효한 검정이다.
#   "time"    시간으로 가르는 축(국면·월). **라벨을 섞으면 시간 뭉침이 깨진다.**
#             같은 시기 거래는 같은 시장을 겪으므로, 시간축은 무엇이든 "유의"하게
#             나온다. 이 축의 차이는 대부분 **시장 방향**이지 전략이 아니다.
#             (2026-08-31 진입조건 재심사에서 같은 함정을 밟았다 — 전후반 원수익
#             비교가 시장 하락을 조건의 열화로 읽었다.)
#   "tauto"   정의상 결과가 정해진 축(청산사유). 손절은 정의상 손실, 익절은 이익이다.
#             **발견이 아니라 양성 대조로만 쓴다** — 여기서 유의가 안 나오면
#             검정 자체가 고장 난 것이다.
#   "outcome" 라벨이 결과에 **부분적으로** 딸려 오는 축(보유기간). 청산 규칙이
#             기간을 정한다 — 손절(−7%)은 빨리 끝나고 익절(+20%)은 늦게 끝난다.
#             2026-08-31 실측: 6~20일 칸은 익절 비중 52%(11/21)인데 나머지 두
#             칸은 29%다. "오래 들고 있으면 좋다"가 아니라 "익절까지 간 거래가
#             거기 모여 있다"에 가깝다. 그래서 발견으로 올리지 않는다.
AXIS_KINDS = {"cross", "time", "tauto", "outcome"}

_KIND_NOTE = {
    "time": "시간축 — 같은 시기 거래는 같은 시장을 겪는다. 이 차이는 "
            "대부분 시장 방향이며 전략 성과와 분리되지 않는다",
    "tauto": "순환 정의 — 결과가 라벨을 정한다. 발견이 아니라 검정이 "
             "살아 있는지 보는 양성 대조다",
    "outcome": "결과가 라벨에 딸려 온다 — 청산 규칙이 보유기간을 정한다"
               "(손절은 빨리, 익절은 늦게 끝난다). 칸의 차이는 상당 부분 "
               "청산 구성의 되풀이다",
}


def findings(rts: list[dict], trades: Optional[list] = None,
             *, axes: Optional[dict] = None,
             stop_fn=None, whipsaw_fn=None,
             concentration_fn=None, slippage_fn=None) -> dict:
    """실패 진단 묶음(순수 — 계산 함수는 주입받는다).

    `axes`는 `{이름: (성격, 라벨함수)}`다. 성격이 `cross`인 축만 **발견**으로
    올린다. `time`·`tauto`는 표에는 싣되 signals에서 빼고 이유를 적는다.

    `trade_analytics`의 진단 함수는 주입으로 받는다. 이 모듈을 테스트할 때
    DB도 시세도 필요 없게 하려는 것이다.
    """
    axes = axes or {}
    out: dict = {"n": len(rts), "axes": {}, "axis_kinds": {},
                 "signals": [], "controls": [], "unknowns": []}
    if not rts:
        out["unknowns"].append("완결된 거래가 없어 진단할 것이 없습니다")
        return out

    for name, spec in axes.items():
        kind, fn = spec if isinstance(spec, tuple) else ("cross", spec)
        if kind not in AXIS_KINDS:
            raise ValueError(f"알 수 없는 축 성격: {kind}")
        cells = label_cells(rts, fn)
        out["axes"][name] = cells
        out["axis_kinds"][name] = kind
        if kind in _KIND_NOTE:
            out["unknowns"].append(f"{name}: {_KIND_NOTE[kind]}")
        unknown = sum(c["n"] for c in cells if c["label"] in ("미분류", "미상", "?"))
        if unknown:
            out["unknowns"].append(f"{name}: 라벨 없음 {unknown}건")
        for c in cells:
            if c["verdict"] not in ("유의하게 나쁨", "유의하게 좋음"):
                continue
            row = {"axis": name, "kind": kind, **c}
            (out["signals"] if kind == "cross" else out["controls"]).append(row)

    if stop_fn is not None:
        out["stop"] = stop_fn(rts)
    if slippage_fn is not None:
        # 전체 평균만 보면 **이미 고친 문제를 현재 문제로 읽는다.** 손절 실행
        # 지연은 2026-07-09에 고쳤고, 그 전 거래가 평균을 끌어내리고 있다.
        out["slippage"] = slippage_fn(rts)
    if whipsaw_fn is not None and trades is not None:
        wh = whipsaw_fn(trades, rts)
        out["whipsaw"] = {"n": len(wh), "rows": wh[:10]}
    if concentration_fn is not None:
        out["concentration"] = concentration_fn(rts)
    return out


def format_findings(f: dict) -> str:
    """사람이 읽는 요약(순수). **'우연 범위'를 지우지 않는다.**"""
    if not f.get("n"):
        return "🔎 실패 원인 분석: 완결된 거래가 아직 없습니다."
    lines = [f"🔎 *실패 원인 분석* — 완결 {f['n']}건", ""]

    stop = f.get("stop") or {}
    if stop.get("n"):
        lines.append(
            f"손절 실행: {stop['n']}건 · 평균 {stop['avg_ret']}% "
            f"(손절선 {stop['threshold']}%, 슬리피지 {stop['slippage']}%p) · "
            f"손절선보다 깊게 {stop['deeper_than_stop']}건")
    sc = f.get("slippage") or {}
    b, a = (sc.get("before") or {}), (sc.get("after") or {})
    if b.get("n") and a.get("n"):
        lines.append(
            f"  └ 수정({sc['cutoff']}) 전 {b['n']}건 슬리피지 {b['slippage']}%p "
            f"→ 후 {a['n']}건 {a['slippage']}%p "
            f"(전체 평균은 수정 전 거래가 끌어내린 값이다)")
    wh = f.get("whipsaw") or {}
    if wh:
        lines.append(f"휩쏘(손절 후 재매수): {wh['n']}건")
    lc = f.get("concentration") or {}
    if lc.get("top_share_pct") is not None:
        names = ", ".join(t["name"] for t in lc.get("top", [])[:3])
        lines.append(f"손실 집중: 워스트3({names})이 총손실의 {lc['top_share_pct']}%")

    lines.append("")
    if f["signals"]:
        lines.append("우연으로 설명되지 않는 칸:")
        for s in f["signals"]:
            lines.append(f"• [{s['axis']}] {s['label']} {s['n']}건 "
                         f"{s['pnl']:,}원 평균 {s['mean_ret']:+.2f}% "
                         f"— {s['verdict']}(백분위 {s['percentile']})")
    else:
        lines.append("**우연으로 설명되지 않는 칸이 없습니다.** 축별 차이는 "
                     "라벨을 무작위로 섞었을 때와 구분되지 않습니다 — "
                     "지금 표본으로는 원인을 지목할 수 없다는 뜻입니다.")

    if f.get("controls"):
        lines.append("")
        lines.append("대조용(발견 아님):")
        for s in f["controls"]:
            why = _KIND_NOTE.get(s["kind"], "").split(" — ")[0]
            lines.append(f"• [{s['axis']}] {s['label']} {s['n']}건 "
                         f"{s['pnl']:,}원 — {why}")

    if f["unknowns"]:
        lines.append("")
        lines.append("확인하지 못한 것: " + " · ".join(f["unknowns"]))
    return "\n".join(lines)
