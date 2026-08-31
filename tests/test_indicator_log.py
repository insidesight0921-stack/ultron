"""test_indicator_log.py — 대리 지표 시계열 적재 (hermetic, 네트워크 없음).

2026-08-31 점검: 나우캐스팅 가중치를 정하려면 "지표가 국면을 앞서 맞히는가"를
재야 하는데 **지표 시계열이 0**이었다. `proxy_indicators`는 스냅샷만 만들고
저장하지 않았다. 시계가 아예 안 돌고 있었다.
"""
from __future__ import annotations

import json

import indicator_log as il


def _snap(**vals):
    names = ["코스피 200일선 기울기", "외국인 순매수(5일)", "원/달러 환율", "VIX"]
    items = [{"name": n, "value": vals.get(n), "state": "unknown", "unit": "",
              "as_of": vals.get(f"{n}__as_of"), "source": "t",
              "available": vals.get(n) is not None} for n in names]
    got = sum(1 for i in items if i["value"] is not None)
    return {"indicators": items,
            "summary": {"n_available": got, "n_total": len(items),
                        "lean": "중립",
                        "missing": [i["name"] for i in items if i["value"] is None]},
            "missing_env": []}


# ─── 결측을 지우지 않는다 ────────────────────────────


def test_unavailable_indicators_are_recorded_not_dropped():
    """값 없는 날을 건너뛰면 표본이 '수집 성공한 날'로 치우친다.

    VIX가 시장이 요동칠 때만 실패한다면 그 편향이 결론을 뒤집는다.
    """
    row = il.row_from_snapshot(_snap(**{"VIX": None, "코스피 200일선 기울기": 4.8}),
                               "20260831")
    names = [i["name"] for i in row["indicators"]]
    assert len(names) == 4                       # 하나도 빠지지 않는다
    vix = next(i for i in row["indicators"] if i["name"] == "VIX")
    assert vix["value"] is None and vix["available"] is False


def test_the_missing_list_is_kept():
    row = il.row_from_snapshot(_snap(VIX=None), "20260831")
    assert "VIX" in row["missing"]


def test_a_series_keeps_the_gaps():
    """결측을 지우면 연속처럼 보인다."""
    rows = [il.row_from_snapshot(_snap(VIX=20.0), "20260830"),
            il.row_from_snapshot(_snap(VIX=None), "20260831")]
    s = il.series(rows, "VIX")
    assert [v for _, v, _ in s] == [20.0, None]


# ─── as_of와 관측일은 다르다 ─────────────────────────


def test_the_indicator_date_is_kept_apart_from_the_observation_date():
    """오늘 받은 값이 3일 전 기준일 수 있다. 합치면 오래된 값이 오늘 값처럼 보인다
    — 2026-08-31 탭 E 커버리지 18% 사고가 정확히 그 실수였다."""
    row = il.row_from_snapshot(
        _snap(**{"VIX": 20.0, "VIX__as_of": "20260828"}), "20260831")
    vix = next(i for i in row["indicators"] if i["name"] == "VIX")
    assert row["day"] == "20260831" and vix["as_of"] == "20260828"


def test_coverage_counts_stale_readings():
    rows = [il.row_from_snapshot(
        _snap(**{"VIX": 20.0, "VIX__as_of": "20260828"}), "20260831")]
    cov = il.coverage(rows)
    assert cov["by_indicator"]["VIX"]["stale"] == 1


def test_a_same_day_reading_is_not_stale():
    rows = [il.row_from_snapshot(
        _snap(**{"VIX": 20.0, "VIX__as_of": "20260831"}), "20260831")]
    assert il.coverage(rows)["by_indicator"]["VIX"]["stale"] == 0


# ─── 하루 한 줄, 덧붙이기만 ──────────────────────────


def test_a_second_write_for_the_same_day_is_skipped(tmp_path):
    """봇이 하루에 여러 번 재시작해도 하루 한 줄이어야 한다."""
    p = tmp_path / "log.jsonl"
    assert il.append(il.row_from_snapshot(_snap(VIX=20.0), "20260831"), p) is True
    assert il.append(il.row_from_snapshot(_snap(VIX=21.0), "20260831"), p) is False
    rows = il.load(p)
    assert len(rows) == 1
    assert il.series(rows, "VIX")[0][1] == 20.0      # 덮어쓰지 않는다


