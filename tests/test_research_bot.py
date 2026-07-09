"""research_bot 테스트 — RAG 약함 판정·DDG 파싱·research 오케스트레이션(mock)."""
from __future__ import annotations
import pytest

import research_bot as rb


# ─── rag_is_weak ────────────────────────────────────


def test_rag_weak_empty():
    assert rb.rag_is_weak([]) is True


def test_rag_weak_far():
    assert rb.rag_is_weak([{"distance": 0.6}, {"distance": 0.7}]) is True


def test_rag_strong_close():
    assert rb.rag_is_weak([{"distance": 0.2}, {"distance": 0.6}]) is False


def test_rag_weak_no_distance_field():
    assert rb.rag_is_weak([{"text": "x"}]) is True


def test_rag_weak_threshold_boundary():
    assert rb.rag_is_weak([{"distance": 0.50}]) is False   # 0.50 == threshold (not >)
    assert rb.rag_is_weak([{"distance": 0.51}]) is True


# ─── DDG 파싱 ───────────────────────────────────────

_DDG_HTML = '''
<div class="result"><a class="result__a" href="https://a.com/1">결과 하나</a>
<a class="result__snippet">스니펫 하나</a></div>
<div class="result"><a class="result__a" href="https://b.com/2">결과 둘</a>
<a class="result__snippet">스니펫 둘</a></div>
'''


def test_parse_ddg():
    res = rb.parse_ddg(_DDG_HTML)
    assert len(res) == 2
    assert res[0].title == "결과 하나" and res[0].url == "https://a.com/1"


def test_parse_ddg_limit():
    res = rb.parse_ddg(_DDG_HTML, limit=1)
    assert len(res) == 1


def test_parse_ddg_empty():
    assert rb.parse_ddg("<html>none</html>") == []


# ─── research 오케스트레이션 (mock) ─────────────────


def _mock_env(monkeypatch, saved):
    monkeypatch.setattr(rb, "_ddg_search_raw", lambda q: _DDG_HTML)
    monkeypatch.setattr(rb, "_fetch_page", lambda u: f"본문 of {u}")
    monkeypatch.setattr(rb, "_summarize", lambda q, r, p: "정리된 요약 내용")

    def save_fn(prefix, content):
        saved.append((prefix, content))
    return save_fn


def test_research_full_flow(monkeypatch):
    saved = []
    save_fn = _mock_env(monkeypatch, saved)
    out = rb.research("스토캐스틱 RSI란", save_fn=save_fn)
    assert "정리된 요약 내용" in out
    assert "위키에 저장" in out
    assert len(saved) == 1
    assert "스토캐스틱 RSI란" in saved[0][1]


def test_research_no_results(monkeypatch):
    monkeypatch.setattr(rb, "_ddg_search_raw", lambda q: "<html>none</html>")
    out = rb.research("뭔가", save_fn=lambda p, c: None)
    assert "찾지 못" in out


def test_research_search_fail(monkeypatch):
    def boom(q):
        raise ConnectionError("net")
    monkeypatch.setattr(rb, "_ddg_search_raw", boom)
    out = rb.research("뭔가", save_fn=lambda p, c: None)
    assert "실패" in out


def test_research_summarize_fail_falls_back_to_links(monkeypatch):
    saved = []
    monkeypatch.setattr(rb, "_ddg_search_raw", lambda q: _DDG_HTML)
    monkeypatch.setattr(rb, "_fetch_page", lambda u: "본문")

    def boom(q, r, p):
        raise RuntimeError("llm down")
    monkeypatch.setattr(rb, "_summarize", boom)
    out = rb.research("질문", save_fn=lambda p, c: saved.append((p, c)))
    assert "결과" in out and len(saved) == 1  # 링크 fallback로라도 저장


def test_research_empty_query():
    assert "없습니다" in rb.research("", save_fn=None)


def test_research_no_save_fn(monkeypatch):
    _mock_env(monkeypatch, [])
    out = rb.research("질문", save_fn=None)
    assert "정리된 요약" in out and "저장" not in out.split("정리된 요약")[0]
