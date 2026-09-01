"""nowcast_eval.py — 지표가 다음 날 시장 방향을 맞히는가 (순수 코어)

**왜 이 파일이 생겼나.** 나우캐스팅 가중치를 정하려면 "각 지표가 국면을 앞서
맞히는가"를 재야 하는데, 2026-08-31 점검 결과 **정답을 월 단위 국면으로 두는
한 필요 표본이 23년**이었다(적중률 60% 판정, 지표 4종, 본페로니 보정).

    적중률 60% 판정에 필요한 관측 276건
      월 단위 → 23.0년 · 주 단위 → 5.3년 · **일 단위 → 1.1년**

그래서 정답을 **다음 거래일 코스피 방향**으로 바꾼다(2026-08-31 결정). 일 단위는
표본이 빨리 쌓이고, 무엇보다 **정답 시계열을 지금 만들 수 있다** — 코스피 종가
캐시가 이미 있다.

━━━ 이 도구가 조심하는 것 ━━━

**① 미래를 보지 않는다.** t일 지표는 t일까지의 데이터로만 계산하고, t→t+1
방향을 맞히는지 본다. 하루라도 밀리면 적중률이 가짜로 올라간다.

**② 무작위와 대조한다.** 방향 예측은 원래 50% 근처다. 지표가 55%를 냈다고
"맞힌다"고 하면 안 된다 — 같은 예측 빈도로 **무작위로 찍었을 때**의 분포와
대조해야 한다. 이 프로젝트에서 순열검정이 검사 자체의 결함을 세 번 잡았다.

**③ 상승 편향을 대조군에 넣는다.** 시장이 오른 날이 많으면 "항상 상승"이라고
찍는 것만으로 적중률이 50%를 넘는다. 그래서 `항상 상승`·`항상 하락`을 음성
대조로 함께 낸다 — 지표가 그 둘을 못 이기면 아무것도 아니다.

**④ 판정하지 않는 것을 판정으로 세지 않는다.** 지표가 `unknown`·`neutral`인
날은 예측을 안 한 것이다. 그런 날을 맞힌 것으로 세면 적중률이 부풀려진다.
"""
from __future__ import annotations

import math
import random
from statistics import NormalDist
from typing import Iterable

TRIALS = 2000
SEED = 20260831

UP, DOWN, NONE = "up", "down", None


# ─── 정답 시계열 ─────────────────────────────────────


def direction_series(dates: list[str], closes: list[float]) -> list[tuple]:
    """[(예측기준일, 다음날 방향)] (순수).

    t일 종가와 t+1일 종가를 비교한다. **t일에 알 수 있는 정보로 t+1을 맞히는가**를
    보려는 것이므로, 정답은 t일에 붙이되 값은 t+1에서 온다.
    보합(변화 0)은 방향이 없으므로 제외한다 — 억지로 한쪽에 넣으면 편향이 생긴다.
    """
    out = []
    for i in range(len(closes) - 1):
        a, b = closes[i], closes[i + 1]
        if not a or not b:
            continue
        if b > a:
            out.append((str(dates[i]), UP))
        elif b < a:
            out.append((str(dates[i]), DOWN))
    return out


# ─── 예측 평가 ───────────────────────────────────────


def score(predictions: Iterable[tuple], truth: dict) -> dict:
    """예측 [(day, 'up'|'down'|None)] vs 정답 {day: 방향} (순수).

    반환: {n, hits, rate, skipped, covered}
      n        **실제로 예측한** 날 수(판정 안 한 날은 빼고 센다)
      skipped  지표가 방향을 내지 않은 날
      covered  정답이 있는 날 중 예측한 비율
    """
    n = hits = skipped = 0
    available = 0
    for day, pred in predictions or []:
        want = truth.get(str(day))
        if want is None:
            continue
        available += 1
        if pred not in (UP, DOWN):
            skipped += 1
            continue
        n += 1
        if pred == want:
            hits += 1
    return {"n": n, "hits": hits, "skipped": skipped,
            "rate": round(hits / n * 100, 1) if n else None,
            "covered": round(n / available * 100, 1) if available else None}


def always(direction: str, days: Iterable[str]) -> list[tuple]:
    """음성 대조 — 늘 한 방향으로 찍는다(순수)."""
    return [(str(d), direction) for d in days]


