"""phase_factor_eval.py — 국면 판정이 팩터 방향을 맞히는가(탭 C).

**크기가 아니라 부호만 본다.** 2026-09-02 실측: 사이즈 스프레드 199개월을
확보했지만 검정 단위는 에피소드 18개이고, 에피소드 평균의 sd가 3.72%p라
최소 검출 차이가 월 4.91~5.48%p다. 현실적인 국면-팩터 효과(월 0.3~0.5%p)의
10배다. **평균 차이는 이 표본으로 잴 수 없다.**

부호는 다르다. 「이 국면에서 소형이 대형을 이기는가」는 에피소드당 하나의
예/아니오이고, 18개면 이항검정이 선다 — 여전히 얇지만 판정선이 현실적이다.

세 가지를 지킨다:

1. **가설을 내가 쓰지 않는다.** `PHASE_FACTOR_WEIGHT`는 이 측정보다 먼저
   코드에 있었다. 봇이 국면별로 Size를 얼마나 사는지가 곧 봇의 가설이다.
   사후에 방향을 정하면 무엇이든 맞는다.

2. **우연 기대는 50%가 아니다.** 스프레드에 방향성 편향이 있으면 「늘 음수」로
   찍어도 맞는다 — nowcast에서 「늘 오른다」가 63.4%였던 것과 같다. 항상
   다수 부호로 찍는 전략의 적중률을 함께 낸다.

3. **발표 시차를 반영한다.** CLI는 참조월보다 늦게 나온다. 그 달의 국면으로
   그 달의 수익률을 설명하면 실전에서 쓸 수 없는 판정이 된다. 기본은
   `lag=2` — 봇이 실제로 쓰는 값이 그만큼 과거다.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

LAG_DEFAULT = 2          # 개월. CLI 발표 시차 — 봇이 실제로 보는 값의 나이
ALPHA = 0.05             # 단측. 검증 전에 고정한다
MIN_EPISODES = 12        # 이보다 적으면 비율을 내지 않는다


# ─── 순수: 가설 ───────────────────────────────────────

# ─── 검정 가족 (2026-09-02 재기 전에 고정) ──────────────
#
# 411개 지수 목록을 보고 **측정 가능한 짝만** 골랐다. 셋뿐이다.
#
#   · Momentum · Quality — **지수가 없다.** 봇 가중치의 대부분이 여기에
#     걸려 있는데(Expansion Momentum 0.533, Contraction Quality 0.375)
#     검증할 원자료 자체가 KRX 승인분에 없다.
#   · 배당·동일가중 — 지수는 있지만 **봇이 걸지 않는 팩터**다. 가설이
#     없는 것을 재면 방향을 내가 정하게 되고, 그건 사후 맞춤이다.
#   · TR 지수는 TR끼리만 짝짓는다. 가격지수와 비교하면 배당만큼 가짜
#     초과수익이 생긴다.
#
# 셋을 재므로 합격선은 α = 0.05/3. 나중에 하나 더 붙이면 다시 낮아진다.
FAMILY = (
    {"key": "Size(코스피)",
     "long": ("KOSPI 시리즈", "코스피 소형주"),
     "short": ("KOSPI 시리즈", "코스피 대형주"),
     "factors": ("Size",)},
    {"key": "Size(KRX TMI)",          # 같은 팩터를 **다른 지수 계열로 재현**
     "long": ("KRX 시리즈", "KRX 소형 TMI"),
     "short": ("KRX 시리즈", "KRX 중대형 TMI"),
     "factors": ("Size",)},
    {"key": "Value+LowVol",           # 한 지수가 두 팩터를 겸한다
     "long": ("파생상품지수", "코스피 200 가치저변동성"),
     "short": ("KOSPI 시리즈", "코스피 200"),
     "factors": ("Value", "LowVol")},
)


# ─── 더 앞선 질문 (2026-09-02, 재기 전에 고정) ──────────
#
# 팩터 셋이 모두 우연을 못 넘었다. 그런데 그 앞에 물어야 할 것이 있다:
# **국면 판정은 시장 방향이라도 맞히는가?**
#
# 이 가설은 내가 고르지 않았다 — 국면 이름 자체가 방향이다. Recovery와
# Expansion은 경기가 오르는 국면이고 Slowdown·Contraction은 내리는
# 국면이다(CLI level×momentum의 4분면 정의). 팩터 우위보다 훨씬 약한
# 주장이므로, 이것마저 못 넘으면 국면 판정 자체가 비어 있는 것이다.
#
# 우연 기대는 50%가 아니다 — 시장은 대체로 오른다(nowcast 63.4% 교훈).
MARKET_TEST = {
    "key": "시장 방향",
    "long": ("KOSPI 시리즈", "코스피 200"),
    "short": None,                       # 롱 단독 — 시장 그 자체
    "expected": {"Recovery": 1, "Expansion": 1,
                 "Slowdown": -1, "Contraction": -1},
}


def expected_signs(weights: dict, factor="Size", *, tol: float = 1e-9) -> dict:
    """국면별 기대 부호(순수). **봇의 가중치에서 기계적으로 끌어낸다.**

    그 국면의 팩터 비중이 전체 평균보다 크면 그 팩터가 이길 것으로 본
    것이고(+1), 작으면 질 것으로 본 것이다(−1). 같으면 예측 없음(0).

    `tol` 없이 부동소수 비교를 하면 **완전히 평평한 가중치에서도 부호가
    생긴다**(0.1의 평균이 0.1이 아니다). 없는 예측을 만들어내는 셈이다.
    """
    names = (factor,) if isinstance(factor, str) else tuple(factor)
    values = {phase: sum(w.get(f, 0.0) for f in names) / len(names)
              for phase, w in (weights or {}).items()}
    if not values:
        return {}
    mean = sum(values.values()) / len(values)
    out = {}
    for phase, value in values.items():
        if abs(value - mean) <= tol:
            out[phase] = 0
        else:
            out[phase] = 1 if value > mean else -1
    return out


def hypothesis_note(weights: dict, factor) -> str:
    """그날 쓴 가중치를 문장으로 남긴다(순수).

    가중치는 wiki에서 오고 사람이 고칠 수 있다. 바뀐 줄 모르고 옛 가설로
    판정하면 사후 맞춤과 구분되지 않는다 — **쓴 값을 그대로 적어둔다.**
    """
    names = (factor,) if isinstance(factor, str) else tuple(factor)
    items = []
    for phase, w in sorted((weights or {}).items()):
        value = sum(w.get(f, 0.0) for f in names) / len(names)
        items.append(f"{phase} {value:.3f}")
    return f"{'+'.join(names)} 가중: " + " · ".join(items)


# ─── 순수: 국면 이력 ──────────────────────────────────

def phase_by_month(history: list, key: str = "consensus") -> dict:
    """{'YYYY-MM': 국면}(순수)."""
    out = {}
    for row in history or []:
        month = str(row.get("month", ""))[:7]
        if month and row.get(key):
            out[month] = row[key]
    return out


def shift(phases: dict, lag: int) -> dict:
    """국면을 lag개월 **뒤로** 민다(순수).

    M월 국면은 M+lag월에야 알 수 있다. 미루지 않으면 아직 발표되지 않은
    값으로 그 달을 설명하게 된다 — 소급 재구성이 미래를 안 보는 것과
    발표 시차를 반영하는 것은 **다른 문제**다.
    """
    out = {}
    for month, phase in phases.items():
        y, m = int(month[:4]), int(month[5:7])
        m += lag
        y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
        out[f"{y:04d}-{m:02d}"] = phase
    return out


def episodes(phases: dict, *, start: Optional[str] = None) -> list:
    """연속한 같은 국면을 한 덩어리로(순수). [(국면, 시작, 끝), ...]"""
    out, prev = [], None
    for month in sorted(phases):
        if start and month < start:
            continue
        phase = phases[month]
        if phase != prev:
            out.append([phase, month, month])
            prev = phase
        else:
            out[-1][2] = month
    return [tuple(e) for e in out]


# ─── 순수: 판정 ───────────────────────────────────────

def episode_rows(values_by_month: dict, eps: list, *, min_months: int = 1) -> list:
    """에피소드마다 평균과 부호(순수). 값이 없는 에피소드는 빠진다."""
    rows = []
    for phase, first, last in eps:
        xs = [v for m, v in values_by_month.items() if first <= m <= last and v is not None]
        if len(xs) < min_months:
            continue
        mean = sum(xs) / len(xs)
        rows.append({"phase": phase, "start": first, "end": last,
                     "months": len(xs), "mean": mean,
                     "sign": 1 if mean > 0 else (-1 if mean < 0 else 0)})
    return rows


def score(rows: list, expected: dict) -> dict:
    """예측 부호와 실제 부호의 일치(순수). 예측 없음(0)과 무승부는 뺀다."""
    used, hit = 0, 0
    for row in rows:
        want = expected.get(row["phase"], 0)
        if want == 0 or row["sign"] == 0:
            continue
        used += 1
        hit += 1 if want == row["sign"] else 0
    return {"n": used, "hits": hit, "rate": (hit / used) if used else None}


def constant_baseline(rows: list) -> dict:
    """**항상 같은 부호로 찍는** 전략의 적중률(순수) — 진짜 우연 기대.

    스프레드가 대체로 음수인 시장에서 「늘 음수」는 그냥 맞는다.
    이것을 넘지 못하면 국면은 아무것도 더해주지 않은 것이다.
    """
    signs = [r["sign"] for r in rows if r["sign"] != 0]
    if not signs:
        return {"n": 0, "rate": None, "sign": 0}
    plus = sum(1 for s in signs if s > 0)
    best = max(plus, len(signs) - plus)
    return {"n": len(signs), "rate": best / len(signs),
            "sign": 1 if plus >= len(signs) - plus else -1}


def _comb(n: int, k: int) -> int:
    from math import comb
    return comb(n, k)


def binom_p_ge(hits: int, n: int, p: float) -> Optional[float]:
    """P(X >= hits) (순수, 단측). n=0이면 None."""
    if n <= 0 or not 0.0 < p < 1.0:
        return None
    return sum(_comb(n, k) * p ** k * (1 - p) ** (n - k) for k in range(hits, n + 1))


def required_hits(n: int, p: float, alpha: float = ALPHA) -> Optional[int]:
    """합격선(순수) — **결과를 보기 전에** 계산한다."""
    if n <= 0 or not 0.0 < p < 1.0:
        return None
    for k in range(0, n + 1):
        value = binom_p_ge(k, n, p)
        if value is not None and value <= alpha:
            return k
    return None


def bar(rows: list, *, alpha: float = ALPHA) -> dict:
    """넘어야 할 선(순수). 우연 기대와 0.5 중 **큰 쪽**을 기준으로."""
    base = constant_baseline(rows)
    n = base["n"]
    p0 = max(0.5, base["rate"] or 0.5)
    # **모든 에피소드의 부호가 같으면 아무것도 우연을 못 이긴다.**
    # 「늘 같은 쪽」이 100%이므로 이 표본으로는 국면의 기여를 가릴 수 없다.
    degenerate = p0 >= 1.0
    need = None if degenerate else required_hits(n, p0, alpha)
    return {"n": n, "p0": p0, "need": need, "degenerate": degenerate,
            "need_rate": (need / n) if (need is not None and n) else None,
            "baseline_sign": base["sign"]}


def verdict(rows: list, expected: dict, *, alpha: float = ALPHA,
            min_episodes: int = MIN_EPISODES) -> dict:
    got = score(rows, expected)
    line = bar(rows, alpha=alpha)
    if got["n"] < min_episodes:
        return {**got, **{"bar": line}, "verdict": "표본 부족",
                "p": None, "passed": None}
    if line.get("degenerate"):
        return {**got, "bar": line, "p": None, "passed": None,
                "verdict": "판정 불가(모든 에피소드가 같은 부호)"}
    p = binom_p_ge(got["hits"], got["n"], line["p0"])
    passed = bool(line["need"] is not None and got["hits"] >= line["need"])
    return {**got, "bar": line, "p": p, "passed": passed,
            "verdict": "우위 확인" if passed else "우위 확인 불가"}


def family_alpha(k: int, alpha: float = ALPHA) -> float:
    """검정을 k개 하면 선을 k로 나눈다(순수, Bonferroni).

    **팩터를 넷 재고 그중 하나가 유의하면 그것은 발견이 아니다.** α=0.05로
    넷을 재면 하나라도 걸릴 확률이 18.5%다. 탭 D에서 축마다 같은 검정을
    대면 안 된다고 적어둔 것과 같은 문제다.

    보수적이다(팩터 스프레드끼리 상관이 있어 실제 위험은 이보다 작다).
    그래도 느슨한 쪽으로 틀리지 않는 편을 고른다.
    """
    return alpha / max(1, int(k))


def family_verdict(results: dict, *, alpha: float = ALPHA) -> dict:
    """여러 팩터를 한꺼번에 판정(순수). **검정 수를 미리 세어 선을 낮춘다.**"""
    k = len(results)
    adj = family_alpha(k, alpha)
    out = {}
    for name, r in results.items():
        n, hits = r.get("n", 0), r.get("hits", 0)
        p0 = (r.get("bar") or {}).get("p0", 0.5)
        need = required_hits(n, p0, adj)
        passed = bool(need is not None and hits >= need and r.get("p") is not None)
        out[name] = {**r, "family_need": need, "family_alpha": adj,
                     "family_passed": passed,
                     "family_verdict": "우위 확인" if passed else "우위 확인 불가"}
    return {"k": k, "alpha": adj, "results": out}


def format_family(family: dict) -> str:
    """가족 단위 보고(순수)."""
    lines = [f"🧪 팩터 {family['k']}종 동시 검정", ""]
    lines.append(f"  검정을 {family['k']}개 하므로 합격선을 낮춘다: "
                 f"α = {ALPHA} / {family['k']} = {family['alpha']:.4f}")
    lines.append("  (넷을 α=0.05로 재면 하나라도 걸릴 확률이 18.5%다)")
    lines.append("")
    for name, r in family["results"].items():
        if r.get("verdict") == "표본 부족" or r.get("rate") is None:
            lines.append(f"  · {name}: 표본 부족(에피소드 {r.get('n', 0)}개)")
            continue
        if (r.get("bar") or {}).get("degenerate"):
            lines.append(f"  · {name}: {r['hits']}/{r['n']} — "
                         f"**판정 불가**(모든 에피소드가 같은 부호, 우연 기대 100%)")
            continue
        need = r.get("family_need")
        need_s = f"{need}/{r['n']}" if need is not None else "도달 불가"
        p_s = f"{r['p']:.3f}" if r.get("p") is not None else "N/A"
        lines.append(f"  · {name}: {r['hits']}/{r['n']} = {r['rate'] * 100:.1f}% "
                     f"· 우연 {r['bar']['p0'] * 100:.1f}% · 선 {need_s} "
                     f"· p={p_s} → **{r['family_verdict']}**")
    lines.append("")
    lines.append("_검정 목록은 재기 전에 고정했습니다. 나중에 하나 더 붙이면 선이 다시 낮아집니다._")
    return chr(10).join(lines)


def format_verdict(result: dict, expected: dict, rows: list, *,
                   label: str = "", lag: int = LAG_DEFAULT,
                   words: tuple = ("소형 우위", "대형 우위"),
                   subject: str = "팩터 방향") -> str:
    lines = [f"🎯 국면 → 팩터 방향 검정{(' — ' + label) if label else ''}", ""]
    lines.append(f"  발표 시차 {lag}개월 반영 · 에피소드 {len(rows)}개")
    lines.append("")
    lines.append("  사전 등록된 예측(봇의 PHASE_FACTOR_WEIGHT에서 기계적으로 유도):")
    for phase, want in sorted(expected.items()):
        word = {1: words[0], -1: words[1], 0: "예측 없음"}[want]
        lines.append(f"   · {phase}: {word}")
    lines.append("")
    for row in rows:
        want = expected.get(row["phase"], 0)
        mark = "—" if want == 0 or row["sign"] == 0 else ("○" if want == row["sign"] else "✗")
        lines.append(f"   {mark} {row['phase']:12s} {row['start']}~{row['end']} "
                     f"({row['months']:2d}개월) 평균 {row['mean'] * 100:+6.2f}%p")
    line = result.get("bar", {})
    lines.append("")
    if result.get("rate") is None:
        lines.append("  판정할 에피소드가 없습니다.")
        return chr(10).join(lines)
    lines.append(f"  적중 {result['hits']}/{result['n']} = {result['rate'] * 100:.1f}%")
    base_sign = {1: f"늘 「{words[0]}」", -1: f"늘 「{words[1]}」",
                 0: "-"}[line.get("baseline_sign", 0)]
    lines.append(f"  우연 기대: {line['p0'] * 100:.1f}% ({base_sign} 한쪽으로만 찍었을 때)")
    if line.get("need") is not None:
        lines.append(f"  합격선(α={ALPHA}, 단측, 검증 전 고정): "
                     f"{line['need']}/{line['n']} = {line['need_rate'] * 100:.1f}%")
    if result.get("p") is not None:
        lines.append(f"  p = {result['p']:.3f}")
    elif (result.get("bar") or {}).get("degenerate"):
        lines.append("  모든 에피소드의 부호가 같습니다 — 「늘 같은 쪽」이 100%이므로")
        lines.append("  국면이 무엇을 더했는지 이 표본으로는 가릴 수 없습니다.")
    lines.append("")
    lines.append(f"  판정: **{result['verdict']}**")
    if result.get("verdict") == "우위 확인 불가":
        lines.append(f"  국면이 {subject}을 예측한다는 근거가 이 표본에서는 없습니다.")
        lines.append("  (없다는 증명이 아닙니다 — 이 크기로는 가릴 수 없다는 뜻입니다.)")
    return chr(10).join(lines)


# ─── 판정 원장 ───────────────────────────────────────
#
# 화면과 텔레그램이 매번 다시 계산하지 않게, 잰 결과를 **추가만 되는**
# 원장에 남긴다. 임계값 원장과 같은 규칙이다 — 지난 판정을 고치지 않는다.

VALIDATION_FILE = "phase_validation.json"


def validation_record(family: dict, market: Optional[dict], *,
                      at: str, note: str = "") -> dict:
    """원장에 남길 한 건(순수)."""
    tests = {}
    for key, r in (family or {}).get("results", {}).items():
        tests[key] = {"n": r.get("n", 0), "hits": r.get("hits", 0),
                      "p0": (r.get("bar") or {}).get("p0"),
                      "need": r.get("family_need"), "p": r.get("p"),
                      "verdict": r.get("family_verdict") or r.get("verdict")}
    if market:
        tests["시장 방향"] = {"n": market.get("n", 0), "hits": market.get("hits", 0),
                           "p0": (market.get("bar") or {}).get("p0"),
                           "need": (market.get("bar") or {}).get("need"),
                           "p": market.get("p"), "verdict": market.get("verdict")}
    passed = [k for k, t in tests.items() if t.get("verdict") == "우위 확인"]
    return {"at": at, "alpha": (family or {}).get("alpha"), "tests": tests,
            "any_passed": bool(passed), "passed": passed, "note": note}


def validation_line(record: Optional[dict]) -> str:
    """화면에 한 줄로(순수). **없으면 '미측정'이라고 말한다.**"""
    if not record:
        return "⚠️ 국면 판정 검증 기록이 없습니다 — 미측정 상태입니다."
    tests = record.get("tests") or {}
    if record.get("any_passed"):
        names = ", ".join(record["passed"])
        return f"✅ 국면 판정 일부 검증됨({names}) · {record.get('at', '')[:10]}"
    best = ""
    for key, t in tests.items():
        if t.get("n"):
            best = f"{key} {t['hits']}/{t['n']}"
            break
    return (f"⚠️ **미검증** — 국면이 팩터·시장 방향을 예측한다는 근거가 아직 없습니다"
            f"(검정 {len(tests)}종 전부 우연 기대 미달, 예: {best}"
            f" · {record.get('at', '')[:10]} 측정)")


def append_validation(record: dict, path) -> list:
    """추가만 한다. 지난 판정을 고치지 않는다."""
    import json as _json
    path = Path(path)
    try:
        log = _json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(log, list):
            log = []
    except (OSError, ValueError):
        log = []
    log.append(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(_json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return log


def latest_validation(path) -> Optional[dict]:
    """마지막 판정. 없으면 None."""
    import json as _json
    try:
        log = _json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return log[-1] if isinstance(log, list) and log else None


def run_one(spec: dict, load_pair, phases: dict, weights: dict, *,
            start: str = "2010-01") -> tuple[dict, list, dict]:
    """한 짝을 잰다. `load_pair(service, name) -> {월: 종가}`."""
    import factor_series as fs
    long_series = load_pair(*spec["long"])
    short_series = load_pair(*spec["short"])
    if not long_series or not short_series:
        return ({"n": 0, "hits": 0, "rate": None, "verdict": "표본 부족",
                 "bar": {"n": 0, "p0": 0.5, "need": None}, "p": None,
                 "missing": [name for (svc, name), got in
                             ((spec["long"], long_series), (spec["short"], short_series))
                             if not got]}, [], {})
    sp = fs.spread(fs.monthly_returns(long_series), fs.monthly_returns(short_series))
    rows = episode_rows(sp, episodes(phases, start=start))
    expected = expected_signs(weights, spec["factors"])
    return verdict(rows, expected), rows, expected


def _cli() -> int:
    import argparse
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import factor_series as fs
    import quant_bot as qb

    ap = argparse.ArgumentParser(description="국면 → 팩터 방향 검정(탭 C)")
    ap.add_argument("--long", default="코스피 소형주")
    ap.add_argument("--short", default="코스피 대형주")
    ap.add_argument("--service", default="KOSPI 시리즈")
    ap.add_argument("--factor", default="Size")
    ap.add_argument("--lag", type=int, default=LAG_DEFAULT)
    ap.add_argument("--from", dest="start", default="2010-01")
    ap.add_argument("--key", default="consensus")
    ap.add_argument("--long-service", dest="long_service", default=None)
    ap.add_argument("--short-service", dest="short_service", default=None)
    ap.add_argument("--record", action="store_true",
                    help="--family와 함께: 판정을 원장에 남겨 화면이 읽게 한다")
    ap.add_argument("--market", action="store_true",
                    help="국면이 시장 방향이라도 맞히는가(팩터보다 앞선 질문)")
    ap.add_argument("--family", action="store_true",
                    help="사전 고정된 검정 가족 전체를 재고 합격선을 나눈다")
    args = ap.parse_args()

    root = Path(__file__).resolve().parents[1] / "data" / "cache"

    def load_pair(service: str, name: str) -> dict:
        return fs.load_cache(fs.cache_path(service, root)).get(name, {})

    if args.market:
        hist_path = (Path(__file__).resolve().parents[1] / "data" / "private" /
                     "state" / "phase_history.json")
        history = json.loads(hist_path.read_text(encoding="utf-8"))
        phases = shift(phase_by_month(history, args.key), args.lag)
        series = load_pair(*MARKET_TEST["long"])
        if not series:
            print(f"❌ 캐시에 「{MARKET_TEST['long'][1]}」이 없습니다.")
            return 1
        rets = fs.monthly_returns(series)
        rows = episode_rows(rets, episodes(phases, start=args.start))
        expected = MARKET_TEST["expected"]
        got = verdict(rows, expected)
        print(format_verdict(got, expected, rows,
                             label=f"{MARKET_TEST['long'][1]} 단독(시장 방향)",
                             lag=args.lag, words=("상승", "하락"),
                             subject="시장 방향"))
        print()
        print("  이 가설은 국면 **이름 자체**에서 온다(CLI level×momentum 4분면).")
        print("  팩터 우위보다 약한 주장이다 — 이것마저 못 넘으면 국면 판정이 비어 있다.")
        return 0

    if args.family:
        hist_path = (Path(__file__).resolve().parents[1] / "data" / "private" /
                     "state" / "phase_history.json")
        history = json.loads(hist_path.read_text(encoding="utf-8"))
        phases = shift(phase_by_month(history, args.key), args.lag)
        weights = qb.parse_phase_weights_from_wiki()
        results, details = {}, {}
        for spec in FAMILY:
            got, rows, expected = run_one(spec, load_pair, phases, weights,
                                          start=args.start)
            results[spec["key"]] = got
            details[spec["key"]] = (rows, expected, spec)
        fam = family_verdict(results)
        print(format_family(fam))
        if args.record:
            from datetime import datetime
            market_series = load_pair(*MARKET_TEST["long"])
            market = None
            if market_series:
                mrows = episode_rows(fs.monthly_returns(market_series),
                                     episodes(phases, start=args.start))
                market = verdict(mrows, MARKET_TEST["expected"])
            rec = validation_record(
                fam, market,
                at=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                note=f"lag={args.lag} · start={args.start} · key={args.key}")
            ledger = (Path(__file__).resolve().parents[1] / "data" / "private" /
                      "state" / VALIDATION_FILE)
            append_validation(rec, ledger)
            print()
            print(f"  원장에 기록했습니다 — {ledger.name}")
            print(f"  화면 문구: {validation_line(rec)}")
        print()
        for key, (rows, expected, spec) in details.items():
            miss = results[key].get("missing")
            if miss:
                print(f"  {key}: 캐시에 없는 지수 {miss} — factor_series.py로 먼저 받으세요")
                continue
            print(f"  [{key}] 에피소드 {len(rows)}개 · "
                  f"{hypothesis_note(weights, spec['factors'])}")
        return 0
    long_series = load_pair(args.long_service or args.service, args.long)
    short_series = load_pair(args.short_service or args.service, args.short)
    for name, got in ((args.long, long_series), (args.short, short_series)):
        if not got:
            print(f"❌ 캐시에 「{name}」이 없습니다. factor_series.py로 먼저 받으세요.")
            return 1
    sp = fs.spread(fs.monthly_returns(long_series), fs.monthly_returns(short_series))

    hist_path = Path(__file__).resolve().parents[1] / "data" / "private" / "state" / "phase_history.json"
    history = json.loads(hist_path.read_text(encoding="utf-8"))
    phases = shift(phase_by_month(history, args.key), args.lag)
    eps = episodes(phases, start=args.start)
    rows = episode_rows(sp, eps)
    weights = qb.parse_phase_weights_from_wiki()
    source = ("wiki" if weights != qb._normalize_phase_weights(
        qb.PHASE_FACTOR_WEIGHTS_FALLBACK) else "코드 fallback")
    expected = expected_signs(weights, args.factor)
    result = verdict(rows, expected)
    print(format_verdict(result, expected, rows,
                         label=f"{args.long} − {args.short} ({args.factor})",
                         lag=args.lag))
    print()
    print(f"  가설 출처: {source} · {hypothesis_note(weights, args.factor)}")
    print(f"  스프레드 {len(sp)}개월 · 국면 키 {args.key} · 시작 {args.start}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
