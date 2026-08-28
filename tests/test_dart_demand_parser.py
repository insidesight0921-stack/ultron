"""test_dart_demand_parser.py — DART 공시 본문 수치 추출 (hermetic).

2026-08-28 실측: 저장된 실제 공시 본문 20건에 돌려 보니 밴드 0/20, 경쟁률 0/20,
확약 0/20이었다. 원문을 열자 어긋난 지점이 분명했다 — 공시는 "공모희망가격"이
아니라 **"희망공모가액"**을 쓴다(어순도 어미도 다르다).

아래 문자열은 전부 그 20건에서 그대로 발췌한 것이다. 추측한 문장으로 테스트를
만들면 추측한 정규식을 통과시킬 뿐이다.
"""
from __future__ import annotations

from dart_demand_parser import extract_ipo_metrics


# ─── 실제 본문 발췌 ──────────────────────────────────

SKYLABS_BAND = (
    "인수금액 및 인수대가는 ㈜스카이랩스의 제시 희망공모가액인 13,000원 ~ 16,000원 "
    "중 최저가액인 13,000원 기준입니다"
)
SKYLABS_FINAL = (
    "주2) 모집(매출)가액, 모집(매출)총액, 인수금액 및 인수대가는 대표주관회사와 "
    "발행회사가 협의하여 결정한 확정공모가액인 10,000원 기준입니다"
)
SPAC_SINGLE = (
    "금번 공모로 발행할 주식의 희망공모가액은 1주당 2,000원이며, 희망공모가액 "
    "2,000원으로 공모가액이 결정되는 경우"
)
NOT_A_RESULT = (
    "차. 수요예측 경쟁률에 관한 주의사항 당사의 수요예측 예정일은 2026년 9월 28일(월) "
    "~ 10월 02일(금)입니다. 수요예측에 참여한 기관투자자들은 가격확정 후 실투자 여부를 "
    "결정하여 청약 예정일인 2026년 10월 12일"
)


# ─── 밴드 ────────────────────────────────────────────


def test_the_real_wording_is_huimang_gongmo_gaaek():
    """'공모희망가격'을 찾던 정규식이 0/20이었던 이유."""
    out = extract_ipo_metrics(SKYLABS_BAND)
    assert out["offer_band_low"] == 13000.0
    assert out["offer_band_high"] == 16000.0


def test_full_width_tilde_is_accepted():
    out = extract_ipo_metrics("희망공모가액 8,000원 ～ 10,000원")
    assert out["offer_band_high"] == 10000.0


def test_a_reversed_pair_is_rejected():
    """하단 > 상단이면 밴드가 아니라 다른 숫자쌍이다.

    뒤집어 담으면 밴드 위치 점수가 통째로 거꾸로 나온다.
    """
    out = extract_ipo_metrics("희망공모가액 16,000원 ~ 13,000원")
    assert out["offer_band_low"] is None and out["offer_band_high"] is None


def test_a_year_range_is_not_a_band():
    """'원'이 붙지 않은 숫자쌍(연도 등)을 밴드로 읽으면 안 된다."""
    out = extract_ipo_metrics("희망공모가액 산출 기간은 2024 ~ 2026 입니다")
    assert out["offer_band_low"] is None


# ─── 확정가 ──────────────────────────────────────────


def test_the_real_wording_is_hwakjeong_gongmo_gaaek():
    assert extract_ipo_metrics(SKYLABS_FINAL)["final_price"] == 10000.0


def test_a_band_low_is_not_mistaken_for_a_final_price():
    """이전 패턴이 잡은 5건 중 3건이 밴드 하단을 확정가로 오인한 것이었다.

    확정가가 잘못 채워지면 단계가 '확정'으로 넘어가고 밴드 하단 확정(2점)이
    매겨져, **확정되지도 않은 종목이 최저 점수를 받는다.**
    """
    assert extract_ipo_metrics(SKYLABS_BAND)["final_price"] is None


def test_a_spac_single_price_is_a_band_not_a_final_price():
    out = extract_ipo_metrics(SPAC_SINGLE)
    assert out["final_price"] is None


# ─── 경쟁률: 없는 값을 만들어 내지 않는다 ────────────


