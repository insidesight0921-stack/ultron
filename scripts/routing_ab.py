"""routing_ab.py — 마스터 라우터 모델 A/B (같은 발화, 도구 일치로 채점).

**왜 재나.** `router.py`에는 "26B 라우터가 잘못된 JSON을 뱉어 knowledge_bot으로
빠지는 실환경 문제 우회"라는 주석과 함께 정규식 단락 처리가 여러 개 붙어
있다 — 라우터가 못 미더워 규칙으로 덧댄 자국이다. 7월 가중치 갱신이
툴콜링을 고쳤다고 하니 그것부터 확인하고, 후보(qwen3.6:27b)와 나란히 잰다.

**발화와 정답은 재기 전에 고정한다**(이 파일이 그 기록이다). 정규식
단락에 걸리는 발화는 넣지 않는다 — 그건 LLM이 아니라 규칙을 재는 것이다
(테스트가 이를 확인한다).

**크기에 대해 정직하게.** 발화 30개면 일치 수 차이 5건 이하는 우연과
구분되지 않는다. 「명백히 나쁘지 않은가」를 보는 크기다.

봇과 같은 경로로 부른다: `router.route(query, model=…)` 그대로 —
system prompt·format=json·temperature 0·num_ctx 16384 전부 봇의 것이다.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Optional

SAME_AS_CHANCE = 5

# (발화, 기대 도구, 기대 action 또는 None)
FIXTURE = [
    # knowledge_bot — 누적된 원칙·메모
    ("내 매매 청산 규칙이 뭐였지?", "knowledge_bot", None),
    ("콴텍봇 슬롯 비율 어떻게 정해놨더라", "knowledge_bot", None),
    ("모멘텀 크래시 신호 정의 알려줘", "knowledge_bot", None),
    ("투자 원칙 총론 요약해줘", "knowledge_bot", None),
    ("IPO 매력지수 기준이 뭐야", "knowledge_bot", None),
    ("국면별 팩터 가중 표 보여줘", "knowledge_bot", None),
    # schedule_bot
    ("다음주 화요일 오후 3시에 콴텍봇 리뷰 잡아줘", "schedule_bot", "add"),
    ("내일 오전 10시 치과", "schedule_bot", "add"),
    ("매주 월수금 아침 9시 주간 리뷰 반복으로", "schedule_bot", "add"),
    ("다가오는 일정 뭐 있어", "schedule_bot", "upcoming"),
    ("내 일정 전부 보여줘", "schedule_bot", "list"),
    # finance_bot
    ("지금 환율 얼마야", "finance_bot", "latest"),
    ("VIX 지금 몇이야", "finance_bot", "latest"),
    ("경제 지표 대시보드 보여줘", "finance_bot", "dashboard"),
    ("지금 시장이 내 매매 원칙이랑 맞아?", "finance_bot", "compare_with_principles"),
    # invest_bot
    ("삼성전자 차트 봐줘", "invest_bot", "analyze"),
    ("005930 분석해줘", "invest_bot", "analyze"),
    ("SK하이닉스 지금 매수 조건 충족해?", "invest_bot", "compare_with_rules"),
    ("현대차 내 원칙 기준으로 평가해줘", "invest_bot", "compare_with_rules"),
    # kium_bot
    ("키움봇 모멘텀 스캔 돌려", "kium_bot", "scan"),
    ("오늘 모멘텀 좋은 종목 Top 10", "kium_bot", "scan"),
    ("모멘텀 스캔하고 시장 크래시 감지도 같이", "kium_bot", "scan"),
    # quant_bot
    ("지금 거시 국면 뭐야", "quant_bot", "phase"),
    ("콴텍봇 종목 추천해줘", "quant_bot", "recommend"),
    ("국면 기반으로 코스닥150에서 8개 추천", "quant_bot", "recommend"),
    # coding_bot
    ("피보나치 N번째 구하는 함수 짜줘", "coding_bot", "code"),
    ("이 에러 좀 봐줘: TypeError: NoneType has no len()", "coding_bot", "debug"),
    ("파이썬 장바구니 클래스 설계해줘", "coding_bot", "design"),
    # respond_directly — 도구가 필요 없는 말
    ("고마워", "respond_directly", None),
    ("안녕, 오늘 기분 어때?", "respond_directly", None),
]


# ─── 순수 ────────────────────────────────────────────

def judge(got: dict, expected_tool: str, expected_action: Optional[str]) -> dict:
    """한 건 채점(순수)."""
    tool_ok = got.get("tool") == expected_tool
    action = (got.get("args") or {}).get("action")
    action_ok = (True if expected_action is None else action == expected_action)
    return {"tool_ok": tool_ok, "action_ok": tool_ok and action_ok,
            "got_tool": got.get("tool"), "got_action": action}


def score(results: list[dict]) -> dict:
    out: dict = {}
    for r in results:
        m = out.setdefault(r["model"], {"n": 0, "tool": 0, "action": 0,
                                        "json_fail": 0, "wall": 0.0})
        m["n"] += 1
        m["tool"] += 1 if r["tool_ok"] else 0
        m["action"] += 1 if r["action_ok"] else 0
        m["json_fail"] += 1 if r.get("json_fail") else 0
        m["wall"] += r.get("wall", 0.0)
    for m in out.values():
        m["wall_avg"] = round(m["wall"] / m["n"], 1) if m["n"] else None
    return out


def verdict(summary: dict, *, same_as_chance: int = SAME_AS_CHANCE) -> str:
    """**재기 전에 정한 규칙**(순수). 도구 일치 수가 기준, 동률이면 JSON 실패 수, 그다음 속도."""
    if len(summary) != 2:
        return "모델이 둘이 아니다"
    (a, sa), (b, sb) = sorted(summary.items(),
                              key=lambda kv: (-kv[1]["tool"], kv[1]["json_fail"],
                                              kv[1]["wall_avg"] or 0))
    diff = sa["tool"] - sb["tool"]
    if diff <= same_as_chance:
        tie = (f"동률 근처 — JSON 실패 {sa['json_fail']} vs {sb['json_fail']}, "
               f"평균 {sa['wall_avg']}s vs {sb['wall_avg']}s")
        return (f"도구 일치 차이 {diff}건 — n={sa['n']}에서는 우연과 구분되지 않는다. "
                f"「{b}가 명백히 나쁘지 않다」까지만 말할 수 있다 ({tie})")
    return f"{a}가 {diff}건 더 맞힘 — 이 크기에서는 드문 차이(재실행으로 확인할 것)"


def format_report(results: list[dict], summary: dict) -> str:
    lines = ["🧭 마스터 라우터 A/B — 도구 일치 채점", ""]
    models = list(summary)
    lines.append(f"{'발화':30}{'기대':16}" + "".join(f"{m:>22}" for m in models))
    by_q: dict = {}
    for r in results:
        by_q.setdefault(r["query"], {})[r["model"]] = r
    for q, cells in by_q.items():
        exp = next(iter(cells.values()))
        row = f"{q[:28]:30}{(exp['expected_tool'] + ('/' + exp['expected_action'] if exp['expected_action'] else ''))[:15]:16}"
        for m in models:
            c = cells.get(m)
            if not c:
                row += f"{'-':>22}"
                continue
            mark = "○" if c["action_ok"] else ("△" if c["tool_ok"] else "✗")
            shown = (c["got_tool"] or "?") + (("/" + c["got_action"]) if c.get("got_action") else "")
            if c.get("json_fail"):
                shown = "JSON✗→" + shown
            row += f"{(mark + ' ' + shown)[:21]:>22}"
        lines.append(row)
    lines.append("")
    for m, s in summary.items():
        lines.append(f"  {m}: 도구 {s['tool']}/{s['n']} · 도구+action {s['action']}/{s['n']} · "
                     f"JSON 실패 {s['json_fail']} · 평균 {s['wall_avg']}s")
    lines.append("")
    lines.append(f"  판정(사전 규칙): {verdict(summary)}")
    lines.append("  _○ 도구·action 일치 · △ 도구만 일치 · ✗ 불일치 · JSON✗ 파싱 실패로 fallback._")
    lines.append("  _정규식 단락에 걸리는 발화는 일부러 뺐다 — LLM을 재는 것이지 규칙을 재는 게 아니다._")
    return "\n".join(lines)


# ─── I/O ─────────────────────────────────────────────

def run(models: list[str], *, fixture=FIXTURE, think_for=None, on_progress=None) -> list[dict]:
    """`router.route`를 봇과 같은 경로로 부른다. JSON 파싱 실패를 세기 위해
    `_parse_json`을 감싼다(반환은 바꾸지 않는다)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import router

    fails = {"n": 0}
    original = router._parse_json

    def counting(text):
        got = original(text)
        if got is None:
            fails["n"] += 1
        return got

    router._parse_json = counting
    results = []
    try:
        for model in models:
            think = (think_for or {}).get(model)
            for query, exp_tool, exp_action in fixture:
                before = fails["n"]
                t0 = time.time()
                try:
                    got = router.route(query, model=model, think=think)
                except Exception as e:                       # noqa: BLE001
                    got = {"tool": None, "args": {}, "error": str(e)[:120]}
                rec = {"model": model, "query": query,
                       "expected_tool": exp_tool, "expected_action": exp_action,
                       "wall": round(time.time() - t0, 1),
                       "json_fail": fails["n"] > before,
                       **judge(got, exp_tool, exp_action)}
                results.append(rec)
                if on_progress:
                    on_progress(rec)
    finally:
        router._parse_json = original
    return results


