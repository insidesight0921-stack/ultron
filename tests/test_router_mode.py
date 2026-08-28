"""
라우터 출력의 mode 필드 검증.

LLM 호출(_route 안의 ollama)은 monkeypatch로 가짜화. 시스템 프롬프트는 실제
빌드해서 도구 정의가 들어있는지 확인.
"""
from __future__ import annotations
import json
import pytest

import router


# ─── 시스템 프롬프트 ─────────────────────────────────


def test_system_prompt_has_all_tools():
    sp = router._build_system_prompt()
    for tool in ("knowledge_bot", "schedule_bot", "finance_bot",
                 "invest_bot", "watchlist_bot", "respond_directly"):
        assert tool in sp, f"{tool} 누락"


def test_system_prompt_has_mode_section():
    sp = router._build_system_prompt()
    assert "mode 필드" in sp
    assert "fast" in sp and "accurate" in sp


def test_system_prompt_has_current_time():
    sp = router._build_system_prompt()
    # 동적 주입된 시각 (YYYY-MM-DD)
    import re
    assert re.search(r"\d{4}-\d{2}-\d{2}", sp)


# ─── invest_bot validator ───────────────────────────


def test_validate_invest_args_analyze():
    v = router._validate_invest_args(
        {"action": "analyze", "ticker_or_name": "삼성전자"})
    assert v == {"action": "analyze", "ticker_or_name": "삼성전자"}


def test_validate_invest_args_compare():
    v = router._validate_invest_args(
        {"action": "compare_with_rules", "ticker_or_name": "005930"})
    assert v == {"action": "compare_with_rules", "ticker_or_name": "005930"}


def test_validate_invest_args_missing_ticker():
    assert router._validate_invest_args({"action": "analyze"}) is None


def test_validate_invest_args_unknown_action():
    assert router._validate_invest_args(
        {"action": "buy", "ticker_or_name": "x"}) is None


def test_validate_invest_args_strips_whitespace():
    v = router._validate_invest_args(
        {"action": "analyze", "ticker_or_name": "  삼성전자  "})
    assert v["ticker_or_name"] == "삼성전자"


# ─── watchlist_bot validator / 결정론 라우팅 ─────────


def test_validate_watchlist_args():
    assert router._validate_watchlist_args({"action": "list"}) == {"action": "list"}
    assert router._validate_watchlist_args(
        {"action": "add", "ticker_or_name": "  삼성전자  "}
    ) == {"action": "add", "ticker_or_name": "삼성전자"}
    assert router._validate_watchlist_args(
        {"action": "remove", "ticker_or_name": "005930"}
    ) == {"action": "remove", "ticker_or_name": "005930"}


def test_validate_watchlist_args_rejects_missing_target():
    assert router._validate_watchlist_args({"action": "add"}) is None
    assert router._validate_watchlist_args({"action": "remove"}) is None
    assert router._validate_watchlist_args({"action": "delete", "ticker_or_name": "x"}) is None


@pytest.mark.parametrize(
    ("query", "action", "target"),
    [
        ("삼성전자 관심종목에 추가해줘", "add", "삼성전자"),
        ("관심종목에 005930 등록해줘", "add", "005930"),
        ("에코프로 관심종목에 넣어줘", "add", "에코프로"),
        ("카카오를 관심종목에 추가해줘", "add", "카카오"),
        ("삼성전자 관심종목에서 빼줘", "remove", "삼성전자"),
    ],
)
def test_detect_watchlist_mutation(query, action, target):
    out = router._detect_watchlist(query)
    assert out == {"action": action, "ticker_or_name": target}


def test_detect_watchlist_list():
    assert router._detect_watchlist("내 관심종목 목록 보여줘") == {"action": "list"}