def permuted(predictions: list[tuple], *, trials: int = TRIALS,
             seed: int = SEED) -> list[list[tuple]]:
    """예측 **빈도와 방향 구성은 그대로 두고 날짜만 섞은** 대조군(순수).

    상승 예측 개수를 보존하는 것이 핵심이다 — 안 그러면 상승장에서 '상승을 많이
    찍는 지표'가 유리해진 것을 실력으로 오독한다.
    """
    rng = random.Random(seed)
    days = [d for d, _ in predictions]
    labels = [p for _, p in predictions]
    out = []
    for _ in range(trials):
        shuffled = labels[:]
        rng.shuffle(shuffled)
        out.append(list(zip(days, shuffled)))
    return out


def is_constant(predictions: Iterable[tuple]) -> bool:
    """늘 같은 방향만 찍는가(순수).

    **순열검정으로는 상수 예측을 잡을 수 없다.** 라벨 구성을 보존한 채 날짜만
    섞으므로, 라벨이 전부 같으면 섞어도 결과가 그대로다 → 백분위가 항상 50이
    나온다. 2026-08-31 실측에서 200일선 기울기가 평가 구간 100일 내내
    `risk_on`이라 '항상 상승'과 **완전히 같은 예측**이었는데, 순열검정은
    "우연 범위"라고만 했다. 잡아낸 것은 `항상 상승` 음성 대조였다.
    """
    labels = {p for _, p in (predictions or []) if p in (UP, DOWN)}
    return len(labels) == 1


def evaluate(predictions: list[tuple], truth: dict, *,
             trials: int = TRIALS, seed: int = SEED) -> dict:
    """적중률 + 무작위 대조 백분위(순수).

    반환: {actual, baseline_mean, percentile, verdict, constant, ...}
    """
    actual = score(predictions, truth)
    constant = is_constant(predictions)
    if not actual["n"]:
        return {**actual, "percentile": None, "verdict": "예측 없음",
                "baseline_mean": None, "constant": constant}
    if constant:
        # 상수 예측은 시장 편향을 그대로 되풀이할 뿐이다 — 순열검정에 태우지 않는다.
        return {**actual, "percentile": None, "baseline_mean": None,
                "constant": True,
                "verdict": "상수 예측 — 방향 정보 없음"}
    sims = [score(p, truth)["rate"] for p in permuted(predictions, trials=trials,
                                                      seed=seed)]
    sims = [s for s in sims if s is not None]
    if not sims:
        return {**actual, "percentile": None, "verdict": "판정 불가",
                "baseline_mean": None}
    below = sum(1 for s in sims if s < actual["rate"])
    ties = sum(1 for s in sims if s == actual["rate"])
    pct = (below + ties / 2) / len(sims) * 100
    ctrl = control_rate(predictions, truth)
    beats_control = ctrl is None or actual["rate"] > ctrl
    if not beats_control:
        # **순열검정을 통과해도 여기서 떨어질 수 있다.** 순열은 "이 지표의
        # 예측 구성으로 무작위로 찍었을 때"와 비교할 뿐, 시장 상승 편향을
        # 이기는지는 묻지 않는다. 2026-08-31에 200일선 기울기가 이 틈으로
        # 빠져나갈 뻔했다.
        verdict = "음성 대조 미달"
    elif pct < 95:
        verdict = "우연 범위"
    else:
        verdict = VERDICT_FINDING
    return {
        **actual, "constant": False,
        "baseline_mean": round(sum(sims) / len(sims), 1),
        "percentile": round(pct, 1),
        "control_rate": ctrl,
        "verdict": verdict,
    }


VERDICT_FINDING = "기준선 초과"


def is_finding(result: dict) -> bool:
    """이 결과를 '발견'으로 다뤄도 되는가(순수).

    **문자열을 밖에서 비교하지 않게 한다.** 판정 종류가 늘 때마다(v3.64에서
    「음성 대조 미달」이 늘었다) 호출부가 조용히 틀리기 때문이다.
    """
    return (result or {}).get("verdict") == VERDICT_FINDING


def control_rate(predictions: Iterable[tuple], truth: dict):
    """**그 지표가 실제로 예측한 날 위에서** '항상 상승'의 적중률(순수).

    전체 기간의 상승 비율과 비교하면 안 된다 — 지표가 예측한 날이 전체와 다른
    성격일 수 있고(예: 변동성 높은 날만), 그러면 비교 자체가 어긋난다.
    이 프로젝트에서 '기간이 다른 두 수치를 나란히 놓는' 실수가 이미 있었다
    (2026-08-31 전후반 원수익 비교).
    """
    days = [d for d, p in (predictions or []) if p in (UP, DOWN)]
    ctrl = score(always(UP, days), truth)
    return ctrl["rate"]


# ─── 표본 계산 ───────────────────────────────────────


