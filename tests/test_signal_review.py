"""test_signal_review.py — 신호 적중률 평가(순수) 검증 (hermetic, 네트워크 없음)."""
from __future__ import annotations

import json

import signal_review as sr


def _series(prices, start_day=1):
    """(날짜, 종가) 시계열 — 2026-08-01부터 하루씩."""
    return [(f"2026-08-{start_day + i:02d}", float(p)) for i, p in enumerate(prices)]


def _sig(action="매수", at="2026-08-03 10:00", price=100.0, strategy="볼린저",
         suppressed=False, ticker="102110"):
    return {"at": at, "ticker": ticker, "name": "TIGER 200", "strategy": strategy,
            "action": action, "price": price, "weight": 20, "trend": "up",
            "suppressed": suppressed, "reason": "테스트"}


# ─── 기록 파싱 ───────────────────────────────────────


def test_parse_lines_skips_broken_and_incomplete():
    lines = ['{"at":"2026-08-03","ticker":"102110"}', "not json", "", '{"at":"x"}']
    assert len(sr.parse_lines(lines)) == 1


def test_signal_key_is_stable_and_distinguishes_action():
    a = sr.signal_key(_sig(action="매수"))
    b = sr.signal_key(_sig(action="매도"))
    assert a == sr.signal_key(_sig(action="매수")) and a != b


# ─── 수익률 ──────────────────────────────────────────


def test_forward_return_uses_signal_price_as_base():
    s = _series([100, 101, 102, 103, 104, 105])
    # 2026-08-03 = index 2, +2거래일 → index 4 (104), 기준가 100 → +4%
    assert round(sr.forward_return(s, "2026-08-03", 2, entry_price=100.0), 4) == 0.04


def test_forward_return_falls_back_to_close_when_no_entry_price():
    s = _series([100, 110, 121])
    assert round(sr.forward_return(s, "2026-08-02", 1), 4) == 0.1


def test_forward_return_none_when_not_enough_days():
    s = _series([100, 101, 102])
    assert sr.forward_return(s, "2026-08-03", 5) is None


def test_forward_return_uses_last_trading_day_on_holiday():
    """휴장일 신호는 직전 거래일 기준으로 붙는다."""
    s = [("2026-08-03", 100.0), ("2026-08-06", 110.0)]
    assert round(sr.forward_return(s, "2026-08-05", 1, entry_price=100.0), 4) == 0.1


def test_baseline_excludes_days_after_signal():
    """신호 이후 구간이 비교 기준에 섞이면 평가가 오염된다."""
    s = _series([100, 100, 100, 100, 200, 400])       # 신호 뒤 급등
    base = sr.baseline_return(s, 1, until="2026-08-03")
    assert base == 0.0


def test_baseline_none_without_enough_history():
    assert sr.baseline_return(_series([100]), 5) is None


def test_directional_flips_for_bearish_actions():
    assert sr.directional(-0.05, "매도") == 0.05      # 하락을 맞히면 성공
    assert sr.directional(-0.05, "매수") == -0.05
    assert sr.directional(None, "매도") is None


# ─── 신호 평가 ───────────────────────────────────────


def test_evaluate_signal_fills_ret_base_edge():
    s = _series([100, 100, 100, 105, 110, 115])
    out = sr.evaluate_signal(_sig(at="2026-08-03 10:00", price=100.0), s, horizons=(1,))
    assert out["ret_1d"] == 5.0                       # 100 → 105
    assert out["base_1d"] is not None
    assert out["edge_1d"] == round(out["ret_1d"] - out["base_1d"], 3)
    assert out["pending"] is False


def test_evaluate_signal_pending_when_horizon_not_reached():
    s = _series([100, 100, 100])
    out = sr.evaluate_signal(_sig(at="2026-08-03 10:00"), s, horizons=(5,))
    assert out["pending"] and out["ret_5d"] is None


