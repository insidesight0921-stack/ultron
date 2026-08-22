from __future__ import annotations

import json

import pytest

import storage_paths as sp


def test_missing_config_keeps_legacy_layout(tmp_path):
    paths = sp.get_paths(tmp_path)
    assert paths.layout == sp.LEGACY_LAYOUT
    assert paths.paper_db == tmp_path / "data" / "paper.db"
    assert paths.schedule_db == tmp_path / "data" / "schedule.db"
    assert paths.watchlist_db == tmp_path / "data" / "private.db"
    assert paths.rag_dir == tmp_path / "data" / "lancedb"


def test_private_v1_layout_separates_private_and_shareable(tmp_path):
    config = tmp_path / "data" / sp.LAYOUT_FILE_NAME
    config.parent.mkdir()
    config.write_text(json.dumps({"layout": "private-v1"}), encoding="utf-8")

    paths = sp.get_paths(tmp_path)

    assert paths.paper_db == tmp_path / "data" / "private" / "paper.db"
    assert paths.schedule_db == tmp_path / "data" / "private" / "assistant.db"
    assert paths.watchlist_db == paths.schedule_db
    assert paths.rag_dir == tmp_path / "data" / "private" / "rag"
    assert paths.private_state_file("signal_last.json") == (
        tmp_path / "data" / "private" / "state" / "signal_last.json"
    )
    assert paths.shareable_cache_file("corp_codes.json") == (
        tmp_path / "data" / "shareable" / "cache" / "corp_codes.json"
    )
    assert paths.shareable_samples_dir == tmp_path / "data" / "shareable" / "samples"
    assert paths.dart_finance_validation.parent == paths.shareable_samples_dir
    assert paths.ipo_demand_validation.parent == paths.shareable_samples_dir


def test_environment_override_wins_over_config(tmp_path, monkeypatch):
    config = tmp_path / "data" / sp.LAYOUT_FILE_NAME
    config.parent.mkdir()
    config.write_text(json.dumps({"layout": "private-v1"}), encoding="utf-8")
    monkeypatch.setenv(sp.LAYOUT_ENV, "legacy")
    assert sp.get_paths(tmp_path).layout == "legacy"


def test_invalid_config_fails_closed_to_legacy(tmp_path):
    config = tmp_path / "data" / sp.LAYOUT_FILE_NAME
    config.parent.mkdir()
    config.write_text('{"layout":"unknown"}', encoding="utf-8")
    assert sp.get_paths(tmp_path).layout == "legacy"


def test_explicit_invalid_layout_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        sp.get_paths(tmp_path, layout="unknown")
