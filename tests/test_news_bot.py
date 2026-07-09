"""news_bot 단위 테스트 — RSS 파싱·요약 매칭·dedup·수집·포맷 (mock).

외부 호출(_http_get / _summarize_via_llm)은 monkeypatch. 네트워크/LLM 불필요.
"""
from __future__ import annotations
import pytest

import news_bot as nb


# ─── RSS 파서 ───────────────────────────────────────

_RSS = """<?xml version='1.0'?>
<rss><channel>
<item><title>제목1</title><link>http://x/1</link><description>설명 &lt;b&gt;하나&lt;/b&gt;</description></item>
<item><title>제목2</title><link>http://x/2</link><description>설명2</description></item>
</channel></rss>"""

_ATOM = """<?xml version='1.0'?>
<feed xmlns='http://www.w3.org/2005/Atom'>
<entry><title>A1</title><link href='http://y/1'/><summary>요약1</summary></entry>
</feed>"""


def test_parse_rss_items():
    items = nb._parse_rss(_RSS)
    assert len(items) == 2
    assert items[0]["title"] == "제목1"
    assert items[0]["link"] == "http://x/1"
    assert "<b>" not in items[0]["summary"]  # 태그 제거


def test_parse_atom_link_href():
    items = nb._parse_rss(_ATOM)
    assert items[0]["link"] == "http://y/1"


def test_parse_rss_garbage():
    assert nb._parse_rss("not xml at all") == []


# ─── 요약 매칭 ──────────────────────────────────────


def test_apply_summaries_numbered():
    arts = [nb.Article("S", "T1", "l1", "raw1", "ko"),
            nb.Article("S", "T2", "l2", "raw2", "ko")]
    nb._apply_summaries(arts, "1. 요약 하나\n2. 요약 둘")
    assert arts[0].summary3 == "요약 하나"
    assert arts[1].summary3 == "요약 둘"


def test_apply_summaries_multiline():
    arts = [nb.Article("S", "T1", "l1", "raw1", "ko")]
    nb._apply_summaries(arts, "1. 첫줄\n   이어지는 줄")
    assert "첫줄" in arts[0].summary3 and "이어지는" in arts[0].summary3


def test_apply_summaries_fallback_to_raw():
    arts = [nb.Article("S", "T1", "l1", "원문요약", "ko")]
    nb._apply_summaries(arts, "엉뚱한 출력")  # 번호 없음
    assert arts[0].summary3 == "원문요약"


# ─── dedup ──────────────────────────────────────────


def test_filter_unseen():
    arts = [nb.Article("S", "T1", "l1", "", "ko"), nb.Article("S", "T2", "l2", "", "ko")]
    fresh, updated = nb.filter_unseen(arts, ["l1"])
    assert [a.link for a in fresh] == ["l2"]
    assert "l2" in updated


def test_seen_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(nb, "_SEEN_PATH", tmp_path / "seen.json")
    nb.save_seen(["a", "b"])
    assert nb.load_seen() == ["a", "b"]


def test_seen_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(nb, "_SEEN_PATH", tmp_path / "seen.json")
    monkeypatch.setattr(nb, "_SEEN_CAP", 3)
    nb.save_seen([str(i) for i in range(10)])
    assert nb.load_seen() == ["7", "8", "9"]


# ─── 수집·실행 (mock fetch/LLM) ─────────────────────


def _mock_sources(monkeypatch):
    src = [nb.Source("Mock", "rss", "http://m/feed", "ko")]
    monkeypatch.setattr(nb, "SOURCES", src)
    monkeypatch.setattr(nb, "_http_get", lambda url, timeout=15: _RSS)
    return src


def test_collect_mock(monkeypatch):
    _mock_sources(monkeypatch)
    arts = nb.collect()
    assert len(arts) == 2 and arts[0].source == "Mock"


def test_run_summarizes_and_dedups(monkeypatch, tmp_path):
    _mock_sources(monkeypatch)
    monkeypatch.setattr(nb, "_SEEN_PATH", tmp_path / "seen.json")
    monkeypatch.setattr(nb, "_summarize_via_llm",
                        lambda arts: [setattr(a, "summary3", "요약됨") for a in arts])
    msg, arts = nb.run()
    assert len(arts) == 2 and "요약됨" in msg and "Mock" in msg
    # 두 번째 호출은 dedup으로 0건
    msg2, arts2 = nb.run()
    assert arts2 == [] and msg2 == ""


def test_run_no_summary(monkeypatch, tmp_path):
    _mock_sources(monkeypatch)
    monkeypatch.setattr(nb, "_SEEN_PATH", tmp_path / "seen.json")
    msg, arts = nb.run(summarize=False)
    assert len(arts) == 2


def test_format_digest_groups_by_source():
    arts = [nb.Article("A", "t1", "l1", "", "ko", summary3="s1"),
            nb.Article("B", "t2", "l2", "", "ko", summary3="s2")]
    out = nb.format_digest(arts)
    assert "【A】" in out and "【B】" in out and "s1" in out


def test_format_digest_empty():
    assert nb.format_digest([]) == ""


def test_collect_source_failure_graceful(monkeypatch):
    monkeypatch.setattr(nb, "SOURCES", [nb.Source("Bad", "rss", "http://b", "ko")])
    monkeypatch.setattr(nb, "_http_get", lambda *a, **k: None)
    assert nb.collect() == []
