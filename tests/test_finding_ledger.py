"""test_finding_ledger.py — 공용 판정 원장."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import finding_ledger as fl  # noqa: E402


def test_the_first_record_lands(tmp_path):
    rows, added = fl.append({"at": "1", "x": 1}, tmp_path / "l.json")
    assert added is True and len(rows) == 1


def test_the_same_finding_is_not_written_twice(tmp_path):
    """**새 표본이 없는 재실행은 새 판정이 아니다.**"""
    p = tmp_path / "l.json"
    fl.append({"at": "1", "x": 1}, p)
    rows, added = fl.append({"at": "2", "x": 1}, p)
    assert added is False and len(rows) == 1
    assert fl.latest(p)["at"] == "1"


def test_a_changed_finding_appends_and_keeps_the_old_one(tmp_path):
    p = tmp_path / "l.json"
    fl.append({"at": "1", "x": 1}, p)
    rows, added = fl.append({"at": "2", "x": 2}, p)
    assert added is True and len(rows) == 2 and rows[0]["x"] == 1


def test_time_alone_is_not_a_change():
    assert fl.same_finding({"at": "1", "x": 1}, {"at": "9", "x": 1}) is True


def test_a_new_key_is_a_change():
    """한쪽에만 있는 키를 무시하면 판정이 늘어난 것을 놓친다."""
    assert fl.same_finding({"at": "1", "x": 1}, {"at": "1", "x": 1, "y": 2}) is False


def test_nothing_is_not_a_match():
    assert fl.same_finding(None, {"x": 1}) is False
    assert fl.same_finding({"x": 1}, None) is False


def test_a_broken_ledger_is_not_fatal(tmp_path):
    p = tmp_path / "l.json"
    p.write_text("{망가진", encoding="utf-8")
    assert fl.load(p) == [] and fl.latest(p) is None
    rows, added = fl.append({"at": "1"}, p)
    assert added is True and len(rows) == 1


def test_a_non_list_ledger_is_treated_as_empty(tmp_path):
    p = tmp_path / "l.json"
    p.write_text('{"not": "a list"}', encoding="utf-8")
    assert fl.load(p) == []


def test_a_missing_ledger_reads_as_empty(tmp_path):
    assert fl.latest(tmp_path / "없음.json") is None


def test_the_file_is_valid_json_after_appends(tmp_path):
    p = tmp_path / "l.json"
    fl.append({"at": "1", "x": 1}, p)
    fl.append({"at": "2", "x": 2}, p)
    assert len(json.loads(p.read_text(encoding="utf-8"))) == 2