def test_evaluate_sell_signal_scores_decline_as_hit():
    s = _series([100, 100, 100, 90])
    out = sr.evaluate_signal(_sig(action="매도", at="2026-08-03 10:00", price=100.0),
                             s, horizons=(1,))
    assert out["ret_1d"] == 10.0                      # -10% 하락 → 매도 성공


def test_evaluate_keeps_suppressed_flag():
    s = _series([100, 100, 100, 105])
    out = sr.evaluate_signal(_sig(suppressed=True, at="2026-08-03 10:00"), s, horizons=(1,))
    assert out["suppressed"] is True


# ─── 집계 ────────────────────────────────────────────


_DAY_SEQ = iter(range(1, 400))


def _out(ret, edge, strategy="볼린저", action="매수", suppressed=False,
         pending=False, day=None):
    """기본은 **서로 다른 날**이다.

    2026-09-02에 「신호일 5일 미만이면 판정하지 않는다」를 넣으면서, 날짜가
    없던 이 헬퍼가 전부 같은 날로 취급돼 기존 테스트가 깨졌다. 여기 테스트들이
    보려는 것은 edge·MTF 의미이지 날 수가 아니므로 날짜를 흩어 준다.
    """
    n = next(_DAY_SEQ)
    return {"strategy": strategy, "action": action, "suppressed": suppressed,
            "pending": pending, "ret_5d": ret, "edge_5d": edge,
            "day": day or f"2026-{(n % 12) + 1:02d}-{(n % 28) + 1:02d}"}


def test_summarize_hit_rate_and_edge():
    rows = [_out(5, 3), _out(-2, -1), _out(1, 0.5)]
    s = sr.summarize(rows, horizon=5)
    assert s["total"]["n"] == 3
    assert s["total"]["hit_rate"] == 66.7
    assert s["total"]["avg_ret"] == round((5 - 2 + 1) / 3, 3)


def test_summarize_excludes_pending():
    rows = [_out(5, 3), _out(None, None, pending=True)]
    assert sr.summarize(rows)["total"]["n"] == 1


def test_summarize_splits_mtf_sent_and_suppressed():
    rows = [_out(5, 3), _out(-4, -3, suppressed=True)]
    s = sr.summarize(rows)
    assert s["mtf"]["sent"]["n"] == 1 and s["mtf"]["suppressed"]["n"] == 1
    assert s["mtf"]["suppressed"]["avg_edge"] == -3


def test_summarize_groups_by_strategy_and_action():
    rows = [_out(5, 3, strategy="MACD"), _out(1, 1, strategy="볼린저", action="매도")]
    s = sr.summarize(rows)
    assert set(s["by_strategy"]) == {"MACD", "볼린저"}
    assert set(s["by_action"]) == {"매수", "매도"}


def test_verdict_holds_below_min_sample():
    rows = [_out(9, 9) for _ in range(sr.MIN_SAMPLE - 1)]
    assert sr.summarize(rows)["total"]["verdict"] == "표본 부족"


def test_verdict_is_not_given_by_edge_alone():
    """수익이 나고 edge가 양수라도, 방향이 한 종류뿐이면 검정할 수 없다(2026-09-16).

    전에는 edge>0이면 「우위 있음」이었다. 내리는 장에 약세 신호만 내면 edge는
    저절로 양수라서, 그 규칙은 시장 방향을 신호의 공으로 돌렸다.
    """
    rows = [_out(5, 1) for _ in range(sr.MIN_SAMPLE)]
    assert sr.summarize(rows)["total"]["verdict"] == "검정 불가(한 방향뿐)"


def test_format_summary_reports_empty_state():
    text = sr.format_summary(sr.summarize([], horizon=5))
    assert "표본이 아직 없습니다" in text


def test_format_summary_mentions_mtf_interpretation():
    rows = [_out(5, 3), _out(-4, -3, suppressed=True)]
    text = sr.format_summary(sr.summarize(rows))
    assert "억제분이 더 좋으면" in text and "미반영" in text


