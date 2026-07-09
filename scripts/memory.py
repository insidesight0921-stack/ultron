#!/usr/bin/env python3
"""
chat_id 단위 멀티턴 메모리 (in-memory).

각 chat_id마다 deque(maxlen=N) 유지. 봇 재시작 시 휘발.
영속화는 다음 세션에 SQLite로 추가 가능.

사용:
    from memory import ChatMemory
    mem = ChatMemory(max_turns=12)
    mem.add("12345", "user", "내 매매 규칙은?")
    mem.add("12345", "assistant", "...")
    history = mem.history("12345", n=6)  # [{role, content}, ...]
    mem.clear("12345")
"""
from __future__ import annotations
from collections import defaultdict, deque
from threading import Lock

DEFAULT_MAX_TURNS = 12  # 사용자+봇 합쳐서 12턴 = 6왕복


class ChatMemory:
    """thread-safe in-memory chat 히스토리."""

    def __init__(self, max_turns: int = DEFAULT_MAX_TURNS) -> None:
        # deque의 maxlen 자동 처리로 오래된 턴 자동 폐기
        self._store: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=max_turns)
        )
        self._lock = Lock()
        self.max_turns = max_turns

    def add(self, chat_id: str, role: str, content: str) -> None:
        """role: 'user' | 'assistant'"""
        if not content:
            return
        with self._lock:
            self._store[chat_id].append({"role": role, "content": content})

    def history(self, chat_id: str, n: int | None = None) -> list[dict]:
        """직전 N턴 (n=None이면 전체)."""
        with self._lock:
            turns = list(self._store[chat_id])
        if n is not None and n < len(turns):
            turns = turns[-n:]
        return turns

    def clear(self, chat_id: str) -> int:
        """해당 chat_id 메모리 초기화. 지워진 턴 수 반환."""
        with self._lock:
            old = self._store.pop(chat_id, None)
            return len(old) if old else 0

    def stats(self) -> dict:
        """전체 chat_id별 턴 수 (디버깅용)."""
        with self._lock:
            return {cid: len(turns) for cid, turns in self._store.items()}
