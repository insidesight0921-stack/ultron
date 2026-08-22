from __future__ import annotations

import pandas as pd

import index_wiki as iw


class _FakeTable:
    def __init__(self, files: list[str]):
        self._files = files
        self.deleted: list[str] = []

    def count_rows(self) -> int:
        return len(self._files)

    def to_pandas(self) -> pd.DataFrame:
        return pd.DataFrame({"file": self._files})

    def delete(self, predicate: str) -> None:
        self.deleted.append(predicate)


def test_stale_index_files_returns_only_deleted_paths_sorted():
    indexed = {"wiki/z.md", "wiki/keep.md", "wiki/a.md"}
    actual = {"wiki/keep.md", "wiki/new.md"}

    assert iw.stale_index_files(indexed, actual) == ["wiki/a.md", "wiki/z.md"]


def test_purge_deleted_files_deletes_every_stale_file_once():
    table = _FakeTable(["wiki/keep.md", "wiki/gone.md", "wiki/gone.md"])

    purged = iw.purge_deleted_files(table, {"wiki/keep.md"})

    assert purged == ["wiki/gone.md"]
    assert table.deleted == ["file = 'wiki/gone.md'"]


def test_filter_string_escapes_single_quote():
    assert iw._filter_string("wiki/user's-note.md") == "'wiki/user''s-note.md'"


def test_purge_deleted_files_skips_empty_table():
    table = _FakeTable([])

    assert iw.purge_deleted_files(table, set()) == []
    assert table.deleted == []


def test_process_file_combines_short_sections_into_one_document(
    monkeypatch, tmp_path
):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    note = wiki / "짧은_체크.md"
    note.write_text(
        "# 짧은 체크\n\n"
        "## 📌 요약\n다음 주 실적 발표 일정을 확인한다.\n\n"
        "## 📖 내용\n발표일과 컨센서스 변경 여부를 함께 점검한다.\n\n"
        "## 확인 항목\n발표 전날 텔레그램 일정도 확인한다.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(iw, "VAULT", tmp_path)

    records = iw.process_file(note)

    assert len(records) == 1
    assert records[0]["section"] == "__document__"
    assert records[0]["file"] == "wiki/짧은_체크.md"
    assert "실적 발표" in records[0]["content"]