# ─── 심볼·저장 ───────────────────────────────────────


def test_yf_symbol_for_korean_ticker_and_fx():
    assert sr.yf_symbol_for({"ticker": "102110"}) == "102110.KS"
    assert sr.yf_symbol_for({"ticker": "KRW=X"}) == "KRW=X"


def test_save_and_load_outcomes_roundtrip(tmp_path):
    p = tmp_path / "signal_outcomes.jsonl"
    sr.save_outcomes({"k1": {"key": "k1", "ret_5d": 1.0}}, p)
    assert sr.load_outcomes(p)["k1"]["ret_5d"] == 1.0
    assert list(p.parent.glob(".tmp_*")) == []


def test_refresh_evaluates_only_new_or_pending(tmp_path):
    sig = tmp_path / "signal_log.jsonl"
    out = tmp_path / "signal_outcomes.jsonl"
    sig.write_text(json.dumps(_sig(at="2026-08-03 10:00", price=100.0)) + "\n",
                   encoding="utf-8")
    calls = []

    def fake_fetch(sym):
        calls.append(sym)
        return _series([100, 100, 100, 110, 120, 130])

    outcomes, updated = sr.refresh(sig, out, horizons=(1,), fetch=fake_fetch)
    assert updated == 1 and calls == ["102110.KS"]
    # 두 번째 호출은 이미 평가돼 있으므로 조회하지 않는다
    outcomes2, updated2 = sr.refresh(sig, out, horizons=(1,), fetch=fake_fetch)
    assert updated2 == 0 and calls == ["102110.KS"]


def test_refresh_retries_pending_signals(tmp_path):
    sig = tmp_path / "signal_log.jsonl"
    out = tmp_path / "signal_outcomes.jsonl"
    sig.write_text(json.dumps(_sig(at="2026-08-03 10:00", price=100.0)) + "\n",
                   encoding="utf-8")
    short = _series([100, 100, 100])                    # 아직 5일 안 지남
    long = _series([100, 100, 100, 105, 110, 115, 120, 125])
    sr.refresh(sig, out, horizons=(5,), fetch=lambda s: short)
    assert sr.load_outcomes(out)[list(sr.load_outcomes(out))[0]]["pending"] is True
    _, updated = sr.refresh(sig, out, horizons=(5,), fetch=lambda s: long)
    assert updated == 1                                  # pending은 다시 평가한다


def test_refresh_skips_when_price_unavailable(tmp_path):
    sig = tmp_path / "signal_log.jsonl"
    sig.write_text(json.dumps(_sig()) + "\n", encoding="utf-8")
    outcomes, updated = sr.refresh(sig, tmp_path / "o.jsonl", fetch=lambda s: [])
    assert updated == 0 and outcomes == {}


def test_load_signals_missing_file_is_empty(tmp_path):
    assert sr.load_signals(tmp_path / "none.jsonl") == []


def test_outcome_rows_are_not_filtered_like_signals():
    """결과 레코드는 ticker/at 없이도 읽혀야 한다 — 신호 기록과 스키마가 다르다."""
    rows = sr.parse_jsonl(['{"key":"k1","ret_5d":1.0}'])
    assert rows and rows[0]["key"] == "k1"
    assert sr.parse_lines(['{"key":"k1","ret_5d":1.0}']) == []


# ─── 병행 기록과 MTF 평가의 분리 (2026-08-31) ────────


def _outcome(strategy, *, shadow=False, suppressed=False, ret=1.0):
    return {"key": f"k-{strategy}-{shadow}", "strategy": strategy,
            "action": "매수", "suppressed": suppressed, "shadow": shadow,
            "pending": False, "ret_5d": ret, "base_5d": 0.0, "edge_5d": ret}


