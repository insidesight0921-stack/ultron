"""tests/test_ipo_bot.py — IPO봇 단위 테스트 (v3.34)

대상:
  - 5점수 함수 (score_demand / score_band_position / score_float_ratio /
                 score_underwriter / score_offer_size)
  - compute_attraction_score (정상 / 요소 부족 / 전체 None)
  - _parse_38_html (목 HTML — 실제 38.co.kr 컬럼 구조)
  - _parse_kind_progcom_html (목 HTML — KIND 공모기업현황 구조)
  - _demand_forecast_done (rcept_no / final_price / demand_end 분기)
  - enrich_with_dart + scan_upcoming 밴드 보완 (v3.34 — 공모가 밴드 DART 백필)
  - format_result 밴드 표시
  - run() 스모크 테스트 (네트워크 mock)
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "scripts"))

from ipo_bot import (
    IpoItem,
    score_demand,
    score_band_position,
    score_float_ratio,
    score_underwriter,
    score_offer_size,
    compute_attraction_score,
    _parse_38_html,
    _parse_kind_progcom_html,
    _demand_forecast_done,
    enrich_with_dart,
    scan_upcoming,
    format_result,
    run,
)


# ═══════════════════════════════════════════════════════
# helpers
# ═══════════════════════════════════════════════════════

def _item(**kw) -> IpoItem:
    defaults = dict(
        corp_name="테스트기업",
        band_low=None, band_high=None, final_price=None,
        sub_start=None, sub_end=None, listing_date=None,
        competition_rate=None, float_ratio=None,
        underwriter=None, offer_amount=None, source="test",
    )
    defaults.update(kw)
    return IpoItem(**defaults)


# ═══════════════════════════════════════════════════════
# 1. score_demand
# ═══════════════════════════════════════════════════════

class TestScoreDemand:
    def test_none(self):
        assert score_demand(None) is None

    def test_very_low(self):
        assert score_demand(10) == 2.0

    def test_50(self):
        assert score_demand(50) == 5.0

    def test_100(self):
        assert score_demand(100) == 8.0

    def test_200(self):
        assert score_demand(200) == 11.0

    def test_500(self):
        assert score_demand(500) == 14.0

    def test_1000(self):
        assert score_demand(1000) == 17.0

    def test_1500(self):
        assert score_demand(1500) == 20.0

    def test_over_1500(self):
        assert score_demand(9999) == 20.0

    def test_300_in_200_bracket(self):
        assert score_demand(300) == 11.0


# ═══════════════════════════════════════════════════════
# 2. score_band_position
# ═══════════════════════════════════════════════════════

class TestScoreBandPosition:
    def test_all_none(self):
        assert score_band_position(None, None, None) is None

    def test_final_none(self):
        assert score_band_position(10000, 12000, None) is None

    def test_above_band(self):
        assert score_band_position(10000, 12000, 13000) == 20.0

    def test_ratio_0_9(self):
        # ratio = (11800-10000)/(12000-10000) = 0.9
        assert score_band_position(10000, 12000, 11800) == 16.0

    def test_ratio_0_7(self):
        # ratio = 0.7
        assert score_band_position(10000, 12000, 11400) == 12.0

    def test_ratio_0_5(self):
        assert score_band_position(10000, 12000, 11000) == 8.0

    def test_ratio_0_2(self):
        assert score_band_position(10000, 12000, 10400) == 4.0

    def test_ratio_0(self):
        assert score_band_position(10000, 12000, 10000) == 2.0

    def test_single_band(self):
        assert score_band_position(10000, 10000, 10000) == 10.0

    def test_high_none_no_above(self):
        # band_high None, final not exceeding → None
        assert score_band_position(10000, None, 9000) is None

    def test_band_missing_with_final(self):
        # 확정가는 있으나 밴드 미확정 → None (밴드 보완 대상)
        assert score_band_position(None, None, 12000) is None


# ═══════════════════════════════════════════════════════
# 3. score_float_ratio
# ═══════════════════════════════════════════════════════

class TestScoreFloatRatio:
    def test_none(self):
        assert score_float_ratio(None) is None

    def test_le_15(self):
        assert score_float_ratio(15) == 20.0

    def test_le_20(self):
        assert score_float_ratio(20) == 17.0

    def test_le_25(self):
        assert score_float_ratio(25) == 14.0

    def test_le_30(self):
        assert score_float_ratio(30) == 11.0

    def test_le_35(self):
        assert score_float_ratio(35) == 8.0

    def test_le_40(self):
        assert score_float_ratio(40) == 5.0

    def test_over_40(self):
        assert score_float_ratio(50) == 2.0

    def test_lockup_lowers_effective_ratio(self):
        # 명목 38.5% + 확약 78% → 실질 ~18.9% → 17점
        assert score_float_ratio(38.5, lockup_ratio=78.0) == 17.0


# ═══════════════════════════════════════════════════════
# 4. score_underwriter
# ═══════════════════════════════════════════════════════

class TestScoreUnderwriter:
    def test_none(self):
        assert score_underwriter(None) is None

    def test_empty(self):
        assert score_underwriter("") is None

    def test_tier1_mirae(self):
        assert score_underwriter("미래에셋증권") == 20.0

    def test_tier1_kb(self):
        assert score_underwriter("KB증권") == 20.0

    def test_tier1_nh(self):
        assert score_underwriter("NH투자증권") == 20.0

    def test_tier1_hantou(self):
        assert score_underwriter("한국투자증권") == 20.0

    def test_tier1_samsung(self):
        assert score_underwriter("삼성증권") == 20.0

    def test_tier2_kiwoom(self):
        assert score_underwriter("키움증권") == 14.0

    def test_tier2_hana(self):
        assert score_underwriter("하나증권") == 14.0

    def test_unknown_returns_tier3(self):
        result = score_underwriter("알수없는소형증권")
        assert result == 8.0


# ═══════════════════════════════════════════════════════
# 5. score_offer_size  (2026-08-28: 시총 → 공모금액으로 입력 교체)
#    자동으로 얻을 수 있는 값이 공모금액뿐이고, 단기 수급 부담을 결정하는 것도
#    시장에 새로 풀리는 금액 쪽이다. 임계값은 전부 가설이다.
# ═══════════════════════════════════════════════════════

class TestScoreOfferSize:
    def test_none(self):
        assert score_offer_size(None) is None

    def test_le_100(self):
        assert score_offer_size(100) == 20.0

    def test_le_200(self):
        assert score_offer_size(200) == 17.0

    def test_le_400(self):
        assert score_offer_size(400) == 14.0

    def test_le_800(self):
        assert score_offer_size(800) == 11.0

    def test_le_1500(self):
        assert score_offer_size(1500) == 8.0

    def test_le_3000(self):
        assert score_offer_size(3000) == 5.0

    def test_over_3000(self):
        assert score_offer_size(5000) == 2.0

    def test_smaller_offer_scores_higher(self):
        """방향이 뒤집히면 큰 공모를 좋다고 추천하게 된다."""
        assert score_offer_size(100) > score_offer_size(1000) > score_offer_size(5000)


# ═══════════════════════════════════════════════════════
# 6. compute_attraction_score
# ═══════════════════════════════════════════════════════

class TestComputeAttractionScore:
    def test_all_none_grade_question(self):
        res = compute_attraction_score(_item())
        assert res.grade == "?"
        assert res.total_score is None
        assert res.confirmed_factors == 0

    def test_two_factors_still_question(self):
        res = compute_attraction_score(_item(competition_rate=1000, float_ratio=20))
        assert res.grade == "?"
        assert res.confirmed_factors == 2

    def test_three_factors_gives_grade(self):
        # demand=17, float=17, size=20 → 54/60 → 90.0 → A++
        res = compute_attraction_score(_item(
            competition_rate=1000, float_ratio=20, offer_amount=100,
        ))
        assert res.grade != "?"
        assert res.total_score is not None
        assert res.confirmed_factors == 3
        assert res.grade == "A++"

    def test_all_five_perfect(self):
        res = compute_attraction_score(_item(
            competition_rate=1500,
            band_low=10000, band_high=12000, final_price=13000,
            float_ratio=10,
            underwriter="미래에셋증권",
            offer_amount=100,
        ))
        assert res.total_score == 100.0
        assert res.grade == "A++"
        assert res.confirmed_factors == 5

    def test_all_five_low(self):
        res = compute_attraction_score(_item(
            competition_rate=10,
            band_low=10000, band_high=12000, final_price=10000,
            float_ratio=60,
            underwriter="알수없는소형증권",
            offer_amount=5000,
        ))
        assert res.total_score is not None
        assert res.grade in ("C", "B")

    def test_note_when_partial(self):
        res = compute_attraction_score(_item(
            competition_rate=500, float_ratio=25, offer_amount=1000,
        ))
        assert res.confirmed_factors == 3
        assert "미확정" in res.note or "확정" in res.note

    def test_no_note_when_full(self):
        res = compute_attraction_score(_item(
            competition_rate=1000,
            band_low=10000, band_high=12000, final_price=11500,
            float_ratio=20,
            underwriter="KB증권",
            offer_amount=2000,
        ))
        assert res.confirmed_factors == 5
        assert res.note == ""

    def test_band_missing_drops_factor(self):
        # 확정가는 있으나 밴드 미확정 → band_score None → 4개 요소만 확정
        res = compute_attraction_score(_item(
            competition_rate=1000,
            final_price=11500,        # 밴드 없음
            float_ratio=20,
            underwriter="KB증권",
            offer_amount=2000,
        ))
        assert res.band_score is None
        assert res.confirmed_factors == 4

    def test_grade_thresholds(self):
        # A++ >= 85
        res = compute_attraction_score(_item(
            competition_rate=1500, float_ratio=15, offer_amount=100,
            underwriter="NH투자증권", band_low=10000, band_high=12000, final_price=13000,
        ))
        assert res.grade == "A++"

        # B: 50~64
        res2 = compute_attraction_score(_item(
            competition_rate=100,  # 8
            float_ratio=35,        # 8
            offer_amount=1500,      # 8
        ))
        # 24/60 → 40.0 → C
        assert res2.grade in ("C", "B")


# ═══════════════════════════════════════════════════════
# 7. _parse_38_html  (실제 38.co.kr 컬럼 구조)
#    col0 종목명 / col1 청약기간 / col2 확정공모가 /
#    col3 공모가범위 / col4 구분 / col5 주관사 / col6 분석버튼
# ═══════════════════════════════════════════════════════

_MOCK_38_HTML = """
<html><body>
<table>
  <tr>
    <th>종목명</th><th>청약기간</th><th>확정공모가</th>
    <th>공모가범위</th><th>구분</th><th>주관사</th><th>분석</th>
  </tr>
  <tr>
    <td><a href="#">알파테크</a></td>
    <td>2026.05.20~05.21</td>
    <td>13,000</td>
    <td>10,000~12,000</td>
    <td></td>
    <td>미래에셋증권,한국투자증권</td>
    <td></td>
  </tr>
  <tr>
    <td><a href="#">베타솔루션</a></td>
    <td>2026.06.03~06.04</td>
    <td>-</td>
    <td>8,000~10,000</td>
    <td></td>
    <td>KB증권</td>
    <td></td>
  </tr>
