"""test_env_config.py — `.env` 로딩 검증 (hermetic, 파일시스템은 tmp_path만)."""
from __future__ import annotations

import os

import env_config as ec
import pytest


# ─── 파싱 ────────────────────────────────────────────


def test_parse_handles_comments_quotes_and_blanks():
    text = '\n'.join(['# 주석', '', 'A=1', 'B = "두 번째"', "C='셋'", '깨진줄', 'D='])
    assert ec.parse_env_file(text) == {"A": "1", "B": "두 번째", "C": "셋", "D": ""}


def test_parse_keeps_equals_inside_values():
    assert ec.parse_env_file("URL=https://x?a=1&b=2")["URL"] == "https://x?a=1&b=2"


# ─── 채우기 ──────────────────────────────────────────


def test_fills_missing_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("나의키", raising=False)
    env = tmp_path / ".env"
    env.write_text("나의키=abc\n", encoding="utf-8")
    assert ec.ensure_env(["나의키"], env) == []
    assert os.environ["나의키"] == "abc"


def test_never_overrides_an_existing_value(tmp_path, monkeypatch):
    monkeypatch.setenv("나의키", "이미있음")
    monkeypatch.delenv("다른키", raising=False)
    env = tmp_path / ".env"
    env.write_text("나의키=파일값\n다른키=7\n", encoding="utf-8")
    ec.ensure_env(["나의키", "다른키"], env)
    assert os.environ["나의키"] == "이미있음"
    assert os.environ["다른키"] == "7"


def test_missing_file_is_quiet_but_reports_what_is_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("없는키", raising=False)
    assert ec.ensure_env(["없는키"], tmp_path / "없음.env") == ["없는키"]


def test_reports_keys_the_file_does_not_have(tmp_path, monkeypatch):
    """조용한 실패를 막는 반환값 — 호출부가 무엇이 없는지 그대로 말할 수 있어야 한다."""
    monkeypatch.delenv("있는키", raising=False)
    monkeypatch.delenv("빠진키", raising=False)
    env = tmp_path / ".env"
    env.write_text("있는키=1\n", encoding="utf-8")
    assert ec.ensure_env(["있는키", "빠진키"], env) == ["빠진키"]


def test_all_present_does_not_read_the_file(monkeypatch, tmp_path):
    monkeypatch.setenv("있음", "v")
    assert ec.ensure_env(["있음"], tmp_path / "존재하지않음.env") == []


def test_a_coding_error_is_not_disguised_as_a_missing_env_file(monkeypatch, tmp_path):
    """넓은 except가 NameError를 삼켜 '.env 없음'으로 보이게 만든 적이 있다.

    왜 안 되는지 알 수 없게 되므로, 파일 오류(OSError)만 조용히 넘어간다.
    """
    monkeypatch.delenv("아무키", raising=False)
    env = tmp_path / ".env"
    env.write_text("아무키=abc\n", encoding="utf-8")

    def boom(_text):
        raise NameError("Path is not defined")

    monkeypatch.setattr(ec, "parse_env_file", boom)
    with pytest.raises(NameError):
        ec.ensure_env(["아무키"], env)