def test_shadow_rows_are_labelled_in_the_strategy_table():
    """발송된 적 없는 지표의 성적이 발송 지표와 같은 얼굴로 보이면 안 된다."""
    s = sr.summarize([_outcome("stochrsi"), _outcome("macd", shadow=True,
                                                     suppressed=True)])
    assert "stochrsi" in s["by_strategy"]
    assert "macd(병행)" in s["by_strategy"]
    assert "macd" not in s["by_strategy"]


def test_shadow_rows_do_not_pollute_the_mtf_split():
    """병행 기록은 suppressed=True로 저장되지만 MTF가 막은 것이 아니다.

    섞이면 '억제분이 나빴다/좋았다'는 필터 평가가 후보 지표 성적으로 오염된다.
    """
    s = sr.summarize([
        _outcome("stochrsi", ret=2.0),                          # 발송
        _outcome("stochrsi2", suppressed=True, ret=-1.0),       # MTF 억제
        _outcome("macd", shadow=True, suppressed=True, ret=9.9) # 병행
    ])
    assert s["mtf"]["sent"]["n"] == 1
    assert s["mtf"]["suppressed"]["n"] == 1      # 병행이 끼면 2가 된다
    assert s["mtf"]["suppressed"]["avg_ret"] == -1.0


def test_old_records_without_the_shadow_field_still_summarize():
    """기존 기록에는 shadow 필드가 없다 — 없으면 False로 읽혀야 한다."""
    legacy = {"key": "k", "strategy": "stochrsi", "action": "매수",
              "suppressed": False, "pending": False,
              "ret_5d": 1.0, "base_5d": 0.0, "edge_5d": 1.0}
    s = sr.summarize([legacy])
    assert s["by_strategy"]["stochrsi"]["n"] == 1


def test_evaluate_signal_carries_the_shadow_flag():
    rec = {"at": "2026-08-31 10:00:00", "ticker": "005930", "name": "삼성전자",
           "strategy": "macd", "action": "매수", "price": 100.0,
           "suppressed": True, "shadow": True, "trend": "up"}
    out = sr.evaluate_signal(rec, [])
    assert out["shadow"] is True


# ─── 있는 데이터를 못 쓰고 있었다 (2026-09-02) ──────────

def _partial(**kw):
    base = {"key": "k", "at": "2026-09-01 10:00", "day": "2026-09-01",
            "ticker": "005930", "strategy": "MACD", "action": "매수",
            "suppressed": False, "shadow": False, "pending": True,
            "ret_1d": 1.0, "base_1d": 0.2, "edge_1d": 0.8,
            "ret_5d": None, "base_5d": None, "edge_5d": None}
    base.update(kw)
    return base


def test_a_one_day_result_counts_even_while_five_days_is_pending():
    """**5거래일이 안 지났다고 1거래일 결과까지 버리면 안 된다.**

    `evaluate_signal`은 지평 하나라도 비면 pending을 세운다. 예전
    `summarize`는 pending을 통째로 버려서, 1일 결과가 39건 채워져 있는데도
    1일 화면이 n=0이었다. 화면은 「아직 평가된 신호가 없습니다」라고만 했다.
    """
    rows = [_partial(key=f"k{i}") for i in range(12)]
    got = sr.summarize(rows, horizon=1)
    assert got["total"]["n"] == 12
    assert got["total"]["verdict"] != "표본 부족"


def test_the_five_day_view_still_shows_nothing_when_it_has_nothing():
    """지평별로 고른다 — 없는 것을 있는 척하지 않는다."""
    rows = [_partial(key=f"k{i}") for i in range(12)]
    assert sr.summarize(rows, horizon=5)["total"]["n"] == 0


def test_the_summary_says_how_many_each_horizon_has():
    """「아직 없습니다」만 뜨면 사람은 수집이 고장난 줄 안다."""
    rows = [_partial(key=f"k{i}") for i in range(12)]
    rows.append(_partial(key="done", ret_5d=2.0, base_5d=0.5, edge_5d=1.5,
                         pending=False))
    got = sr.summarize(rows, horizon=5)
    assert got["available"] == {1: 13, 5: 1}
    assert got["waiting"] == 12


