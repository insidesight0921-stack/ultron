"""test_kium_flag.py — 주간 중복 처리 방지 플래그 (소스 수준 배선 검증).

2026-08-31 로그에서 드러났다.

    File "telegram_bot.py", line 2399, in _save_kium_flag
    NameError: name 'json' is not defined

모듈 레벨 `import json`이 없어 저장이 실패했다. 읽기(`_load_kium_flag`)는 같은
NameError를 `except Exception`이 삼켜 **항상 빈 dict를 돌려주고 있었다.**
플래그가 늘 비면 "이번 주 이미 처리했는가"를 판정할 수 없다.

실제 피해(운영 DB 실측): 2026-06-08 하루에 키움 주간 스캔이 **두 번** 돌아
같은 주 같은 종목을 중복 매수했다 — 7종목(SK하이닉스·미래에셋증권·삼성전기·
LS ELECTRIC·대우건설·효성중공업·SK스퀘어). 10:58 배치가 세션에서 따로 다뤘던
**스테일 진입가 배치**와 같은 것이다. 그때는 가격만 봤고 "왜 같은 날 두 번
샀는가"는 짚지 못했다.
"""
from __future__ import annotations

import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "scripts"
       / "telegram_bot.py").read_text(encoding="utf-8")


def _func(name: str) -> str:
    i = SRC.index(f"def {name}(")
    return SRC[i:SRC.index("\ndef ", i + 10)]


def _code(name: str) -> str:
    """docstring을 뺀 본문. 설명 문구를 코드로 오인하지 않도록.

    (이 테스트가 처음에 그렇게 실패했다 — docstring에 적어 둔
     "`except Exception`이 삼켜서"라는 설명을 코드로 읽었다.)
    """
    import ast
    tree = ast.parse(_func(name).replace("\n", "\n", 1))
    fn = tree.body[0]
    body = fn.body[1:] if (fn.body and isinstance(fn.body[0], ast.Expr)
                           and isinstance(fn.body[0].value, ast.Constant)) else fn.body
    return "\n".join(ast.unparse(node) for node in body)


def test_json_is_imported_at_module_level():
    """플래그 저장·로드가 모듈 레벨 json을 쓴다."""
    assert re.search(r"^import json$", SRC, re.M)


def test_the_loader_does_not_swallow_coding_errors():
    """넓은 except가 NameError를 삼켜 '플래그 없음'으로 위장했다.

    `.env` 로딩에서 똑같은 교훈을 얻고도 여기 남아 있었다.
    """
    body = _code("_load_kium_flag")
    assert "except Exception" not in body
    assert "except (OSError, ValueError)" in body


def test_every_flag_loader_uses_a_defined_json_name():
    """`json`을 쓰는 곳과 `_json`(함수 내 import)을 쓰는 곳이 섞여 있었다.

    키움만 모듈 레벨 이름을 참조했고, 그 import가 없어 깨졌다.
    """
    for name in ("_load_kium_flag", "_save_kium_flag"):
        body = _code(name)
        if "json." in body and "_json." not in body:
            assert re.search(r"^import json$", SRC, re.M), name


def test_the_weekly_job_still_saves_the_flag():
    """저장 호출이 사라지면 중복 방지가 통째로 없어진다."""
    i = SRC.index("async def kium_weekly_scan_job")
    body = SRC[i:SRC.index("\nasync def ", i + 10)]
    assert "_save_kium_flag(" in body


def test_the_flag_is_checked_before_pushing():
    i = SRC.index("async def kium_weekly_scan_job")
    body = SRC[i:SRC.index("\nasync def ", i + 10)]
    assert "_load_kium_flag(" in body
    assert body.index("_load_kium_flag(") < body.index("_save_kium_flag(")
