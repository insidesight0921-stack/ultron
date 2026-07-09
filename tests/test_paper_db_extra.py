"""test_paper_db_extra.py — ensure_slot (hermetic, tmp db)."""
from __future__ import annotations

import paper_db as pdb


def test_ensure_slot_idempotent(tmp_path):
    db = tmp_path / "t.db"
    pdb.ensure_seed(db_path=db) if hasattr(pdb, "ensure_seed") else None
    sid1 = pdb.ensure_slot("마이퀀트", db_path=db)
    sid2 = pdb.ensure_slot("마이퀀트", db_path=db)
    assert sid1 == sid2
    slots = {s["name"]: s for s in pdb.list_slots(db_path=db)}
    assert "마이퀀트" in slots
    assert slots["마이퀀트"]["current_capital"] == 10_000_000.0
