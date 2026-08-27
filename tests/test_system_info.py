"""test_system_info.py — 봇 자기/시스템 인식 (system_info + 라우터 감지 + research 가드).

전부 hermetic: 플래그 파일은 tmp_path로 격리, 라우터 LLM은 호출 안 됨(단락 처리).
"""
from __future__ import annotations
import json

import pytest

import system_info as si
import router
import research_bot as rb


# ─── system_info: access / service / bots ───────────


def test_access_info_has_paper_port():
    out = si.access_info()
    assert "8080" in out
    assert "8090" in out
    assert "8091" in out
    assert "8081" not in out
    assert "localhost" in out


def test_service_info_lists_active_services():
    out = si.service_info()
    for name in ("data-api", "private-data-api", "watch-raw", "telegram", "paper", "market-data-collector"):
        assert name in out
    assert "web-ui" not in out


def test_bots_info_lists_core_bots():
    out = si.bots_info()
    for name in ("knowledge", "schedule", "quant", "ipo", "news",
                 "action_schedule", "agent"):
        assert name in out


# ─── system_info: 플래그 기반 상태 (tmp 격리) ─────────


def _write(cache, name, obj):
    p = cache / name
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def test_signal_info_present(tmp_path):
    _write(tmp_path, "signal_last.json",
           {"date": "2026-06-17", "keys": ["005930|MACD|매수", "KRW=X|StochRSI|경고"]})
    out = si.signal_info(cache_dir=tmp_path)
    assert "2026-06-17" in out and "2건" in out
    assert "005930|MACD|매수" in out


def test_signal_info_absent(tmp_path):
    out = si.signal_info(cache_dir=tmp_path)
    assert "없" in out


def test_rebalance_info_present(tmp_path):
    _write(tmp_path, "quant_rebalance_last.json", {"2026-05": [111], "2026-06": [111]})
    out = si.rebalance_info(cache_dir=tmp_path)
    assert "2026-06" in out  # 최신 월


def test_rebalance_info_absent(tmp_path):
    out = si.rebalance_info(cache_dir=tmp_path)
    assert "없" in out


def test_rebalance_ignores_empty_period(tmp_path):
    # 빈 리스트 월은 무시하고 직전 비어있지 않은 월을 보고
    _write(tmp_path, "quant_rebalance_last.json", {"2026-05": [9], "2026-06": []})
    out = si.rebalance_info(cache_dir=tmp_path)
    assert "2026-05" in out


def test_scan_info(tmp_path):
    _write(tmp_path, "ipo_weekly_last.json", {"2026-W23": [1], "2026-W24": [1]})
    _write(tmp_path, "kium_weekly_last.json", {"2026-W24": [1]})
    out = si.scan_info(cache_dir=tmp_path)
    assert "2026-W24" in out


def test_automation_info_with_schedules(tmp_path):
    sched = tmp_path / "action_schedules.json"
    sched.write_text(json.dumps({"schedules": [{
        "id": 1, "action": "news", "freq": "daily", "time": "08:00",
        "weekday": None, "until": "2026-06-11", "enabled": True,
        "last_fired": "2026-06-10", "created": "2026-06-08 21:00"}]}),
        encoding="utf-8")
    out = si.automation_info(cache_dir=tmp_path, sched_path=sched)
    assert "뉴스" in out
    assert "2026-06-10" in out  # last_fired 표시


def test_automation_info_empty(tmp_path):
    sched = tmp_path / "empty.json"
    sched.write_text(json.dumps({"schedules": []}), encoding="utf-8")
    out = si.automation_info(cache_dir=tmp_path, sched_path=sched)
    assert "없음" in out


def test_answer_unknown_topic_falls_back_to_status():
    out = si.answer("nonsense_topic")
    # status 요약 = access + bots + automation 합본
    assert "접속 정보" in out and "등록된 봇" in out


def test_run_returns_tuple():
    ans, chunks = si.run("access")
    assert isinstance(ans, str) and chunks == []


# ─── 라우터 결정론 감지 ──────────────────────────────


@pytest.mark.parametrize("q,topic", [
    ("paper 주소 뭐야", "access"),
    ("내 로컬 사이트 주소 줘봐", "access"),
    ("서비스 상태 어때", "service"),
    ("무슨 봇 돌고 있어?", "bots"),
    ("어떤 봇 실행 중이야", "bots"),
    ("마지막 신호 언제 보냈어?", "signal"),
    ("리밸런싱 됐어?", "rebalance"),
    ("리밸런싱 완료된거야?", "rebalance"),
    ("주간 스캔 마지막이 언제야", "scan"),
])
def test_detect_system_info_positive(q, topic):
    assert router._detect_system_info(q) == {"topic": topic}