def required_n(rate: float, base: float = 0.5, *, alpha: float = 0.05,
               k: int = 1, power: float = 0.8) -> int:
    """`rate`를 `base`와 가르려면 몇 건이 필요한가(순수).

    **`base`의 기본값 0.5는 '무작위 동전'이지 '이겨야 할 상대'가 아니다.**
    방향 예측에서 상대는 '항상 상승'이고 그 값은 표본마다 다르다.
    """
    h = 2 * math.asin(math.sqrt(rate)) - 2 * math.asin(math.sqrt(base))
    if h == 0:
        return 0
    za = NormalDist().inv_cdf(1 - alpha / (2 * k))
    zb = NormalDist().inv_cdf(power)
    return math.ceil(((za + zb) / h) ** 2)


# 동시에 **검정하는** 지표 수(본페로니 보정용).
#
# **지표를 늘리면 필요 표본도 는다** — 여러 개를 동시에 보면 그중 하나가 우연히
# 잘 나올 확률이 커지기 때문이다. 지표 추가는 공짜가 아니다.
#
# 세는 것은 "수집하는 지표"가 아니라 **"가설을 검정하는 지표"**다.
#   +1 VKOSPI (2026-09-01 추가)
#   −1 원/달러 (2026-09-01 제외 — 아래)
# 원/달러는 답이 나왔다. 매매기준율은 **주가의 후행 기록**이다:
# 정렬 후 같은 날 상관 **−0.378**, 다음 날 **+0.029**(표본 361일). 같은 날
# 관계는 뚜렷하지만 다음 날에는 없다 — 후행 계열은 구조적으로 예측할 수 없다.
# 이미 답이 난 질문을 계속 검정하면 다른 지표의 표본만 축낸다. **수집은 계속한다.**
N_INDICATORS = 4


def base_rate(truth: dict) -> Optional[float]:
    """이 표본에서 '항상 상승'의 적중률(순수) — **진짜 이겨야 할 기준선.**"""
    vals = [v for v in (truth or {}).values() if v in (UP, DOWN)]
    if not vals:
        return None
    return sum(1 for v in vals if v == UP) / len(vals)


def progress(n: int, rate: float = 0.60, k: int = N_INDICATORS,
             base: float = 0.5) -> dict:
    """지금 표본이 목표의 몇 %인가(순수). **아직 멀었다는 것을 숨기지 않는다.**

    **`base`를 0.5로 두면 안 되는 경우가 대부분이다.** 방향 예측에서 이겨야 할
    상대는 동전이 아니라 '항상 상승'이고, 그 값은 표본의 상승일 비율이다.
    2026-09-01 실측에서 이 비율이 **63.4%**였다 — 즉 목표로 잡아둔 60%는
    **기준선 미달**이었고, 진행률 표시는 없는 목표를 향해 51.4%를 가리키고
    있었다. 무작위 대조(50%)와 기준선 대조(63.4%)는 다른 것이다.
    """
    need = required_n(rate, base, k=k)
    return {"have": n, "need": need, "base": base, "target": rate,
            "pct": round(n / need * 100, 1) if need else None,
            "trading_days_left": max(0, need - n),
            "years_left": round(max(0, need - n) / 252, 1)}


def bar_table(base: float, *, margins=(0.05, 0.10, 0.15),
              k: int = N_INDICATORS) -> list[dict]:
    """기준선을 몇 %p 이기려면 표본이 얼마나 필요한가(순수).

    목표 적중률을 고정하지 않는다 — 기준선이 표본마다 다르므로 **차이(%p)로
    말해야 뜻이 유지된다.**
    """
    out = []
    for m in margins:
        target = min(base + m, 0.999)
        need = required_n(target, base, k=k)
        out.append({"margin": m, "target": target, "need": need,
                    "years": round(need / 252, 1)})
    return out


def readiness(truth: dict, n: int, *, margin: float = 0.10,
              k: int = N_INDICATORS) -> dict:
    """판정할 수 있는 상태인가 — **관문이 둘이다**(순수).

    ① 표본 수   기준선을 `margin`만큼 이기는 것을 가릴 만큼 모였는가
    ② 국면      표본에 상승·하락이 둘 다 들어 있는가

    ②가 막혀 있으면 ①을 채워도 소용없다. 2026-09-01 실측에서 이 구분이
    없어 "142/276건 (51.4%)"가 **곧 될 것처럼** 보였다 — 실제로는 시장이
    꺾이기 전까지 아무것도 판정할 수 없는 상태였다.
    """
    b = base_rate(truth)
    if b is None:
        return {"ok": False, "reason": "정답 표본 없음"}
    need = required_n(min(b + margin, 0.999), b, k=k)
    warn = regime_warning(truth)
    return {"ok": (n >= need) and (warn is None),
            "have": n, "need": need, "base": b,
            "sample_ok": n >= need,
            "regime_ok": warn is None,
            "blocker": ("국면" if warn else ("표본" if n < need else None)),
            "reason": warn or ""}


