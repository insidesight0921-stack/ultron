"""test_exit_rules.py — 장중 손절·익절 판정 순수 모듈 (hermetic)."""
from __future__ import annotations

import exit_rules as ex


# ─── should_exit: 기본 손절·익절선 ───────────────────


def test_hold_in_normal_range():
    assert ex.should_exit(0.0) is None
    assert ex.should_exit(-0.05) is None
    assert ex.should_exit(0.15) is None


def test_stop_loss_at_line():
    assert ex.should_exit(-0.07) == ("손절", "손절선")
    assert ex.should_exit(-0.08) == ("손절", "손절선")


def test_take_profit_at_line():
    assert ex.should_exit(0.20) == ("익절", "익절선")
    assert ex.should_exit(0.25) == ("익절", "익절선")


def test_hard_stop_beats_stop_line():
    assert ex.should_exit(-0.12) == ("손절", "급락")
    assert ex.should_exit(-0.20) == ("손절", "급락")   # 갭하락도 급락 사유


def test_custom_thresholds():
    assert ex.should_exit(-0.05, stop=-0.05) == ("손절", "손절선")
    assert ex.should_exit(0.10, take=0.10) == ("익절", "익절선")
    assert ex.should_exit(-0.09, hard_stop=-0.09) == ("손절", "급락")


# ─── should_exit: 트레일링 ───────────────────────────


def test_trailing_after_arm_and_drop():
    # 피크 +12% → 현재 +4% (8%p 반납, arm 10%/drop 7%p 충족) → 익절 라벨
    assert ex.should_exit(0.04, 0.12) == ("익절", "트레일링")


def test_trailing_negative_pnl_labeled_stop():
    # 피크 +10% → 현재 -1% → 손절 라벨(analytics 사유 분류 호환)
    assert ex.should_exit(-0.01, 0.10) == ("손절", "트레일링")


def test_trailing_not_armed():
    # 피크 +9% (arm 10% 미달) → 반납 커도 보유
    assert ex.should_exit(0.01, 0.09) is None


def test_trailing_drop_too_small():
    # 피크 +12% → 현재 +6% (6%p 반납 < 7%p) → 보유
    assert ex.should_exit(0.06, 0.12) is None


def test_trailing_without_peak_is_ignored():
    assert ex.should_exit(0.05) is None
    assert ex.should_exit(0.05, None) is None


def test_priority_hard_stop_over_trailing():
    assert ex.should_exit(-0.13, 0.15) == ("손절", "급락")


# ─── 피크 상태 관리 ──────────────────────────────────


def test_update_peak_monotonic():
    peaks = {}
    assert ex.update_peak(peaks, "k", 0.03) == 0.03
    assert ex.update_peak(peaks, "k", 0.08) == 0.08
    assert ex.update_peak(peaks, "k", 0.02) == 0.08  # 하락해도 피크 유지


def test_peak_key_resets_on_avg_price_change():
    assert ex.peak_key(1, "005930", 70000.0) != ex.peak_key(1, "005930", 68000.0)
    assert ex.peak_key(1, "005930", 70000.0) == ex.peak_key(1, "005930", 70000)


def test_prune_peaks():
    peaks = {"a": 0.1, "b": 0.2}
    assert ex.prune_peaks(peaks, {"a"}) == {"a": 0.1}
    assert ex.prune_peaks(peaks, set()) == {}


# ─── 네이버 시세 파싱 ────────────────────────────────


def test_parse_naver_price_new_format():
    assert ex.parse_naver_price({"datas": [{"closePrice": "71,900"}]}) == 71900.0


def test_parse_naver_price_old_format():
    data = {"result": {"areas": [{"datas": [{"nv": 71900}]}]}}
    assert ex.parse_naver_price(data) == 71900.0


def test_parse_naver_price_bad_inputs():
    assert ex.parse_naver_price(None) is None
    assert ex.parse_naver_price({}) is None
    assert ex.parse_naver_price({"datas": []}) is None
    assert ex.parse_naver_price({"datas": [{"closePrice": "0"}]}) is None
    assert ex.parse_naver_price({"datas": [{"closePrice": "abc"}]}) is None
    assert ex.parse_naver_price({"result": {"areas": []}}) is None


# ─── 오호가 방어 (confirm_price) ─────────────────────


def test_confirm_price_normal_move_accepted():
    assert ex.confirm_price(70000, 69000) == 70000
    assert ex.confirm_price(70000, None) == 70000  # 직전가 없으면 그대로


def test_confirm_price_none_primary():
    assert ex.confirm_price(None, 70000) is None
    assert ex.confirm_price(0, 70000) is None


def test_confirm_price_jump_confirmed_by_fallback():
    # 직전 70,000 → 55,000 (-21% 급변), 2차 소스 55,500 일치 → 실제 급락으로 채택
    assert ex.confirm_price(55000, 70000, lambda: 55500) == 55000


def test_confirm_price_jump_rejected_without_confirmation():
    # 급변인데 2차 소스가 직전가 근처(70,100) → 의심 틱, 스킵
    assert ex.confirm_price(55000, 70000, lambda: 70100) is None
    assert ex.confirm_price(55000, 70000, None) is None  # 2차 소스 없음
    assert ex.confirm_price(55000, 70000, lambda: None) is None


def test_confirm_price_fallback_error_rejected():
    def boom():
        raise RuntimeError("pykrx down")
    assert ex.confirm_price(55000, 70000, boom) is None


def test_confirm_price_custom_thresholds():
    assert ex.confirm_price(93000, 100000, max_jump=0.05, tol=0.01,
                            fallback_fn=lambda: 93500) == 93000
    assert ex.confirm_price(93000, 100000, max_jump=0.10) == 93000  # 7% ≤ 10%