@pytest.mark.parametrize("q", [
    "내 매매 청산 규칙 뭐야?",   # knowledge
    "자동작업 목록 알려줘",       # action_schedule가 처리
    "매일 8시 뉴스 보내",         # action_schedule
    "IT 뉴스 요약해줘",           # news
    "삼성전자 차트 봐줘",         # invest
    "안녕",
])
def test_detect_system_info_negative(q):
    assert router._detect_system_info(q) is None


def test_route_short_circuits_without_llm(monkeypatch):
    """system_info 질문은 LLM(urlopen) 호출 없이 단락 처리되어야 한다."""
    def _boom(*a, **k):
        raise AssertionError("LLM must not be called")
    monkeypatch.setattr(router, "urlopen", _boom)
    out = router.route("paper 주소 뭐야")
    assert out["tool"] == "system_info"
    assert out["args"] == {"topic": "access"}
    assert out["mode"] == "fast"


def test_route_action_schedule_still_wins_for_list(monkeypatch):
    """'자동작업 목록'은 system_info가 가로채면 안 되고 action_schedule로 가야 한다."""
    monkeypatch.setattr(router, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no LLM")))
    out = router.route("자동작업 목록 알려줘")
    assert out["tool"] == "action_schedule"


def test_validate_system_info_defaults_to_status():
    assert router._validate_system_info({}) == {"topic": "status"}
    assert router._validate_system_info({"topic": "bogus"}) == {"topic": "status"}
    assert router._validate_system_info({"topic": "ACCESS"}) == {"topic": "access"}


def test_system_info_in_known_tools():
    assert "system_info" in router.KNOWN_TOOLS


def test_prompt_under_limit():
    assert len(router._build_system_prompt()) < 13000


# ─── research_bot 웹 폴백 가드 ────────────────────────


@pytest.mark.parametrize("q", [
    "내 매매 청산 규칙 뭐야?",
    "리밸런싱 됐어?",
    "내 포지션 어때",
    "우리 시스템 봇 목록",
    "paper 주소 뭐야",
])
def test_is_self_referential_positive(q):
    assert rb.is_self_referential(q) is True


@pytest.mark.parametrize("q", [
    "스토캐스틱 RSI Band Walk 뭐야",
    "MACD가 뭐야",
    "코스피200 구성종목 알려줘",
])
def test_is_self_referential_negative(q):
    assert rb.is_self_referential(q) is False


# ─── 신호 대상 종목 (v3.48) ──────────────────────────


def test_signal_targets_topic_registered():
    assert "signal_targets" in si._DISPATCH


def test_signal_targets_uses_signal_bot_watchlist(monkeypatch):
    """단일 진실 유지 — 별도 목록을 만들지 않고 signal_bot이 스캔하는 목록을 그대로 보여준다."""
    import types, sys
    stub = types.ModuleType("signal_bot")
    stub.format_watchlist = lambda: "📡 목록 본문"
    sys.modules["signal_bot"] = stub
    try:
        assert si.answer("signal_targets") == "📡 목록 본문"
    finally:
        del sys.modules["signal_bot"]


def test_signal_targets_degrades_gracefully(monkeypatch):
    import types, sys
    stub = types.ModuleType("signal_bot")
    def boom():
        raise RuntimeError("DB 없음")
    stub.format_watchlist = boom
    sys.modules["signal_bot"] = stub
    try:
        out = si.answer("signal_targets")
        assert "불러오지 못했습니다" in out and "핵심_자산배분_포트폴리오" in out
    finally:
        del sys.modules["signal_bot"]


def test_signal_targets_is_separate_from_signal_topic():
    """'마지막 신호 언제'(signal)와 '신호 보는 종목'(signal_targets)은 다른 답이다."""
    assert si._DISPATCH["signal"] is not si._DISPATCH["signal_targets"]


def test_router_detects_signal_targets():
    """'기술적 신호를 보는 종목들 리스트' — 도구가 없어 위키 검색으로 폴백하던 발화."""
    for q in ["기술적 신호를 보는 종목들 리스트",
              "신호 대상 종목 뭐야",
              "신호 보는 종목 알려줘",
              "기술적 분석 종목 목록"]:
        assert router._detect_system_info(q) == {"topic": "signal_targets"}, q


def test_router_keeps_last_signal_topic():
    """'마지막 신호 언제'는 여전히 발송 이력(signal)으로 간다."""
    for q in ["마지막 신호 언제 보냈어", "신호 언제 줬어"]:
        assert router._detect_system_info(q) == {"topic": "signal"}, q


def test_router_validates_signal_targets_topic():
    assert router._validate_system_info({"topic": "signal_targets"}) == {"topic": "signal_targets"}
