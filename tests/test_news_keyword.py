"""v3.47 키워드 뉴스(run_keyword) 단위 테스트 — 네트워크/LLM 없이 monkeypatch."""
from __future__ import annotations

import news_bot as nb
import action_scheduler as asch


_DISTINCT = ["퇴직연금 적립금 증가", "시니어 라운지 개설", "신입행원 최종면접",
             "외화예금 금리 인상", "기업대출 잔액 확대", "모바일 앱 개편"]


def _arts(n):
    return [nb.Article("src", f"하나은행, {_DISTINCT[i]}", f"http://k/{i}", f"요약{i}", "ko")
            for i in range(n)]


def test_keyword_source_url():
    s = nb.keyword_source("하나은행")
    assert s.kind == "rss" and "news.google.com/rss/search" in s.url
    assert "when:1d" in s.url and "%ED%95%98%EB%82%98" in s.url


def test_run_keyword_limit_and_dedup(monkeypatch):
    saved = {}
    monkeypatch.setattr(nb, "_fetch_rss_raw", lambda src, n: _arts(6))
    monkeypatch.setattr(nb, "load_seen", lambda: ["http://k/0"])
    monkeypatch.setattr(nb, "save_seen", lambda links: saved.setdefault("l", links))
    monkeypatch.setattr(nb, "_summarize_via_llm", lambda arts, **kw: None)
    msg, arts = nb.run_keyword("하나은행", 3)
    assert [a.link for a in arts] == ["http://k/1", "http://k/2", "http://k/3"]
    assert saved["l"] == ["http://k/0", "http://k/1", "http://k/2", "http://k/3"]
    assert "하나은행 뉴스 3건" in msg


def test_run_keyword_empty(monkeypatch):
    monkeypatch.setattr(nb, "_fetch_rss_raw", lambda src, n: [])
    monkeypatch.setattr(nb, "load_seen", lambda: [])
    assert nb.run_keyword("하나은행", 3) == ("", [])


def test_hana_news_action_registered():
    assert "hana_news" in asch.ACTION_LABELS


def test_pick_distinct_drops_same_event():
    titles = [
        "하나은행, 광주에 '시니어 금융 라운지'...특화 공간 확대 - 초이스경제",
        "하나은행, '하나더넥스트 광주금융 라운지' 조성... 호남권 최초 시니어 맞춤 금융공간 - 뉴스락",
        "하나은행, 3분기 퇴직연금 적립금 증가 1위 - 뉴스핌",
        "하나은행, 호남권 첫 시니어 특화 '광주금융 라운지' 개설 - 뉴시스",
        "하나은행 신입행원 채용 최종면접 진행 - 한경",
    ]
    arts = [nb.Article("s", t, f"http://d/{i}", "", "ko") for i, t in enumerate(titles)]
    picked, skipped = nb.pick_distinct(arts, "하나은행", 3)
    assert [a.link for a in picked] == ["http://d/0", "http://d/2", "http://d/4"]
    assert [a.link for a in skipped] == ["http://d/1", "http://d/3"]


def test_clean_unescapes_entities():
    assert nb._clean("제목&nbsp;&nbsp;초이스경제") == "제목 초이스경제"