def _cli() -> int:
    import argparse
    from datetime import datetime

    ap = argparse.ArgumentParser(description="마스터 라우터 모델 A/B")
    ap.add_argument("--models", nargs="+", default=["gemma4:26b", "qwen3.6:27b"])
    ap.add_argument("--think-off", nargs="*", default=["qwen3.6:27b"],
                    help="이 모델들에는 think=false를 보낸다(추론 모델)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    think_for = {m: False for m in (args.think_off or [])}

    print(f"  모델 {args.models} · 발화 {len(FIXTURE)}개 · think off: {list(think_for)}")
    print()

    def progress(r):
        mark = "○" if r["action_ok"] else ("△" if r["tool_ok"] else "✗")
        print(f"  {mark} {r['model']:14} {r['query'][:26]:28} → {r['got_tool']}"
              f"{('/' + r['got_action']) if r.get('got_action') else ''}"
              f"{'  [JSON✗]' if r['json_fail'] else ''}  {r['wall']}s")

    results = run(args.models, think_for=think_for, on_progress=progress)
    summary = score(results)
    print()
    print(format_report(results, summary))
    out = Path(args.out) if args.out else (
        Path(__file__).resolve().parents[1] / "docs" / "internal" /
        f"routing_ab_{datetime.now().strftime('%Y%m%d_%H%M')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"at": datetime.now().isoformat(timespec="seconds"),
                               "models": args.models, "think_off": list(think_for),
                               "results": results, "summary": summary,
                               "verdict": verdict(summary)},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n  저장: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
