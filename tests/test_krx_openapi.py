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
    assert k.probe_verdict(result) == "absent"


# ─── 실패한 측정에서 결론 내지 않기 (2026-08-31) ─────


ALL_401 = {label: {"ok": False, "error": "HTTP 401",
                   "body": '{"respMsg":"Unauthorized API Call","respCode":"401"}'}
           for label in ("KOSPI 시리즈", "KOSDAQ 시리즈", "KRX 시리즈", "채권지수")}


def test_every_call_failing_is_not_evidence_of_absence():
    """**이 도구의 첫 구현이 여기서 틀렸다.**

    2026-08-31 실측에서 네 엔드포인트가 전부 401로 실패했는데 "변동성지수가
    없다 → 이 API로는 받을 수 없다"고 단정했다. 호출이 실패했으면 **아무것도
    확인하지 못한 것**이다. 실패한 측정에서 결론을 내는 것이 이 프로젝트에서
    반복된 오류다(샤프 √252, 커버리지 18%, 전후반 원수익 비교).
    """
    assert k.probe_verdict(ALL_401) == "unknown"
    text = k.format_probe(ALL_401, "20260828")
    assert "판정 불가" in text
    assert "아직 아무것도 확인하지 못했습니다" in text
    assert "변동성지수가 없습니다" not in text


def test_a_partial_success_still_judges_on_what_came_back():
    """하나라도 응답이 오면 그 응답에 대해서는 판정할 수 있다."""
    mixed = dict(ALL_401)
    mixed["KOSPI 시리즈"] = {"ok": True, "n": 1, "fields": ["IDX_NM"],
                            "name_field": "IDX_NM", "names": ["코스피 200"]}
    assert k.probe_verdict(mixed) == "absent"


def test_a_found_index_wins_over_failed_siblings():
    mixed = dict(ALL_401)
    mixed["KRX 시리즈"] = {"ok": True, "n": 1, "fields": ["IDX_NM"],
                          "name_field": "IDX_NM", "names": ["코스피200 변동성지수"]}
    assert k.probe_verdict(mixed) == "found"


def test_the_401_hint_points_at_service_subscription():
    """KRX는 키 발급과 API별 이용 신청이 따로다 — 그걸 모르면 키를 의심하게 된다."""
    hint = k.auth_hint(ALL_401)
    assert "이용 신청" in hint and "401" in hint


def test_the_hint_notes_the_request_reached_the_server():
    """respCode가 왔다는 것은 주소·헤더 문제가 아니라는 단서다."""
    assert "서버까지 닿았습니다" in k.auth_hint(ALL_401)


def test_no_hint_when_the_failure_is_not_authorization():
    net = {"KOSPI 시리즈": {"ok": False, "error": "네트워크 실패: timeout", "body": ""}}
    assert k.auth_hint(net) == ""


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


def test_the_volatility_index_endpoint_is_present():
    """**유추 목록이 이걸 빠뜨렸다.**

    2026-08-31: 이름 규칙으로 만든 4종은 넷 다 맞았지만 `drvprod_dd_trd`
    (파생상품지수)가 통째로 없었다. 하필 그게 VKOSPI 최유력 후보다 —
    코스피200 변동성지수는 옵션에서 산출되는 파생상품지수이고, 공개 목록
    페이지에는 렌더링되지 않아 보이지 않았다.

    **목록을 유추로 만들면 없는 것을 없다고 말하게 된다.** 401이 아니라
    200이 왔더라도 이걸 안 불렀으면 "변동성지수 없음"이라는 오답이 나왔다.
    """
    assert "/svc/apis/idx/drvprod_dd_trd" in k.INDEX_ENDPOINTS.values()


def test_only_approved_endpoints_are_probed():
    """신청하지 않은 API를 부르면 401이 섞여 판정이 흐려진다.

    승인분(2026-09-01~): 파생상품지수·KOSPI 시리즈·KRX 시리즈.
    채권지수·KOSDAQ 시리즈는 신청하지 않았다.
    """
    paths = set(k.INDEX_ENDPOINTS.values())
    assert "/svc/apis/idx/bon_dd_trd" not in paths
    assert "/svc/apis/idx/kosdaq_dd_trd" not in paths


