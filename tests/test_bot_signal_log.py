"""test_bot_signal_log.py — 봇 추천 기록.

2026-09-02 실측: 키움봇은 txt/html 리포트만, 콴텍봇은 chat_id만 남겼다.
**봇 신호 정확도는 잴 원자료가 없었다.** 여기서 시계를 켠다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bot_signal_log as bsl  # noqa: E402


def _ranked(n=20):
    return [{"ticker": f"{i:06d}", "name": f"종목{i}", "score": 100 - i,
             "current_price": 1000 + i} for i in range(1, n + 1)]


# ─── 선정만 남기면 선정을 평가할 수 없다 ────────────────

def test_the_control_group_is_recorded_too():
    """**Top 10만 남기면 「고른 것이 값을 더했나」를 영영 못 묻는다.**"""
    rows = bsl.build_records("kium", _ranked(30), at="2026-09-02T16:40:00", top_n=5)
    kinds = [r["kind"] for r in rows]
    assert kinds.count(bsl.SELECTED) == 5
    assert kinds.count(bsl.CONTROL) == 5
    assert rows[0]["rank"] == 1 and rows[-1]["rank"] == 10


def test_the_control_size_is_fixed_in_advance():
    """나중에 경계를 고르면 유리한 경계가 반드시 나온다."""
    assert bsl.control_size(10) == 10
    assert bsl.control_size(10, 2) == 20
    assert bsl.control_size(0) == 0


def test_a_short_ranking_records_what_exists():
    rows = bsl.build_records("kium", _ranked(7), at="t", top_n=5)
    assert len(rows) == 7 and rows[-1]["kind"] == bsl.CONTROL


def test_the_universe_size_is_kept():
    """대조군이 전체의 어디쯤인지 모르면 나중에 해석이 안 된다."""
    rows = bsl.build_records("kium", _ranked(300), at="t", top_n=10)
    assert rows[0]["universe_n"] == 300


def test_a_row_without_a_ticker_is_skipped():
    rows = bsl.build_records("kium", [{"name": "이름만"}], at="t", top_n=5)
    assert rows == []


# ─── 신호 시점 값 ─────────────────────────────────────

def test_the_price_at_signal_time_is_recorded():
    """나중에 조회한 가격으로 기준을 삼으면 그날을 재현할 수 없다."""
    rows = bsl.build_records("kium", _ranked(3), at="t", top_n=2)
    assert rows[0]["price"] == 1001 and rows[0]["score"] == 99


def test_a_dataclass_like_record_also_works():
    class R:
        ticker, name, composite_score, current_price = "005930", "삼성전자", 1.5, 70000
    rows = bsl.build_records("quant", [R()], at="t", top_n=1,
                             score_of=lambda r: r.composite_score)
    assert rows[0]["score"] == 1.5 and rows[0]["ticker"] == "005930"


def test_extra_fields_ride_along():
    rows = bsl.build_records("quant", _ranked(2), at="t", top_n=1, phase="Expansion",
                             extra_of=lambda r: {"주도": "Momentum"})
    assert rows[0]["phase"] == "Expansion"
    assert rows[0]["extra"]["주도"] == "Momentum"


def test_none_common_fields_are_not_written():
    """모르는 것을 빈 값으로 채우면 그 빈 값이 하나의 범주가 된다."""
    rows = bsl.build_records("kium", _ranked(2), at="t", top_n=1, phase=None)
    assert "phase" not in rows[0]


# ─── 같은 판단을 두 번 적지 않는다 ──────────────────────

def test_rerunning_the_same_day_does_not_double_the_log(tmp_path):
    p = tmp_path / "b.jsonl"
    rows = bsl.build_records("kium", _ranked(6), at="2026-09-02T16:40:00", top_n=3)
    assert bsl.append_records(rows, p) == 6
    assert bsl.append_records(rows, p) == 0
    assert len(bsl.load_records(p)) == 6


def test_a_different_day_appends(tmp_path):
    p = tmp_path / "b.jsonl"
    bsl.append_records(bsl.build_records("kium", _ranked(4), at="2026-09-02T16:40", top_n=2), p)
    bsl.append_records(bsl.build_records("kium", _ranked(4), at="2026-09-09T16:40", top_n=2), p)
    assert len(bsl.load_records(p)) == 8


def test_two_bots_on_one_day_do_not_collide(tmp_path):
    p = tmp_path / "b.jsonl"
    bsl.append_records(bsl.build_records("kium", _ranked(2), at="2026-09-02", top_n=1), p)
    bsl.append_records(bsl.build_records("quant", _ranked(2), at="2026-09-02", top_n=1), p)
    assert len({r["bot"] for r in bsl.load_records(p)}) == 2


def test_merging_never_edits_existing_rows():
    old = [{"at": "2026-09-02", "bot": "kium", "ticker": "A", "score": 1}]
    new = [{"at": "2026-09-02", "bot": "kium", "ticker": "A", "score": 999}]
    assert bsl.merge_records(old, new)[0]["score"] == 1


# ─── 기록 실패가 봇을 멈추면 안 된다 ────────────────────

def test_a_broken_line_does_not_lose_the_rest(tmp_path):
    p = tmp_path / "b.jsonl"
    p.write_text('{"at":"1","bot":"kium","ticker":"A"}\n{망가진\n'
                 '{"at":"1","bot":"kium","ticker":"B"}\n', encoding="utf-8")
    assert len(bsl.load_records(p)) == 2


def test_an_unwritable_path_is_swallowed(tmp_path):
    bad = tmp_path / "없는폴더" / "x" / "b.jsonl"
    bad.parent.mkdir(parents=True)
    bad.parent.chmod(0o500)
    try:
        assert bsl.append_records([{"at": "1", "bot": "k", "ticker": "A"}], bad) == 0
    finally:
        bad.parent.chmod(0o700)


def test_an_empty_batch_is_not_an_error(tmp_path):
    assert bsl.append_records([], tmp_path / "b.jsonl") == 0


def test_a_missing_file_reads_as_empty(tmp_path):
    assert bsl.load_records(tmp_path / "없음.jsonl") == []


# ─── 표본을 먼저 보게 한다 ────────────────────────────

def test_the_summary_counts_by_bot_and_day():
    rows = (bsl.build_records("kium", _ranked(4), at="2026-09-02", top_n=2)
            + bsl.build_records("kium", _ranked(4), at="2026-09-09", top_n=2)
            + bsl.build_records("quant", _ranked(2), at="2026-09-02", top_n=1))
    got = bsl.summarize_records(rows)
    assert got["kium"] == {"선정": 4, "대조": 4, "days": 2}
    assert got["quant"]["days"] == 1


def test_the_vocabulary_is_fixed():
    """사후에 종류를 늘리면 우연히 좋아 보이는 조합이 나온다."""
    assert bsl.KINDS == ("선정", "대조")


# ─── 배선이 조용히 끊기지 않게 (AST) ───────────────────
#
# 2026-09-01 봇 부팅 크래시, 2026-09-02 CLI NameError — 둘 다 테스트가
# 실행 경로를 타지 않아 통과한 채로 죽었다. 여기서는 **호출부에 인자가
# 실제로 붙어 있는지**를 실행 없이 본다.

import ast

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _call_name(node: ast.Call):
    return (node.func.attr if isinstance(node.func, ast.Attribute)
            else getattr(node.func, "id", None))


def _calls_with_kw(path: Path, func_name: str, kw: str) -> list[bool]:
    """`f(...)`와 **`to_thread(f, ...)` 형태를 모두 본다.**

    첫 판본은 직접 호출만 봤다. 실제 코드는
    `asyncio.to_thread(quant_recommend, phase, log_path=...)`라 함수 이름이
    **인자 자리**에 있었고, 검사는 호출을 하나도 못 찾은 채 실패했다 —
    2026-09-02 guard 검사가 `_cli()` 안을 못 본 것과 같은 계열이다.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name == func_name:
            out.append(any(k.arg == kw for k in node.keywords))
        elif (name in ("to_thread", "run_in_executor", "partial")
              and node.args
              and getattr(node.args[0], "id", None) == func_name):
            out.append(any(k.arg == kw for k in node.keywords))
    return out


