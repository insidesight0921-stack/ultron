"""test_failure_analysis.py — 실패 원인 분석 (탭 D, hermetic·순수 함수만).

이 화면의 실패 모드는 **이야기가 그럴듯한 것**이다. 63건짜리 표본에서 축을 넷만
잡으면 "어느 국면에서 크게 잃었다" 같은 문장은 반드시 나오고, 대개 사실이 아니다.
그래서 테스트는 "값을 잘 내는가"보다 **없는 원인을 지목하지 않는가**를 본다.
"""
from __future__ import annotations

import failure_analysis as fa


def _rt(ret, label="A", pnl=None, **kw):
    base = {"ret": ret, "pnl": pnl if pnl is not None else ret * 1_000_000,
            "label": label, "slot": "키움", "reason": "손절",
            "name": "x", "ticker": "000000"}
    base.update(kw)
    return base


def _by_label(r):
    return r["label"]


# ─── 라벨 유의성 ─────────────────────────────────────


def test_a_pure_noise_split_is_called_chance():
    """같은 분포를 둘로 자른 것이 '유의'로 나오면 이 도구는 해롭다."""
    rows = [_rt((i % 7 - 3) / 100, "A" if i % 2 else "B") for i in range(60)]
    cells = fa.label_cells(rows, _by_label, trials=400)
    assert all(c["verdict"] == "우연 범위" for c in cells), cells


def test_a_real_separation_is_detected():
    """진짜 차이까지 '우연'이라 하면 검정이 죽은 것이다."""
    rows = ([_rt(0.20, "좋음") for _ in range(20)]
            + [_rt(-0.20, "나쁨") for _ in range(20)])
    cells = {c["label"]: c for c in fa.label_cells(rows, _by_label, trials=400)}
    assert cells["좋음"]["verdict"] == "유의하게 좋음"
    assert cells["나쁨"]["verdict"] == "유의하게 나쁨"


def test_a_tiny_cell_is_not_judged():
    """3건짜리 칸의 평균은 무엇이든 나온다 — 판정하지 않는 것이 맞다."""
    rows = [_rt(0.5, "작음")] * 3 + [_rt(-0.01, "큼") for _ in range(40)]
    cells = {c["label"]: c for c in fa.label_cells(rows, _by_label, trials=200)}
    assert cells["작음"]["verdict"] == "표본 부족"
    assert cells["작음"]["percentile"] is None


def test_a_small_cell_of_ordinary_rows_is_not_called_special():
    """칸 크기를 무시하고 비교하면 작은 칸이 항상 '특별해' 보인다.

    수익률 패턴과 라벨을 **독립적으로** 만든다(라벨이 값과 얽히면 그건 진짜
    차이지 크기 편향이 아니다 — 이 테스트를 처음 썼을 때 그 실수를 했다).
    """
    vals = [(i % 7 - 3) / 100 for i in range(80)]
    rows = [_rt(v, "작음" if k in (3, 17, 41, 58, 62, 70, 71) else "큼")
            for k, v in enumerate(vals)]
    cells = {c["label"]: c for c in fa.label_cells(rows, _by_label, trials=800)}
    assert cells["작음"]["n"] == 7 and cells["큼"]["n"] == 73
    assert cells["작음"]["verdict"] == "우연 범위", cells["작음"]


def test_the_result_is_deterministic():
    rows = [_rt((i % 7 - 3) / 100, "A" if i % 3 else "B") for i in range(50)]
    assert (fa.label_cells(rows, _by_label, trials=200)
            == fa.label_cells(rows, _by_label, trials=200))


def test_an_empty_sample_yields_no_cells():
    assert fa.label_cells([], _by_label) == []


# ─── 축의 성격 ───────────────────────────────────────


def test_a_tautological_axis_is_not_reported_as_a_finding():
    """손절은 정의상 손실이다. 이것을 '발견'으로 내면 화면이 거짓말을 한다."""
    rows = ([_rt(-0.15, reason="손절") for _ in range(20)]
            + [_rt(0.20, reason="익절") for _ in range(20)])
    f = fa.findings(rows, axes={"청산사유": ("tauto", lambda r: r["reason"])})
    assert f["signals"] == []
    assert {c["label"] for c in f["controls"]} == {"손절", "익절"}


def test_a_time_axis_is_not_reported_as_a_finding():
    """시간축은 라벨을 섞으면 시간 뭉침이 깨져 무엇이든 유의해진다."""
    rows = ([_rt(-0.15, "하락장") for _ in range(20)]
            + [_rt(0.20, "상승장") for _ in range(20)])
    f = fa.findings(rows, axes={"국면": ("time", _by_label)})
    assert f["signals"] == []
    assert f["controls"] and all(c["kind"] == "time" for c in f["controls"])