def test_route_watchlist_short_circuits_without_llm(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("명백한 관심종목 명령은 LLM을 호출하면 안 됨")

    monkeypatch.setattr(router, "urlopen", boom)
    result = router.route("삼성전자 관심종목에 추가해줘")
    assert result == {
        "tool": "watchlist_bot",
        "args": {"action": "add", "ticker_or_name": "삼성전자"},
        "mode": "fast",
    }


def test_route_watchlist_missing_target_asks_user(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("명백한 관심종목 명령은 LLM을 호출하면 안 됨")

    monkeypatch.setattr(router, "urlopen", boom)
    result = router.route("관심종목에 추가해줘")
    assert result["tool"] == "respond_directly"
    assert "종목명" in result["args"]["answer"]


# ─── route()에 mode 포함 ──────────────────────────────


def _fake_ollama(payload_dict, monkeypatch):
    """ollama가 반환할 JSON을 미리 set."""
    body = json.dumps(payload_dict).encode("utf-8")

    class FakeResp:
        def __init__(self, body):
            self._body = body
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return self._body

    def fake_urlopen(req, timeout=60):
        return FakeResp(json.dumps({
            "message": {"content": json.dumps(payload_dict)}
        }).encode("utf-8"))

    monkeypatch.setattr(router, "urlopen", fake_urlopen)


def test_route_includes_mode_field(monkeypatch):
    _fake_ollama({
        "tool": "knowledge_bot",
        "args": {"query": "x"},
        "mode": "accurate",
    }, monkeypatch)
    res = router.route("아무 질문")
    assert "mode" in res
    assert res["mode"] == "accurate"


def test_route_default_mode_when_missing(monkeypatch):
    _fake_ollama({
        "tool": "respond_directly",
        "args": {"answer": "안녕"},
    }, monkeypatch)
    res = router.route("안녕")
    assert res["mode"] == router.DEFAULT_MODE


def test_route_unknown_mode_falls_back(monkeypatch):
    _fake_ollama({
        "tool": "respond_directly",
        "args": {"answer": "x"},
        "mode": "ULTRAFAST",
    }, monkeypatch)
    res = router.route("x")
    assert res["mode"] == router.DEFAULT_MODE


def test_route_fast_mode_passes_through(monkeypatch):
    _fake_ollama({
        "tool": "schedule_bot",
        "args": {"action": "upcoming"},
        "mode": "fast",
    }, monkeypatch)
    res = router.route("내 일정")
    assert res["tool"] == "schedule_bot"
    assert res["mode"] == "fast"


def test_route_invest_bot_routing(monkeypatch):
    _fake_ollama({
        "tool": "invest_bot",
        "args": {"action": "analyze", "ticker_or_name": "삼성전자"},
        "mode": "fast",
    }, monkeypatch)
    res = router.route("삼성전자 차트")
    assert res["tool"] == "invest_bot"
    assert res["args"]["ticker_or_name"] == "삼성전자"
    assert res["mode"] == "fast"


def test_route_invest_invalid_falls_back_with_mode(monkeypatch):
    """invest_bot validator 실패 → knowledge_bot fallback. mode 필수 포함."""
    _fake_ollama({
        "tool": "invest_bot",
        "args": {"action": "analyze"},  # ticker 누락
        "mode": "accurate",
    }, monkeypatch)
    res = router.route("뭐?")
    assert res["tool"] == "knowledge_bot"
    assert "mode" in res


# ─── 결정론적 mode override (B-2, v3.13) ─────────────


def test_override_accurate_keyword_forces_accurate():
    """LLM이 fast로 보냈어도 사용자가 '정확하게' 명시하면 accurate."""
    assert router._override_mode_by_keywords("정확하게 1+1 짜줘", "fast") == "accurate"
    assert router._override_mode_by_keywords("자세히 설명해", "fast") == "accurate"
    assert router._override_mode_by_keywords("심층 분석", "fast") == "accurate"
    assert router._override_mode_by_keywords("원칙대로 평가", "fast") == "accurate"


def test_override_fast_keyword_forces_fast():
    """LLM이 accurate로 보냈어도 '빠르게/간단히' 명시하면 fast."""
    assert router._override_mode_by_keywords("빠르게 짜줘", "accurate") == "fast"
    assert router._override_mode_by_keywords("간단히 알려줘", "accurate") == "fast"
    assert router._override_mode_by_keywords("간략히 요약", "accurate") == "fast"


def test_override_no_keyword_keeps_llm_decision():
    """키워드 없으면 LLM 판단 그대로."""
    assert router._override_mode_by_keywords("RSI가 뭐야", "fast") == "fast"
    assert router._override_mode_by_keywords("내 매매 청산 규칙", "accurate") == "accurate"


def test_override_conflicting_keywords_keeps_llm():
    """accurate·fast 키워드 둘 다 있으면 LLM 판단 그대로 (충돌 무시)."""
    assert router._override_mode_by_keywords("정확하게 빠르게 짜줘", "accurate") == "accurate"
    assert router._override_mode_by_keywords("자세히 간단히", "fast") == "fast"


def test_override_empty_query():
    assert router._override_mode_by_keywords("", "fast") == "fast"
    assert router._override_mode_by_keywords("", "accurate") == "accurate"


def test_route_applies_mode_override(monkeypatch):
    """route() end-to-end — LLM이 fast 보냈어도 사용자 입력에 '정확하게' 있으면 accurate."""
    _fake_ollama({
        "tool": "coding_bot",
        "args": {"action": "code", "content": "1+1"},
        "mode": "fast",  # LLM 실수
    }, monkeypatch)
    res = router.route("정확하게 1+1 더하는 함수 짜줘")
    assert res["tool"] == "coding_bot"
    assert res["mode"] == "accurate", "사용자 명시 키워드 override 안 됨"


def test_route_override_fast_direction(monkeypatch):
    """반대 방향 — LLM이 accurate 보냈어도 '빠르게' 명시면 fast."""
    _fake_ollama({
        "tool": "coding_bot",
        "args": {"action": "code", "content": "X"},
        "mode": "accurate",
    }, monkeypatch)
    res = router.route("X 빠르게 짜줘")
    assert res["mode"] == "fast"


# ─── _validate_schedule_args: conflict_window_minutes (v3.14) ───


def test_validate_schedule_args_accepts_conflict_window():
    args = {
        "action": "add",
        "title": "회의",
        "when_at": "2027-03-15T14:00:00",
        "conflict_window_minutes": 30,
    }
    out = router._validate_schedule_args(args)
    assert out is not None
    assert out["conflict_window_minutes"] == 30


def test_validate_schedule_args_accepts_zero_conflict_window():
    """0은 '검사 비활성' 의미로 통과."""
    args = {
        "action": "add",
        "title": "강제 등록",
        "when_at": "2027-03-15T14:00:00",
        "conflict_window_minutes": 0,
    }
    out = router._validate_schedule_args(args)
    assert out is not None
    assert out["conflict_window_minutes"] == 0


def test_validate_schedule_args_rejects_negative_conflict_window():
    """음수는 무시 (out에 포함 안 됨 → schedule_bot이 디폴트 적용)."""
    args = {
        "action": "add",
        "title": "회의",
        "when_at": "2027-03-15T14:00:00",
        "conflict_window_minutes": -5,
    }
    out = router._validate_schedule_args(args)
    assert out is not None
    assert "conflict_window_minutes" not in out


def test_validate_schedule_args_no_conflict_window_omits_field():
    """안 주면 out에 없음 → schedule_bot이 DEFAULT 사용."""
    args = {
        "action": "add",
        "title": "회의",
        "when_at": "2027-03-15T14:00:00",
    }
    out = router._validate_schedule_args(args)
    assert out is not None
    assert "conflict_window_minutes" not in out


def test_validate_schedule_args_invalid_conflict_window_ignored():
    """문자열 등 변환 실패는 무시."""
    args = {
        "action": "add",
        "title": "회의",
        "when_at": "2027-03-15T14:00:00",
        "conflict_window_minutes": "abc",
    }
    out = router._validate_schedule_args(args)
    assert out is not None
    assert "conflict_window_minutes" not in out


# ─── _validate_schedule_args: pre_notify_minutes list (v3.15) ───


def test_validate_schedule_args_pre_notify_list():
    args = {
        "action": "add",
        "title": "회의",
        "when_at": "2027-04-01T15:00:00",
        "pre_notify_minutes": [5, 30],
    }
    out = router._validate_schedule_args(args)
    assert out is not None
    # 큰 값 먼저
    assert out["pre_notify_minutes"] == [30, 5]


def test_validate_schedule_args_pre_notify_list_dedupes():
    args = {
        "action": "add",
        "title": "회의",
        "when_at": "2027-04-01T15:00:00",
        "pre_notify_minutes": [5, 5, "30", -1, 0],
    }
    out = router._validate_schedule_args(args)
    assert out is not None
    assert out["pre_notify_minutes"] == [30, 5]


def test_validate_schedule_args_pre_notify_int_still_works():
    """단일 int 입력은 v3.9 그대로 (BC)."""
    args = {
        "action": "add",
        "title": "회의",
        "when_at": "2027-04-01T15:00:00",
        "pre_notify_minutes": 10,
    }
    out = router._validate_schedule_args(args)
    assert out is not None
    assert out["pre_notify_minutes"] == 10  # int 유지


def test_validate_schedule_args_pre_notify_empty_list_omits():
    """빈 list 또는 모두 무효 → out에 키 없음."""
    args = {
        "action": "add",
        "title": "회의",
        "when_at": "2027-04-01T15:00:00",
        "pre_notify_minutes": [-1, 0, "abc"],
    }
    out = router._validate_schedule_args(args)
    assert out is not None
    assert "pre_notify_minutes" not in out


# ─── ipo_bot lockup_ratio 전파 (v3.36 회귀) ─────────────


def test_ipo_validator_keeps_lockup_ratio():
    """확약 비율이 validator 화이트리스트를 통과해야 한다 (텔레그램→analyze_manual 전파)."""
    out = router._validate_ipo_args({
        "action": "analyze",
        "corp_name": "마키나락스",
        "competition_rate": 1200,
        "float_ratio": 38.5,
        "lockup_ratio": 78.0,
    })
    assert out is not None
    assert out["lockup_ratio"] == 78.0


def test_ipo_validator_lockup_non_numeric_dropped():
    """숫자 아닌 lockup_ratio는 조용히 무시(다른 키 유지)."""
    out = router._validate_ipo_args({
        "action": "analyze",
        "corp_name": "OO",
        "lockup_ratio": "확약많음",
    })
    assert out is not None
    assert "lockup_ratio" not in out


def test_system_prompt_mentions_lockup():
    sp = router._build_system_prompt()
    assert "lockup_ratio" in sp


# ─── ipo_bot analyze 정규식 추출 (v3.37 회귀) ─────────


def test_ipo_extract_full_message():
    """완전한 자연어에서 7개 필드 모두 추출 (실환경 실측 메시지)."""
    q = ("마키나락스 공모주 경쟁률 1200 밴드 13000~15000 확정 16000 "
         "유통비율 38.5% 확약 78% 공모금액 800억 미래에셋 매력지수 계산해줘")
    out = router._extract_ipo_fields_from_query(q)
    assert out["competition_rate"] == 1200.0
    assert out["band_low"] == 13000.0 and out["band_high"] == 15000.0
    assert out["final_price"] == 16000.0
    assert out["float_ratio"] == 38.5
    assert out["lockup_ratio"] == 78.0
    assert out["offer_amount"] == 800.0
    assert out["underwriter"] == "미래에셋"


def test_ipo_extract_final_not_confused_by_band():
    """확정공모가가 밴드 숫자를 오인식하지 않아야."""
    out = router._extract_ipo_fields_from_query("밴드 13000~15000 확정 16000")
    assert out["final_price"] == 16000.0


def test_ipo_extract_underwriter_with_jeunggwon_suffix():
    out = router._extract_ipo_fields_from_query("한국투자증권 주관 공모주")
    assert out["underwriter"] == "한국투자증권"


def test_ipo_extract_comma_numbers():
    out = router._extract_ipo_fields_from_query("공모금액 1,200억 경쟁률 1,050")
    assert out["offer_amount"] == 1200.0
    assert out["competition_rate"] == 1050.0


def test_ipo_extract_empty_when_no_fields():
    assert router._extract_ipo_fields_from_query("그냥 잡담") == {}


def test_route_ipo_regex_backfills_partial_llm(monkeypatch):
    """LLM이 corp_name만 줘도(실환경 26B 행동) 정규식이 숫자 필드 보강."""
    _fake_ollama({
        "tool": "ipo_bot",
        "args": {"action": "analyze", "corp_name": "마키나락스"},
        "mode": "fast",
    }, monkeypatch)
    q = ("마키나락스 공모주 경쟁률 1200 밴드 13000~15000 확정 16000 "
         "유통비율 38.5% 확약 78% 공모금액 800억 미래에셋 매력지수 계산해줘")
    res = router.route(q)
    a = res["args"]
    assert res["tool"] == "ipo_bot"
    assert a["corp_name"] == "마키나락스"
    assert a["competition_rate"] == 1200.0
    assert a["band_low"] == 13000.0 and a["band_high"] == 15000.0
    assert a["final_price"] == 16000.0
    assert a["float_ratio"] == 38.5
    assert a["lockup_ratio"] == 78.0
    assert a["offer_amount"] == 800.0
    assert a["underwriter"] == "미래에셋"


def test_route_ipo_regex_overrides_wrong_llm_value(monkeypatch):
    """LLM이 틀린 band를 줘도 정규식 값이 우선."""
    _fake_ollama({
        "tool": "ipo_bot",
        "args": {"action": "analyze", "corp_name": "OO", "band_low": 999, "band_high": 888},
        "mode": "fast",
    }, monkeypatch)
    res = router.route("OO 공모주 밴드 13000~15000 분석")
    assert res["args"]["band_low"] == 13000.0
    assert res["args"]["band_high"] == 15000.0


# ─── IPO analyze 결정론 override (v3.38 회귀) ─────────


def test_detect_ipo_analyze_full():
    q = ("마키나락스 공모주 경쟁률 1200 밴드 13000~15000 확정 16000 "
         "유통비율 38.5% 확약 78% 공모금액 800억 미래에셋 매력지수 계산해줘")
    out = router._detect_ipo_analyze(q)
    assert out is not None
    assert out["action"] == "analyze"
    assert out["corp_name"] == "마키나락스"
    assert out["lockup_ratio"] == 78.0


def test_detect_ipo_analyze_corp_before_maeryeok():
    out = router._detect_ipo_analyze("두산로보틱스 매력지수 경쟁률 900 공모금액 1000억")
    assert out is not None
    assert out["corp_name"] == "두산로보틱스"


def test_detect_ipo_no_cue_returns_none():
    assert router._detect_ipo_analyze("마키나락스 경쟁률 1200 시총 800억 분석") is None


def test_detect_ipo_too_few_numbers_returns_none():
    # 공모주 단서는 있지만 숫자 1개뿐 → 오발동 방지
    assert router._detect_ipo_analyze("다음달 공모주 일정 알려줘") is None


def test_detect_ipo_chitchat_none():
    assert router._detect_ipo_analyze("오늘 날씨 어때") is None


def test_detect_ipo_tech_note_none():
    """기술적 분석 노트(밴드·이평선 언급)는 IPO 단서 없으면 발동 안 함."""
    assert router._detect_ipo_analyze(
        "볼린저 밴드 20일 이동평균선 MACD 0선 RSI 과매수 13000 15000") is None


def test_route_ipo_override_when_llm_picks_knowledge(monkeypatch):
    """LLM이 knowledge_bot으로 오라우팅해도 IPO 단서 있으면 ipo_bot 강제(실환경 버그)."""
    _fake_ollama({
        "tool": "knowledge_bot",
        "args": {"query": "마키나락스 공모주"},
        "mode": "fast",
    }, monkeypatch)
    q = ("마키나락스 공모주 경쟁률 1200 밴드 13000~15000 확정 16000 "
         "유통비율 38.5% 확약 78% 공모금액 800억 미래에셋 매력지수 계산해줘")
    res = router.route(q)
    assert res["tool"] == "ipo_bot"
    a = res["args"]
    assert a["action"] == "analyze"
    assert a["corp_name"] == "마키나락스"
    assert a["float_ratio"] == 38.5
    assert a["lockup_ratio"] == 78.0
    assert a["underwriter"] == "미래에셋"


def test_route_ipo_short_circuits_without_llm(monkeypatch):
    """구조화된 IPO 질문은 LLM 호출 없이 ipo_bot 반환(JSON 깨짐/네트워크 무관)."""
    def boom(*a, **k):
        raise AssertionError("LLM(urlopen)이 호출되면 안 됨 — 단락 실패")
    monkeypatch.setattr(router, "urlopen", boom)
    q = ("마키나락스 공모주 경쟁률 1200 밴드 13000~15000 확정 16000 "
         "유통비율 38.5% 확약 78% 공모금액 800억 미래에셋 매력지수 계산해줘")
    res = router.route(q)
    assert res["tool"] == "ipo_bot"
    assert res["args"]["corp_name"] == "마키나락스"
    assert res["args"]["lockup_ratio"] == 78.0


def test_route_nonipo_still_calls_llm(monkeypatch):
    """IPO 단서 없는 질문은 단락되지 않고 LLM 경로로."""
    _fake_ollama({"tool": "knowledge_bot", "args": {"query": "x"}, "mode": "fast"}, monkeypatch)
    res = router.route("리스크 관리 원칙 알려줘")
    assert res["tool"] == "knowledge_bot"


# ─── news_bot 라우팅 (v3.41) ────────────────────────


def test_detect_news_true():
    assert router._detect_news("IT 뉴스 요약해줘")
    assert router._detect_news("오늘 AI 뉴스 브리핑")


def test_detect_news_false():
    assert not router._detect_news("안녕")
    assert not router._detect_news("삼성전자 분석해줘")


def test_route_news_short_circuit(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("뉴스는 LLM 호출 없이 단락돼야")
    monkeypatch.setattr(router, "urlopen", boom)
    res = router.route("IT 뉴스 요약해줘")
    assert res["tool"] == "news_bot"


def test_validate_news_args_clamp():
    assert router._validate_news_args({"per_source": 99})["per_source"] == 10
    assert router._validate_news_args({}) == {}


# ─── action_schedule 파싱/라우팅 (v3.43) ────────────


def test_detect_action_schedule_daily():
    out = router._detect_action_schedule("매일 8시 IT 뉴스 보내 6/11까지")
    assert out["op"] == "add" and out["action"] == "news"
    assert out["time"] == "08:00" and out["until"] == "2026-06-11"


def test_detect_action_schedule_weekly():
    out = router._detect_action_schedule("매주 월요일 9시 10분에 공모주 스캔해줘")
    assert out["op"] == "add" and out["action"] == "ipo"
    assert out["freq"] == "weekly" and out["weekday"] == 0 and out["time"] == "09:10"


def test_detect_action_schedule_list():
    assert router._detect_action_schedule("자동 작업 목록 보여줘") == {"op": "list"}


def test_detect_action_schedule_delete():
    out = router._detect_action_schedule("자동작업 2 삭제해줘")
    assert out == {"op": "delete", "id": 2}


def test_detect_action_schedule_none_without_recur():
    assert router._detect_action_schedule("IT 뉴스 요약해줘") is None


def test_route_action_schedule_short_circuit(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("예약은 LLM 없이 단락돼야")
    monkeypatch.setattr(router, "urlopen", boom)
    res = router.route("매일 8시 뉴스 보내줘")
    assert res["tool"] == "action_schedule"
    assert res["args"]["action"] == "news"


def test_validate_action_schedule_add_bad_time():
    assert router._validate_action_schedule({"op": "add", "action": "news", "time": "8시"}) is None


def test_validate_action_schedule_add_bad_action():
    assert router._validate_action_schedule({"op": "add", "action": "weather", "time": "08:00"}) is None


def test_detect_action_schedule_list_automation_word():
    assert router._detect_action_schedule("자동화 목록 보여줘") == {"op": "list"}
    assert router._detect_action_schedule("내 자동화 작업 알려줘") == {"op": "list"}


def test_ipo_size_keyword_is_offer_amount_not_market_cap():
    """2026-08-28: 규모 요소의 입력이 시총 → 공모금액으로 바뀌었다.

    옛 표현('시총 800억')을 계속 받아 공모금액 자리에 넣으면, 사용자는 시총을
    말했는데 공모금액으로 채점된다 — 조용히 다른 값이 되는 쪽이 더 나쁘다.
    """
    assert router._extract_ipo_fields_from_query("시총 800억") == {}
    assert router._extract_ipo_fields_from_query("공모규모 800억")["offer_amount"] == 800.0