def test_the_weekly_kium_scan_records_its_judgment():
    """**주간 스캔은 봇의 진짜 판단이다.** 여기서 log_path가 빠지면
    아무도 모르게 표본이 끊긴다."""
    got = _calls_with_kw(SCRIPTS / "weekly_kium_scan.py", "scan_universe", "log_path")
    assert got and all(got), "주간 스캔이 log_path 없이 scan_universe를 부른다"


def test_the_monthly_rebalance_records_its_recommendation():
    got = _calls_with_kw(SCRIPTS / "telegram_bot.py", "quant_recommend", "log_path")
    assert got and all(got), "월간 리밸런싱이 log_path 없이 추천을 부른다"


def test_the_ad_hoc_ui_scan_does_not_record():
    """화면에서 사람이 눌러 보는 스캔은 top_n이 그때그때 달라
    봇의 판단으로 오해된다 — 남기지 않는다."""
    got = _calls_with_kw(SCRIPTS / "paper_ui.py", "scan_universe", "log_path")
    assert not any(got), "임시 UI 스캔이 봇 판단으로 기록되고 있다"


def test_both_bots_can_take_a_log_path():
    """서명에서 인자가 사라지면 호출부만 고쳐도 조용히 깨진다."""
    for mod, func in (("kium_bot.py", "scan_universe"),
                      ("quant_bot.py", "recommend_top_n")):
        tree = ast.parse((SCRIPTS / mod).read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == func)
        names = [a.arg for a in fn.args.args + fn.args.kwonlyargs]
        assert "log_path" in names, f"{mod}:{func}"


def test_a_different_top_n_on_the_same_day_is_a_different_judgment(tmp_path):
    """**판단 조건이 다르면 다른 판단이다.** 같은 조건의 재실행만 한 줄로."""
    p = tmp_path / "b.jsonl"
    bsl.append_records(bsl.build_records("kium", _ranked(6), at="2026-09-02T16:40", top_n=3), p)
    n = bsl.append_records(bsl.build_records("kium", _ranked(6), at="2026-09-02T18:00", top_n=5), p)
    assert n > 0
    assert len({r["top_n"] for r in bsl.load_records(p)}) == 2


def test_a_different_phase_on_the_same_day_is_a_different_judgment(tmp_path):
    p = tmp_path / "b.jsonl"
    bsl.append_records(bsl.build_records("quant", _ranked(2), at="2026-09-02", top_n=1,
                                         phase="Expansion"), p)
    n = bsl.append_records(bsl.build_records("quant", _ranked(2), at="2026-09-02", top_n=1,
                                             phase="Contraction"), p)
    assert n == 2