def test_a_row_with_no_result_at_all_is_counted_nowhere():
    rows = [_partial(ret_1d=None, base_1d=None, edge_1d=None)]
    got = sr.summarize(rows, horizon=1)
    assert got["total"]["n"] == 0 and got["available"][1] == 0


def test_the_mtf_split_also_uses_the_horizon(  ):
    """억제분도 같은 규칙으로 골라야 한쪽만 표본이 다르지 않다."""
    rows = ([_partial(key=f"s{i}", suppressed=False) for i in range(6)]
            + [_partial(key=f"h{i}", suppressed=True) for i in range(4)])
    got = sr.summarize(rows, horizon=1)
    assert got["mtf"]["sent"]["n"] == 6 and got["mtf"]["suppressed"]["n"] == 4


def test_the_cli_summary_says_which_horizon_has_data():
    """**같은 결함을 한 곳만 고치면 다른 곳이 남는다.**

    화면에는 지평별 건수를 붙였는데 `format_summary`에 안 붙여서,
    CLI만 「표본이 없습니다」라고 했다 — 실제로는 1거래일 결과가 125건
    있었다(2026-09-02 실측).
    """
    rows = [_partial(key=f"k{i}") for i in range(12)]
    msg = sr.format_summary(sr.summarize(rows, horizon=5))
    assert "1거래일 12건" in msg
    assert "--horizon 1" in msg


def test_the_summary_shows_the_counts_even_when_it_has_a_verdict():
    rows = [_partial(key=f"k{i}") for i in range(12)]
    msg = sr.format_summary(sr.summarize(rows, horizon=1))
    assert "지평별 평가 완료" in msg
    assert "전체 12건" in msg


# ─── 독립 단위는 건이 아니라 날이다 (2026-09-02) ────────

def test_many_signals_on_few_days_is_not_a_verdict():
    """**125건이라도 4일치면 사실상 표본 4다.**

    같은 날 신호는 같은 시장을 겪는다 — 하루의 방향이 그날 신호 전체의
    부호를 정한다. 탭 D에서 시간축을 발견에서 뺀 것, 탭 C에서 달 대신
    에피소드를 센 것과 같은 이유다.
    """
    rows = [_partial(key=f"k{i}", day="2026-09-01") for i in range(20)]
    got = sr.summarize(rows, horizon=1)
    assert got["total"]["n"] == 20
    assert got["total"]["days"] == 1
    assert got["total"]["verdict"] == "날 부족"


def test_enough_days_allows_a_verdict():
    rows = [_partial(key=f"k{i}", day=f"2026-09-{(i % 6) + 1:02d}") for i in range(20)]
    got = sr.summarize(rows, horizon=1)
    assert got["total"]["days"] == 6
    assert got["total"]["verdict"] not in ("표본 부족", "날 부족")


def test_the_day_warning_is_printed():
    rows = [_partial(key=f"k{i}", day="2026-09-01") for i in range(20)]
    msg = sr.format_summary(sr.summarize(rows, horizon=1))
    assert "독립 표본이 아니다" in msg
    assert "1일뿐" in msg


def test_sample_shortage_still_wins_over_day_shortage():
    """건수가 모자라면 그것이 먼저다 — 두 결함을 한 문장에 섞지 않는다."""
    rows = [_partial(key=f"k{i}", day=f"2026-09-{i+1:02d}") for i in range(5)]
    assert sr.summarize(rows, horizon=1)["total"]["verdict"] == "표본 부족"


# ─── 신호 하나가 12번 세어지고 있었다 (2026-09-16) ────────