def regime_warning(truth: dict, *, extreme: float = 0.60) -> Optional[str]:
    """표본이 한 국면에 쏠려 있는가(순수).

    **표본 수만 채우면 되는 게 아니다.** 상승일이 63%인 구간에서는 어떤
    지표도 '항상 상승'을 이기기 어렵고, 이기더라도 그 지표가 하락 국면에서
    작동한다는 근거가 되지 않는다. 표본에 **국면 전환이 들어와야** 한다.
    """
    b = base_rate(truth)
    if b is None:
        return None
    if b >= extreme:
        return (f"이 표본은 상승일이 {b*100:.1f}%다 — 한 국면에 쏠려 있다. "
                "표본 수를 채워도 하락 국면이 들어오기 전에는 판정할 수 없다.")
    if b <= 1 - extreme:
        return (f"이 표본은 하락일이 {(1-b)*100:.1f}%다 — 한 국면에 쏠려 있다. "
                "표본 수를 채워도 상승 국면이 들어오기 전에는 판정할 수 없다.")
    return None


# ─── 표시 ────────────────────────────────────────────


def format_report(results: dict, truth: dict, *, target: str = "다음 거래일 코스피 방향") -> str:
    """지표별 결과(순수). **음성 대조를 먼저 보여준다** — 기준을 모르면 숫자가 커 보인다."""
    up = sum(1 for v in truth.values() if v == UP)
    total = len(truth)
    lines = [f"📈 나우캐스팅 — 정답: {target}",
             f"   정답 표본 {total}일 · 상승 {up}일({up/total*100:.1f}%)" if total else
             "   정답 표본 없음", ""]
    if not total:
        return "\n".join(lines)
    lines.append(f"{'지표':22}{'예측':>6}{'적중률':>8}{'항상상승':>8}{'백분위':>8}  판정")
    for name, r in results.items():
        if not r.get("n"):
            lines.append(f"{name:22}{'—':>6}{'—':>8}{'—':>8}{'—':>8}  {r.get('verdict','')}")
            continue
        # **무작위 대조가 아니라 '항상 상승'을 나란히 놓는다.** 읽는 사람이
        # 이겨야 할 상대는 무작위가 아니라 시장 상승 편향이다.
        ctrl = ("—" if r.get("control_rate") is None
                else f"{r['control_rate']:.1f}%")
        pctl = "—" if r.get("percentile") is None else f"{r['percentile']:.1f}"
        lines.append(f"{name:22}{r['n']:>6}{r['rate']:>7.1f}%"
                     f"{ctrl:>8}{pctl:>8}  {r['verdict']}")
    lines.append("")
    best = max((r.get("n") or 0) for r in results.values()) if results else 0
    b = base_rate(truth)
    warn = regime_warning(truth)
    lines.append(f"표본 {best}건 · **이겨야 할 기준선 '항상 상승' {b*100:.1f}%**")
    lines.append("   (무작위 50%가 아니다 — 기준선을 넘지 못하면 발견이 아니다)")
    if warn:
        # **막힌 이유를 먼저 말한다.** 표본 수 표를 위에 두면 "며칠만 더 모으면
        # 된다"로 읽힌다 — 지금 막고 있는 것은 일수가 아니라 국면이다.
        lines.append("")
        lines.append(f"   ⛔ **지금은 표본 수가 문제가 아니다.** {warn}")
        lines.append("   아래 표는 국면이 들어온 **뒤에** 필요한 양입니다.")
    for row in bar_table(b):
        lines.append(f"   기준선 +{row['margin']*100:.0f}%p"
                     f"(적중 {row['target']*100:.1f}%) 판정에 "
                     f"{row['need']:,}건 (~{row['years']}년)")
    lines.append("")
    lines.append("_백분위 95 미만은 우연으로 설명되는 범위입니다._")
    lines.append("_'항상 상승'을 못 이기는 지표는 방향 정보를 담고 있지 않습니다._")
    lines.append("_상수 예측(늘 한 방향)은 순열검정으로 잡히지 않습니다 — 음성 대조가 잡습니다._")
    lines.append("_원/달러는 후행 지표로 판명되어 검정 대상에서 빠졌습니다"
                 "(같은 날 −0.378 · 다음 날 +0.029, 2026-09-01)._")
    return "\n".join(lines)


# ─── CLI (소급 평가) ─────────────────────────────────