def test_a_cross_sectional_axis_can_be_a_finding():
    rows = ([_rt(-0.20, "반도체") for _ in range(20)]
            + [_rt(0.20, "건설") for _ in range(20)])
    f = fa.findings(rows, axes={"섹터": ("cross", _by_label)})
    assert {s["label"] for s in f["signals"]} == {"반도체", "건설"}


def test_a_bare_function_defaults_to_cross_sectional():
    rows = [_rt((i % 5 - 2) / 100, "A" if i % 2 else "B") for i in range(40)]
    f = fa.findings(rows, axes={"섹터": _by_label})
    assert f["axis_kinds"]["섹터"] == "cross"


def test_an_unknown_axis_kind_is_refused():
    """오타 난 성격을 조용히 cross로 처리하면 대조군이 발견으로 승격된다."""
    try:
        fa.findings([_rt(0.1)], axes={"x": ("타우토", _by_label)})
    except ValueError as e:
        assert "축 성격" in str(e)
    else:
        raise AssertionError("알 수 없는 성격을 통과시켰다")


def test_the_reason_a_control_is_not_a_finding_is_written_down():
    rows = ([_rt(-0.15, "하락장") for _ in range(20)]
            + [_rt(0.20, "상승장") for _ in range(20)])
    text = fa.format_findings(fa.findings(rows, axes={"국면": ("time", _by_label)}))
    assert "대조용(발견 아님)" in text and "시간축" in text


# ─── 표시 ────────────────────────────────────────────


def test_no_finding_says_so_plainly():
    """빈 결과를 침묵으로 두면 '원인이 없다'가 아니라 '분석이 없다'로 읽힌다."""
    rows = [_rt((i % 7 - 3) / 100, "A" if i % 2 else "B") for i in range(60)]
    text = fa.format_findings(fa.findings(rows, axes={"섹터": ("cross", _by_label)}))
    assert "우연으로 설명되지 않는 칸이 없습니다" in text
    assert "원인을 지목할 수 없다" in text


def test_unlabelled_rows_are_counted_not_hidden():
    rows = [_rt(0.01, "미분류") for _ in range(6)] + [_rt(0.01, "건설") for _ in range(6)]
    f = fa.findings(rows, axes={"섹터": ("cross", _by_label)})
    assert any("라벨 없음 6건" in u for u in f["unknowns"])


def test_the_slippage_split_is_shown_when_available():
    """전체 평균만 보면 이미 고친 문제를 현재 문제로 읽는다."""
    f = fa.findings(
        [_rt(-0.1)], stop_fn=lambda r: {"n": 1, "avg_ret": -10.0, "threshold": -7.0,
                                        "slippage": -3.0, "deeper_than_stop": 1},
        slippage_fn=lambda r: {"cutoff": "2026-07-09",
                               "before": {"n": 22, "slippage": -5.9},
                               "after": {"n": 16, "slippage": -1.1}})
    text = fa.format_findings(f)
    assert "-5.9%p" in text and "-1.1%p" in text and "2026-07-09" in text


def test_an_empty_sample_says_there_is_nothing_to_diagnose():
    assert "완결된 거래가 아직 없습니다" in fa.format_findings(fa.findings([]))


# ─── 결과에 딸려 오는 축 (2026-08-31) ────────────────


def test_an_outcome_driven_axis_is_not_reported_as_a_finding():
    """보유기간은 청산 규칙이 정한다 — 손절은 빨리, 익절은 늦게 끝난다.

    실측(2026-08-31): 6~20일 칸의 익절 비중 52%(11/21), 나머지 칸 29%.
    "오래 들면 좋다"가 아니라 "익절까지 간 거래가 거기 모여 있다"에 가깝다.
    """
    rows = ([_rt(-0.12, "2~5일", reason="손절") for _ in range(20)]
            + [_rt(0.18, "6~20일", reason="익절") for _ in range(20)])
    f = fa.findings(rows, axes={"보유기간": ("outcome", _by_label)})
    assert f["signals"] == []
    assert {c["label"] for c in f["controls"]} == {"2~5일", "6~20일"}


def test_the_outcome_axis_note_explains_the_mechanism():
    """'대조군'이라고만 적으면 왜인지 몰라 다음에 또 발견으로 승격된다."""
    rows = [_rt(-0.1, "짧음", reason="손절") for _ in range(10)] + \
           [_rt(0.1, "김", reason="익절") for _ in range(10)]
    text = fa.format_findings(fa.findings(rows, axes={"보유기간": ("outcome", _by_label)}))
    assert "결과가 라벨에 딸려 온다" in text


def test_every_axis_kind_has_a_note_except_the_plain_one():
    """성격을 추가하고 설명을 빠뜨리면 화면에 근거 없는 '대조군'만 남는다."""
    assert fa.AXIS_KINDS - {"cross"} == set(fa._KIND_NOTE)
