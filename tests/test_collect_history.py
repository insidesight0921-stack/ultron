"""test_collect_history.py — 소급 가능한 것만 채운다. 없는 과거를 만들지 않는다.

**2026-09-01 사용자 질문에서 나온 파일.** "이전 주가 자료를 보고 수집은 못
하는 거야?" — 할 수 있는데 안 하고 있었다. 하루 종일 "표본이 없다"고 말한
것 중 상당수가 없는 게 아니라 **안 가져온 것**이었다.

이 파일이 고정하는 두 가지.
1. 소급은 **병합**이다 — 짧은 창이 긴 이력을 덮으면 안 된다(어제 사고).
2. 소급 가능/불가의 경계 — 시장이 남긴 기록만 채운다. 봇의 거래·사용자
   행동을 "채우면" 그건 과거를 지어내는 것이다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import collect_history as ch  # noqa: E402


# ─── 국면 판정 ───────────────────────────────────────


def test_a_one_sided_span_is_named_as_such():
    """나우캐스팅이 막힌 진짜 이유는 표본 수가 아니라 국면이 하나뿐인 것."""
    closes = [100.0 + i for i in range(400)]        # 계속 상승
    span = ch.regime_span([f"d{i}" for i in range(400)], closes)
    assert span["below"] == 0
    assert span["has_both"] is False


def test_a_span_with_both_regimes_is_recognized():
    closes = [100.0 + i for i in range(300)] + [400.0 - i * 2 for i in range(200)]
    span = ch.regime_span([f"d{i}" for i in range(500)], closes)
    assert span["above"] > 0 and span["below"] > 0
    assert span["has_both"] is True
    assert span["flips"] >= 1


def test_a_short_series_yields_no_verdict():
    span = ch.regime_span(["d1", "d2"], [100.0, 101.0])
    assert span["n"] == 0
    assert span["has_both"] is False


# ─── 병합 (어제 사고의 재발 방지) ────────────────────


def test_backfill_merges_instead_of_overwriting(tmp_path, monkeypatch):
    monkeypatch.setattr(ch, "_cache_root", lambda: tmp_path)
    ch._write_series("vix", "vix", [f"2025{m:02d}01" for m in range(1, 13)],
                     [15.0] * 12, source="t")
    out = ch._write_series("vix", "vix", ["20260101"], [50.0], source="t")
    payload = json.loads(Path(out).read_text(encoding="utf-8"))
    assert len(payload["series"]["date"]) == 13


def test_the_newer_value_wins_for_the_same_day(tmp_path, monkeypatch):
    monkeypatch.setattr(ch, "_cache_root", lambda: tmp_path)
    ch._write_series("vix", "vix", ["20260101", "20260102"], [15.0, 16.0], source="t")
    out = ch._write_series("vix", "vix", ["20260102"], [99.0], source="t")
    p = json.loads(Path(out).read_text(encoding="utf-8"))["series"]
    assert p["close"][p["date"].index("20260102")] == 99.0
    assert p["close"][p["date"].index("20260101")] == 15.0


def test_the_file_is_named_by_the_latest_day(tmp_path, monkeypatch):
    """읽는 쪽이 sorted(glob)[-1]을 쓴다 — 최신 파일이 전체 이력을 담아야 한다."""
    monkeypatch.setattr(ch, "_cache_root", lambda: tmp_path)
    out = ch._write_series("vix", "vix", ["20250101", "20260301"], [15.0, 20.0],
                           source="t")
    assert Path(out).name == "vix_20260301.json"


# ─── 경계: 소급 가능/불가 ────────────────────────────


def test_only_market_records_are_backfillable():
    """봇의 거래·mode 판정·사용자 행동을 채우면 과거를 지어내는 것이다."""
    assert set(ch.BACKFILLABLE) == {"kospi", "vkospi", "vix", "fx", "flow"}
    for forbidden in ("paper", "trade", "mode", "signal"):
        assert not any(forbidden in item for item in ch.BACKFILLABLE)


def test_the_status_names_what_cannot_be_backfilled(tmp_path, monkeypatch):
    monkeypatch.setattr(ch, "_cache_root", lambda: tmp_path)
    (tmp_path / "indices").mkdir(parents=True)
    msg = ch.format_status()
    assert "소급 불가" in msg
    assert "거래 기록" in msg


def test_the_status_tells_you_to_collect_more_rather_than_wait(tmp_path, monkeypatch):
    """'국면을 기다려야 한다'가 아니라 '더 소급하면 된다'로 말해야 한다."""
    monkeypatch.setattr(ch, "_cache_root", lambda: tmp_path)
    d = tmp_path / "indices"
    d.mkdir(parents=True)
    closes = [100.0 + i for i in range(400)]
    (d / "market_index_KOSPI_20260901.json").write_text(json.dumps(
        {"series": {"date": [f"2026{i:04d}" for i in range(1, 401)],
                    "close": closes}}), encoding="utf-8")
    msg = ch.format_status()
    assert "더 소급하면 됩니다" in msg


# ─── 원인을 정확히 말한다 (2026-09-02) ──────────────────

def test_a_login_requirement_is_named_not_called_an_empty_response(monkeypatch):
    """**「응답이 비었습니다」는 네트워크를 의심하게 만든다.**

    실제 원인은 KRX 회원 로그인이었다. CLI 시리즈가 낡았을 때
    「키·네트워크 점검」이라 말하던 것과 같은 실수다.
    """
    monkeypatch.delenv("KRX_ID", raising=False)
    monkeypatch.delenv("KRX_PW", raising=False)
    msg = ch._flow_failure_reason(
        RuntimeError("KRX 로그인 실패: KRX_ID 또는 KRX_PW 환경 변수가 없습니다"))
    assert "KRX 회원 로그인" in msg
    assert "KRX_ID" in msg and "KRX_PW" in msg
    assert "네트워크·키 문제가 아닙니다" in msg


def test_a_real_empty_response_is_still_reported_as_such(monkeypatch):
    """자격증명이 있는데도 비면 그때는 진짜 빈 응답이다."""
    monkeypatch.setenv("KRX_ID", "id")
    monkeypatch.setenv("KRX_PW", "pw")
    msg = ch._flow_failure_reason(None)
    assert "비었습니다" in msg and "로그인" not in msg