def _kospi_truth():
    import json
    import price_sanity as ps
    files = sorted((ps._cache_root() / "indices").glob("market_index_KOSPI_*.json"))
    if not files:
        return {}, [], []
    payload = json.loads(files[-1].read_text(encoding="utf-8"))
    dates = [str(d) for d in payload["series"]["date"]]
    closes = [float(c) for c in payload["series"]["close"]]
    return dict(direction_series(dates, closes)), dates, closes


def _slope_predictions(dates, closes):
    """200일선 기울기는 **캐시로 소급 계산된다** — 유일하게 오늘 잴 수 있는 지표다.

    t일까지의 종가만 써서 t일 예측을 만든다(미래를 보지 않는다).
    """
    import proxy_indicators as pi
    out = []
    for i in range(len(closes)):
        slope = pi.ma_slope_pct(closes[:i + 1], pi.MA_WINDOW, pi.SLOPE_LOOKBACK)
        if slope is None:
            continue
        state = pi.slope_state(slope)
        out.append((dates[i], UP if state == "risk_on"
                    else (DOWN if state == "risk_off" else None)))
    return out


def _vkospi_predictions(thresholds=None):
    """VKOSPI는 **캐시로 소급 계산된다** — 로그가 쌓이길 기다릴 필요가 없다.

    판정 임계값은 비중 규칙이 실제로 쓰는 값을 그대로 쓴다. 나우캐스팅이 다른
    임계값으로 판정하면, 어느 쪽이 맞았는지 알아도 왜 맞았는지 모른다.

    **여기서 재는 것은 "다음 날 방향"이지 "위험 방어"가 아니다.** 이 검정에서
    떨어졌다고 비중 규칙이 쓸모없다는 뜻이 아니다 — 비중 규칙은 낙폭을 줄이는
    쪽으로 재야 하고, 그건 별도로 쟀다(2026-09-01 · MDD 28.29%→24.40%).
    """
    import json

    import price_sanity as ps

    files = sorted((ps._cache_root() / "indices").glob("market_index_VKOSPI_*.json"))
    if not files:
        return []
    payload = json.loads(files[-1].read_text(encoding="utf-8"))
    series = payload.get("series") or {}
    dates = [str(d) for d in series.get("date") or []]
    closes = [float(c) for c in series.get("close") or []]
    if thresholds is None:
        try:
            import kium_bot as kb

            high, low, _src = kb.active_thresholds()
        except Exception:  # noqa: BLE001
            return []
    else:
        high, low = float(thresholds[0]), float(thresholds[1])
    out = []
    for d, v in zip(dates, closes):
        # 변동성이 높으면 하락 쪽, 낮으면 상승 쪽. 중립 밴드는 **예측하지 않는다.**
        out.append((d, DOWN if v > high else (UP if v < low else None)))
    return out


def _logged_predictions(name: str):
    """적재된 지표 로그에서 예측을 만든다(지표별 시계열이 쌓인 뒤에 쓴다)."""
    import indicator_log as il
    rows = il.load()
    out = []
    for row in rows:
        for it in row.get("indicators", []) or []:
            if it.get("name") != name:
                continue
            state = it.get("state")
            out.append((str(row.get("day")),
                        UP if state == "risk_on"
                        else (DOWN if state == "risk_off" else None)))
            break
    return out


def _cli() -> int:
    truth, dates, closes = _kospi_truth()
    if not truth:
        print("코스피 지수 캐시가 없어 정답 시계열을 만들 수 없습니다.")
        return 1
    results = {}
    slope = _slope_predictions(dates, closes)
    results["코스피 200일선 기울기"] = evaluate(slope, truth)
    vkospi = _vkospi_predictions()
    if vkospi:
        results["VKOSPI 밴드"] = evaluate(vkospi, truth)
    days = [d for d, _ in slope]
    results["대조: 항상 상승"] = evaluate(always(UP, days), truth)
    results["대조: 항상 하락"] = evaluate(always(DOWN, days), truth)

    try:
        import indicator_log as il
        logged = il.load()
    except ImportError:
        logged = []
    # VKOSPI는 위에서 캐시로 소급했으므로 로그 대기 목록에 넣지 않는다.
    # 원/달러도 빠졌다 — 후행 지표로 판명되어 검정 대상이 아니다(위 주석).
    for name in ("외국인 순매수(5일)", "VIX"):
        preds = _logged_predictions(name)
        results[name] = (evaluate(preds, truth) if preds
                         else {"n": 0, "verdict": f"로그 {len(logged)}일 — 적재 대기"})
    print(format_report(results, truth))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