def test_the_key_is_the_day_not_the_hour():
    """매시간 다시 적힌 같은 신호는 같은 키다."""
    a = sr.signal_key(_sig(at="2026-09-01 09:14"))
    b = sr.signal_key(_sig(at="2026-09-01 15:14"))
    assert a == b
    assert a != sr.signal_key(_sig(at="2026-09-02 09:14"))       # 다른 날은 다른 신호
    assert a != sr.signal_key(_sig(at="2026-09-01 09:14", action="매도"))


def test_first_per_key_keeps_the_first_sighting_and_its_price():
    sigs = [_sig(at="2026-09-01 09:14", price=100.0),
            _sig(at="2026-09-01 10:14", price=103.0),
            _sig(at="2026-09-01 11:14", price=97.0),
            _sig(at="2026-09-02 09:14", price=90.0)]
    kept = sr.first_per_key(sigs)
    assert [s["price"] for s in kept] == [100.0, 90.0]


def test_refresh_evaluates_a_repeated_signal_once(tmp_path):
    """음성 대조가 아니라 양성: 12번 적힌 신호가 결과 원장에 1행으로 남는다."""
    sig = tmp_path / "signal_log.jsonl"
    out = tmp_path / "signal_outcomes.jsonl"
    sig.write_text("".join(json.dumps(_sig(at=f"2026-08-03 {h:02d}:00", price=100.0 + h))
                           + "\n" for h in range(9, 16)), encoding="utf-8")
    outcomes, updated = sr.refresh(sig, out, horizons=(1,),
                                   fetch=lambda s: _series([100, 100, 100, 110, 120]))
    assert updated == 1 and len(outcomes) == 1
    (row,) = outcomes.values()
    assert row["entry"] == 109.0                                  # 처음 본 가격
    assert row["at"] == "2026-08-03 09:00"


def test_two_different_signals_on_the_same_day_are_still_two(tmp_path):
    """음성 대조: 접기가 과하지 않다 — 다른 전략·다른 종목은 살아남는다."""
    sig = tmp_path / "signal_log.jsonl"
    rows = [_sig(at="2026-08-03 09:00"), _sig(at="2026-08-03 09:00", strategy="MACD"),
            _sig(at="2026-08-03 09:00", ticker="005930"),
            _sig(at="2026-08-03 09:00", action="매도")]
    sig.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    outcomes, updated = sr.refresh(sig, tmp_path / "o.jsonl", horizons=(1,),
                                   fetch=lambda s: _series([100, 100, 100, 110, 120]))
    assert updated == 4 and len(outcomes) == 4


def test_legacy_hour_keys_collapse_to_the_day_key_on_load(tmp_path):
    """옛 원장(시각 키 7행)은 읽을 때 1행으로 접히고, 가장 이른 행이 남는다."""
    out = tmp_path / "signal_outcomes.jsonl"
    legacy = {}
    for h in range(9, 16):
        at = f"2026-09-01 {h:02d}:14:44"
        k = f"{at}|005930|MACD|매수"
        legacy[k] = {"key": k, "at": at, "day": "2026-09-01", "ticker": "005930",
                     "strategy": "MACD", "action": "매수", "suppressed": False,
                     "entry": 100.0 + h, "ret_1d": 1.0, "pending": False}
    sr.save_outcomes(legacy, out)
    loaded = sr.load_outcomes(out)
    assert list(loaded) == ["2026-09-01|005930|MACD|매수"]
    assert loaded["2026-09-01|005930|MACD|매수"]["entry"] == 109.0


def test_refresh_rewrites_a_legacy_ledger_even_with_nothing_new(tmp_path):
    """접힌 형태로 원장을 다시 써서, 다음 읽기가 접을 것이 없게 한다."""
    out = tmp_path / "signal_outcomes.jsonl"
    sig = tmp_path / "signal_log.jsonl"
    sig.write_text("", encoding="utf-8")
    legacy = {}
    for h in (9, 10):
        at = f"2026-09-01 {h:02d}:14"
        k = f"{at}|005930|MACD|매수"
        legacy[k] = {"key": k, "at": at, "day": "2026-09-01", "ticker": "005930",
                     "strategy": "MACD", "action": "매수", "ret_1d": 1.0, "pending": False}
    sr.save_outcomes(legacy, out)
    assert sr._raw_outcome_rows(out) == 2
    _, updated = sr.refresh(sig, out, horizons=(1,), fetch=lambda s: [])
    assert updated == 0 and sr._raw_outcome_rows(out) == 1


