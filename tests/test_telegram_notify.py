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


# ─── .env 로딩 (2026-08-28) ──────────────────────────
#
# launchd가 띄운 스크립트는 봇 프로세스의 환경을 물려받지 못한다.
# 첫 실행에서 "환경변수 없음 — 발송 건너뜀"으로 조용히 아무것도 안 갔다.
# 파싱·파일 처리 자체는 env_config로 옮겼다(tests/test_env_config.py).
# 여기서는 **텔레그램 키가 실제로 채워지는지**만 본다.


def test_ensure_env_fills_missing_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ALLOWED_TELEGRAM_USER_ID", raising=False)
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=abc\nALLOWED_TELEGRAM_USER_ID=7\n", encoding="utf-8")
    tn.ensure_env(env)
    assert tn.os.environ["TELEGRAM_BOT_TOKEN"] == "abc"


def test_ensure_env_never_overrides_an_existing_value(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "이미있음")
    monkeypatch.delenv("ALLOWED_TELEGRAM_USER_ID", raising=False)
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=파일값\nALLOWED_TELEGRAM_USER_ID=7\n", encoding="utf-8")
    tn.ensure_env(env)
    assert tn.os.environ["TELEGRAM_BOT_TOKEN"] == "이미있음"
    assert tn.os.environ["ALLOWED_TELEGRAM_USER_ID"] == "7"


def test_ensure_env_missing_file_is_quiet(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    tn.ensure_env(tmp_path / "없음.env")          # 예외 없이 지나가야 한다


def test_send_picks_up_credentials_from_the_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ALLOWED_TELEGRAM_USER_ID", raising=False)
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=T\nALLOWED_TELEGRAM_USER_ID=1,2\n", encoding="utf-8")
    seen = []
    n = tn.send("안녕", env_path=env,
                poster=lambda t, c, x, p: seen.append((t, c)) or True)
    assert n == 2 and seen == [("T", "1"), ("T", "2")]


def test_explicit_arguments_skip_the_env_file(tmp_path):
    """호출부가 값을 넘겼으면 파일을 읽을 이유가 없다."""
    env = tmp_path / ".env"          # 존재하지 않는다
    assert tn.send("x", token="T", chat_ids=["9"], env_path=env,
                   poster=lambda *a: True) == 1
