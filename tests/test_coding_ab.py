"""test_coding_ab.py — 코딩봇 A/B 채점기.

채점기 자체를 먼저 검증한다: **정답이 통과하고 오답이 떨어지는가.**
assert가 틀려 있으면 모델 탓이 된다 — 검증 코드도 검증 대상이다.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import coding_ab as ab  # noqa: E402

REFERENCE = {
    "파싱: 종목코드 추출": '''
import re
def tickers(text):
    out = []
    for m in re.findall(r"(?<!\\d)\\d{6}(?!\\d)", text):
        if m not in out:
            out.append(m)
    return out
''',
    "날짜: 다음 거래일": '''
from datetime import date, timedelta
def next_weekday(d):
    x = date.fromisoformat(d) + timedelta(days=1)
    while x.weekday() >= 5:
        x += timedelta(days=1)
    return x.isoformat()
''',
    "수치: 최대 낙폭": '''
def max_drawdown(values):
    if len(values) < 2:
        return 0.0
    peak, worst = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        worst = max(worst, (peak - v) / peak)
    return worst
''',
    "버그 수정: 이동평균": '''
def moving_average(xs, n):
    out = []
    for i in range(len(xs)):
        if i + 1 < n:
            out.append(None)
        else:
            out.append(sum(xs[i + 1 - n:i + 1]) / n)
    return out
''',
    "문자열: 천 단위 구분": '''
def won(n):
    return f"{n:,}원"
''',
    "자료구조: 상위 N": '''
def top_n(rows, key, n):
    have = [r for r in rows if key in r]
    return sorted(have, key=lambda r: -r[key])[:n]
''',
    "JSONL: 깨진 줄 건너뛰기": '''
import json
def read_jsonl(text):
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
''',
    "수치: 수익률 → 누적": '''
def cumulative(returns):
    v = 1.0
    for r in returns:
        v *= (1 + r)
    return v - 1.0 if returns else 0.0
''',
    "정규식: 시각 파싱": '''
import re
def hhmm(s):
    m = re.fullmatch(r"(\\d{1,2}):(\\d{2})", s)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return (h, mi) if 0 <= h <= 23 and 0 <= mi <= 59 else None
''',
    "리팩터: 중복 제거": '''
def dedupe(items):
    seen, out = set(), []
    for it in items:
        k = it.lower()
        if k not in seen:
            seen.add(k)
            out.append(it)
    return out
''',
}


def test_every_task_has_a_reference_solution():
    """과제를 더하면 정답도 더해야 한다 — 채점기가 검증되지 않은 과제를 두지 않는다."""
    assert {t[0] for t in ab.TASKS} == set(REFERENCE)


def test_the_reference_passes_every_check():
    """**assert가 틀려 있으면 모델 탓이 된다.**"""
    for name, _, check in ab.TASKS:
        ok, why = ab.grade(REFERENCE[name], check)
        assert ok, f"{name}: {why}"


def test_a_wrong_answer_fails():
    """음성 대조 — 그럴듯하지만 틀린 답이 떨어지는가."""
    wrong = {
        "수치: 최대 낙폭": "def max_drawdown(values):\n    return 0.5\n",
        "문자열: 천 단위 구분": "def won(n):\n    return str(n) + '원'\n",
        "리팩터: 중복 제거": "def dedupe(items):\n    return list(dict.fromkeys(items))\n",
        "버그 수정: 이동평균": ("def moving_average(xs, n):\n    return [sum(xs[i:i+n])/n "
                          "for i in range(len(xs))]\n"),
    }
    for name, _, check in ab.TASKS:
        if name in wrong:
            ok, _ = ab.grade(wrong[name], check)
            assert not ok, name


def test_an_empty_or_crashing_answer_fails_with_a_reason():
    ok, why = ab.grade("", "assert True")
    assert not ok and why == "빈 답"
    ok, why = ab.grade("def f(): pass", "assert f() == 1")
    assert not ok and "AssertionError" in why


def test_an_infinite_loop_is_cut_off():
    ok, why = ab.grade("while True: pass", "assert True", timeout=1)
    assert not ok and "시간 초과" in why


def test_code_block_extraction():
    assert ab.extract_code("here\n```python\nx = 1\n```\nbye") == "x = 1"
    assert ab.extract_code("```\nx = 2\n```") == "x = 2"
    assert ab.extract_code("x = 3") == "x = 3"


def test_score_and_verdict_rules():
    rows = ([{"model": "A", "task": f"t{i}", "ok": i < 7, "wall": 10.0,
              "eval_count": 100, "eval_duration_ns": 2e9} for i in range(10)]
            + [{"model": "B", "task": f"t{i}", "ok": i < 6, "wall": 5.0,
                "eval_count": 100, "eval_duration_ns": 1e9} for i in range(10)])
    s = ab.score(rows)
    assert s["A"]["pass"] == 7 and s["B"]["pass"] == 6
    assert s["B"]["tok_s"] == 100.0
    assert "구분되지 않는다" in ab.verdict(s)          # 차이 1건


def test_a_large_gap_is_named_but_asked_to_rerun():
    rows = ([{"model": "A", "task": f"t{i}", "ok": True, "wall": 1.0} for i in range(10)]
            + [{"model": "B", "task": f"t{i}", "ok": i < 5, "wall": 1.0} for i in range(10)])
    v = ab.verdict(ab.score(rows))
    assert "A가 5건 더 통과" in v and "재실행" in v


def test_the_report_says_what_the_size_can_and_cannot_tell():
    rows = ([{"model": "A", "task": "t", "ok": True, "why": "통과", "wall": 1.0},
             {"model": "B", "task": "t", "ok": False, "why": "AssertionError", "wall": 1.0}])
    msg = ab.format_report(rows, ab.score(rows))
    assert "명백히 나쁘지 않은가" in msg


def test_the_conditions_match_the_bot():
    """봇과 다른 조건으로 재면 다른 것을 재는 것이다."""
    import coding_bot
    assert ab.NUM_CTX == 16384
    assert ab.TEMPERATURE == 0.2
    assert coding_bot.QWEN_MODEL in ("qwen2.5-coder:32b", coding_bot.QWEN_MODEL)


def test_a_thinking_gap_is_flagged_in_the_report():
    """**tok/s가 더 빠른데 21배 느렸다** — 답 토큰이 37배였다(2026-09-04).
    이 숫자가 보고서에 없으면 「생성이 느리다」로 오독한다."""
    rows = ([{"model": "A", "task": "t", "ok": True, "why": "통과", "wall": 5.6,
              "eval_count": 76, "eval_duration_ns": 5.6e9},
             {"model": "B", "task": "t", "ok": True, "why": "통과", "wall": 117.7,
              "eval_count": 2837, "eval_duration_ns": 117.7e9}])
    s = ab.score(rows)
    assert s["A"]["tokens_avg"] == 76 and s["B"]["tokens_avg"] == 2837
    msg = ab.format_report(rows, s)
    assert "thinking 모드일 가능성" in msg and "--think off" in msg


def test_similar_token_counts_do_not_trigger_the_flag():
    rows = ([{"model": "A", "task": "t", "ok": True, "why": "통과", "wall": 5.0,
              "eval_count": 80, "eval_duration_ns": 5e9},
             {"model": "B", "task": "t", "ok": True, "why": "통과", "wall": 6.0,
              "eval_count": 120, "eval_duration_ns": 6e9}])
    assert "thinking" not in ab.format_report(rows, ab.score(rows))


def test_think_flag_reaches_the_payload(monkeypatch):
    seen = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"message":{"content":"```python\\nx=1\\n```"},"eval_count":3,"eval_duration":1000}'

    def fake_urlopen(req, timeout=0):
        seen["body"] = __import__("json").loads(req.data.decode("utf-8"))
        return _Resp()
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    ab.ask("m", "do", think=False)
    assert seen["body"]["think"] is False
    ab.ask("m", "do")
    assert "think" not in seen["body"]
