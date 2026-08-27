"""test_slot_diversify.py — 슬롯 내 집중 위험 판정(순수) 검증 (hermetic)."""
from __future__ import annotations

import slot_diversify as sd

SEC = {"005930": "반도체", "000660": "반도체", "011070": "IT부품"}


def _r(ticker, name, **kw):
    return {"ticker": ticker, "name": name, **kw}


# ─── 그룹 판정 ───────────────────────────────────────


def test_sector_wins_over_group_prefix():
    """삼성전자는 '계열:삼성'이 아니라 '섹터:반도체'로 잡힌다."""
    assert sd.group_of("005930", "삼성전자", SEC) == "섹터:반도체"


def test_group_prefix_when_sector_unknown():
    assert sd.group_of("034730", "SK", SEC) == "계열:SK"
    assert sd.group_of("402340", "SK스퀘어", SEC) == "계열:SK"


def test_longer_prefix_matches_first():
    """HD현대는 '현대'가 아니라 'HD현대'로 잡혀야 한다."""
    assert sd.group_of("267260", "HD현대일렉트릭", {}) == "계열:HD현대"


def test_solo_when_nothing_matches():
    assert sd.group_of("047040", "대우건설", {}) == "단독:047040"


def test_empty_name_is_solo():
    assert sd.group_of("999999", "", {}) == "단독:999999"


# ─── 상한 필터 ───────────────────────────────────────


def test_cap_drops_third_in_same_sector():
    rows = [_r("005930", "삼성전자"), _r("000660", "SK하이닉스"), _r("042700", "한미반도체")]
    sector = {**SEC, "042700": "반도체"}
    kept, dropped = sd.apply_caps(rows, max_per_group=2, sector_map=sector)
    assert [r["ticker"] for r in kept] == ["005930", "000660"]
    assert dropped[0]["ticker"] == "042700"
    assert "상한 2" in dropped[0]["drop_reason"]


def test_cap_keeps_input_order_as_priority():
    """입력이 점수 내림차순이라는 전제 — 앞의 것이 살아남는다."""
    rows = [_r("000660", "SK하이닉스"), _r("005930", "삼성전자")]
    kept, _ = sd.apply_caps(rows, max_per_group=1, sector_map=SEC)
    assert [r["ticker"] for r in kept] == ["000660"]


def test_solo_rows_are_never_capped():
    """미분류를 이유로 걸러내면 정보 부족이 투자 판단을 대신하게 된다."""
    rows = [_r("A", "가"), _r("B", "나"), _r("C", "다")]
    kept, dropped = sd.apply_caps(rows, max_per_group=1, sector_map={})
    assert len(kept) == 3 and dropped == []


def test_max_groups_limits_distinct_groups():
    rows = [_r("005930", "삼성전자"), _r("011070", "LG이노텍"), _r("034730", "SK")]
    kept, dropped = sd.apply_caps(rows, max_per_group=2, max_groups=2, sector_map=SEC)
    assert [r["ticker"] for r in kept] == ["005930", "011070"]
    assert "그룹 수 상한" in dropped[0]["drop_reason"]


def test_apply_caps_does_not_mutate_input():
    rows = [_r("005930", "삼성전자")]
    sd.apply_caps(rows, sector_map=SEC)
    assert "group" not in rows[0]


def test_dropped_rows_carry_reason_for_logging():
    rows = [_r("005930", "삼성전자"), _r("000660", "SK하이닉스")]
    _, dropped = sd.apply_caps(rows, max_per_group=1, sector_map=SEC)
    assert all("drop_reason" in r for r in dropped)


# ─── 집중도 ──────────────────────────────────────────


def test_concentration_by_amount():
    rows = [_r("005930", "삼성전자", amount=800), _r("000660", "SK하이닉스", amount=1200),
            _r("047040", "대우건설", amount=1000)]
    out = sd.concentration(rows, value_key="amount", sector_map=SEC)
    top = out[0]
    assert top["group"] == "섹터:반도체" and top["n"] == 2
    assert top["share_pct"] == 66.7          # 2000 / 3000


def test_concentration_by_count_without_value_key():
    rows = [_r("005930", "삼성전자"), _r("000660", "SK하이닉스"), _r("047040", "대우건설")]
    out = sd.concentration(rows, sector_map=SEC)
    assert out[0]["share_pct"] == 66.7


def test_concentration_empty():
    assert sd.concentration([]) == []


# ─── 포맷 ────────────────────────────────────────────


def test_format_warns_above_threshold():
    rows = [_r("005930", "삼성전자", amount=900), _r("047040", "대우건설", amount=100)]
    text = sd.format_concentration(sd.concentration(rows, value_key="amount", sector_map=SEC),
                                   top_share_warn=40.0)
    assert "⚠️" in text and "섹터:반도체" in text


def test_format_no_warning_when_spread():
    rows = [_r("A", "가", amount=100), _r("B", "나", amount=100),
            _r("C", "다", amount=100)]
    text = sd.format_concentration(sd.concentration(rows, value_key="amount", sector_map={}),
                                   top_share_warn=40.0)
    assert "⚠️" not in text


def test_format_reports_unmapped_count():
    rows = [_r("A", "가"), _r("B", "나")]
    text = sd.format_concentration(sd.concentration(rows, sector_map={}))
    assert "미분류 2종목" in text


def test_format_caps_lists_dropped():
    rows = [_r("005930", "삼성전자"), _r("000660", "SK하이닉스")]
    kept, dropped = sd.apply_caps(rows, max_per_group=1, sector_map=SEC)
    text = sd.format_caps(kept, dropped)
    assert "✅ 삼성전자" in text and "⛔ SK하이닉스" in text


def test_format_concentration_empty():
    assert "없습니다" in sd.format_concentration([])


# ─── Private API 입력 경계 ───────────────────────────


def test_load_position_rows_uses_injected_private_client():
    class Client:
        def __init__(self):
            self.slots = []

        def list_paper_positions(self, slot):
            self.slots.append(slot)
            return [{
                "ticker": "005930", "name": "삼성전자", "slot_name": "키움",
                "quantity": 2, "avg_price": 70000.0,
            }]

    client = Client()
    rows = sd.load_position_rows(slot="키움", private_client=client)
    assert client.slots == ["키움"]
    assert rows == [{
        "ticker": "005930", "name": "삼성전자", "slot": "키움", "amount": 140000.0,
    }]


def test_load_position_rows_excludes_empty_positions():
    class Client:
        def list_paper_positions(self, slot):
            return [{
                "ticker": "005930", "name": None, "slot_name": "키움",
                "quantity": 0, "avg_price": 70000.0,
            }]

    assert sd.load_position_rows(private_client=Client()) == []
