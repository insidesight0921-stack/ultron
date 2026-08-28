"""test_telegram_notify.py — 텔레그램 발송(순수 + 주입) 검증 (hermetic, 네트워크 없음)."""
from __future__ import annotations

import telegram_notify as tn


# ─── 분할 ────────────────────────────────────────────


def test_short_text_is_one_chunk():
    assert tn.split_chunks("안녕") == ["안녕"]


def test_empty_text_is_no_chunk():
    assert tn.split_chunks("") == [] and tn.split_chunks(None or "") == []


def test_every_chunk_is_within_the_limit():
    """상한을 넘기면 텔레그램이 메시지 전체를 거절한다 — 리포트가 통째로 사라진다."""
    text = "\n".join(f"줄 {i}" * 20 for i in range(400))
    for chunk in tn.split_chunks(text, limit=200):
        assert len(chunk) <= 200


def test_split_prefers_line_boundaries():
    """글자 수로만 자르면 표가 중간에서 끊겨 읽을 수 없다."""
    text = "\n".join(["a" * 40] * 10)
    chunks = tn.split_chunks(text, limit=100)
    assert all(not c.startswith("\n") for c in chunks)
    for c in chunks:
        assert all(len(line) == 40 for line in c.split("\n"))


def test_a_single_overlong_line_is_cut_by_length():
    chunks = tn.split_chunks("x" * 250, limit=100)
    assert [len(c) for c in chunks] == [100, 100, 50]


def test_nothing_is_lost_in_splitting():
    text = "\n".join(f"행{i}" for i in range(200))
    assert "\n".join(tn.split_chunks(text, limit=50)) == text


def test_chat_ids_parsing():
    assert tn.chat_ids_from(" 1, 2 ,,3 ") == ["1", "2", "3"]
    assert tn.chat_ids_from(None) == [] and tn.chat_ids_from("") == []


# ─── 발송 ────────────────────────────────────────────


def test_send_posts_each_chunk_to_each_chat():
    calls = []
    n = tn.send("가" * 250, token="T", chat_ids=["1", "2"],
                poster=lambda t, c, x, p: calls.append((c, len(x))) or True)
    assert n == len(calls) and {c for c, _ in calls} == {"1", "2"}


def test_send_without_credentials_is_a_quiet_zero():
    """토큰 없는 환경에서 리포트 생성이 실패하면 안 된다."""
    assert tn.send("x", token="", chat_ids=["1"]) == 0
    assert tn.send("x", token="T", chat_ids=[]) == 0


def test_send_counts_only_successes():
    n = tn.send("짧음", token="T", chat_ids=["1", "2"],
                poster=lambda t, c, x, p: c == "1")
    assert n == 1


def test_send_defaults_to_plain_text():
    """성과 리포트에는 -, _, *가 흔하다. Markdown으로 보내면 거절당한다."""
    seen = []
    tn.send("a_b*c", token="T", chat_ids=["1"],
            poster=lambda t, c, x, p: seen.append(p) or True)
    assert seen == [None]


def test_send_passes_parse_mode_when_asked():
    seen = []
    tn.send("x", token="T", chat_ids=["1"], parse_mode="HTML",
            poster=lambda t, c, x, p: seen.append(p) or True)
    assert seen == ["HTML"]


def test_send_reads_environment_when_not_given(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_ID", "10,20")
    seen = []
    n = tn.send("x", poster=lambda t, c, x_, p: seen.append((t, c)) or True)
    assert n == 2 and seen == [("T", "10"), ("T", "20")]


def test_send_empty_text_sends_nothing():
    calls = []
    assert tn.send("", token="T", chat_ids=["1"],
                   poster=lambda *a: calls.append(a) or True) == 0
    assert calls == []