def test_rows_without_a_ticker_keep_their_stored_key_on_load(tmp_path):
    p = tmp_path / "o.jsonl"
    sr.save_outcomes({"k1": {"key": "k1", "ret_5d": 1.0}}, p)
    assert sr.load_outcomes(p)["k1"]["ret_5d"] == 1.0


# ─── 홀딩유지는 채점하지 않는다 (2026-09-16) ────────────


def test_hold_rows_are_counted_but_not_scored():
    rows = [_out(5, 3), _out(-2, -1),
            _out(-9, -9, action="홀딩유지"), _out(-9, -9, action="홀딩유지")]
    s = sr.summarize(rows, horizon=5)
    assert s["total"]["n"] == 2 and s["total"]["hit_rate"] == 50.0
    assert s["hold"] == {"n": 2, "days": 2}
    assert "홀딩유지" not in s["by_action"]


def test_hold_rows_do_not_change_the_verdict():
    """음성 대조: 홀딩유지 100건이 나빠도 실행신호 판정은 그대로다."""
    good = [_out(2, 1) for _ in range(12)]
    s_clean = sr.summarize(good, horizon=5)
    s_dirty = sr.summarize(good + [_out(-20, -20, action="홀딩유지") for _ in range(100)],
                           horizon=5)
    assert s_clean["total"] == s_dirty["total"]
    assert s_clean["by_strategy"] == s_dirty["by_strategy"]
    assert s_clean["mtf"] == s_dirty["mtf"]


def test_available_and_waiting_count_scored_rows_only():
    rows = [_partial(key="a"), _partial(key="b", action="홀딩유지"),
            _partial(key="c", action="홀딩유지", ret_1d=None)]
    s = sr.summarize(rows, horizon=5)
    assert s["available"] == {1: 1, 5: 0}
    assert s["waiting"] == 1


def test_format_summary_says_hold_is_not_scored():
    rows = [_out(2, 1) for _ in range(12)] + [_out(0, 0, action="홀딩유지")]
    text = sr.format_summary(sr.summarize(rows, horizon=5))
    assert "홀딩유지: 1건" in text and "채점 안 함" in text


# ─── 판정은 날 안 방향표 섞기 순열검정으로 (2026-09-16) ────


def _mixed_day(day, bull_rets, bear_rets):
    """같은 날에 강세 신호(원수익률 bull_rets)와 약세 신호(원수익률 bear_rets)."""
    rows = []
    for i, raw in enumerate(bull_rets):
        rows.append({"key": f"{day}-b{i}", "day": day, "ticker": f"B{i}", "strategy": "S",
                     "action": "매수", "suppressed": False, "shadow": False,
                     "ret_1d": raw, "edge_1d": raw})
    for i, raw in enumerate(bear_rets):
        rows.append({"key": f"{day}-s{i}", "day": day, "ticker": f"S{i}", "strategy": "S",
                     "action": "매도", "suppressed": False, "shadow": False,
                     "ret_1d": -raw, "edge_1d": -raw})       # 저장값은 방향정렬
    return rows


def test_a_bot_that_picks_direction_well_clears_the_bar():
    """양성 대조: 매일 오르는 종목에 매수, 내리는 종목에 매도를 붙이면 백분위가 높다."""
    rows = []
    for d in range(1, 9):
        rows += _mixed_day(f"2026-09-{d:02d}", bull_rets=[2.0, 1.5, 1.0], bear_rets=[-2.0, -1.5, -1.0])
    s = sr.summarize(rows, horizon=1)
    assert s["total"]["perm"]["pct"] >= sr.PERM_BAR
    assert s["total"]["verdict"].startswith("우위 있음")