</table>
</body></html>
"""


class TestParse38Html:
    def test_returns_list(self):
        assert isinstance(_parse_38_html(_MOCK_38_HTML), list)

    def test_skips_header(self):
        names = [i.corp_name for i in _parse_38_html(_MOCK_38_HTML)]
        assert "종목명" not in names

    def test_finds_alpha_tech(self):
        names = [i.corp_name for i in _parse_38_html(_MOCK_38_HTML)]
        assert any("알파테크" in n for n in names)

    def test_band_parsed(self):
        items = _parse_38_html(_MOCK_38_HTML)
        alpha = next((i for i in items if "알파테크" in i.corp_name), None)
        assert alpha is not None
        assert alpha.band_low == 10000.0
        assert alpha.band_high == 12000.0

    def test_final_price_parsed(self):
        items = _parse_38_html(_MOCK_38_HTML)
        alpha = next((i for i in items if "알파테크" in i.corp_name), None)
        assert alpha is not None
        assert alpha.final_price == 13000.0

    def test_dash_final_price_is_none(self):
        items = _parse_38_html(_MOCK_38_HTML)
        beta = next((i for i in items if "베타솔루션" in i.corp_name), None)
        assert beta is not None
        assert beta.final_price is None
        # 밴드는 확정가와 무관하게 추출됨
        assert beta.band_low == 8000.0
        assert beta.band_high == 10000.0

    def test_underwriter_first_only(self):
        items = _parse_38_html(_MOCK_38_HTML)
        alpha = next((i for i in items if "알파테크" in i.corp_name), None)
        assert alpha is not None
        assert alpha.underwriter == "미래에셋증권"

    def test_sub_dates_parsed(self):
        items = _parse_38_html(_MOCK_38_HTML)
        alpha = next((i for i in items if "알파테크" in i.corp_name), None)
        assert alpha is not None
        assert alpha.sub_start == "20260520"
        assert alpha.sub_end == "20260521"

    def test_source_38(self):
        for item in _parse_38_html(_MOCK_38_HTML):
            assert item.source == "38"

    def test_empty_html(self):
        assert _parse_38_html("") == []


# ═══════════════════════════════════════════════════════
# 8. _parse_kind_progcom_html  (KIND 공모기업현황)
#    progcom 테이블엔 공모가 밴드 컬럼이 없음 → band_* 는 항상 None
# ═══════════════════════════════════════════════════════

_MOCK_PROGCOM_HTML = """
<table class="list type-00">
  <tbody>
    <tr style="cursor: pointer;" onclick="fnDetailView('20250930000202')">
      <td class="first" title="마키나락스">
        <img src='x.gif' alt='코스닥' /> 마키나락스
      </td>
      <td class="txc">2026-03-25</td>
      <td class="txc">2026-04-28<br/> ~ 2026-05-06</td>
      <td class="txc">2026-05-11<br/> ~ 2026-05-12</td>
      <td class="txc">2026-05-14</td>
      <td class="txr">15,000</td>
      <td class="txr">39,525</td>
      <td class="txc">2026-05-20</td>
      <td class="txl">미래에셋증권 주식회사</td>
    </tr>
    <tr style="cursor: pointer;" onclick="fnDetailView('20250124000429')">
      <td class="first" title="져스텍">
        <img src='x.gif' alt='코스닥' /> 져스텍
      </td>
      <td class="txc">2026-04-16</td>
      <td class="txc">2026-05-18<br/> ~ 2026-05-22</td>
      <td class="txc">2026-05-29<br/> ~ 2026-06-01</td>
      <td class="txc">2026-06-04</td>
      <td class="txr">-</td>
      <td class="txr">-</td>
      <td class="txc">2026-06-11</td>
      <td class="txl">삼성증권(주)</td>
    </tr>
  </tbody>
