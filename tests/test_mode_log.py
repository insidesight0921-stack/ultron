"""test_mode_log.py — 정답을 사람이 달아주지 않는다. 재질문이 라벨이다.

계획서에는 "실사용에서 가짜 fast/accurate 분류 사례를 모아 키워드 보강"이라고
적혀 있었다. 2026-09-01에 로그를 뒤졌더니 **라우팅 13일치 5건 · override 0건**
이었고, 게다가 최종 mode만 남아 있어 표본이 쌓여도 판단할 수 없는 형태였다.
이 파일은 그 시계를 시작하고, 시작한 시계가 거짓 라벨을 만들지 않게 한다.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import mode_log as ml  # noqa: E402


def _r(at, query, final, user="u1", llm=None, overridden=False):
    return ml.row(query, llm_mode=llm or final, final_mode=final,
                  tool="knowledge_bot", at=at, overridden=overridden, user=user)


# ─── 기록 ────────────────────────────────────────────


def test_the_row_keeps_the_llm_mode_separately():
    """최종 mode만 남기면 '안전망이 일했다'와 'LLM이 맞혔다'를 못 가른다."""
    r = ml.row("자세히 알려줘", llm_mode="fast", final_mode="accurate",
               tool="t", at="2026-09-01T10:00:00", overridden=True)
    assert r["llm_mode"] == "fast" and r["final_mode"] == "accurate"
    assert r["overridden"] is True


def test_a_long_query_is_truncated_but_its_length_is_kept():
    """로그가 대화 사본이 되면 안 되지만, 길이는 판정에 쓸 수 있는 정보다."""
    q = "가" * 500
    r = ml.row(q, llm_mode="fast", final_mode="fast", tool="t", at="x")
    assert len(r["query"]) == ml.QUERY_MAX
    assert r["query_len"] == 500


def test_a_broken_line_does_not_discard_the_whole_log(tmp_path):
    p = tmp_path / "m.jsonl"
    ml.append(p, _r("2026-09-01T10:00:00", "안녕", "fast"))
    with p.open("a", encoding="utf-8") as f:
        f.write("{ 깨진 줄\n")
    ml.append(p, _r("2026-09-01T10:01:00", "잘가", "fast"))
    assert len(ml.load(p)) == 2


def test_a_missing_log_is_empty_not_an_error(tmp_path):
    assert ml.load(tmp_path / "없음.jsonl") == []


# ─── 재질문이 정답이다 ───────────────────────────────


def test_a_reask_in_the_other_direction_labels_the_previous_call():
    rows = [_r("2026-09-01T10:00:00", "삼성전자 어때", "fast"),
            _r("2026-09-01T10:03:00", "좀 더 자세히 알려줘", "accurate")]
    out = ml.label_reasks(rows)
    assert out[0]["misclassified"] is True
    assert out[0]["should_have_been"] == "accurate"
    assert "자세히" in out[0]["evidence"]


def test_a_reask_in_the_same_direction_is_not_a_correction():
    """같은 방향이면 확인차 덧붙인 것이지 고쳐달라는 뜻이 아니다."""
    rows = [_r("2026-09-01T10:00:00", "삼성전자 어때", "accurate"),
            _r("2026-09-01T10:03:00", "자세히 부탁해", "accurate")]
    out = ml.label_reasks(rows)
    assert not out[0].get("misclassified")


def test_a_late_reask_is_not_a_correction():
    """한참 뒤의 질문은 앞 답변에 대한 반응이 아니다."""
    rows = [_r("2026-09-01T10:00:00", "삼성전자 어때", "fast"),
            _r("2026-09-01T14:00:00", "자세히 알려줘", "accurate")]
    out = ml.label_reasks(rows)
    assert not out[0].get("misclassified")


def test_another_users_reask_does_not_label_my_call():
    rows = [_r("2026-09-01T10:00:00", "삼성전자 어때", "fast", user="u1"),
            _r("2026-09-01T10:02:00", "자세히 알려줘", "accurate", user="u2")]
    out = ml.label_reasks(rows)
    assert not out[0].get("misclassified")


def test_only_the_immediately_preceding_call_is_labeled():
    """두 턴 전까지 거슬러 올리면 없는 오분류가 만들어진다."""
    rows = [_r("2026-09-01T10:00:00", "첫 질문", "fast"),
            _r("2026-09-01T10:01:00", "둘째 질문", "accurate"),
            _r("2026-09-01T10:02:00", "자세히 알려줘", "accurate")]
    out = ml.label_reasks(rows)
    assert not out[0].get("misclassified")


def test_a_reask_without_mode_words_labels_nothing():
    rows = [_r("2026-09-01T10:00:00", "삼성전자 어때", "fast"),
            _r("2026-09-01T10:01:00", "그럼 SK하이닉스는", "fast")]
    out = ml.label_reasks(rows)
    assert not any(r.get("misclassified") for r in out)


def test_the_labeler_uses_the_same_regex_as_the_router():
    """두 곳에 따로 두면 한쪽만 바뀌어 측정이 라우터를 설명하지 못한다."""
    import router as r

    assert ml.wanted_mode("자세히") == "accurate"
    assert ml.wanted_mode("간단히") == "fast"
    assert ml.wanted_mode("자세히 간단히") is None      # 둘 다면 판정 없음
    assert r._ACCURATE_KEYWORDS_RE.search("자세히")


# ─── 요약 ────────────────────────────────────────────


def test_a_thin_sample_gets_no_rate():
    """**표본이 적으면 비율을 내지 않는다.** 5건에서 20%는 1건이다."""
    s = ml.summarize([_r(f"2026-09-01T10:0{i}:00", "x", "fast") for i in range(5)])
    assert s["rate"] is None and "30건 미만" in s["rate_note"]


def test_a_real_sample_gets_a_rate():
    rows = []
    for i in range(40):
        r = _r(f"2026-09-01T10:{i:02d}:00", "x", "fast")
        if i < 8:
            r["misclassified"] = True
            r["should_have_been"] = "accurate"
        rows.append(r)
    s = ml.summarize(rows)
    assert s["rate"] == 20.0
    assert s["by_direction"]["fast→accurate"] == 8


def test_the_summary_separates_the_safety_net_from_the_llm():
    rows = [_r("2026-09-01T10:00:00", "x", "accurate", llm="fast", overridden=True),
            _r("2026-09-01T10:01:00", "y", "fast")]
    s = ml.summarize(rows)
    assert s["overridden"] == 1 and s["override_saved"] == 1


# ─── 키워드 후보 ────────────────────────────────────


def test_candidates_need_repetition_not_a_single_case():
    rows = [dict(_r("2026-09-01T10:00:00", "찬찬히 봐줘", "fast"),
                 misclassified=True, should_have_been="accurate")]
    assert ml.keyword_candidates(rows) == []


def test_a_repeated_word_becomes_a_candidate():
    rows = []
    for i in range(4):
        rows.append(dict(_r(f"2026-09-01T10:0{i}:00", "찬찬히 봐줘", "fast"),
                         misclassified=True, should_have_been="accurate"))
    cands = ml.keyword_candidates(rows)
    assert any(c["word"] == "찬찬히" and c["suggests"] == "accurate" for c in cands)


def test_words_already_caught_are_not_proposed_again():
    rows = []
    for i in range(4):
        rows.append(dict(_r(f"2026-09-01T10:0{i}:00", "자세히 봐줘", "fast"),
                         misclassified=True, should_have_been="accurate"))
    assert not any(c["word"] == "자세히" for c in ml.keyword_candidates(rows))


def test_the_report_says_it_will_not_change_the_keywords():
    """근거 없이 정규식을 늘리는 것이 반복된 실패다 — 문구로 못 박는다."""
    msg = ml.format_report([_r("2026-09-01T10:00:00", "x", "fast")])
    assert "키워드를 고치지 않습니다" in msg


def test_an_empty_log_says_so():
    assert "아직 기록이 없습니다" in ml.format_report([])
