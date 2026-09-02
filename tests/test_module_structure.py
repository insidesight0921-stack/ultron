"""test_module_structure.py — 임포트는 되는데 실행하면 죽는 유형을 막는다.

**2026-09-01 사고.** `krx_openapi.py`의 `if __name__ == "__main__"` 블록이
파일 **중간**(471행)에 있었고, 내가 새 함수를 그 뒤(509행)에 넣었다.

    테스트: 통과 — import는 파일 전체를 로드한다
    CLI 실행: NameError — guard에 닿을 때 아래 정의는 아직 없다

오늘 아침 봇이 부팅 때마다 죽던 것과 같은 계열이다: **테스트가 실행 경로를
타지 않으면 2,900개가 통과해도 실행은 죽는다.**

여기서는 라이브러리를 부르지 않고 AST로 구조만 본다 — 실행 없이도 도는
검사여야 어디서든 걸린다.
"""
from __future__ import annotations

import ast
import builtins
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _guard_line(tree: ast.Module) -> int | None:
    """`if __name__ == "__main__":`의 줄 번호. 없으면 None."""
    for node in tree.body:
        if isinstance(node, ast.If) and "__name__" in ast.dump(node.test):
            return node.lineno
    return None


def _top_level_defs_after(tree: ast.Module, line: int) -> list[str]:
    """guard 뒤에 남은 모듈 레벨 정의 — 실행 시점에 아직 없는 이름들."""
    out = []
    for node in tree.body:
        if node.lineno <= line:
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append(node.name)
        elif isinstance(node, ast.Assign):
            out += [t.id for t in node.targets if isinstance(t, ast.Name)]
    return out


def test_no_module_defines_anything_after_its_main_guard():
    """**guard 뒤의 정의는 CLI 실행 시점에 존재하지 않는다.**"""
    offenders = {}
    for path in sorted(SCRIPTS.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        line = _guard_line(tree)
        if line is None:
            continue
        leftovers = _top_level_defs_after(tree, line)
        if leftovers:
            offenders[path.name] = leftovers
    assert not offenders, (
        "if __name__ 뒤에 정의가 남아 있다 — 임포트는 되지만 CLI는 죽는다: "
        f"{offenders}")


def _imported_names(tree: ast.Module) -> set[str]:
    """임포트로 들어온 이름 — 이 파일이 정의하지 않았어도 실행 시점에 있다."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                out.add(a.asname or a.name)
    return out


def _bound_names(node: ast.AST) -> set[str]:
    """이 스코프가 묶는 이름 — 대입·for·with·except·정의·인자."""
    out: set[str] = set()
    args = getattr(node, "args", None)
    if isinstance(args, ast.arguments):
        for a in (args.posonlyargs + args.args + args.kwonlyargs):
            out.add(a.arg)
        for a in (args.vararg, args.kwarg):
            if a is not None:
                out.add(a.arg)
    stack = list(ast.iter_child_nodes(node))
    while stack:
        cur = stack.pop()
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(cur.name)          # 이름만 취하고 몸통은 자기 스코프에서 본다
            continue
        if isinstance(cur, (ast.Lambda,)):
            continue
        if isinstance(cur, ast.Name) and isinstance(cur.ctx, (ast.Store, ast.Del)):
            out.add(cur.id)
        elif isinstance(cur, ast.ExceptHandler) and cur.name:
            out.add(cur.name)
        elif isinstance(cur, (ast.Import, ast.ImportFrom)):
            for a in cur.names:
                out.add(a.asname or a.name.split(".")[0])
        elif isinstance(cur, ast.Global):
            out.update(cur.names)
        stack += list(ast.iter_child_nodes(cur))
    return out


def _undefined_names(tree: ast.Module, known: set[str]) -> set[str]:
    """어느 스코프에서도 묶이지 않은 채 **읽히는** 이름.

    `format_factor_candidates` 같은 오타·미정의는 실행할 때만 터진다.
    여기서 스코프 사슬을 따라가며 실행 없이 같은 것을 잡는다.
    """
    module_scope = known | _bound_names(tree) | _imported_names(tree)
    bad: set[str] = set()

    def visit(node: ast.AST, scopes: list[set[str]]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.Lambda, ast.ClassDef)):
                visit(child, scopes + [_bound_names(child)])
                continue
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                if not any(child.id in s for s in scopes):
                    bad.add(child.id)
            visit(child, scopes)

    visit(tree, [module_scope])
    return bad


def test_no_script_reads_a_name_that_is_never_defined():
    """**정의되지 않은 이름을 읽는 코드** — 임포트는 되고 실행만 죽는다.

    2026-09-01 `--factors`가 `NameError: format_factor_candidates`로 죽었을 때,
    2,931개 테스트는 전부 통과했다. 임포트가 파일 전체를 로드하기 때문이다.
    이 검사는 실행 없이 이름만 따라간다.
    """
    known = set(dir(builtins)) | {"__name__", "__file__", "__doc__", "__spec__",
                                  "__package__", "__builtins__", "__loader__",
                                  "__debug__", "WindowsError"}
    offenders = {}
    for path in sorted(SCRIPTS.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        bad = _undefined_names(tree, known)
        if bad:
            offenders[path.name] = sorted(bad)
    assert not offenders, f"정의되지 않은 이름을 읽는다: {offenders}"


def test_the_guard_is_the_last_thing_in_the_file():
    """관례이자 안전장치 — guard가 끝에 있으면 이 사고가 구조적으로 불가능하다."""
    late = []
    for path in sorted(SCRIPTS.glob("*.py")):
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        line = _guard_line(tree)
        if line is None:
            continue
        # guard 이후에 남은 것이 주석·빈 줄뿐이어야 한다.
        tail = [l for l in src.splitlines()[line:] if l.strip()
                and not l.strip().startswith("#") and not l.startswith((" ", "\t"))]
        if tail:
            late.append((path.name, tail[:2]))
    assert not late, f"guard 뒤에 코드가 있다: {late}"