def test_the_endpoint_ids_match_the_console_listing():
    """화면에서 확인한 API ID와 코드가 어긋나면 조용히 404가 난다."""
    for path in k.INDEX_ENDPOINTS.values():
        assert path.startswith("/svc/apis/idx/") and path.endswith("_dd_trd")
    for path in k.OTHER_ENDPOINTS.values():
        assert path.startswith("/svc/apis/")


# ─── 인증 진단 · 음성 대조 (2026-08-31) ──────────────
#
# "헤더 이름이 틀렸나, 권한이 없나"는 둘 다 401을 준다 — 추측으로는 못 가른다.
# 키 없음 / 엉터리 키와 응답을 비교하면 서버가 키를 보고 있는지 알 수 있다.


def _diag(real, garbage, none):
    return {"path": "/x",
            "variants": {"헤더 AUTH_KEY": real},
            "controls": {"엉터리 키": garbage, "키 없음": none}}


U401 = {"status": 401, "body": '{"respCode":"401"}'}


def test_identical_responses_mean_the_key_is_ignored():
    """진짜 키·엉터리 키·키 없음이 같으면 서버가 키를 안 보고 있다."""
    assert k.auth_verdict(_diag(U401, U401, U401)) == "key_ignored"


def test_a_different_response_for_a_bad_key_means_the_key_is_read():
    """엉터리 키에만 다른 응답이 오면 헤더는 읽히고 있다 — 권한 문제다."""
    bad = {"status": 401, "body": '{"respCode":"E0002","respMsg":"invalid key"}'}
    assert k.auth_verdict(_diag(U401, bad, U401)) == "key_read"


def test_a_success_wins():
    ok = {"status": 200, "body": '{"OutBlock_1":[]}'}
    assert k.auth_verdict(_diag(ok, U401, U401)) == "ok"


def test_a_network_failure_makes_the_comparison_impossible():
    """대조군이 못 돌면 판정하지 않는다 — 실패한 측정에서 결론 내지 않는다."""
    dead = {"status": None, "body": "네트워크 실패: timeout"}
    assert k.auth_verdict(_diag(dead, U401, U401)) == "unknown"


def test_an_empty_diagnosis_is_unknown():
    assert k.auth_verdict({}) == "unknown"
    assert k.auth_verdict({"variants": {}, "controls": {}}) == "unknown"


def test_the_report_names_the_likelier_cause_when_the_key_is_ignored():
    """두 가능성을 나열만 하면 사용자가 무엇부터 볼지 모른다."""
    text = k.format_auth_diagnose(_diag(U401, U401, U401))
    assert "키가 응답에 아무 영향을 주지 않습니다" in text
    assert "활용신청" in text


def test_the_report_rules_out_the_header_when_the_key_is_read():
    """무엇이 원인이 아닌지를 말해야 엉뚱한 데를 안 고친다."""
    bad = {"status": 401, "body": "다름"}
    text = k.format_auth_diagnose(_diag(U401, bad, U401))
    assert "헤더 이름 문제가 아니라" in text


def test_the_controls_include_no_key_and_a_bad_key():
    """대조군이 하나면 '키를 읽는가'를 가릴 수 없다."""
    assert k.GARBAGE_KEY and len(k.AUTH_VARIANTS) >= 4


def test_a_missing_key_is_reported_before_calling(monkeypatch):
    monkeypatch.setattr(k, "_auth_key", lambda: "")
    assert "미설정" in k.auth_diagnose("20260828")["error"]


# ─── 두 401 메시지의 뜻이 다르다 (2026-08-31 실측) ───

API_CALL = {"status": 401,
            "body": '{"respMsg":"Unauthorized API Call","respCode":"401"}'}
BAD_KEY = {"status": 401,
           "body": '{"respMsg":"Unauthorized Key","respCode":"401"}'}


def _real_diag():
    """2026-08-31 맥에서 실제로 나온 응답."""
    return {
        "path": "/svc/apis/idx/kospi_dd_trd",
        "variants": {
            "헤더 AUTH_KEY": API_CALL, "헤더 auth_key": API_CALL,
            "헤더 authKey": BAD_KEY, "헤더 apiKey": BAD_KEY,
            "헤더 Authorization: Bearer": API_CALL, "쿼리 AUTH_KEY": API_CALL,
        },
        "controls": {"키 없음": BAD_KEY, "엉터리 키": BAD_KEY},
    }


