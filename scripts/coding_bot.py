#!/usr/bin/env python3
"""
코딩봇 (coding_bot) — 하이브리드 LLM (Qwen2.5-Coder 32B 로컬 + Claude API).

설계 결정:
- 단일 봇, action으로 분기 — design / code / debug / review
- mode:
    fast     → Qwen2.5-Coder 32B (ollama, 무료, ~30-60초)
    accurate → Claude Sonnet 4.6 API (~5-15초, 비용 발생)
  ANTHROPIC_API_KEY 미설정 시 accurate도 자동으로 qwen으로 fallback.
- LLM 호출은 _call_qwen / _call_claude로 분리 → 테스트는 monkeypatch.
- 외부 API 키·모델명은 환경변수로 override 가능:
    ANTHROPIC_API_KEY     Claude API 키 (없으면 자동 qwen fallback)
    CODING_BOT_CLAUDE_MODEL  기본 'claude-sonnet-4-6'
    CODING_BOT_QWEN_MODEL    기본 'qwen2.5-coder:32b'

API:
  run(action, content, language=None, files=None, mode="accurate", **kwargs)
    -> tuple[str, list[dict]]
  files: [{"name": "foo.py", "text": "..."}, ...] (선택, debug/review 시 유용)
"""
from __future__ import annotations

import json
import logging
import os
from urllib.request import Request, urlopen

OLLAMA_URL = "http://127.0.0.1:11434"
QWEN_MODEL = os.getenv("CODING_BOT_QWEN_MODEL", "qwen2.5-coder:32b").strip()
# Qwen3.x 같은 추론 모델은 기본으로 thinking 토큰을 낸다. 2026-09-04 A/B에서
# qwen3.6:27b가 함수 하나에 평균 117초(답당 ~2,800토큰)를 썼다 — fast 티어가
# 아니게 된다. "off"면 /api/chat에 think=false를 보낸다. 비추론 모델(qwen2.5)에
# 이 필드를 보내면 거부될 수 있어 **설정했을 때만** 보낸다.
_THINK = os.getenv("CODING_BOT_QWEN_THINK", "").strip().lower()
QWEN_THINK = {"off": False, "on": True}.get(_THINK)   # 미설정 → None(보내지 않음)
CLAUDE_MODEL = os.getenv("CODING_BOT_CLAUDE_MODEL", "claude-sonnet-4-6").strip()
KEEP_ALIVE = "30m"

# 출력 토큰 한도 (Claude는 명시 필요, 너무 크면 비용)
CLAUDE_MAX_TOKENS = 4096
QWEN_NUM_PREDICT_FAST = 1500       # fast 모드 짧게
QWEN_NUM_PREDICT_ACCURATE = -1     # 무제한

# action ∈
VALID_ACTIONS = {"design", "code", "debug", "review"}

log = logging.getLogger("coding_bot")


# ─── 시스템 프롬프트 (action별 페르소나) ─────────────


_BASE_RULES = """공통 규칙:
- 한국어로 답변 (코드는 영어 그대로).
- 답변에 들어가는 코드는 항상 ```언어 코드블록``` 으로 감쌀 것.
- 추측 금지. 정보 부족하면 명시적으로 "추가로 알려달라"고 요청.
- 보안 위험(eval, shell injection, 비밀키 노출 등) 발견 시 반드시 경고.
"""

