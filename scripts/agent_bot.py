"""agent_bot.py — 로컬 Gemma 기반 범용 오케스트레이션 에이전트 (v1)

목적: 새 봇을 매번 코딩하지 않고, 텔레그램 자연어 명령을 받아 **여러 도구를
조합(다단계)** 해 수행한다. 라우터(Gemma 26B)가 단일 도구 1회 분기라면,
agent_bot은 ReAct 루프로 도구를 연쇄 호출하고 결과를 종합한다.

설계·안전:
  - 완전 로컬 (Gemma 31B). 외부 LLM 미사용.
  - v1은 **안전 도구만**: 파일 읽기(경로 샌드박스)·목록·RAG·기존 봇 호출.
    임의 셸/코드 실행·파일 쓰기는 v1 제외(승인형 v2 후보).
  - LLM 호출(_chat)·도구는 분리 → 테스트는 monkeypatch(네트워크 불필요).
  - JSON 액션 프로토콜(라우터와 동일 검증된 방식). 파싱 실패/도구 오류 graceful.
  - step cap(기본 5)으로 무한 루프 차단.

액션 프로토콜 (Gemma 출력):
  {"thought": "...", "action": {"tool": "<name>", "args": {...}}}
  또는 {"thought": "...", "final": "<사용자 답변>"}
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.request import Request, urlopen

log = logging.getLogger("agent_bot")

OLLAMA_URL = "http://127.0.0.1:11434"
LLM_MODEL = "gemma4:31b"
KEEP_ALIVE = "30m"

MAX_STEPS = 5
MAX_OBSERVATION_CHARS = 2000
MAX_FILE_CHARS = 4000

# 파일 접근 샌드박스 — 허용 디렉터리와 텍스트 형식을 모두 만족해야 읽기 허용
PROJECT_ROOT = Path(__file__).resolve().parent.parent
VAULT_ROOT = PROJECT_ROOT.parent / "obsidian-vault"
ALLOWED_ROOTS = [PROJECT_ROOT / "scripts", PROJECT_ROOT / "docs", VAULT_ROOT / "wiki"]
ALLOWED_FILES = [PROJECT_ROOT / "README.md"]
ALLOWED_SUFFIXES = {".md", ".py", ".sh", ".txt", ".yaml", ".yml", ".toml"}
PATH_BASES = [PROJECT_ROOT, VAULT_ROOT]

# 쓰기는 오직 이 디렉터리(raw/inbox)만 허용 — watch_raw가 자동 정제→wiki
INBOX_DIR = VAULT_ROOT / "raw" / "inbox"


# ─── 도구 레지스트리 ─────────────────────────────────


@dataclass
class Tool:
    name: str
    desc: str               # LLM에게 보여줄 설명
    func: Callable[[dict], str]   # args dict → 관찰(문자열)
    args_hint: str = ""     # 인자 예시


_TOOLS: dict[str, Tool] = {}


def register_tool(tool: Tool) -> None:
    _TOOLS[tool.name] = tool


def clear_tools() -> None:
    _TOOLS.clear()


def list_tools() -> list[Tool]:
    return list(_TOOLS.values())


# ─── 안전 내장 도구 (파일 읽기 — 샌드박스) ─────────────


def _resolve_safe(path_str: str) -> Optional[Path]:
    """읽기 allowlist 하위로만 해석한다. 경로 탈출·숨김 경로는 차단한다."""
    try:
        p = Path(path_str).expanduser()
        if not p.is_absolute():
            # 기존 wiki/...·scripts/... 호출을 유지하면서 좁은 allowlist를 적용한다.
            for base in [*PATH_BASES, *ALLOWED_ROOTS]:
                cand = (base / path_str).resolve()
                if _within_allowed(cand):
                    return cand
            return None
        p = p.resolve()
        return p if _within_allowed(p) else None
    except Exception:
        return None


def _within_allowed(p: Path) -> bool:
    resolved = p.resolve()
    for allowed_file in ALLOWED_FILES:
        if resolved == allowed_file.resolve():
            return True
    for root in ALLOWED_ROOTS:
        try:
            relative = resolved.relative_to(root.resolve())
            return not any(part.startswith(".") for part in relative.parts)
        except ValueError:
            continue
    return False


def _is_readable_file(p: Path) -> bool:
    return _within_allowed(p) and p.suffix.lower() in ALLOWED_SUFFIXES


def _tool_read_file(args: dict) -> str:
    p = _resolve_safe(str(args.get("path", "")))
    if p is None or not p.exists() or not p.is_file() or not _is_readable_file(p):
        return "오류: 허용된 경로의 파일이 아닙니다."
    try:
        txt = p.read_text(encoding="utf-8", errors="replace")
        return txt[:MAX_FILE_CHARS]
    except Exception as e:
        return f"오류: 읽기 실패 {e}"


def _tool_list_files(args: dict) -> str:
    base = _resolve_safe(str(args.get("dir", ".")))
    if base is None or not base.exists():
        return "오류: 허용된 디렉터리가 아닙니다."
    pattern = str(args.get("pattern", "*"))
    try:
        if base.is_file():
            base = base.parent
        items = sorted(
            str(p.relative_to(base))
            for p in base.glob(pattern)
            if p.is_file() and _is_readable_file(p)
        )
        return "\n".join(items[:100]) or "(파일 없음)"
    except Exception as e:
        return f"오류: 목록 실패 {e}"


def register_builtin_tools() -> None:
    register_tool(Tool("read_file",
                       "허용 경로(프로젝트 scripts/docs, README, 볼트 wiki)의 텍스트 파일 읽기",
                       _tool_read_file, args_hint='{"path": "wiki/투자/리스크_관리_원칙.md"}'))
    register_tool(Tool("list_files",
                       "허용 경로의 텍스트 파일 목록(glob pattern 가능)",
                       _tool_list_files, args_hint='{"dir": "wiki/투자", "pattern": "*.md"}'))


def _tool_write_inbox(args: dict) -> str:
    """메모를 raw/inbox/에만 저장(쓰기 허용 범위는 inbox 한정). watch_raw가 정제→wiki."""
    content = str(args.get("content", "")).strip()
    if not content:
        return "오류: content가 비어 있습니다."
    title = str(args.get("title", "")).strip()
    safe = re.sub(r"[^0-9A-Za-z가-힣 _-]", "", title)[:40].strip().replace(" ", "_") or "메모"
    fname = f"{datetime.now():%Y%m%d_%H%M%S}_{safe}.md"
    try:
        INBOX_DIR.mkdir(parents=True, exist_ok=True)
        target = (INBOX_DIR / fname).resolve()
        # 이중 안전장치: 반드시 INBOX_DIR 하위
        target.relative_to(INBOX_DIR.resolve())
        target.write_text(content, encoding="utf-8")
        return f"저장됨: raw/inbox/{fname} ({len(content)}자). watch_raw가 곧 wiki로 정제합니다."
    except Exception as e:
        return f"오류: 저장 실패 {e}"


def register_inbox_tool() -> None:
    """쓰기 도구(write_inbox) 등록. 기본 builtin과 분리 — 명시적으로만 켠다."""
    register_tool(Tool("write_inbox",
                       "메모/노트를 raw/inbox에 저장(자동 정제→wiki). 쓰기는 inbox만 가능",
                       _tool_write_inbox, args_hint='{"title":"제목", "content":"본문"}'))


# ─── LLM 호출 (테스트는 monkeypatch) ─────────────────


def _chat(prompt: str, temperature: float = 0.2) -> str:
    body = json.dumps({
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "keep_alive": KEEP_ALIVE,
        "format": "json",
        "options": {"temperature": temperature, "num_ctx": 16384, "num_predict": 1024},
    }).encode("utf-8")
    req = Request(f"{OLLAMA_URL}/api/chat", data=body,
                  headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))["message"]["content"]


def _parse_action(text: str) -> Optional[dict]:
    """LLM 출력에서 JSON 액션 추출. 실패 시 None."""
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def _build_prompt(task: str, history: list[tuple]) -> str:
    tool_lines = "\n".join(
        f'- {t.name}: {t.desc}' + (f' (args 예: {t.args_hint})' if t.args_hint else '')
        for t in list_tools()
    )
    hist = ""
    for action, obs in history:
        hist += f"\n[행동] {json.dumps(action, ensure_ascii=False)}\n[관찰] {obs[:MAX_OBSERVATION_CHARS]}\n"
    return (
        "너는 사용자의 명령을 도구를 조합해 수행하는 한국어 에이전트다.\n"
        "사용 가능한 도구:\n" + tool_lines + "\n\n"
        "규칙:\n"
        "1) 매 단계 JSON 하나만 출력. 형식:\n"
        '   {"thought":"...", "action":{"tool":"<도구>","args":{...}}}\n'
        '   또는 최종 답이면 {"thought":"...","final":"<사용자에게 보낼 답>"}\n'
        "2) 도구 결과(관찰)를 보고 다음 단계를 정한다. 충분하면 final.\n"
        "3) 모르는 도구는 쓰지 말 것. 안전한 읽기 작업만 가능.\n\n"
        f"[사용자 명령]\n{task}\n"
        + (f"\n[지금까지 진행]{hist}" if hist else "")
        + "\n이제 다음 JSON 하나를 출력:"
    )


_CONJ_RE = re.compile(r"그리고|하고|또한|또 |동시에|같이|함께|이후|다음에|랑 |와 함께|및 ")
_ACTVERB_RE = re.compile(r"요약|정리|스캔|찾아|알려|보여|분석|브리핑|확인|검색|읽어|보내|뽑아|모아")


def is_compound_command(text: str) -> bool:
    """다단계(복합) 명령 감지 — 자동 에이전트 위임용.

    보수적 기준: 서로 다른 행동 동사 2개 이상 + 연결어 존재.
    단일 명령/질문은 False(기존 라우터 단일 도구로 빠르게).
    """
    if not text:
        return False
    verbs = len(set(_ACTVERB_RE.findall(text)))
    return verbs >= 2 and bool(_CONJ_RE.search(text))


def run(task: str, max_steps: int = MAX_STEPS, llm: Callable[[str], str] = None) -> str:
    """에이전트 루프. 최종 답(문자열) 반환."""
    llm = llm or _chat
    if not task or not task.strip():
        return "명령이 비어 있습니다."
    history: list[tuple] = []
    for step in range(max_steps):
        try:
            raw = llm(_build_prompt(task, history))
        except Exception as e:
            log.warning(f"에이전트 LLM 실패: {e}")
            return f"에이전트 오류: LLM 호출 실패 ({e})"
        action = _parse_action(raw)
        if action is None:
            history.append(({"error": "parse_fail"}, "JSON 파싱 실패 — 다시 시도"))
            continue
        if "final" in action:
            return str(action["final"]).strip() or "(빈 응답)"
        act = action.get("action") or {}
        tool_name = act.get("tool")
        args = act.get("args") or {}
        tool = _TOOLS.get(tool_name)
        if tool is None:
            obs = f"오류: '{tool_name}'는 사용 가능한 도구가 아님. 사용 가능: {', '.join(_TOOLS)}"
        else:
            try:
                obs = str(tool.func(args))
            except Exception as e:
                obs = f"오류: 도구 실행 실패 {e}"
        history.append((action, obs))
    # step cap 도달 — 마지막 관찰로 best-effort 종합
    last_obs = history[-1][1] if history else ""
    return ("최대 단계에 도달했습니다. 지금까지 확인한 내용:\n" + last_obs[:MAX_OBSERVATION_CHARS]) \
        if last_obs else "수행할 수 없었습니다 (도구 응답 없음)."


# ─── CLI ────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    register_builtin_tools()
    q = " ".join(sys.argv[1:]) or "wiki 투자 폴더 파일 목록 보여줘"
    print(run(q))