def test_a_notice_about_the_forecast_is_not_a_result():
    """수집한 20건은 전부 수요예측 *전* 문서였다. 등장하는 '경쟁률'은 주의사항
    문구이거나 일반청약 안내다 — 여기서 숫자를 뽑아내면 없는 사실이 생긴다."""
    out = extract_ipo_metrics(NOT_A_RESULT)
    assert out["competition_rate"] is None


def test_a_result_line_is_parsed_in_both_word_orders():
    """⚠️ 이 두 문장은 **발췌가 아니라 지어낸 예시**다.

    확보한 20건에 수요예측 결과 문서가 하나도 없어 원문으로 확인하지 못했다.
    흔한 형태라 넣어 두지만, 결과 문서를 얻으면 이 테스트부터 실제 문장으로
    바꿔야 한다. (천 단위 쉼표와 소수점이 함께 오는 것은 실측에서 확인된 사실이다.)
    """
    assert extract_ipo_metrics(
        "수요예측 결과 기관 경쟁률은 1,234.56 : 1 로 집계되었습니다"
    )["competition_rate"] == 1234.56
    assert extract_ipo_metrics(
        "총 2,109개 기관이 참여하여 1,159.36 대 1의 경쟁률을 기록하였습니다"
    )["competition_rate"] == 1159.36


def test_above_band_needs_both_values():
    out = extract_ipo_metrics(SKYLABS_BAND)
    assert out["above_band"] is None      # 확정가가 없으니 비교할 수 없다


def test_above_band_is_computed_when_both_exist():
    out = extract_ipo_metrics("희망공모가액 10,000원 ~ 12,000원 확정공모가액 13,000원")
    assert out["above_band"] is True


# ─── 경쟁률: 문장이 아니라 표다 (2026-08-28 실측) ────
#
# 아래 두 블록은 스카이랩스 [발행조건확정]증권신고서(20260825000417)와
# 클로봇 증권발행실적보고서(20260824000231)에서 그대로 발췌했다.

SKYLABS_TABLE = (
    "일반청약자 배정분 500,000주 (25.00%) 는 수요예측 참여 대상주식이 아닙니다. "
    "(13) 수요예측 결과 (가) 수요예측 참여 내역 (단위: 건, 주) 구 분 국내 기관투자자 "
    "해외 기관투자자 합 계 건수 2 41 24 4 2 45 92 36 - 246 "
    "수량 890,000 16,208,000 4,779,000 1,532,000 1,501,000 8,990,000 47,192,000 "
    "14,020,000 - 95,112,000 "
    "경쟁률 0.59 10.81 3.19 1.02 1.00 5.99 31.46 9.35 - 63.41"
)
RIGHTS_OFFERING_TABLE = (
    "3. 초과청약 배정 후 실권주 처리 내역 : 실권주 일반공모 (단위: 주) "
    "일반공모 주식수 일반공모 청약주식수 일반공모 청약 경쟁률 292,491 143,321,648 490.00 : 1"
)


def test_the_total_of_the_table_is_the_competition_rate():
    """'XXX : 1' 문장을 찾던 패턴으로는 원리적으로 잡을 수 없었다 —
    그 문자열이 문서에 없다. 마지막 값이 합계다(95,112,000 ÷ 1,500,000 = 63.41)."""
    assert extract_ipo_metrics(SKYLABS_TABLE)["competition_rate"] == 63.41


def test_a_rights_offering_subscription_rate_is_not_a_demand_forecast():
    """같은 모양의 표가 유상증자 실권주 일반공모에도 있다. 문맥을 안 보면
    그 청약 경쟁률(490.00)을 기관 수요예측 경쟁률로 읽는다 — 실제로 그랬다."""
    assert extract_ipo_metrics(RIGHTS_OFFERING_TABLE)["competition_rate"] is None


def test_a_bare_mention_without_numbers_is_ignored():
    assert extract_ipo_metrics(
        "수요예측 경쟁률에 관한 주의사항 당사의 수요예측 예정일은 2026년 9월 28일입니다"
    )["competition_rate"] is None


def test_too_few_numbers_is_not_a_table():
    """숫자 두어 개는 표가 아니다 — 문장 속 숫자를 표로 오인하면 안 된다."""
    assert extract_ipo_metrics("수요예측 경쟁률 12.5")["competition_rate"] is None