ACTION_SYSTEM_PROMPTS = {
    "design": (
        "당신은 시니어 소프트웨어 아키텍트입니다. 사용자의 요구사항을 받아 "
        "구현 전 단계의 설계서를 작성합니다.\n\n"
        "출력 구조:\n"
        "1. 요구사항 요약 (1-3줄)\n"
        "2. 핵심 데이터 구조 (필요한 클래스·테이블·인터페이스)\n"
        "3. 모듈 분리 (책임별 파일/함수)\n"
        "4. 핵심 알고리즘 (의사코드 또는 단계별 설명)\n"
        "5. 엣지 케이스·실패 시나리오\n"
        "6. 검증 전략 (테스트 포인트)\n"
        "\n실제 구현 코드는 최소화하고, 의사코드 또는 시그니처 수준으로.\n\n"
        + _BASE_RULES
    ),
    "code": (
        "당신은 시니어 소프트웨어 엔지니어입니다. 사용자의 명세를 받아 "
        "프로덕션 품질의 코드를 작성합니다.\n\n"
        "원칙:\n"
        "- 명확한 변수·함수명. 불필요한 약어 금지.\n"
        "- 적절한 타입 힌트, docstring (한국어), 주석은 핵심만.\n"
        "- 순수 함수 우선. 부수효과는 명시적으로 분리.\n"
        "- 에러 처리는 예외 명확히 raise (silent fail 금지).\n"
        "- 테스트 가능한 구조 (의존성 주입, 모듈 함수 분리).\n"
        "\n출력 구조:\n"
        "1. 구현 코드 (```언어 ...``` 블록)\n"
        "2. 사용 예 (1-2줄)\n"
        "3. (필요 시) 주의 사항\n\n"
        + _BASE_RULES
    ),
    "debug": (
        "당신은 시니어 디버깅 전문가입니다. 에러 메시지·실패 케이스·코드를 받아 "
        "원인을 분석하고 수정안을 제안합니다.\n\n"
        "출력 구조:\n"
        "1. 증상 요약 (사용자가 보고한 것)\n"
        "2. 가장 가능성 높은 원인 (1-2개, 근거와 함께)\n"
        "3. 그 외 가능성 (시간 순)\n"
        "4. 수정 코드 (diff 형태로 또는 전체 함수 교체)\n"
        "5. 재현·검증 절차 (어떻게 fix를 확인할지)\n"
        "\n사용자가 코드만 주고 에러 메시지를 안 줬으면 \"에러 메시지 또는 stacktrace를 알려달라\"고 요청.\n\n"
        + _BASE_RULES
    ),
    "review": (
        "당신은 시니어 코드 리뷰어입니다. 기존 코드를 받아 개선점을 우선순위와 함께 제안합니다.\n\n"
        "출력 구조:\n"
        "1. 한 줄 평가 (전체 인상)\n"
        "2. 🔴 Must Fix — 버그·보안·동시성 위험 (있으면)\n"
        "3. 🟡 Should Fix — 가독성·유지보수성·성능\n"
        "4. 🟢 Nice to Have — 개선 제안 (개인 취향에 가까움)\n"
        "5. (필요 시) 수정 예시 코드\n"
        "\n각 항목은 구체적인 라인·이유 명시. \"리팩토링하라\"는 모호한 말 금지.\n\n"
        + _BASE_RULES
    ),
}


# ─── LLM 호출 (분리) ─────────────────────────────────


def _call_qwen(messages: list[dict], num_predict: int = -1, num_ctx: int = 16384) -> str:
    """Qwen2.5-Coder via ollama. RuntimeError 던질 수 있음."""
    payload = {
        "model": QWEN_MODEL,
        "messages": messages,
        "stream": False,
        "keep_alive": KEEP_ALIVE,
        "options": {
            "temperature": 0.2,
            "num_ctx": num_ctx,
            "num_predict": num_predict,
        },
    }
    if QWEN_THINK is not None:
        payload["think"] = QWEN_THINK
    body = json.dumps(payload).encode("utf-8")
    req = Request(
        f"{OLLAMA_URL}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urlopen(req, timeout=600) as r:
        resp = json.loads(r.read().decode("utf-8"))
    return (resp.get("message", {}).get("content") or "").strip()


def _call_claude(messages: list[dict], system: str) -> str:
    """Claude API. ANTHROPIC_API_KEY 없으면 RuntimeError. RPC 실패도 RuntimeError."""
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY 미설정")
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic SDK 미설치 (pip install anthropic)")

    client = anthropic.Anthropic(api_key=key)
    # anthropic API는 system을 별도 인자로 받음
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=CLAUDE_MAX_TOKENS,
        system=system,
        messages=messages,
    )
    parts = []
    for block in resp.content:
        text = getattr(block, "text", "") or ""
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _call_llm(
    messages: list[dict], system: str, mode: str
) -> tuple[str, str]:
    """mode 따라 모델 분기. (answer, model_label) 반환.

    fast → Qwen 직행
    accurate → Claude 우선, 실패 시 (키 없음/네트워크 에러) Qwen으로 fallback
    """
    if mode == "fast":
        # Qwen은 system을 messages에 넣어야 함
        full_msgs = [{"role": "system", "content": system}] + messages
        try:
            answer = _call_qwen(full_msgs, num_predict=QWEN_NUM_PREDICT_FAST)
            return (answer, f"Qwen 로컬 ({QWEN_MODEL})")
        except Exception as e:
            log.exception("Qwen 호출 실패")
            return (f"❌ Qwen 호출 실패: {e}", "Qwen 실패")

    # accurate
    try:
        answer = _call_claude(messages, system=system)
        return (answer, f"Claude API ({CLAUDE_MODEL})")
    except RuntimeError as e:
        log.warning(f"Claude 사용 불가 → Qwen으로 fallback: {e}")
        full_msgs = [{"role": "system", "content": system}] + messages
        try:
            answer = _call_qwen(
                full_msgs, num_predict=QWEN_NUM_PREDICT_ACCURATE, num_ctx=32768
            )
            return (answer, f"Qwen 로컬 fallback ({QWEN_MODEL}) — Claude 사용 불가: {e}")
        except Exception as e2:
            log.exception("Qwen fallback도 실패")
            return (
                f"❌ Claude 실패: {e}\n❌ Qwen fallback도 실패: {e2}",
                "양쪽 실패",
            )


