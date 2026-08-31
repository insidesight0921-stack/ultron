"""test_krx_openapi.py — KRX OPEN API 클라이언트 (hermetic, 네트워크 없음).

VKOSPI 수집을 붙이기 **전에** 탐침부터 만든 이유: 공개 서비스 목록에 실린 지수
API는 KRX/KOSPI/KOSDAQ 시리즈와 채권지수 4종이고 **변동성지수는 목록에 없다.**
KOSPI 시리즈 응답에 섞여 오는지는 실제 호출로만 알 수 있다.

이 프로젝트는 거래소 코드를 추측했다가 **다른 지수를 VKOSPI로 알고 쓴 적이 있다.**
그래서 응답 구조를 가정하지 않고 실측으로 확정한다.
"""
from __future__ import annotations

import krx_openapi as k


# ─── 응답 파싱 ───────────────────────────────────────


def test_the_documented_block_is_read():
    payload = {"OutBlock_1": [{"IDX_NM": "코스피", "CLSPRC_IDX": "6788.88"}]}
    assert k.rows_of(payload)[0]["IDX_NM"] == "코스피"


def test_a_renamed_block_still_yields_rows():
    """블록 이름이 바뀌면 조용히 0건이 된다 — 그러면 '데이터 없음'으로 위장한다."""
    payload = {"OutBlock_2": [{"IDX_NM": "코스피"}]}
    assert len(k.rows_of(payload)) == 1


def test_an_error_payload_is_not_rows():
    assert k.rows_of({"error": "HTTP 401"}) == []
    assert k.rows_of(None) == []
    assert k.rows_of({"OutBlock_1": "리스트아님"}) == []


def test_non_dict_entries_are_dropped():
    assert k.rows_of({"OutBlock_1": [{"a": 1}, "쓰레기", None]}) == [{"a": 1}]


def test_the_name_field_is_detected_not_assumed():
    """필드명을 하드코딩하면 규격이 바뀔 때 조용히 빈 값이 된다."""
    assert k.pick_field({"IDX_NM": "x"}, k.NAME_FIELDS) == "IDX_NM"
    assert k.pick_field({"IDX_NAME": "x"}, k.NAME_FIELDS) == "IDX_NAME"
    assert k.pick_field({"전혀다름": "x"}, k.NAME_FIELDS) is None


# ─── 지수 찾기 ───────────────────────────────────────


def test_the_volatility_index_is_found_by_keyword():
    rows = [{"IDX_NM": "코스피 200"}, {"IDX_NM": "코스피200 변동성지수"}]
    assert k.find_index(rows, "변동성")[0]["IDX_NM"] == "코스피200 변동성지수"


def test_spacing_does_not_break_the_match():
    rows = [{"IDX_NM": "코스피 200 변동성 지수"}]
    assert len(k.find_index(rows, "변동성지수")) == 1


def test_a_row_without_a_name_field_is_skipped():
    assert k.find_index([{"CLSPRC_IDX": "1"}], "변동성") == []


# ─── 호출 전 관문 ────────────────────────────────────


def test_a_missing_key_does_not_call_the_api(monkeypatch):
    """빈 키로 부르면 서버 오류가 '키 없음'인지 '권한 없음'인지 구분되지 않는다."""
    called = []
    monkeypatch.setattr(k, "_auth_key", lambda: "")
    monkeypatch.setattr(k.urllib.request, "urlopen",
                        lambda *a, **kw: called.append(1))
    result = k.fetch("/svc/apis/idx/kospi_dd_trd", "20260828")
    assert "미설정" in result["error"] and not called


def test_a_network_failure_is_returned_not_raised(monkeypatch):
    """수집 하나가 터져서 전체가 멈추면 안 된다."""
    monkeypatch.setattr(k, "_auth_key", lambda: "키")

    def boom(*a, **kw):
        raise OSError("차단됨")

    monkeypatch.setattr(k.urllib.request, "urlopen", boom)
    assert "네트워크 실패" in k.fetch("/x", "20260828")["error"]


# ─── 탐침 결과 표시 ──────────────────────────────────


def test_the_probe_says_plainly_when_there_is_no_volatility_index():
    """'없다'를 분명히 말해야 한다 — 침묵하면 '아직 확인 안 함'과 구분되지 않는다."""
    result = {"KOSPI 시리즈": {"ok": True, "n": 2, "fields": ["IDX_NM"],
                             "name_field": "IDX_NM",
                             "names": ["코스피", "코스피 200"]}}
    text = k.format_probe(result, "20260828")
    assert "어느 응답에도 변동성지수가 없습니다" in text


def test_the_probe_highlights_a_found_volatility_index():
    result = {"KOSPI 시리즈": {"ok": True, "n": 2, "fields": ["IDX_NM"],
                             "name_field": "IDX_NM",
                             "names": ["코스피", "코스피200 변동성지수"]}}
    text = k.format_probe(result, "20260828")
    assert "🎯" in text and "변동성지수가 있습니다" in text


def test_a_failed_endpoint_shows_its_error():
    result = {"KRX 시리즈": {"ok": False, "error": "HTTP 401", "body": "unauthorized"}}
    text = k.format_probe(result, "20260828")
    assert "HTTP 401" in text and "unauthorized" in text


def test_the_endpoint_paths_are_marked_as_unconfirmed():
    """추측한 경로를 확정처럼 두면 다음 사람이 그대로 믿는다."""
    assert "확정이 아니다" in k.__doc__ or "확정이 아니다" in \
        open(k.__file__, encoding="utf-8").read()