def test_a_falling_market_with_only_bearish_calls_is_not_an_edge():
    """음성 대조 ①: 방향표가 무작위면 백분위는 우연 범위다.

    각 날 종목 수익률은 같은데 어느 종목에 매수·매도를 붙였는지가 무작위 —
    그래도 시장이 내리면 약세 쪽 적중률·edge는 높게 나온다. 판정은 속지 않아야 한다.
    """
    import random
    rng = random.Random(3)
    rows = []
    for d in range(1, 13):
        rets = [rng.gauss(-1.0, 1.5) for _ in range(6)]      # 내리는 장
        rng.shuffle(rets)
        rows += _mixed_day(f"2026-09-{d:02d}", bull_rets=rets[:2], bear_rets=rets[2:])
    s = sr.summarize(rows, horizon=1)
    assert s["total"]["avg_ret"] > 0                          # 겉보기 성적은 좋다
    assert s["total"]["perm"]["pct"] < sr.PERM_BAR
    assert s["total"]["verdict"].startswith("우연 범위")


def test_a_single_direction_group_cannot_be_tested():
    """음성 대조 ②: 매도만 있는 묶음은 섞어도 달라질 게 없다 → 검정 불가."""
    rows = []
    for d in range(1, 9):
        rows += _mixed_day(f"2026-09-{d:02d}", bull_rets=[], bear_rets=[-2.0, -1.0])
    assert sr.within_day_percentile(rows, 1) is None
    assert sr.summarize(rows, horizon=1)["total"]["verdict"] == "검정 불가(한 방향뿐)"


def test_the_permutation_is_deterministic():
    rows = []
    for d in range(1, 9):
        rows += _mixed_day(f"2026-09-{d:02d}", bull_rets=[1.0, -0.5], bear_rets=[-1.0, 0.5])
    a = sr.within_day_percentile(rows, 1)
    b = sr.within_day_percentile(rows, 1)
    assert a == b and a["pct"] is not None


def test_the_permutation_reads_stored_returns_as_already_directional():
    """저장된 ret는 방향정렬값이다 — 다시 뒤집으면 약세 신호가 거꾸로 채점된다(내가 낸 오류)."""
    rows = _mixed_day("2026-09-01", bull_rets=[1.0], bear_rets=[-1.0]) \
        + _mixed_day("2026-09-02", bull_rets=[1.0], bear_rets=[-1.0])
    p = sr.within_day_percentile(rows, 1, n_perm=200)
    assert p["obs"] == 1.0                                    # 둘 다 맞혔으니 +1.0


def test_the_summary_prints_the_verdict_basis():
    rows = []
    for d in range(1, 9):
        rows += _mixed_day(f"2026-09-{d:02d}", bull_rets=[2.0, 1.0], bear_rets=[-2.0, -1.0])
    text = sr.format_summary(sr.summarize(rows, horizon=1))
    assert "판정 근거" in text and "백분위" in text and "대조군이 아니다" in text


def test_too_few_mixed_days_is_untestable_not_a_bad_score():
    """음성 대조 ③: 경고 35건에 매수 2건이면 백분위 0.0이 나와도 판정이 아니다."""
    rows = []
    for d in range(1, 11):
        rows += _mixed_day(f"2026-09-{d:02d}", bull_rets=[], bear_rets=[-1.0, 1.0, -0.5])
    rows += _mixed_day("2026-09-11", bull_rets=[-9.0], bear_rets=[-1.0, 1.0])
    rows += _mixed_day("2026-09-12", bull_rets=[-9.0], bear_rets=[-1.0, 1.0])
    s = sr.summarize(rows, horizon=1)
    assert s["total"]["perm"]["mixed_days"] == 2
    assert s["total"]["verdict"].startswith("검정 불가(방향 섞인 날 2일")