# ─── 사용자 메시지 구성 ──────────────────────────────


def _build_user_message(
    content: str,
    language: str | None,
    files: list[dict] | None,
) -> str:
    """action 본문 + (선택) 첨부 파일들 → 단일 user message."""
    parts = []
    if language:
        parts.append(f"언어: {language}")
    parts.append(content.strip())

    if files:
        parts.append("\n---\n\n참조 파일:")
        for f in files:
            name = (f.get("name") or "untitled").strip()
            text = (f.get("text") or "").rstrip()
            if not text:
                continue
            # 코드블록 포맷 — 언어 추정
            ext = name.split(".")[-1].lower() if "." in name else ""
            lang_hint = {
                "py": "python", "js": "javascript", "ts": "typescript",
                "go": "go", "rs": "rust", "java": "java",
                "c": "c", "cpp": "cpp", "h": "c", "sh": "bash",
                "sql": "sql", "html": "html", "css": "css",
                "yaml": "yaml", "yml": "yaml", "toml": "toml", "json": "json",
            }.get(ext, "")
            parts.append(f"\n### {name}\n```{lang_hint}\n{text}\n```")

    return "\n".join(parts)


# ─── 메인 entrypoint ─────────────────────────────────


def run(
    action: str,
    content: str,
    language: str | None = None,
    files: list[dict] | None = None,
    mode: str = "accurate",
    **kwargs,
) -> tuple[str, list[dict]]:
    """라우터 entrypoint. (answer + 모델 표기, sources=[]) 반환."""
    action = (action or "").strip().lower()
    mode = (mode or "accurate").strip().lower()
    if mode not in ("fast", "accurate"):
        mode = "accurate"

    if action not in VALID_ACTIONS:
        return (
            f"❌ 알 수 없는 action: {action!r} ({'/'.join(sorted(VALID_ACTIONS))})",
            [],
        )

    content = (content or "").strip()
    if not content:
        return ("❌ 요청 내용이 비어있습니다.", [])

    system = ACTION_SYSTEM_PROMPTS[action]
    user_msg = _build_user_message(content, language, files)
    messages = [{"role": "user", "content": user_msg}]

    answer, model_label = _call_llm(messages, system, mode)

    header = f"💻 [{action}] · {model_label}\n"
    return (header + answer, [])


# ─── CLI ─────────────────────────────────────────────


def _cli() -> None:
    import argparse, sys

    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=sorted(VALID_ACTIONS))
    ap.add_argument("content", help="요청 본문")
    ap.add_argument("--language", "-l")
    ap.add_argument("--mode", default="accurate", choices=["fast", "accurate"])
    ap.add_argument("--file", "-f", action="append",
                    help="첨부 파일 경로 (여러 번 가능)")
    args = ap.parse_args()

    files = []
    if args.file:
        from pathlib import Path
        for fp in args.file:
            p = Path(fp).expanduser()
            try:
                files.append({"name": p.name, "text": p.read_text(encoding="utf-8")})
            except Exception as e:
                print(f"⚠️ {fp} 읽기 실패: {e}", file=sys.stderr)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    msg, _ = run(
        args.action,
        args.content,
        language=args.language,
        files=files or None,
        mode=args.mode,
    )
    print(msg)


if __name__ == "__main__":
    _cli()