</table>
"""


class TestParseKindProgcom:
    def test_returns_list(self):
        assert isinstance(_parse_kind_progcom_html(_MOCK_PROGCOM_HTML), list)

    def test_finds_makinarocks(self):
        names = [i.corp_name for i in _parse_kind_progcom_html(_MOCK_PROGCOM_HTML)]
        assert any("마키나락스" in n for n in names)

    def test_band_is_always_none(self):
        # KIND 공모기업현황 테이블엔 공모가 밴드 컬럼이 없음
        for item in _parse_kind_progcom_html(_MOCK_PROGCOM_HTML):
            assert item.band_low is None
            assert item.band_high is None

    def test_final_price_parsed(self):
        items = _parse_kind_progcom_html(_MOCK_PROGCOM_HTML)
        mkn = next((i for i in items if "마키나락스" in i.corp_name), None)
        assert mkn is not None
        assert mkn.final_price == 15000.0

    def test_dash_final_price_is_none(self):
        items = _parse_kind_progcom_html(_MOCK_PROGCOM_HTML)
        jst = next((i for i in items if "져스텍" in i.corp_name), None)
        assert jst is not None
        assert jst.final_price is None

    def test_dates_parsed(self):
        items = _parse_kind_progcom_html(_MOCK_PROGCOM_HTML)
        mkn = next((i for i in items if "마키나락스" in i.corp_name), None)
        assert mkn is not None
        assert mkn.demand_start == "20260428"
        assert mkn.demand_end == "20260506"
        assert mkn.sub_start == "20260511"
        assert mkn.sub_end == "20260512"
        assert mkn.listing_date == "20260520"

    def test_source(self):
        for item in _parse_kind_progcom_html(_MOCK_PROGCOM_HTML):
            assert item.source == "kind_progcom"

    def test_empty_html(self):
        assert _parse_kind_progcom_html("") == []


# ═══════════════════════════════════════════════════════
# 9. _demand_forecast_done  (v3.34 — final_price 분기 추가)
# ═══════════════════════════════════════════════════════

class TestDemandForecastDone:
    def test_rcept_no_true(self):
        assert _demand_forecast_done(_item(rcept_no="20260101000001")) is True

    def test_final_price_set_true(self):
        # 확정공모가가 잡혀 있으면 demand_end 없어도 수요예측 종료 확정
        assert _demand_forecast_done(_item(final_price=15000)) is True

    def test_no_demand_end_false(self):
        assert _demand_forecast_done(_item()) is False

    def test_demand_end_past_true(self):
        assert _demand_forecast_done(_item(demand_end="20200101")) is True

    def test_demand_end_future_false(self):
        assert _demand_forecast_done(_item(demand_end="29991231")) is False


# ═══════════════════════════════════════════════════════
# 10. enrich_with_dart — 공모가 밴드 보완 (v3.34)
# ═══════════════════════════════════════════════════════

class TestEnrichBand:
    def test_dart_fills_missing_band(self):
        item = _item(corp_name="밴드없는종목")  # band_low/high None
        fake = {"offer_band_low": 10000, "offer_band_high": 13000}
        with patch("ipo_bot.fetch_dart_metrics", return_value=fake):
            out = enrich_with_dart(item)
        assert out.band_low == 10000
        assert out.band_high == 13000

    def test_dart_does_not_override_existing_band(self):
        item = _item(corp_name="밴드있는종목", band_low=9000, band_high=9500)
        fake = {"offer_band_low": 10000, "offer_band_high": 13000}
        with patch("ipo_bot.fetch_dart_metrics", return_value=fake):
            out = enrich_with_dart(item)
        assert out.band_low == 9000
        assert out.band_high == 9500

    def test_dart_empty_metrics_leaves_band_none(self):
        item = _item(corp_name="공시없는종목")
        with patch("ipo_bot.fetch_dart_metrics", return_value={}):
            out = enrich_with_dart(item)
        assert out.band_low is None
        assert out.band_high is None

    def test_dart_also_fills_competition_and_final(self):
        item = _item(corp_name="종합보완종목")
        fake = {
            "offer_band_low": 10000, "offer_band_high": 13000,
            "competition_rate": 1200, "final_price": 14000,
        }
        with patch("ipo_bot.fetch_dart_metrics", return_value=fake):
            out = enrich_with_dart(item)
        assert out.band_low == 10000
        assert out.competition_rate == 1200
        assert out.final_price == 14000


# ═══════════════════════════════════════════════════════
# 11. scan_upcoming — 밴드 미확정 시 DART 백필 (v3.34)
# ═══════════════════════════════════════════════════════

class TestScanBandBackfill:
    def test_backfill_band_for_upcoming_item(self):
        # KIND progcom-only 스타일: 밴드 없음 + 확정가 없음 + 수요예측 미래
        upcoming = IpoItem(
            corp_name="신규상장종목", source="kind_progcom",
            sub_start="29990101", sub_end="29990102",
            demand_end="29991231",
            band_low=None, band_high=None, final_price=None,
        )
        dart_metrics = {"offer_band_low": 12000, "offer_band_high": 15000}
        with patch("ipo_bot.fetch_ipo_schedule", return_value=[upcoming]), \
             patch("ipo_bot.fetch_dart_metrics", return_value=dart_metrics):
            results = scan_upcoming(days_ahead=30, top_n=10)
        assert len(results) == 1
        assert results[0]["band_low"] == 12000
        assert results[0]["band_high"] == 15000

    def test_no_dart_call_when_band_present_and_forecast_pending(self):
        # 밴드 이미 있음(38 종목) + 수요예측 미래 → DART 호출 불필요
        item = IpoItem(
            corp_name="38종목", source="38",
            sub_start="29990101", sub_end="29990102",
            demand_end="29991231",
            band_low=10000, band_high=12000, final_price=None,
        )
        dart_mock = MagicMock(return_value={})
        with patch("ipo_bot.fetch_ipo_schedule", return_value=[item]), \
             patch("ipo_bot.fetch_dart_metrics", dart_mock):
            scan_upcoming(days_ahead=30, top_n=10)
        dart_mock.assert_not_called()

    def test_dart_called_when_forecast_done(self):
        # 확정가 있음 → 수요예측 종료 확정 → 전체 보강 트리거
        item = IpoItem(
            corp_name="확정종목", source="kind_progcom",
            sub_start="20260101", sub_end="20260102",
            demand_end="20251231",
            band_low=None, band_high=None, final_price=14000,
        )
        dart_mock = MagicMock(return_value={
            "offer_band_low": 12000, "offer_band_high": 15000,
        })
        with patch("ipo_bot.fetch_ipo_schedule", return_value=[item]), \
             patch("ipo_bot.fetch_dart_metrics", dart_mock):
            results = scan_upcoming(days_ahead=30, top_n=10)
        dart_mock.assert_called_once()
        assert results[0]["band_low"] == 12000


# ═══════════════════════════════════════════════════════
# 12. format_result — 밴드 표시 (v3.34)
# ═══════════════════════════════════════════════════════

class TestFormatResultBand:
    def test_shows_band_when_no_final_price(self):
        results = [{
            "corp_name": "밴드표시종목", "grade": "A", "total_score": 70.0,
            "confirmed_factors": 3, "final_price": None,
            "band_low": 12000, "band_high": 15000,
        }]
        out = format_result(results)
        assert "12,000~15,000원(밴드)" in out

    def test_shows_final_price_when_confirmed(self):
        results = [{
            "corp_name": "확정가종목", "grade": "A", "total_score": 70.0,
            "confirmed_factors": 3, "final_price": 14000,
            "band_low": 12000, "band_high": 15000,
        }]
        out = format_result(results)
        assert "14,000원(확정)" in out

    def test_shows_undetermined_when_nothing(self):
        results = [{
            "corp_name": "미확정종목", "grade": "?", "total_score": None,
            "confirmed_factors": 0, "final_price": None,
            "band_low": None, "band_high": None,
        }]
        out = format_result(results)
        assert "미확정" in out

    def test_single_band_value(self):
        results = [{
            "corp_name": "단일밴드종목", "grade": "B", "total_score": 55.0,
            "confirmed_factors": 3, "final_price": None,
            "band_low": 2000, "band_high": 2000,
        }]
        out = format_result(results)
        assert "2,000원(밴드)" in out


# ═══════════════════════════════════════════════════════
# 13. run() 스모크 테스트
# ═══════════════════════════════════════════════════════

class TestRunSmoke:
    @patch("ipo_bot.fetch_ipo_schedule", return_value=[])
    def test_scan_empty(self, _mock):
        answer, chunks = run(action="scan")
        assert isinstance(answer, str)
        assert isinstance(chunks, list)

    @patch("ipo_bot.fetch_dart_metrics", return_value={})
    @patch("ipo_bot.fetch_ipo_schedule", return_value=[
        IpoItem(
            corp_name="스모크테스트",
            band_low=10000, band_high=12000, final_price=13000,
            sub_start=None, sub_end=None, listing_date=None,
            competition_rate=1000, float_ratio=20,
            underwriter="KB증권", offer_amount=500, source="test",
        )
    ])
    def test_scan_with_item(self, _mock_sched, _mock_dart):
        answer, chunks = run(action="scan")
        all_text = answer + "".join(str(c) for c in chunks)
        assert "스모크테스트" in all_text

    def test_analyze_manual(self):
        answer, chunks = run(
            action="analyze",
            corp_name="수동분석종목",
            competition_rate=800,
            band_low=10000,
            band_high=12000,
            final_price=12500,
            float_ratio=22,
            underwriter="미래에셋증권",
            offer_amount=2000,
        )
        assert isinstance(answer, str)
        assert "수동분석종목" in answer

    def test_analyze_manual_shows_band(self):
        answer, _ = run(
            action="analyze",
            corp_name="밴드표시분석",
            band_low=13000,
            band_high=15000,
            final_price=15000,
        )
        # analyze 출력 ②번 줄에 밴드 숫자 표시
        assert "13,000~15,000원" in answer


# ═══════════════════════════════════════════════════════
# 12. 시점별 2단계 등급 (2026-08-28)
#
# 5요소 중 셋(경쟁률·밴드위치·확정가)은 수요예측이 끝나야 존재한다. 그런데
# 필요 확정 요소를 일률적으로 3개로 두는 바람에, 청약 전 스캔에서는 아무리
# 수집이 잘 돼도 등급이 나올 수 없었다. 몇 달간 IPO봇이 아무것도 다루지 못한
# 원인이 이 어긋남이다.
# ═══════════════════════════════════════════════════════

from ipo_bot import (  # noqa: E402
    STAGE_FINAL,
    STAGE_PRE,
    demand_stage,
    diagnose_scan,
    _parse_offer_amount,
)


class TestDemandStage:
    def test_before_the_forecast_it_is_the_pre_stage(self):
        assert demand_stage(_item(underwriter="KB증권")) == STAGE_PRE

    def test_a_competition_rate_means_the_forecast_is_done(self):
        assert demand_stage(_item(competition_rate=800)) == STAGE_FINAL

    def test_a_final_price_also_means_the_forecast_is_done(self):
        assert demand_stage(_item(final_price=12000)) == STAGE_FINAL

    def test_the_schedule_alone_does_not_decide(self):
        """일정이 지났다고 결과가 손에 들어온 것은 아니다.

        일정만 보고 '확정 단계'라 선언하면, 확보하지도 못한 값을 요구하다
        등급이 통째로 사라진다 — 정확히 그 어긋남이 이번 장애의 원인이었다.
        """
        assert demand_stage(_item(demand_end="20200101")) == STAGE_PRE


class TestTwoStageGrade:
    def test_two_factors_are_enough_before_the_forecast(self):
        res = compute_attraction_score(_item(
            underwriter="미래에셋증권", offer_amount=100))
        assert res.stage == STAGE_PRE
        assert res.grade != "?" and res.confirmed_factors == 2

    def test_the_pre_grade_says_it_is_not_for_subscription(self):
        """요소 2개로 낸 등급을 확정 등급처럼 다루면 과신이 된다."""
        res = compute_attraction_score(_item(
            underwriter="미래에셋증권", offer_amount=100))
        assert "사전등급" in res.note and "자동 구독 대상이 아닙니다" in res.note

    def test_one_factor_is_still_not_enough(self):
        res = compute_attraction_score(_item(underwriter="KB증권"))
        assert res.grade == "?" and "공모금액 미확보" in res.note

    def test_the_missing_piece_is_named(self):
        """'산출불가'만으로는 어느 수집기를 고칠지 알 수 없다."""
        res = compute_attraction_score(_item(offer_amount=100))
        assert "주관사 미확보" in res.note

    def test_the_final_stage_still_needs_three(self):
        res = compute_attraction_score(_item(competition_rate=800, offer_amount=100))
        assert res.stage == STAGE_FINAL and res.grade == "?"

    def test_a_missing_competition_rate_is_flagged_after_the_forecast(self):
        """확정가는 왔는데 경쟁률이 없으면 DART 파싱을 의심해야 한다."""
        res = compute_attraction_score(_item(
            final_price=12000, band_low=10000, band_high=12000,
            underwriter="KB증권", offer_amount=100))
        assert "경쟁률 미확보" in res.note


class TestOfferAmountParsing:
    def test_kind_gives_millions_we_store_hundred_millions(self):
        assert _parse_offer_amount("13,000") == 130.0      # 130억
        assert _parse_offer_amount("110,700") == 1107.0

    def test_undetermined_is_none_not_zero(self):
        """0으로 채우면 '공모금액 0억'이 되어 규모 점수 20점(최고)을 받는다 —
        없는 정보가 최고 점수로 둔갑한다."""
        for raw in ("-", "", "미정", "&nbsp;"):
            assert _parse_offer_amount(raw) is None

    def test_zero_is_treated_as_missing(self):
        assert _parse_offer_amount("0") is None


class TestDiagnoseStages:
    MIN = {"A++", "A+", "A"}

    def _row(self, grade, stage):
        return {"corp_name": "가", "grade": grade, "stage": stage}

    def test_a_pre_grade_does_not_trigger_subscription(self):
        d = diagnose_scan([self._row("A++", STAGE_PRE)], self.MIN)
        assert d["verdict"] == "preview_only" and d["hot"] == []
        assert len(d["preview"]) == 1

    def test_a_final_grade_does(self):
        d = diagnose_scan([self._row("A+", STAGE_FINAL)], self.MIN)
        assert d["verdict"] == "hot" and len(d["hot"]) == 1

    def test_a_row_without_a_stage_is_treated_as_final(self):
        """단계를 싣지 않는 옛 기록이 조용히 무시되면 안 된다."""
        d = diagnose_scan([{"corp_name": "가", "grade": "A"}], self.MIN)
        assert d["verdict"] == "hot"


class TestProgcomOfferAmount:
    """col6 공모금액(백만원) — 값이 있는데도 읽지 않고 있었다(2026-08-28).

    채점 5요소 중 '규모'가 늘 비어 있던 직접 원인이다.
    """

    def test_offer_amount_is_converted_to_hundred_millions(self):
        items = _parse_kind_progcom_html(_MOCK_PROGCOM_HTML)
        mkn = next(i for i in items if "마키나락스" in i.corp_name)
        assert mkn.offer_amount == 395.2      # 39,525 백만원 → 395.2억

    def test_a_dash_stays_none(self):
        items = _parse_kind_progcom_html(_MOCK_PROGCOM_HTML)
        jst = next(i for i in items if "져스텍" in i.corp_name)
        assert jst.offer_amount is None

    def test_the_merge_carries_offer_amount_over(self):
        """공모금액은 KIND 공모기업현황에만 있다. 병합에서 옮기지 않으면
        38에 실린 종목(대부분)은 규모 요소가 영원히 빈다."""
        src = (Path(__file__).resolve().parents[1] / "scripts" / "ipo_bot.py").read_text(
            encoding="utf-8")
        body = src[src.index("def fetch_ipo_schedule("):]
        body = body[:body.index("\ndef ", 10)]
        assert body.count("offer_amount=item.offer_amount or p.offer_amount") == 2


class TestEndToEndGradeRecovery:
    """수정 전에는 이 조합이 전부 '?'였다 — 실측(2026-08-28)으로 확인한 회귀."""

    def test_a_merged_item_reaches_a_grade(self):
        item = _item(
            band_low=12500, band_high=15000, final_price=15000,
            underwriter="미래에셋증권", offer_amount=395.2)
        res = compute_attraction_score(item)
        assert res.grade != "?" and res.confirmed_factors == 3
        assert res.stage == STAGE_FINAL

    def test_without_the_offer_amount_it_falls_back_to_ungraded(self):
        """공모금액 하나가 등급의 성립 여부를 가른다."""
        item = _item(
            band_low=12500, band_high=15000, final_price=15000,
            underwriter="미래에셋증권")
        assert compute_attraction_score(item).grade == "?"


class TestBandPositionBelowBand:
    """밴드 하단 **미만** 확정 — 2026-08 스카이랩스 실측으로 드러난 구멍.

    희망 13,000~16,000 → 확정 10,000원(하단의 77%). 수요예측 경쟁률 63.41이고
    신청 물량의 60%가 하단 미만 가격이었다. 기관 수요가 희망 범위조차 채우지
    못한 것이라, 하단 확정과 같은 점수를 주면 가장 나쁜 신호가 최저 점수와
    동점이 된다.
    """

    def test_below_the_band_scores_lower_than_at_the_band(self):
        from ipo_bot import score_band_position
        below = score_band_position(13000, 16000, 10000)
        at_low = score_band_position(13000, 16000, 13000)
        assert below < at_low

    def test_below_the_band_is_zero(self):
        from ipo_bot import score_band_position
        assert score_band_position(13000, 16000, 10000) == 0.0

    def test_the_ordering_holds_across_the_whole_range(self):
        from ipo_bot import score_band_position
        scores = [score_band_position(10000, 20000, p)
                  for p in (9000, 10000, 14000, 19000, 21000)]
        assert scores == sorted(scores)

    def test_zero_still_counts_as_a_confirmed_factor(self):
        """0점은 '모름'이 아니라 '나쁨'이다 — 요소에서 빠지면 안 된다."""
        res = compute_attraction_score(_item(
            band_low=13000, band_high=16000, final_price=10000,
            underwriter="한국투자증권", offer_amount=200))
        assert res.band_score == 0.0
        assert res.confirmed_factors == 3


# ═══════════════════════════════════════════════════════
# 13. 공시 선택·병합 (2026-08-28)
#
# 필요한 값이 한 문서에 다 있지 않다. 밴드는 증권신고서에, 경쟁률·확정가는
# [발행조건확정]에 있다. 그런데 기존 코드는 rcept_dt 최신순 1건만 읽었다.
# 스카이랩스는 [발행조건확정](…417)과 [기재정정]투자설명서(…414)가 같은 날
# 올라왔고, 경쟁률은 앞쪽에만 있었다.
# ═══════════════════════════════════════════════════════

from ipo_bot import (  # noqa: E402
    filing_rank,
    merge_metrics,
    metrics_complete,
    pick_filings,
)


def _f(nm, dt, no="1"):
    return {"report_nm": nm, "rcept_dt": dt, "rcept_no": no}


class TestFilingRank:
    def test_the_confirmed_terms_filing_comes_first(self):
        assert filing_rank("[발행조건확정]증권신고서(지분증권)") < filing_rank("증권신고서(지분증권)")

    def test_a_prospectus_sits_between(self):
        assert (filing_rank("[발행조건확정]증권신고서")
                < filing_rank("[기재정정]투자설명서")
                < filing_rank("증권신고서(지분증권)"))

    def test_unrelated_filings_rank_last(self):
        for nm in ("철회신고서", "증권발행실적보고서", "분기보고서"):
            assert filing_rank(nm) > filing_rank("증권신고서(지분증권)")


class TestPickFilings:
    def test_kind_beats_recency(self):
        """같은 날 올라온 두 건 중 경쟁률이 든 쪽을 골라야 한다."""
        picked = pick_filings([_f("[기재정정]투자설명서", "20260825", "414"),
                               _f("[발행조건확정]증권신고서", "20260825", "417")])
        assert picked[0]["rcept_no"] == "417"

    def test_recency_breaks_ties_within_a_kind(self):
        picked = pick_filings([_f("증권신고서(지분증권)", "20260701", "old"),
                               _f("증권신고서(지분증권)", "20260825", "new")])
        assert picked[0]["rcept_no"] == "new"

    def test_unrelated_filings_are_dropped_entirely(self):
        """읽어 봐야 IPO 수치가 없고, 유상증자 실권주 청약 경쟁률 같은
        **닮은 값**이 있어 오히려 오탐의 원인이 된다."""
        picked = pick_filings([_f("증권발행실적보고서", "20260828", "x"),
                               _f("철회신고서", "20260827", "y")])
        assert picked == []

    def test_the_limit_is_respected(self):
        many = [_f("증권신고서(지분증권)", f"2026080{i}", str(i)) for i in range(1, 9)]
        assert len(pick_filings(many, limit=3)) == 3


class TestMergeMetrics:
    def test_the_first_value_wins(self):
        """우선순위 높은 문서를 먼저 넣으므로, 뒤의 예정가액이 확정가를
        덮어쓰면 안 된다."""
        merged = merge_metrics([{"final_price": 10000.0},
                                {"final_price": 13000.0}])
        assert merged["final_price"] == 10000.0

    def test_gaps_are_filled_from_later_documents(self):
        merged = merge_metrics([{"competition_rate": 63.41},
                                {"offer_band_low": 13000.0, "offer_band_high": 16000.0}])
        assert merged["competition_rate"] == 63.41
        assert merged["offer_band_high"] == 16000.0

    def test_nothing_produces_all_none(self):
        merged = merge_metrics([])
        assert set(merged.values()) == {None}

    def test_none_never_overwrites_a_value(self):
        merged = merge_metrics([{"final_price": 10000.0}, {"final_price": None}])
        assert merged["final_price"] == 10000.0


class TestMetricsComplete:
    def test_rate_and_band_are_enough_to_stop(self):
        """더 받을 이유가 없으면 다운로드를 멈춘다."""
        assert metrics_complete({"competition_rate": 63.41, "offer_band_high": 16000.0})

    def test_a_band_alone_is_not_enough(self):
        assert not metrics_complete({"offer_band_high": 16000.0})

    def test_a_rate_alone_is_not_enough(self):
        assert not metrics_complete({"competition_rate": 63.41})


# ═══════════════════════════════════════════════════════
# 14. 보강 범위·스팩 (2026-08-28 실측 스캔에서 드러남)
# ═══════════════════════════════════════════════════════

from ipo_bot import is_spac  # noqa: E402


class TestEnrichmentScope:
    """일정 필터와 값 보강은 별개의 일이다.

    실측: KIND 공모기업현황 10건 중 30일 이내는 1건뿐이었고, 그 필터된 목록을
    보강 맵으로 써서 38의 10건이 공모금액을 하나도 못 받았다. 청약일이 범위
    밖이라는 이유로 **이미 보여주기로 한 종목의 값까지 버려진** 것이다.
    """

    def test_progcom_can_return_the_unfiltered_list(self):
        src = (Path(__file__).resolve().parents[1] / "scripts" / "ipo_bot.py").read_text(
            encoding="utf-8")
        assert "include_all: bool = False" in src

    def test_the_merge_map_uses_the_unfiltered_list(self):
        src = (Path(__file__).resolve().parents[1] / "scripts" / "ipo_bot.py").read_text(
            encoding="utf-8")
        body = src[src.index("def fetch_ipo_schedule("):]
        body = body[:body.index("\ndef ", 10)]
        assert "build_match_map(progcom_all)" in body
        assert "progcom_items)" not in body.split("build_match_map(progcom_all)")[1]


class TestSpac:
    def test_a_spac_is_recognised_by_name(self):
        for name in ("KB스팩34호", "한국스팩17호", "엔에이치기업인수목적34호"):
            assert is_spac(_item(corp_name=name))

    def test_an_operating_company_is_not_a_spac(self):
        for name in ("스카이랩스", "빅웨이브로보틱스", "덕산넵코어스"):
            assert not is_spac(_item(corp_name=name))

    def test_a_spac_is_not_flagged_for_a_missing_competition_rate(self):
        """스팩은 기관 수요예측을 하지 않는다. 고칠 것이 없는데 고장으로 읽히면
        진짜 파싱 실패가 그 잡음에 묻힌다."""
        res = compute_attraction_score(_item(corp_name="NH스팩34호", final_price=2000))
        assert "경쟁률 미확보" not in res.note

    def test_an_operating_company_is_still_flagged(self):
        res = compute_attraction_score(_item(corp_name="스카이랩스", final_price=10000))
        assert "경쟁률 미확보" in res.note


class TestDemandForecastLookup:
    """demand_end를 못 받은 종목이 영원히 DART 조회에서 빠지지 않게.

    38 목록에는 수요예측 일정이 없다. `demand_end`만 보면 그 종목들은 청약
    당일까지도 경쟁률을 확인하지 않는다. 수요예측은 제도상 청약 전에 반드시
    끝나므로, 청약이 코앞이면 한 번은 확인한다.
    """

    def _days_from_now(self, n):
        from datetime import date, timedelta
        return (date.today() + timedelta(days=n)).strftime("%Y%m%d")

    def test_an_imminent_subscription_triggers_a_lookup(self):
        from ipo_bot import _demand_forecast_done
        assert _demand_forecast_done(_item(sub_start=self._days_from_now(2)))

    def test_a_distant_subscription_does_not(self):
        """아직 수요예측 전이면 조회해 봐야 값이 없다 — 다운로드만 낭비한다."""
        from ipo_bot import _demand_forecast_done
        assert not _demand_forecast_done(_item(sub_start=self._days_from_now(30)))

    def test_an_explicit_demand_end_still_wins(self):
        from ipo_bot import _demand_forecast_done
        assert _demand_forecast_done(_item(demand_end="20200101",
                                           sub_start=self._days_from_now(30)))

    def test_nothing_known_means_no_lookup(self):
        from ipo_bot import _demand_forecast_done
        assert not _demand_forecast_done(_item())


# ═══════════════════════════════════════════════════════
# 15. 소스 간 종목명 대조 (2026-08-28 실측)
#
# 38 10건 × KIND 10건 → 매칭 0건이었다. 병합이 이름 **완전 일치**만 봤다.
# ═══════════════════════════════════════════════════════

from ipo_bot import build_match_map, match_key  # noqa: E402


class TestMatchKey:
    def test_a_parenthetical_former_name_is_dropped(self):
        """38은 옛 이름을 괄호로 덧붙인다: '덕산넵코어스(구.넵코어스)'."""
        assert match_key("덕산넵코어스(구.넵코어스)") == match_key("덕산넵코어스")

    def test_full_width_parentheses_too(self):
        assert match_key("브릴스（주1）") == match_key("브릴스")

    def test_spacing_and_separators_do_not_matter(self):
        assert match_key("와이즈플래닛 컴퍼니") == match_key("와이즈플래닛컴퍼니")
        assert match_key("에스·케이") == match_key("에스케이")

    def test_corporate_suffixes_are_dropped(self):
        assert match_key("(주)브릴스") == match_key("브릴스 주식회사")

    def test_different_companies_still_differ(self):
        """정규화가 지나치면 엉뚱한 회사가 붙는다 — 값이 비는 것보다 나쁘다."""
        assert match_key("네오사피엔스") != match_key("네오이뮨텍")
        assert match_key("한국스팩17호") != match_key("한국스팩18호")

    def test_an_empty_name_is_an_empty_key(self):
        assert match_key("") == "" and match_key(None) == ""


class TestBuildMatchMap:
    def test_items_are_reachable_by_key(self):
        items = [_item(corp_name="덕산넵코어스")]
        assert build_match_map(items)[match_key("덕산넵코어스(구.넵코어스)")] is items[0]

    def test_colliding_keys_are_dropped_entirely(self):
        """서로 다른 회사가 같은 키로 접히면 엉뚱한 값이 보강된다."""
        items = [_item(corp_name="브릴스"), _item(corp_name="(주)브릴스")]
        assert build_match_map(items) == {}

    def test_nameless_items_are_skipped(self):
        assert build_match_map([_item(corp_name="")]) == {}