def test_history_is_append_only(tmp_path):
    p = tmp_path / "log.jsonl"
    for day, v in (("20260830", 19.0), ("20260831", 20.0)):
        il.append(il.row_from_snapshot(_snap(VIX=v), day), p)
    assert [r["day"] for r in il.load(p)] == ["20260830", "20260831"]


def test_record_does_not_call_the_source_twice(tmp_path):
    """이미 기록된 날이면 지표 조회 자체를 하지 않는다(불필요한 API 호출 방지)."""
    p = tmp_path / "log.jsonl"
    calls = []

    def fake():
        calls.append(1)
        return _snap(VIX=20.0)

    assert il.record("20260831", snapshot_fn=fake, path=p) is not None
    assert il.record("20260831", snapshot_fn=fake, path=p) is None
    assert len(calls) == 1


# ─── 읽기 견고성 ─────────────────────────────────────


def test_a_broken_line_does_not_lose_the_rest(tmp_path):
    p = tmp_path / "log.jsonl"
    p.write_text('{"day":"20260830","indicators":[]}\n깨진줄\n'
                 '{"day":"20260831","indicators":[]}\n', encoding="utf-8")
    assert [r["day"] for r in il.load(p)] == ["20260830", "20260831"]


def test_a_missing_file_is_empty_not_an_error(tmp_path):
    assert il.load(tmp_path / "nope.jsonl") == []


def test_rows_without_a_day_are_dropped():
    assert il.parse_jsonl(['{"indicators":[]}', '{"day":"20260831"}']) == \
        [{"day": "20260831"}]


# ─── 표시 ────────────────────────────────────────────


def test_the_report_says_weights_are_not_being_set():
    """근거 없는 가중치를 만드는 것이 이 프로젝트에서 반복된 실패다."""
    rows = [il.row_from_snapshot(_snap(VIX=20.0), "20260831")]
    text = il.format_coverage(il.coverage(rows))
    assert "가중치는 아직 정하지 않습니다" in text


def test_an_empty_log_says_so():
    assert "아직 기록이 없습니다" in il.format_coverage(il.coverage([]))


def test_a_low_coverage_indicator_is_marked():
    rows = [il.row_from_snapshot(_snap(VIX=None), f"2026083{i}") for i in range(2)]
    text = il.format_coverage(il.coverage(rows))
    assert "❌" in text and "VIX" in text


# ─── 배선 (소스 검사) ────────────────────────────────

import ast
import io
import tokenize
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
BOT = (SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8")


def _func(name: str) -> str:
    tree = ast.parse(BOT)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(BOT, node) or ""
    raise AssertionError(f"{name} 없음")


def test_the_job_is_registered():
    assert 'name="indicator_log"' in BOT and "indicator_log_job," in BOT


def test_record_is_called_with_keyword_arguments():
    """`record`는 키워드 전용 인자다 — 위치로 넘기면 실행 시점에 TypeError가 난다.
    (처음에 그렇게 짰다가 잡혔다.)"""
    body = _func("indicator_log_job")
    assert "recorded_at=" in body
    assert "_il.record, day, None" not in body


def test_the_job_waits_until_after_the_close():
    """장중에 찍으면 같은 날 값이 시각에 따라 달라진다."""
    code = " ".join(t.string for t in
                    tokenize.generate_tokens(io.StringIO(_func("indicator_log_job")).readline)
                    if t.type not in (tokenize.COMMENT, tokenize.STRING))
    assert "INDICATOR_LOG_HOUR" in code and "weekday" in code


def test_missing_indicators_are_warned_not_swallowed():
    """특정 지표가 계속 비면 가중치를 줄 수 없다 — 나중이 아니라 지금 알아야 한다."""
    assert "미확보" in _func("indicator_log_job")


def test_the_job_does_not_compute_any_weight():
    """근거 없는 숫자를 하나 더 만드는 것이 이 프로젝트에서 반복된 실패다.

    **주석·독스트링이 아니라 실행 코드를 본다** — 설명문에 '가중치'라는 낱말이
    나오는 것은 당연하고, 문자열로 세면 거기 걸린다(오늘 세 번째다).
    """
    code = " ".join(
        t.string for t in
        tokenize.generate_tokens(io.StringIO(_func("indicator_log_job")).readline)
        if t.type not in (tokenize.COMMENT, tokenize.STRING))
    for forbidden in ("weight", "가중"):
        assert forbidden not in code, forbidden
