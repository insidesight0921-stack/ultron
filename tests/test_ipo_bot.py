"""tests/test_ipo_bot.py — IPO봇 단위 테스트 (v3.34)

대상:
  - 5점수 함수 (score_demand / score_band_position / score_float_ratio /
                 score_underwriter / score_size)
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
    score_size,
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
        underwriter=None, market_cap=None, source="test",
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
# 5. score_size
# ═══════════════════════════════════════════════════════

class TestScoreSize:
    def test_none(self):
        assert score_size(None) is None

    def test_le_500(self):
        assert score_size(500) == 20.0

    def test_le_1000(self):
        assert score_size(1000) == 17.0

    def test_le_3000(self):
        assert score_size(3000) == 14.0

    def test_le_5000(self):
        assert score_size(5000) == 11.0

    def test_le_10000(self):
        assert score_size(10000) == 8.0

    def test_le_30000(self):
        assert score_size(30000) == 5.0

    def test_over_30000(self):
        assert score_size(100000) == 2.0


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
            competition_rate=1000, float_ratio=20, market_cap=500,
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
            market_cap=300,
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
            market_cap=100000,
        ))
        assert res.total_score is not None
        assert res.grade in ("C", "B")

    def test_note_when_partial(self):
        res = compute_attraction_score(_item(
            competition_rate=500, float_ratio=25, market_cap=1000,
        ))
        assert res.confirmed_factors == 3
        assert "미확정" in res.note or "확정" in res.note

    def test_no_note_when_full(self):
        res = compute_attraction_score(_item(
            competition_rate=1000,
            band_low=10000, band_high=12000, final_price=11500,
            float_ratio=20,
            underwriter="KB증권",
            market_cap=2000,
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
            market_cap=2000,
        ))
        assert res.band_score is None
        assert res.confirmed_factors == 4

    def test_grade_thresholds(self):
        # A++ >= 85
        res = compute_attraction_score(_item(
            competition_rate=1500, float_ratio=15, market_cap=300,
            underwriter="NH투자증권", band_low=10000, band_high=12000, final_price=13000,
        ))
        assert res.grade == "A++"

        # B: 50~64
        res2 = compute_attraction_score(_item(
            competition_rate=100,  # 8
            float_ratio=35,        # 8
            market_cap=10000,      # 8
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
            underwriter="KB증권", market_cap=500, source="test",
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
            market_cap=2000,
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