def test_the_two_401_messages_mean_different_things():
    """'Unauthorized Key'는 키를 못 알아본 것, 'Unauthorized API Call'은
    키는 알아봤는데 그 API가 허용되지 않은 것이다. 둘을 합치면 원인을 못 좁힌다."""
    assert k.auth_verdict(_real_diag()) == "not_subscribed"


def test_the_report_says_the_key_itself_is_fine():
    text = k.format_auth_diagnose(_real_diag())
    assert "키는 유효합니다" in text
    assert "활용신청" in text


def test_the_report_shows_which_auth_styles_carried_the_key():
    """어느 헤더가 맞는지 실측으로 안다 — 문서를 다시 뒤질 필요가 없다."""
    styles = k.working_auth_styles(_real_diag())
    assert "헤더 AUTH_KEY" in styles
    assert "헤더 authKey" not in styles      # 대조군과 같은 응답 → 안 읽힘
    assert "헤더 apiKey" not in styles


def test_an_unrecognised_key_is_not_reported_as_a_subscription_problem():
    """키가 진짜 틀린 경우까지 '활용신청 하세요'로 보내면 엉뚱한 데를 고친다."""
    diag = {"variants": {"헤더 AUTH_KEY": BAD_KEY},
            "controls": {"키 없음": BAD_KEY, "엉터리 키": BAD_KEY}}
    assert k.auth_verdict(diag) == "key_ignored"


def test_a_different_but_unnamed_error_stays_generic():
    """메시지 규격이 바뀌면 특정 원인을 단정하지 않고 일반 판정으로 물러난다."""
    other = {"status": 401, "body": '{"respMsg":"뭔가 다른 오류"}'}
    diag = {"variants": {"헤더 AUTH_KEY": other},
            "controls": {"키 없음": BAD_KEY, "엉터리 키": BAD_KEY}}
    assert k.auth_verdict(diag) == "key_read"


def test_working_styles_is_empty_when_nothing_carried_the_key():
    diag = {"variants": {"헤더 AUTH_KEY": BAD_KEY},
            "controls": {"키 없음": BAD_KEY, "엉터리 키": BAD_KEY}}
    assert k.working_auth_styles(diag) == []


# ─── 팩터 지수 탐색 (탭 C 검증용, 2026-09-01) ───────
#
# 국면별 팩터 가중 가설을 검증하려면 팩터 수익률 시계열이 필요하다.
# KRX가 스타일 지수를 준다면 종목 단위 구성(무겁고 생존편향 있음)을
# 피할 수 있다. **다만 이름으로 자동 선택하지 않는다.**


def test_factor_hints_find_candidates():
    rows = [{"IDX_NM": "KRX 모멘텀"}, {"IDX_NM": "코스피 200 가치"},
            {"IDX_NM": "KRX 저변동성"}, {"IDX_NM": "KRX 중소형주"}]
    found = k.find_factor_indices(rows)
    assert "KRX 모멘텀" in found["Momentum"]
    assert "코스피 200 가치" in found["Value"]
    assert "KRX 저변동성" in found["LowVol"]
    assert "KRX 중소형주" in found["Size"]


def test_the_volatility_index_is_not_mistaken_for_a_low_vol_factor():
    """**VKOSPI는 저변동성 팩터가 아니다.** '변동성'이 든 지수가 6개였던
    2026-09-01 사례 — 부분일치로 자동 선택하면 엉뚱한 것을 집는다."""
    rows = [{"IDX_NM": "코스피 200 변동성지수"}]
    found = k.find_factor_indices(rows)
    assert "LowVol" not in found or "코스피 200 변동성지수" not in found.get("LowVol", [])


def test_no_candidates_says_what_to_do_instead():
    msg = k.format_factor_candidates({}, total=300)
    assert "종목 단위로" in msg


def test_the_candidate_list_says_it_does_not_choose():
    """고르는 것은 사람이다 — 도구는 후보만 낸다."""
    msg = k.format_factor_candidates({"Momentum": ["KRX 모멘텀"]}, total=1)
    assert "사람이 정하고" in msg
    assert "정확일치" in msg


def test_duplicate_names_are_collapsed():
    rows = [{"IDX_NM": "KRX 모멘텀"}] * 3
    assert k.find_factor_indices(rows)["Momentum"] == ["KRX 모멘텀"]
