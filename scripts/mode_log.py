"""mode_log.py — mode 판정을 기록하고, **재질문으로 정답을 만든다**.

**왜 지금 키워드를 보강할 수 없나.** 계획서에는 "실사용에서 가짜 fast/accurate
분류 사례를 모아 키워드 보강"이라고 적혀 있다. 2026-09-01에 로그를 뒤졌다.

    라우팅 로그 13일치 **5건** · mode override **0건**

표본이 없다. 그리고 표본이 쌓여도 지금 로그로는 **판단할 수 없다** — 남는 것이
최종 mode뿐이라 이런 것들을 알 수 없다.

    · LLM이 원래 뭐라고 했는가 (override가 고친 것인가, 원래 그랬던 것인가)
    · 사용자가 무엇을 물었는가 (args만 남고 원문이 없다)
    · **그 판정이 틀렸는가** ← 정답이 아예 없다

**정답을 어디서 얻는가.** 사람이 라벨을 달아주지 않는다. 대신 행동이 라벨이다:
`fast`로 답한 직후 사용자가 "자세히"·"제대로" 같은 말로 **다시 물으면**, 그
직전 판정은 틀린 것이다. 반대도 같다 — `accurate`로 오래 기다리게 한 뒤
"간단히"라고 다시 물으면 과했던 것이다.

    재질문 창(기본 10분) 안에 반대 방향 키워드가 오면 → 직전 판정을 오분류로 표시

**여기서 키워드를 고치지 않는다.** 후보만 내놓는다. 근거 없이 정규식을 늘리는
것이 이 프로젝트에서 반복된 실패이고, 키워드는 분포로 검증할 수 있는 값이
아니라서 `param_store` 창구에도 넣지 않았다 — 사람이 코드로 고쳐야 한다.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger("mode_log")

LOG_NAME = "mode_decisions.jsonl"
REASK_WINDOW_MIN = 10
# 질문 원문을 통째로 남기지 않는다. 길이 제한을 둬서 로그가 대화 사본이 되는
# 것을 막는다 — 판정을 되짚는 데 필요한 만큼만.
QUERY_MAX = 120


def row(query: str, *, llm_mode: str, final_mode: str, tool: str,
        at: str, overridden: bool = False, user: str = "") -> dict:
    """한 줄(순수). **override 여부를 따로 남긴다.**

    최종 mode만 남기면 "안전망이 일했다"와 "LLM이 원래 맞혔다"를 구분할 수
    없고, 그러면 안전망을 떼도 되는지 영원히 모른다.
    """
    q = (query or "").strip()
    return {
        "at": str(at),
        "user": str(user or ""),
        "query": q[:QUERY_MAX],
        "query_len": len(q),
        "llm_mode": str(llm_mode or ""),
        "final_mode": str(final_mode or ""),
        "overridden": bool(overridden),
        "tool": str(tool or ""),
    }


def append(path: Path, entry: dict) -> None:
    """덧붙이기만 한다. 실패해도 봇을 멈추지 않는다(기록은 부가 기능이다)."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        log.warning("mode 기록 실패: %s", exc)


