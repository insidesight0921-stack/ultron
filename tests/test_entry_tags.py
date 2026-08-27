"""test_entry_tags.py — 진입 근거 태그(순수) 검증 (hermetic)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import entry_tags as et


@dataclass
class Rec:
    ticker: str = "005930"
    name: str = "삼성전자"
    z_factors: dict = None
    return_12m: float = None


# ─── 값 꺼내기 ───────────────────────────────────────


def test_attr_reads_dict_and_dataclass():
    assert et.attr({"a": 1}, "a") == 1
    assert et.attr(Rec(name="X"), "name") == "X"
    assert et.attr({}, "a", "기본") == "기본"


# ─── 키움 ────────────────────────────────────────────


def test_rank_buckets():
    assert et.rank_bucket(1) == "랭크1-4"
    assert et.rank_bucket(4) == "랭크1-4"
    assert et.rank_bucket(5) == "랭크5-8"
    assert et.rank_bucket(9) == "랭크9+"


def test_rank_bucket_missing_makes_no_tag():
    """모르는 것을 '미상'으로 채우면 그 '미상'이 하나의 전략처럼 집계된다."""
    assert et.rank_bucket(None) is None and et.rank_bucket(0) is None


def test_momentum_buckets():
    assert et.momentum_bucket(0.75) == "12M강함"
    assert et.momentum_bucket(0.25) == "12M보통"
    assert et.momentum_bucket(-0.1) == "12M약함"
    assert et.momentum_bucket(None) is None


def test_kium_tags_include_bot_and_available_axes():
    tags = et.kium_tags({"return_12m": 0.8}, rank=2)
    assert tags == ["키움모멘텀", "랭크1-4", "12M강함"]


def test_kium_tags_drop_unknown_axes():
    assert et.kium_tags({}, rank=None) == ["키움모멘텀"]


def test_kium_tags_respect_max():
    assert len(et.kium_tags({"return_12m": 0.8}, rank=1)) <= et.MAX_TAGS


# ─── 콴텍 ────────────────────────────────────────────


def test_dominant_factor_picks_highest_z():
    assert et.dominant_factor({"value": 0.2, "momentum": 1.7}) == "주도-momentum"


def test_dominant_factor_ignores_non_numeric():
    assert et.dominant_factor({"value": "x"}) is None
    assert et.dominant_factor({}) is None and et.dominant_factor(None) is None


def test_quant_tags_carry_phase_and_factor():
    rec = Rec(z_factors={"value": 1.2, "quality": 0.1})
    assert et.quant_tags(rec, phase="확장") == ["콴텍팩터", "국면-확장", "주도-value"]


def test_quant_tags_without_phase():
    assert et.quant_tags(Rec(z_factors=None)) == ["콴텍팩터"]


# ─── 메모 합치기 ─────────────────────────────────────


def test_format_note_appends_without_touching_base():
    note = et.format_note("[AUTO] 2026-W34 키움봇", ["키움모멘텀", "랭크1-4"])
    assert note.startswith("[AUTO] 2026-W34 키움봇 ")
    assert note.endswith("MQ[키움모멘텀,랭크1-4]")


def test_format_note_is_readable_by_existing_parser():
    """기존 파서가 그대로 읽어야 한다 — 형식을 새로 만들면 집계가 갈라진다."""
    import trade_analytics as ta
    note = et.format_note("[AUTO] 2026-08 콴텍봇 신규", ["콴텍팩터", "국면-확장"])
    assert ta.extract_tags(note) == ["콴텍팩터", "국면-확장"]


def test_format_note_keeps_week_key_extractable():
    note = et.format_note("[AUTO] 2026-W34 키움봇", ["키움모멘텀"])
    assert "2026-W34" in note and "키움봇" in note


def test_format_note_without_tags_is_unchanged():
    assert et.format_note("[AUTO] x", []) == "[AUTO] x"
    assert et.format_note("[AUTO] x", None) == "[AUTO] x"


def test_format_note_does_not_double_tag():
    once = et.format_note("[AUTO] x", ["a"])
    assert et.format_note(once, ["b"]) == once


def test_clean_strips_characters_that_break_the_parser():
    assert et.clean(["a,b", "[c]", "", None, "a b"]) == ["a b", "c"]


def test_clean_drops_duplicates_keeping_order():
    assert et.clean(["b", "a", "b"]) == ["b", "a"]


# ─── 배선 검증 (소스 수준) ───────────────────────────


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def test_both_auto_buy_paths_write_tags():
    """키움·콴텍 두 자동 매수 경로 모두 태그를 남겨야 커버리지가 찬다."""
    src = (SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8")
    assert "entry_tags.kium_tags" in src
    assert "entry_tags.quant_tags" in src
    assert src.count("entry_tags.format_note") == 2


def test_tagging_failure_does_not_block_the_buy():
    """태그는 부가 정보다. 태그를 못 만들었다고 매수가 실패하면 안 된다."""
    src = (SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8")
    for marker in ("entry_tags.kium_tags", "entry_tags.quant_tags"):
        i = src.index(marker)
        window = src[max(0, i - 400):i + 200]
        assert "except Exception" in window
