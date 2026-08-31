"""test_operational_db_identity.py — 운영 DB가 어느 파일인지 못 틀리게 한다.

**2026-08-31: 하루치 분석을 낡은 DB 사본에서 했다.** `data/paper.db`(08-22 이후
정지)를 운영 DB로 알고 완결 라운드트립·승률·탭 E·마이퀀트 표본을 전부 그 위에서
쟀다. 운영 DB는 `data/private/paper.db`다.

들키지 않은 이유가 문제였다. **낡은 사본에도 표가 있고 데이터가 있어서 수치가
정상적으로 나왔다.** 56건·39.3%는 그 자체로는 이상해 보이지 않는다. 파일을
잘못 열었다는 신호가 어디에도 없었다.

그래서 두 가지를 고정한다.
  1) `DEFAULT_DB_PATH`는 반드시 private 영역을 가리킨다.
  2) 저장소 `data/` 바로 밑에 `paper.db`라는 이름이 **다시 생기지 않는다.**
     같은 이름의 파일이 두 곳에 있으면 다음에도 똑같이 틀린다.
"""
from __future__ import annotations

from pathlib import Path

import paper_db

REPO = Path(__file__).resolve().parents[1]


def test_the_operational_db_lives_under_private():
    p = Path(paper_db.DEFAULT_DB_PATH)
    assert p.name == "paper.db"
    assert "private" in p.parts, f"운영 DB가 private 밖이다: {p}"


def test_no_decoy_paper_db_sits_at_the_data_root():
    """같은 이름의 사본이 옆에 있으면 다음에도 잘못 연다.

    보관이 필요하면 `_STALE_`처럼 **열면 안 된다는 것이 이름에 보이게** 둔다.
    """
    decoy = REPO / "data" / "paper.db"
    assert not decoy.exists(), (
        f"{decoy} 가 다시 생겼다 — 운영 DB는 {paper_db.DEFAULT_DB_PATH} 다. "
        "사본이 필요하면 이름에 _STALE_ 을 붙여 구분되게 두라")


def test_scripts_do_not_hardcode_the_data_root_copy():
    """`data/paper.db` 문자열이 코드에 있으면 배선이 다시 갈라진다."""
    offenders = []
    for path in (REPO / "scripts").glob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            if "data/paper.db" in line and not line.lstrip().startswith("#"):
                offenders.append(f"{path.name}:{i}")
    assert not offenders, f"data/paper.db 하드코딩: {offenders}"
