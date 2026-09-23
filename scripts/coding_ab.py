"""coding_ab.py — 코딩봇 모델 A/B (같은 과제, 실행으로 채점).

**왜 실행으로 채점하나.** 코드는 "그럴듯해 보이는가"가 아니라 **도는가**로
판정한다. 사람이 눈으로 보면 긴 답·자신 있는 말투에 점수를 준다.
여기서는 모델이 낸 함수를 격리 프로세스에서 실행하고 미리 박아둔
assert로 통과/실패만 센다.

**과제와 채점 기준은 재기 전에 고정한다**(이 파일이 그 기록이다).
결과를 본 뒤 과제를 더하거나 빼면 유리한 쪽이 반드시 나온다.

**이 A/B의 크기에 대해 정직하게.** 과제 10개면 통과 수 차이가 2건
이하일 때 우연과 구분되지 않는다(n=10 이항). 이 도구는 「새 모델이
명백히 나쁘지 않은가」를 보는 것이지 「더 낫다」를 증명하는 크기가 아니다.
보고서가 그 말을 스스로 한다.

봇과 같은 조건으로 부른다: `/api/chat`, temperature 0.2, num_ctx 16384.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

OLLAMA_URL = "http://127.0.0.1:11434"
NUM_CTX = 16384            # coding_bot._call_qwen과 같다
TEMPERATURE = 0.2
TIMEOUT_S = 600
EXEC_TIMEOUT_S = 10
SAME_AS_CHANCE = 2         # 통과 수 차이가 이 이하면 「구분 불가」

SYSTEM = ("You are a Python coding assistant. Reply with ONE python code block "
          "containing only the requested function (plus imports). No prose, no tests.")

# ─── 과제 (고정) ─────────────────────────────────────
# (이름, 지시, 검증 코드). 검증 코드는 모델이 낸 코드 뒤에 붙어 실행된다.
# 전부 표준 라이브러리만 쓴다 — 환경 차이로 틀리는 일을 막는다.

TASKS = [
    ("파싱: 종목코드 추출",
     "Write `def tickers(text: str) -> list[str]` returning all 6-digit Korean stock "
     "codes found in text, in order, without duplicates.",
     "assert tickers('삼성전자(005930)와 SK하이닉스(000660), 다시 005930') == ['005930','000660']\n"
     "assert tickers('no codes 12345 1234567') == []\n"),
    ("날짜: 다음 거래일",
     "Write `def next_weekday(d: str) -> str` taking 'YYYY-MM-DD' and returning the next "
     "calendar day that is Monday-Friday, same format. Ignore holidays.",
     "assert next_weekday('2026-09-04') == '2026-09-07'\n"
     "assert next_weekday('2026-09-02') == '2026-09-03'\n"
     "assert next_weekday('2026-12-31') == '2027-01-01'\n"),
    ("수치: 최대 낙폭",
     "Write `def max_drawdown(values: list[float]) -> float` returning the maximum "
     "peak-to-trough drawdown as a fraction (0.25 for 25%). Empty or single → 0.0.",
     "assert abs(max_drawdown([100,120,60,90]) - 0.5) < 1e-9\n"
     "assert max_drawdown([1,2,3]) == 0.0\n"
     "assert max_drawdown([]) == 0.0\n"),
    ("버그 수정: 이동평균",
     "This function is wrong. Fix it and return the corrected `moving_average`:\n"
     "```python\ndef moving_average(xs, n):\n    out = []\n    for i in range(len(xs)):\n"
     "        out.append(sum(xs[i:i+n]) / n)\n    return out\n```\n"
     "It must return None for positions where the window is not yet full, and the "
     "window must END at position i (trailing average).",
     "r = moving_average([1,2,3,4], 2)\n"
     "assert r[0] is None and abs(r[1]-1.5)<1e-9 and abs(r[3]-3.5)<1e-9, r\n"
     "assert moving_average([5], 3) == [None]\n"),
    ("문자열: 천 단위 구분",
     "Write `def won(n: int) -> str` formatting an integer with thousands separators "
     "and a trailing '원', e.g. 1234567 → '1,234,567원'. Negative allowed.",
     "assert won(1234567) == '1,234,567원'\n"
     "assert won(0) == '0원'\n"
     "assert won(-1500) == '-1,500원'\n"),
    ("자료구조: 상위 N",
     "Write `def top_n(rows: list[dict], key: str, n: int) -> list[dict]` returning the "
     "n rows with the largest rows[key], descending, stable for ties. Rows missing the "
     "key are excluded.",
     "rows=[{'t':'a','s':3},{'t':'b','s':9},{'t':'c'},{'t':'d','s':9}]\n"
     "assert [r['t'] for r in top_n(rows,'s',2)] == ['b','d']\n"
     "assert top_n([], 's', 3) == []\n"),
    ("JSONL: 깨진 줄 건너뛰기",
     "Write `def read_jsonl(text: str) -> list[dict]` parsing one JSON object per line, "
     "skipping blank lines and lines that fail to parse, keeping the rest.",
     "t='{\"a\":1}\\n\\n{broken\\n{\"a\":2}\\n'\n"
     "assert read_jsonl(t) == [{'a':1},{'a':2}]\n"),
    ("수치: 수익률 → 누적",
     "Write `def cumulative(returns: list[float]) -> float` compounding daily returns "
     "(0.01 = +1%) into a total return fraction. Empty → 0.0.",
     "assert abs(cumulative([0.1, 0.1]) - 0.21) < 1e-9\n"
     "assert cumulative([]) == 0.0\n"
     "assert abs(cumulative([-0.5, 1.0]) - 0.0) < 1e-9\n"),
    ("정규식: 시각 파싱",
     "Write `def hhmm(s: str) -> tuple[int,int] | None` parsing 'H:MM' or 'HH:MM' "
     "(24h) into (hour, minute), or None if invalid (hour 0-23, minute 0-59).",
     "assert hhmm('9:05') == (9,5)\n"
     "assert hhmm('23:59') == (23,59)\n"
     "assert hhmm('24:00') is None\n"
     "assert hhmm('9:5') is None\n"),
    ("리팩터: 중복 제거",
     "Write `def dedupe(items: list[str]) -> list[str]` that removes duplicates "
     "case-insensitively, keeping the FIRST occurrence's original casing and order.",
     "assert dedupe(['Apple','apple','Banana','APPLE','banana']) == ['Apple','Banana']\n"
     "assert dedupe([]) == []\n"),
]


# ─── 순수 ────────────────────────────────────────────

def extract_code(answer: str) -> str:
    """답에서 파이썬 코드 블록 하나를 꺼낸다(순수). 블록이 없으면 통째로."""
    m = re.search(r"```(?:python|py)?\s*\n(.*?)```", answer or "", re.S)
    return (m.group(1) if m else (answer or "")).strip()


def grade(code: str, check: str, *, timeout: int = EXEC_TIMEOUT_S) -> tuple[bool, str]:
    """격리 프로세스에서 코드+검증을 실행(얇은 I/O). (통과, 사유)."""
    if not code.strip():
        return False, "빈 답"
    src = code + "\n\n# ─── 검증 ───\n" + check
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(src)
        path = fh.name
    try:
        p = subprocess.run([sys.executable, "-I", path], capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"시간 초과 {timeout}s"
    finally:
        Path(path).unlink(missing_ok=True)
    if p.returncode == 0:
        return True, "통과"
    tail = (p.stderr or "").strip().splitlines()
    return False, (tail[-1] if tail else f"exit {p.returncode}")[:120]


def score(results: list[dict]) -> dict:
    """모델별 집계(순수)."""
    out: dict = {}
    for r in results:
        m = out.setdefault(r["model"], {"pass": 0, "n": 0, "wall": 0.0,
                                        "tokens": 0, "eval_s": 0.0})
        m["n"] += 1
        m["pass"] += 1 if r["ok"] else 0
        m["wall"] += r.get("wall", 0.0)
        m["tokens"] += r.get("eval_count") or 0
        m["eval_s"] += (r.get("eval_duration_ns") or 0) / 1e9
    for m in out.values():
        m["tok_s"] = round(m["tokens"] / m["eval_s"], 1) if m["eval_s"] else None
        m["wall_avg"] = round(m["wall"] / m["n"], 1) if m["n"] else None
        # **답 하나의 토큰 수.** 이게 없어서 "느리다"를 "생성이 느리다"로 읽을 뻔했다.
        m["tokens_avg"] = round(m["tokens"] / m["n"]) if m["n"] else None
    return out


def verdict(summary: dict, *, same_as_chance: int = SAME_AS_CHANCE) -> str:
    """**재기 전에 정한 규칙**(순수): 통과 수 차이 ≤ same_as_chance면 구분 불가."""
    if len(summary) != 2:
        return "모델이 둘이 아니다"
    (a, sa), (b, sb) = sorted(summary.items(), key=lambda kv: -kv[1]["pass"])
    diff = sa["pass"] - sb["pass"]
    if diff <= same_as_chance:
        return (f"통과 수 차이 {diff}건 — n={sa['n']}에서는 우연과 구분되지 않는다. "
                f"「{b}가 명백히 나쁘지 않다」까지만 말할 수 있다")
    return f"{a}가 {diff}건 더 통과 — 이 크기에서는 드문 차이(재실행으로 확인할 것)"


def format_report(results: list[dict], summary: dict) -> str:
    lines = ["🧪 코딩봇 A/B — 실행 채점", ""]
    models = list(summary)
    lines.append(f"{'과제':22}" + "".join(f"{m:>22}" for m in models))
    by_task: dict = {}
    for r in results:
        by_task.setdefault(r["task"], {})[r["model"]] = r
    for task, cells in by_task.items():
        row = f"{task:22}"
        for m in models:
            c = cells.get(m)
            row += f"{('○' if c and c['ok'] else '✗') + ' ' + (c['why'][:16] if c else '-'):>22}"
        lines.append(row)
    lines.append("")
    for m, s in summary.items():
        lines.append(f"  {m}: 통과 {s['pass']}/{s['n']} · 평균 {s['wall_avg']}s · "
                     f"{s['tok_s'] if s['tok_s'] is not None else '—'} tok/s · "
                     f"답당 {s.get('tokens_avg') if s.get('tokens_avg') is not None else '—'}토큰")
    toks = [s.get("tokens_avg") for s in summary.values() if s.get("tokens_avg")]
    if len(toks) == 2 and max(toks) >= 5 * min(toks):
        lines.append("  ⚠️ 답 토큰 수가 5배 이상 차이 — 한쪽이 thinking 모드일 가능성. "
                     "`--think off`로 같은 모드에서 다시 재야 속도 비교가 뜻을 가진다.")
    lines.append("")
    lines.append(f"  판정(사전 규칙): {verdict(summary)}")
    lines.append("  _정확도가 먼저, 동률이면 속도. 과제 10개는 「명백히 나쁘지 않은가」를 보는 크기다._")
    return "\n".join(lines)


# ─── I/O ─────────────────────────────────────────────

def ask(model: str, instruction: str, *, think: Optional[bool] = None) -> dict:
    """봇과 같은 조건으로 한 번 묻는다.

    `think`: Qwen3.x 같은 추론 모델은 기본으로 thinking 토큰을 낸다.
    2026-09-04 첫 A/B에서 qwen3.6:27b가 답 하나에 ~2,800토큰(32b는 ~76)을
    내며 평균 117초가 걸렸다 — tok/s는 오히려 더 빨랐다(24.1 vs 13.6).
    **다른 생성 모드를 같은 조건인 척 비교한 것**이다. False면 끈다.
    """
    from urllib.request import Request, urlopen

    payload = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": instruction}],
        "stream": False,
        "options": {"temperature": TEMPERATURE, "num_ctx": NUM_CTX},
    }
    if think is not None:
        payload["think"] = think
    body = json.dumps(payload).encode("utf-8")
    req = Request(f"{OLLAMA_URL}/api/chat", data=body,
                  headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urlopen(req, timeout=TIMEOUT_S) as r:
        resp = json.loads(r.read().decode("utf-8"))
    return {"answer": (resp.get("message") or {}).get("content") or "",
            "wall": round(time.time() - t0, 1),
            "eval_count": resp.get("eval_count"),
            "eval_duration_ns": resp.get("eval_duration")}


def run(models: list[str], *, runs: int = 1, tasks=TASKS, on_progress=None,
        think: Optional[bool] = None) -> list[dict]:
    results = []
    for model in models:
        for name, instruction, check in tasks:
            for k in range(runs):
                try:
                    got = ask(model, instruction, think=think)
                    ok, why = grade(extract_code(got["answer"]), check)
                    rec = {"model": model, "task": name, "run": k, "ok": ok, "why": why,
                           **{key: got[key] for key in ("wall", "eval_count", "eval_duration_ns")}}
                except Exception as e:                       # noqa: BLE001
                    rec = {"model": model, "task": name, "run": k, "ok": False,
                           "why": f"호출 실패: {e}"[:120], "wall": 0.0,
                           "eval_count": None, "eval_duration_ns": None}
                results.append(rec)
                if on_progress:
                    on_progress(rec)
    return results


def _cli() -> int:
    import argparse
    from datetime import datetime

    ap = argparse.ArgumentParser(description="코딩봇 모델 A/B (실행 채점)")
    ap.add_argument("--models", nargs="+", default=["qwen2.5-coder:32b", "qwen3.6:27b"])
    ap.add_argument("--runs", type=int, default=1, help="과제당 반복(기본 1)")
    ap.add_argument("--out", default=None, help="결과 JSON 저장 경로")
    ap.add_argument("--think", choices=("auto", "off", "on"), default="auto",
                    help="추론(thinking) 토큰: auto=모델 기본, off=끔, on=켬")
    args = ap.parse_args()
    think = {"auto": None, "off": False, "on": True}[args.think]

    print(f"  모델 {args.models} · 과제 {len(TASKS)}개 · 반복 {args.runs} · "
          f"조건 temp={TEMPERATURE} num_ctx={NUM_CTX} · thinking={args.think}")
    print()

    def progress(rec):
        print(f"  {'○' if rec['ok'] else '✗'} {rec['model']:20} {rec['task']:18} "
              f"{rec['wall']:>6}s  {rec['why']}")

    results = run(args.models, runs=args.runs, on_progress=progress, think=think)
    summary = score(results)
    print()
    print(format_report(results, summary))
    out = Path(args.out) if args.out else (
        Path(__file__).resolve().parents[1] / "docs" / "internal" /
        f"coding_ab_{datetime.now().strftime('%Y%m%d_%H%M')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"at": datetime.now().isoformat(timespec="seconds"),
                               "models": args.models, "runs": args.runs,
                               "think": args.think,
                               "results": results, "summary": summary,
                               "verdict": verdict(summary)},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n  저장: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