def load(path: Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            # 깨진 줄 하나 때문에 전체를 버리지 않는다. 다만 조용히 넘기지도 않는다.
            log.warning("mode 로그에 읽을 수 없는 줄이 있다 — 건너뛴다")
    return out


# ─── 재질문으로 정답 만들기 (순수) ───────────────────


def _parse(ts: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(ts)[:19], fmt)
        except ValueError:
            continue
    return None


def wanted_mode(query: str) -> Optional[str]:
    """이 문장이 명시적으로 요구하는 mode(순수). 없으면 None.

    라우터의 안전망과 **같은 정규식을 쓴다** — 두 곳에 따로 두면 한쪽만
    바뀌는 날이 오고, 그러면 이 측정이 라우터를 설명하지 못한다.
    """
    import router as _r

    if not query:
        return None
    acc = bool(_r._ACCURATE_KEYWORDS_RE.search(query))
    fast = bool(_r._FAST_KEYWORDS_RE.search(query))
    if acc and not fast:
        return "accurate"
    if fast and not acc:
        return "fast"
    return None


def label_reasks(rows: Iterable[dict], *,
                 window_min: int = REASK_WINDOW_MIN) -> list[dict]:
    """재질문을 정답으로 붙인다(순수).

    직전 판정과 **다른 방향**을 요구하는 재질문만 라벨로 센다. 같은 방향이면
    사용자가 확인차 덧붙인 것이지 고쳐달라는 뜻이 아니다.
    """
    items = sorted((r for r in rows or []), key=lambda r: str(r.get("at")))
    out = [dict(r) for r in items]
    for i, cur in enumerate(out):
        want = wanted_mode(cur.get("query", ""))
        if not want:
            continue
        t_now = _parse(cur.get("at", ""))
        if t_now is None:
            continue
        for j in range(i - 1, -1, -1):
            prev = out[j]
            if prev.get("user") != cur.get("user"):
                continue
            t_prev = _parse(prev.get("at", ""))
            if t_prev is None:
                continue
            if t_now - t_prev > timedelta(minutes=window_min):
                break
            if prev.get("final_mode") and prev["final_mode"] != want:
                prev["misclassified"] = True
                prev["should_have_been"] = want
                prev["evidence"] = cur.get("query", "")[:QUERY_MAX]
            break
    return out


def summarize(rows: Iterable[dict]) -> dict:
    """오분류율과 방향(순수). **표본이 적으면 비율을 내지 않는다.**"""
    items = list(rows or [])
    n = len(items)
    labeled = [r for r in items if r.get("misclassified")]
    by_dir: dict[str, int] = {}
    for r in labeled:
        key = f"{r.get('final_mode')}→{r.get('should_have_been')}"
        by_dir[key] = by_dir.get(key, 0) + 1
    overridden = sum(1 for r in items if r.get("overridden"))
    override_saved = sum(1 for r in items
                         if r.get("overridden") and not r.get("misclassified"))
    return {
        "n": n, "labeled": len(labeled),
        "rate": round(len(labeled) / n * 100, 1) if n >= 30 else None,
        "rate_note": None if n >= 30 else f"표본 {n}건 — 30건 미만이면 비율을 내지 않는다",
        "by_direction": by_dir,
        "overridden": overridden,
        "override_saved": override_saved,
    }


def keyword_candidates(rows: Iterable[dict], *, min_count: int = 3) -> list[dict]:
    """오분류된 질문에 공통으로 나오는 말 후보(순수).

    **여기서 정규식을 고치지 않는다.** 사람이 보고 판단할 목록만 만든다 —
    빈도가 높다는 것과 그 말이 mode를 뜻한다는 것은 다르다.
    """
    import re
    from collections import Counter

    counts: dict[str, Counter] = {}
    for r in rows or []:
        if not r.get("misclassified"):
            continue
        want = r.get("should_have_been") or "?"
        words = re.findall(r"[가-힣]{2,}", r.get("query", ""))
        counts.setdefault(want, Counter()).update(set(words))
    out = []
    for want, counter in counts.items():
        for word, c in counter.most_common():
            if c < min_count:
                break
            if wanted_mode(word) == want:
                continue          # 이미 잡히는 말
            out.append({"word": word, "count": c, "suggests": want})
    return sorted(out, key=lambda d: -d["count"])


def format_report(rows: Iterable[dict]) -> str:
    items = list(rows or [])
    s = summarize(items)
    lines = [f"🧭 mode 판정 기록 — {s['n']}건", ""]
    if not s["n"]:
        lines.append("아직 기록이 없습니다. 봇을 쓰면 쌓입니다.")
        return "\n".join(lines)
    if s["rate"] is None:
        lines.append(f"  {s['rate_note']}")
    else:
        lines.append(f"  재질문으로 드러난 오분류 {s['labeled']}건 ({s['rate']}%)")
    for key, c in sorted(s["by_direction"].items(), key=lambda kv: -kv[1]):
        lines.append(f"    {key} {c}건")
    lines.append(f"  안전망(키워드 override) 발동 {s['overridden']}건 "
                 f"· 그중 뒤집히지 않은 것 {s['override_saved']}건")
    cands = keyword_candidates(items)
    lines.append("")
    if cands:
        lines.append("■ 키워드 후보 (사람이 판단할 것)")
        for c in cands[:10]:
            lines.append(f"  '{c['word']}' {c['count']}회 → {c['suggests']}")
    else:
        lines.append("■ 키워드 후보 없음 — 같은 말이 3번 이상 나온 적이 없습니다")
    lines.append("")
    lines.append("_재질문 창 10분 · 직전과 **다른 방향**을 요구할 때만 오분류로 셉니다._")
    lines.append("_여기서 키워드를 고치지 않습니다. 근거 없이 정규식을 늘리는 것이 반복된 실패입니다._")
    return "\n".join(lines)
