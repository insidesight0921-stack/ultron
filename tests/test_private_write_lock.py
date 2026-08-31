"""test_private_write_lock.py — 기동 게이트가 막는 것은 쓰기여야 한다 (소스 배선 검증).

2026-08-28: 번들 로딩이 실패하면 텔레그램 봇 프로세스가 통째로 죽었다. 장 중
손절·익절 모니터가 그 프로세스 안에서 돌기 때문에, 데이터를 지키려는 가드가
**손절 감시를 꺼서 더 큰 위험을 만들고** 있었다.

봇을 띄우는 것만으로는 부족하다. executor가 없을 때의 폴백이 원본 DB에 직접
쓰기 때문에, 그대로 두면 번들이 끄겠다고 선언한 통로가 오히려 열린다.
여기서는 그 두 가지가 코드에 실제로 배선돼 있는지 본다.
"""
from __future__ import annotations

from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SRC = (SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8")


def test_startup_does_not_die_on_a_gate_failure():
    assert "_start_private_write_runtime()" in SRC
    body = SRC[SRC.index("def _start_private_write_runtime"):]
    body = body[:body.index("\ndef ", 10)]
    assert "_PRIVATE_WRITE_LOCKED = True" in body
    assert "_configure_private_write_runtime(None)" in body


def test_only_gate_errors_are_caught():
    """넓게 잡으면 코딩 오류까지 '게이트 때문'으로 보인다 — .env에서 한 번 겪었다."""
    body = SRC[SRC.index("def _start_private_write_runtime"):]
    body = body[:body.index("\ndef ", 10)]
    assert "except (PrivateWriteRuntimeError, PrivateWriteCutoverError," in body
    assert "except Exception:" not in body.split("telegram_notify.send")[0]


def test_every_direct_write_goes_through_the_lock():
    """폴백이 _pdb를 직접 부르면 잠금이 무의미해진다."""
    for line_no, line in enumerate(SRC.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("_pdb.record_buy(") or stripped.startswith("_pdb.record_sell("):
            raise AssertionError(f"{line_no}행: 직접 쓰기가 잠금을 우회한다 — {stripped}")


def test_the_lock_refuses_instead_of_writing():
    body = SRC[SRC.index("def _direct_paper_write"):]
    body = body[:body.index("\ndef ", 10)]
    assert "if _PRIVATE_WRITE_LOCKED:" in body
    assert "raise PrivateWriteLocked" in body


def test_the_lock_is_off_by_default():
    """Private write를 도입하지 않은 환경에서는 폴백이 그대로 동작해야 한다."""
    assert "_PRIVATE_WRITE_LOCKED = False" in SRC


def test_the_user_is_told_the_bot_is_still_watching_stops():
    """'쓰기만 막혔다'를 안 알리면 봇이 죽은 줄 알고 손을 놓는다."""
    body = SRC[SRC.index("def _start_private_write_runtime"):]
    body = body[:body.index("\ndef ", 10)]
    assert "손절 모니터는 그대로" in body
    # 2026-08-31: 복구 안내를 재발급 도구에서 **백업 생성**으로 바꿨다.
    # 신선도를 고정 핀이 아니라 백업 루트 전체에서 재므로, 백업만 새로 뜨면
    # 번들 재발급 없이 복구된다.
    assert "private_data_security.py all" in body


# ─── 청산은 막지 않는다 (2026-08-31 정정) ────────────
#
# 처음엔 매수·청산을 가리지 않고 전부 막았다. 실제로 이런 로그가 남았다.
#   09:08:50 익절 기록 실패 010120: Private write 잠금 — 직접 쓰기 거부
#
# 이 프로젝트의 기존 원칙은 매수 fail-closed / 청산 fail-open이다. 잠금의
# 목적은 "복구 가능한 백업 없이 **새 포지션을 만들지 않는 것**"이지, 이미 가진
# 포지션을 정리하지 못하게 하는 것이 아니다.


def test_the_lock_only_names_new_buys():
    body = SRC[SRC.index("def _direct_paper_write"):]
    body = body[:body.index("\ndef ", 10)]
    assert "신규 매수 거부" in body


def test_an_exit_write_is_allowed_while_locked():
    body = SRC[SRC.index("def _direct_paper_write"):]
    body = body[:body.index("\ndef ", 10)]
    assert "is_exit" in body
    # 잠금 + 청산이면 raise 하지 않고 기록한다
    assert "if not is_exit:" in body


def test_record_sell_is_classified_as_an_exit():
    assert '_EXIT_WRITERS = ("record_sell",)' in SRC


def test_the_alert_says_exits_still_work():
    """'쓰기가 막혔다'로만 알리면 손절도 멈춘 줄 알고 손을 놓는다."""
    body = SRC[SRC.index("def _start_private_write_runtime"):]
    body = body[:body.index("\ndef ", 10)]
    assert "청산(손절·익절)은 계속 기록됩니다" in body


def test_the_backup_runs_more_often_than_the_freshness_limit():
    """백업 주기가 신선도 한도보다 길면 **반드시** 잠긴다.

    실제로 그랬다 — 주 1회 백업 + 24시간 한도 → 일요일 백업이 월요일에 만료.
    """
    sh = (Path(__file__).resolve().parents[1] / "scripts"
          / "agent_services.sh").read_text(encoding="utf-8")
    i = sh.index("write_plist_private_backup()")
    block = sh[i:sh.index("\nEOF", i)]
    assert "Weekday" not in block          # 요일 고정이면 주 1회다
    assert block.count("<key>Hour</key>") >= 2
