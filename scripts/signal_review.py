"""signal_review.py — 기술적 신호 적중률 평가 (탭 B 데이터 소스, v1)

`signal_bot`이 남긴 발생 기록(`signal_log.jsonl`)에 **결과 수익률을 채워 넣는다.**
계획서의 "탭 B 봇 신호 정확도"와 "result_return 자동 업데이트 스케줄러"에 해당한다.

측정 설계 (여기가 핵심):
  - 신호 후 N거래일 수익률만으로는 아무것도 증명하지 못한다. 워치리스트가 최근 잘 나가는
    종목으로 채워져 있으면 어떤 신호든 좋아 보인다. 그래서 **같은 종목·같은 기간의
    "아무 날이나 진입했을 때" 평균 수익률(베이스라인)과의 차이(edge)** 를 함께 계산한다.
    `entry_backtest.py`가 "베이스라인(무조건)" 대조군을 두는 것과 같은 원리다.
  - **MTF 필터에 걸려 발송되지 않은 신호도 평가한다.** 억제분의 성적을 모르면
    필터가 값을 더하는지 빼는지 영원히 알 수 없다.
  - 기준가는 신호 시점에 기록된 가격이다. 종가가 아니라 그 시각 실제로 보던 값이므로
    "그때 따라 샀다면"에 가장 가깝다.
  - 매도·경고 신호는 하락을 맞히면 성공이므로 부호를 뒤집어 판정한다.

한계 (해석 시 반드시 감안):
  - 실제 체결·수수료·슬리피지를 반영하지 않는다.
  - 표본이 적은 동안에는 어떤 결론도 내리지 않는다(MIN_SAMPLE).
  - 조합을 많이 뒤질수록 우연히 좋아 보이는 것이 나온다. 볼 조합을 미리 정해 두었다:
    전략별 × 액션별, 그리고 MTF 억제 여부. 그 외 조합은 이 모듈이 만들지 않는다.

2026-09-16 수정 두 가지 (원장을 실제로 뜯어보고 찾은 것):
  - **신호 하나가 최대 12번 세어지고 있었다.** `technical_signal`이 매시간 돌며
    같은 신호를 다시 적고, 키에 시각(`at`)이 들어가 전부 다른 신호로 취급됐다.
    584행 중 (날·종목·전략·액션)이 다른 것은 204건 — 2.9배. 「표본 부족」 문턱도
    이 부풀린 수로 넘고 있었다. 이제 **키는 날 단위**다. 같은 날 같은 신호는 처음
    본 것 하나만 평가하고(그 시각 가격이 "그때 따라 샀다면"에 가장 가깝다),
    옛 원장의 시각 키는 읽을 때 날 키로 접는다.
  - **홀딩유지는 채점하지 않는다.** 포지션을 유지하라는 판단이지 종목이 오른다는
    예측이 아닌데, 강세 예측으로 채점하니 전체 행의 71%를 차지하며 결과를 덮었다
    (떨어진 종목에 경고가 붙고 그 뒤 반등하는 단기 반전이 그대로 "틀림"으로 잡혔다).
    적중·edge·판정은 실행신호(매수·매도·경고)만으로 내고, 홀딩유지는 건수만 따로 보인다.
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger("signal_review")

HORIZONS = (1, 5)          # 평가 지평(거래일)
MIN_SAMPLE = 10            # 이 미만이면 판정 보류
BULLISH = {"매수", "홀딩유지"}
BEARISH = {"매도", "경고"}
# 채점 대상은 **방향을 건 실행신호**뿐이다. 홀딩유지는 결과(ret)는 남기되 적중·edge·
# 판정에 넣지 않는다 — 건수만 따로 보인다.
SCORED = {"매수", "매도", "경고"}
UNSCORED = {"홀딩유지"}
LOG_NAME = "signal_log.jsonl"
OUTCOME_NAME = "signal_outcomes.jsonl"


# ─── 기록 읽기 (순수에 가까운 파싱) ──────────────────


def parse_jsonl(lines: Iterable[str]) -> list[dict]:
    """JSONL 문자열들 → dict 목록. 깨진 줄은 건너뛴다(기록이 끊기면 안 된다)."""
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def parse_lines(lines: Iterable[str]) -> list[dict]:
    """신호 기록 전용 — 종목·시각이 없는 줄은 평가할 수 없으므로 버린다."""
    return [r for r in parse_jsonl(lines) if r.get("ticker") and r.get("at")]


def signal_day(rec: dict) -> Optional[str]:
    at = str(rec.get("at") or rec.get("day") or "")[:10]
    return at or None


def signal_key(rec: dict) -> str:
    """같은 신호를 두 번 평가하지 않기 위한 식별자 — **날 단위**.

    시각이 아니라 날이다. 매시간 도는 잡이 같은 신호를 다시 적어도 같은 날
    같은 종목·전략·액션이면 같은 신호다(2026-09-16). 결과 행(`day`만 있고
    `at`이 없을 수 있음)에도 그대로 쓴다.
    """
    return "|".join([signal_day(rec) or "", str(rec.get("ticker", "")),
                     str(rec.get("strategy", "")), str(rec.get("action", ""))])


def first_per_key(records: Iterable[dict]) -> list[dict]:
    """같은 키의 기록 중 **처음 것**만 남긴다(입력 순서 유지, 순수).

    처음 본 시각의 가격이 "그때 따라 샀다면"에 가장 가깝고, 이후 반복 기록은
    같은 신호를 다시 적은 것뿐이다.
    """
    seen: set[str] = set()
    out = []
    for r in records:
        k = signal_key(r)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def collapse_outcomes(rows: Iterable[dict]) -> dict[str, dict]:
    """결과 행들 → {날 키: 행}. 옛 시각 키(`at|…`)로 저장된 행도 날 키로 접는다.

    같은 날 키가 여럿이면 `at`이 가장 이른 행을 택한다(처음 본 신호). 행의
    `key` 필드는 새 키로 바꿔 쓴다 — 다음 저장 때 원장이 스스로 옮겨 간다.
    """
    best: dict[str, dict] = {}
    for r in rows:
        if not (r.get("ticker") and (r.get("day") or r.get("at"))):
            # 날 키를 만들 수 없는 행은 저장된 키 그대로 둔다(버리지 않는다).
            if r.get("key"):
                best.setdefault(str(r["key"]), r)
            continue
        k = signal_key(r)
        cur = best.get(k)
        if cur is None or str(r.get("at") or "") < str(cur.get("at") or ""):
            best[k] = dict(r, key=k)
    return best


# ─── 수익률 계산 (순수) ──────────────────────────────


def _index_of_day(series: list[tuple[str, float]], day: str) -> Optional[int]:
    """해당 날짜(또는 그 이전 마지막 거래일)의 인덱스. 없으면 None."""
    idx = None
    for i, (d, _) in enumerate(series):
        if d <= day:
            idx = i
        else:
            break
    return idx


def forward_return(series: list[tuple[str, float]], day: str, horizon: int,
                   *, entry_price: Optional[float] = None) -> Optional[float]:
    """신호일 기준 N거래일 뒤 종가까지의 수익률. 데이터가 모자라면 None."""
    i = _index_of_day(series, day)
    if i is None or i + horizon >= len(series):
        return None
    base = entry_price if entry_price else series[i][1]
    if not base:
        return None
    return series[i + horizon][1] / base - 1.0


def baseline_return(series: list[tuple[str, float]], horizon: int,
                    *, until: Optional[str] = None) -> Optional[float]:
    """'아무 날이나 진입했을 때' 평균 N거래일 수익률 — 비교 기준.

    until을 주면 그 날짜까지만 사용한다(신호 이후 구간을 기준에 넣지 않기 위함).
    """
    end = len(series)
    if until is not None:
        i = _index_of_day(series, until)
        end = (i + 1) if i is not None else 0
    rets = [series[j + horizon][1] / series[j][1] - 1.0
            for j in range(max(end - horizon, 0))
            if series[j][1]]
    if not rets:
        return None
    return sum(rets) / len(rets)


def directional(value: Optional[float], action: str) -> Optional[float]:
    """액션 방향으로 부호 정렬 — 매도·경고는 하락을 맞히면 양수."""
    if value is None:
        return None
    return -value if action in BEARISH else value


def evaluate_signal(rec: dict, series: list[tuple[str, float]],
                    horizons: tuple[int, ...] = HORIZONS) -> dict:
    """신호 1건 → 결과 레코드(순수). 아직 평가할 수 없으면 pending=True."""
    day = signal_day(rec)
    action = str(rec.get("action") or "")
    out = {
        "key": signal_key(rec), "at": rec.get("at"), "day": day,
        "ticker": rec.get("ticker"), "name": rec.get("name"),
        "strategy": rec.get("strategy"), "action": action,
        "suppressed": bool(rec.get("suppressed")),
        # 병행 기록(후보 지표) 표시 — v3.64. MTF 억제와 구분해 옮긴다.
        "shadow": bool(rec.get("shadow")),
        "trend": rec.get("trend"), "entry": rec.get("price"),
    }
    pending = False
    for h in horizons:
        raw = forward_return(series, day, h, entry_price=rec.get("price")) if day else None
        base = baseline_return(series, h, until=day) if day else None
        if raw is None:
            pending = True
        out[f"ret_{h}d"] = None if raw is None else round(directional(raw, action) * 100, 3)
        out[f"base_{h}d"] = None if base is None else round(directional(base, action) * 100, 3)
        out[f"edge_{h}d"] = (None if (raw is None or base is None)
                             else round((directional(raw, action) - directional(base, action)) * 100, 3))
    out["pending"] = pending
    return out


# ─── 집계 (순수) ─────────────────────────────────────


MIN_DAYS = 5               # 서로 다른 신호일이 이보다 적으면 판정하지 않는다
PERM_N = 2000              # 날 안 방향표 섞기 횟수
PERM_SEED = 20260916       # 고정 — 같은 원장이면 같은 백분위
PERM_BAR = 95.0            # 사전 등록: 백분위 95 이상만 「우위 있음」
MIN_MIXED_DAYS = 5         # 강세·약세가 같이 있는 날이 이보다 적으면 섞기가 의미 없다


def within_day_percentile(rows: list[dict], horizon: int, *,
                          n_perm: int = PERM_N, seed: int = PERM_SEED) -> Optional[dict]:
    """**그날 시장은 그대로 두고, 봇이 어느 종목에 어느 방향을 붙였는지만 섞는다.**

    베이스라인(아무 날이나 진입) 대비 edge는 대조군이 못 된다 — 시장이 내리는
    2주 동안 약세 신호만 내면 edge는 저절로 양수다(2026-09-16 실측: 섞은 귀무
    평균이 +0.59%였다). 신호가 값을 더했는지는 **같은 날 같은 묶음 안에서**
    방향표를 무작위로 다시 붙인 분포와 견줘야 안다.

    돌려주는 것: obs(방향정렬 평균), null_mean, pct(관측이 귀무보다 큰 비율, %).
    묶음 안에 방향이 한 종류뿐이면 섞어도 달라질 게 없다 → None(검정 불가).
    저장된 ret는 이미 방향정렬돼 있다 — 원수익률은 약세면 부호를 되돌려 얻는다.
    """
    import random

    kept = [r for r in rows if r.get(f"ret_{horizon}d") is not None]
    if not kept:
        return None
    by_day: dict[str, list[tuple[float, bool]]] = {}
    for r in kept:
        bear = str(r.get("action") or "") in BEARISH
        raw = -r[f"ret_{horizon}d"] if bear else r[f"ret_{horizon}d"]
        by_day.setdefault(str(r.get("day") or ""), []).append((raw, bear))
    mixed_days = sum(1 for v in by_day.values() if len({b for _, b in v}) > 1)
    if mixed_days == 0:
        return None                                     # 섞을 것이 없다
    obs = sum(r[f"ret_{horizon}d"] for r in kept) / len(kept)
    rng = random.Random(seed)
    nulls = []
    for _ in range(n_perm):
        tot = 0.0
        for items in by_day.values():
            labels = [b for _, b in items]
            rng.shuffle(labels)
            for (raw, _), b in zip(items, labels):
                tot += -raw if b else raw
        nulls.append(tot / len(kept))
    pct = sum(1 for x in nulls if x < obs) / len(nulls) * 100
    # **섞이는 날이 한둘이면 귀무 분포가 몇 개 값뿐이다** — StochRSI가 경고 35건에
    # 매수 2건이라 백분위 0.0이 나왔다(2026-09-16). 그 0.0은 "무작위보다 나쁘다"가
    # 아니라 "잴 수 없다"다. 판정 쪽이 mixed_days로 걸러 낸다.
    return {"obs": round(obs, 3), "null_mean": round(sum(nulls) / len(nulls), 3),
            "pct": round(pct, 1), "mixed_days": mixed_days}


def _agg(rows: list[dict], horizon: int) -> dict:
    kept = [r for r in rows if r.get(f"ret_{horizon}d") is not None]
    vals = [r[f"ret_{horizon}d"] for r in kept]
    edges = [r[f"edge_{horizon}d"] for r in kept if r.get(f"edge_{horizon}d") is not None]
    n = len(vals)
    # **독립 단위는 건이 아니라 날이다.** 같은 날 신호는 같은 시장을 겪는다 —
    # 125건이라도 4일치면 사실상 표본 4다. 탭 D에서 시간축을 발견에서 뺀 것과
    # 같은 이유이고, 탭 C에서 달 대신 에피소드를 센 것과 같은 이유다.
    days = len({r.get("day") for r in kept if r.get("day")})
    hits = sum(1 for v in vals if v > 0)
    perm = (within_day_percentile(kept, horizon)
            if (n >= MIN_SAMPLE and days >= MIN_DAYS) else None)
    if n < MIN_SAMPLE:
        verdict = "표본 부족"
    elif days < MIN_DAYS:
        verdict = "날 부족"
    elif perm is None:
        verdict = "검정 불가(한 방향뿐)"
    elif perm["mixed_days"] < MIN_MIXED_DAYS:
        verdict = f"검정 불가(방향 섞인 날 {perm['mixed_days']}일 < {MIN_MIXED_DAYS})"
    elif perm["pct"] >= PERM_BAR:
        verdict = f"우위 있음(백분위 {perm['pct']})"
    else:
        verdict = f"우연 범위(백분위 {perm['pct']})"
    return {
        "n": n,
        "days": days,
        "hit_rate": round(hits / n * 100, 1) if n else None,
        "avg_ret": round(sum(vals) / n, 3) if n else None,
        "avg_edge": round(sum(edges) / len(edges), 3) if edges else None,
        "perm": perm,
        "verdict": verdict,
    }


def available_counts(outcomes: Iterable[dict],
                     horizons: tuple[int, ...] = HORIZONS) -> dict:
    """지평별로 **평가가 끝난 행이 몇 건인지**(순수).

    5거래일이 안 지났다고 1거래일 결과까지 없는 것은 아니다. 화면이
    「아직 없습니다」라고만 하면 사람은 수집이 고장난 줄 안다.
    """
    rows = [r for r in outcomes if is_scored(r)]
    return {h: sum(1 for r in rows if r.get(f"ret_{h}d") is not None) for h in horizons}


def is_scored(row: dict) -> bool:
    """이 행이 적중·edge 채점에 들어가는가 — 실행신호(매수·매도·경고)만."""
    return str(row.get("action") or "") not in UNSCORED


def summarize(outcomes: Iterable[dict], horizon: int = 5) -> dict:
    """전략×액션, MTF 억제 여부별 집계. **볼 조합을 미리 정해 둔다.**

    **행 선택은 지평별로 한다(2026-09-02 수정).** 전에는 `pending`을 통째로
    버렸는데, `evaluate_signal`은 지평 **하나라도** 비면 pending을 세운다.
    그래서 1거래일 결과가 39건 채워져 있는데도 1일 화면이 n=0으로 떴다 —
    **있는 데이터를 못 쓰고 있었다.** 지금은 그 지평의 값이 있는 행만 본다.

    **홀딩유지는 채점하지 않는다(2026-09-16).** `hold`에 건수·일수만 남긴다.
    """
    outcomes = list(outcomes)
    filled = [o for o in outcomes if o.get(f"ret_{horizon}d") is not None]
    rows = [o for o in filled if is_scored(o)]
    hold = [o for o in filled if not is_scored(o)]
    by_strategy: dict[str, list[dict]] = {}
    by_action: dict[str, list[dict]] = {}
    for r in rows:
        # 병행 기록은 전략 비교에 들어가되 **전략 이름에 표시**한다 — 발송된
        # 적 없는 지표의 성적이 발송 지표와 같은 얼굴로 보이면 안 된다.
        label = (r.get("strategy") or "?") + ("(병행)" if r.get("shadow") else "")
        by_strategy.setdefault(label, []).append(r)
        by_action.setdefault(r.get("action") or "?", []).append(r)
    # MTF 평가에서 병행 기록은 뺀다. 억제는 "보냈을 신호를 필터가 막았다"이고
    # 병행 기록은 애초에 보낼 계획이 없던 후보다 — 섞이면 필터 평가가 오염된다.
    mtf_rows = [r for r in rows if not r.get("shadow")]
    sent = [r for r in mtf_rows if not r["suppressed"]]
    held = [r for r in mtf_rows if r["suppressed"]]
    return {
        "horizon": horizon,
        "available": available_counts(outcomes),
        "waiting": sum(1 for o in outcomes
                       if is_scored(o) and o.get(f"ret_{horizon}d") is None),
        "hold": {"n": len(hold),
                 "days": len({o.get("day") for o in hold if o.get("day")})},
        "total": _agg(rows, horizon),
        "by_strategy": {k: _agg(v, horizon) for k, v in sorted(by_strategy.items())},
        "by_action": {k: _agg(v, horizon) for k, v in sorted(by_action.items())},
        "mtf": {"sent": _agg(sent, horizon), "suppressed": _agg(held, horizon)},
    }


def format_summary(s: dict) -> str:
    """사람이 읽는 요약(순수)."""
    h = s["horizon"]
    t = s["total"]
    # **지평별로 몇 건이 찼는지 먼저 말한다(2026-09-02).** 화면에는 붙였는데
    # 여기에 안 붙여서, CLI만 「표본이 없습니다」라고 했다 — 실제로는 1거래일
    # 결과가 125건 있었다. 같은 결함을 한 곳만 고치면 다른 곳이 남는다.
    avail = s.get("available") or {}
    avail_line = ("   지평별 평가 완료: "
                  + " · ".join(f"{k}거래일 {v}건" for k, v in sorted(avail.items()))
                  ) if avail else ""
    if not t["n"]:
        out = ["📡 신호 적중률: **이 지평에는** 평가 가능한 표본이 아직 없습니다.",
               f"   신호 발생 후 {h}거래일이 지나야 결과가 채워집니다."]
        if avail_line:
            out.append(avail_line)
            other = [k for k, v in avail.items() if v and k != h]
            if other:
                out.append(f"   → 지금 볼 수 있는 지평: --horizon {other[0]}")
        return "\n".join(out)
    lines = [f"📡 *신호 적중률* ({h}거래일 기준)"]
    if avail_line:
        lines.append(avail_line)
    lines += ["",
             f"전체 {t['n']}건 · 적중 {t['hit_rate']}% · 평균 {t['avg_ret']}% "
             f"· 베이스라인 대비 {t['avg_edge']}%p → {t['verdict']}", ""]
    lines.append("전략별")
    for k, a in s["by_strategy"].items():
        lines.append(f"  • {k}: {a['n']}건 · 적중 {a['hit_rate']}% · edge {a['avg_edge']}%p ({a['verdict']})")
    lines.append("액션별")
    for k, a in s["by_action"].items():
        lines.append(f"  • {k}: {a['n']}건 · 적중 {a['hit_rate']}% · edge {a['avg_edge']}%p")
    hold = s.get("hold") or {}
    if hold.get("n"):
        lines.append(f"  • 홀딩유지: {hold['n']}건 · {hold.get('days')}일 — 채점 안 함"
                     "(포지션 유지 판단이지 방향 예측이 아니다)")
    m = s["mtf"]
    lines += ["", "MTF 필터 검증 (발송 vs 억제)",
              f"  • 발송된 신호: {m['sent']['n']}건 · edge {m['sent']['avg_edge']}%p",
              f"  • 억제된 신호: {m['suppressed']['n']}건 · edge {m['suppressed']['avg_edge']}%p",
              "  _억제분이 더 좋으면 필터가 값을 빼고 있다는 뜻이다._"]
    if t.get("days", 0) < MIN_DAYS:
        lines += ["", f"⚠️ **{t['n']}건이지만 서로 다른 신호일은 {t.get('days')}일뿐이다.** "
                      "같은 날 신호는 같은 시장을 겪으므로 독립 표본이 아니다 — "
                      f"하루의 방향이 그날 신호 전체의 부호를 정한다. 서로 다른 날 "
                      f"{MIN_DAYS}일이 모이기 전에는 위 숫자를 판정으로 읽지 않는다."]
    perm = t.get("perm")
    if perm:
        lines += ["", f"판정 근거: 같은 날 안에서 방향표만 섞은 귀무 평균 {perm['null_mean']:+}% "
                      f"vs 관측 {perm['obs']:+}% → 백분위 {perm['pct']} "
                      f"(사전 등록 기준 {PERM_BAR:g} 이상만 우위)."]
        lines.append("   _베이스라인 edge는 대조군이 아니다 — 내리는 장에 약세 신호만 내면 edge는 저절로 양수다._")
    lines += ["", f"_표본 {MIN_SAMPLE}건 미만은 '표본 부족', 신호일 {MIN_DAYS}일 미만은 '날 부족'. "
                  "체결·수수료·슬리피지 미반영._"]
    return "\n".join(lines)


# ─── 경계 (I-O) ──────────────────────────────────────


def default_state_dir() -> Path:
    try:
        from storage_paths import PATHS
        return Path(PATHS.private_state_dir)
    except Exception:  # noqa: BLE001
        return Path(__file__).resolve().parent.parent / "data" / "private" / "state"


def load_signals(path=None) -> list[dict]:
    p = Path(path) if path else default_state_dir() / LOG_NAME
    try:
        return parse_lines(p.read_text(encoding="utf-8").splitlines())
    except FileNotFoundError:
        return []
    except Exception as e:  # noqa: BLE001
        log.warning("신호 기록 읽기 실패: %s", e)
        return []


def load_outcomes(path=None) -> dict[str, dict]:
    p = Path(path) if path else default_state_dir() / OUTCOME_NAME
    try:
        rows = parse_jsonl(p.read_text(encoding="utf-8").splitlines())
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001
        log.warning("결과 기록 읽기 실패: %s", e)
        return {}
    # 옛 시각 키 행은 여기서 날 키로 접힌다. 다음 save가 접힌 형태로 쓴다.
    return collapse_outcomes(r for r in rows if r.get("key"))


def save_outcomes(outcomes: dict[str, dict], path=None) -> Path:
    """전량 다시 쓴다(원자적). pending이 채워지면 같은 key가 갱신되기 때문."""
    import os
    import tempfile
    p = Path(path) if path else default_state_dir() / OUTCOME_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".tmp_", suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for row in outcomes.values():
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(tmp, p)
    return p


def fetch_daily_series(yf_symbol: str, period: str = "1y") -> list[tuple[str, float]]:
    """yfinance 일봉 (날짜, 종가). 실패 시 빈 리스트."""
    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance 없음 — 수익률 평가 불가")
        return []
    try:
        df = yf.Ticker(yf_symbol).history(period=period, interval="1d")
        if df is None or df.empty:
            return []
        return [(idx.strftime("%Y-%m-%d"), float(row))
                for idx, row in zip(df.index, df["Close"].tolist())]
    except Exception as e:  # noqa: BLE001
        log.warning("일봉 조회 실패 %s: %s", yf_symbol, e)
        return []


def yf_symbol_for(rec: dict) -> str:
    """기록의 ticker → yfinance 심볼. FX 등은 기록된 값이 이미 심볼이다."""
    ticker = str(rec.get("ticker") or "")
    return ticker if ("=" in ticker or "." in ticker) else f"{ticker}.KS"


def refresh(signal_path=None, outcome_path=None, *,
            horizons: tuple[int, ...] = HORIZONS,
            fetch=fetch_daily_series) -> tuple[dict[str, dict], int]:
    """미평가·pending 신호의 결과를 채운다. (전체 결과, 갱신 건수)."""
    # 같은 날 같은 신호의 반복 기록은 처음 것 하나만 — 나머지는 재기록이다.
    signals = first_per_key(load_signals(signal_path))
    outcomes = load_outcomes(outcome_path)
    todo = [s for s in signals
            if signal_key(s) not in outcomes or outcomes[signal_key(s)].get("pending")]
    # 옛 시각 키가 날 키로 접혀 행이 줄었으면 원장을 접힌 형태로 다시 쓴다.
    migrated = _raw_outcome_rows(outcome_path) > len(outcomes)
    if not todo:
        if migrated:
            save_outcomes(outcomes, outcome_path)
        return outcomes, 0

    series_cache: dict[str, list[tuple[str, float]]] = {}
    updated = 0
    for rec in todo:
        sym = yf_symbol_for(rec)
        if sym not in series_cache:
            series_cache[sym] = fetch(sym)
        series = series_cache[sym]
        if not series:
            continue
        outcomes[signal_key(rec)] = evaluate_signal(rec, series, horizons)
        updated += 1
    if updated or migrated:
        save_outcomes(outcomes, outcome_path)
    return outcomes, updated


def _raw_outcome_rows(path=None) -> int:
    """원장 파일의 접기 전 행 수(마이그레이션 필요 여부 판단용)."""
    p = Path(path) if path else default_state_dir() / OUTCOME_NAME
    try:
        return len(parse_jsonl(p.read_text(encoding="utf-8").splitlines()))
    except Exception:  # noqa: BLE001
        return 0


def _cli() -> int:
    ap = argparse.ArgumentParser(description="기술적 신호 적중률 평가 (탭 B 데이터)")
    ap.add_argument("--signals"), ap.add_argument("--outcomes")
    ap.add_argument("--horizon", type=int, default=5, help="요약 지평(거래일, 기본 5)")
    ap.add_argument("--no-refresh", action="store_true", help="가격 조회 없이 기존 결과만 집계")
    args = ap.parse_args()

    if args.no_refresh:
        outcomes = load_outcomes(args.outcomes)
        updated = 0
    else:
        outcomes, updated = refresh(args.signals, args.outcomes)
    print(f"평가 대상 {len(outcomes)}건 (이번 갱신 {updated}건)")
    pending = sum(1 for o in outcomes.values() if o.get("pending"))
    if pending:
        print(f"  대기 {pending}건 — 지평만큼 거래일이 지나지 않음")
    print()
    print(format_summary(summarize(outcomes.values(), horizon=args.horizon)))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
