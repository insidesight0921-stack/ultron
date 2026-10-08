"""test_ops_probe.py — 운영 감시(순수 판정·전이·기록) 검증. 네트워크·lsof·launchctl 없음."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import ops_probe as op

NOW = datetime(2026, 10, 8, 11, 0, 0)          # 목요일


# ─── 신선도 판정 ─────────────────────────────────────


def test_weekdays_between_skips_weekends_and_rejects_garbage():
    assert op.weekdays_between("20261007", "20261008") == 1
    assert op.weekdays_between("2026-10-02", "20261008") == 4      # 금→목: 월화수목
    assert op.weekdays_between("20261008", "20261008") == 0
    assert op.weekdays_between("nope", "20261008") == -1


def test_the_frozen_kospi_cache_is_caught():
    """2026-09-17에 멈춘 캐시 — 3주면 15평일, 문턱 3 이상이라 실패."""
    item = op.FRESHNESS[0]
    got = op.judge_freshness(item, "20260917", NOW)
    assert got["ok"] is False and got["lag"] == 15 and "≥ 3" in got["detail"]


def test_a_one_day_lag_is_fine():
    """음성 대조: 어제까지 있으면(오늘 16:20 전) 정상."""
    got = op.judge_freshness(op.FRESHNESS[0], "20261007", NOW)
    assert got["ok"] is True and got["lag"] == 1


def test_a_long_weekend_does_not_trip_the_default_threshold():
    """음성 대조: 추석 연휴(목·금 휴장) 뒤 월요일 아침 — 마지막 수요일, 평일 3개 = 문턱 3 → 실패.

    이게 바로 문턱의 경계다. 연휴 뒤 첫 아침은 울릴 수 있고, 그날 16:20 수집이
    돌면 꺼진다. 「2평일 지연은 항상 정상」만 고정한다.
    """
    monday = datetime(2026, 9, 28, 9, 0)
    assert op.judge_freshness(op.FRESHNESS[0], "20260925", monday)["ok"] is True   # 금 → 월: 0
    assert op.judge_freshness(op.FRESHNESS[0], "20260924", monday)["ok"] is True   # 목 → 월: 1


def test_missing_record_is_a_failure_not_a_pass():
    got = op.judge_freshness(op.FRESHNESS[0], None, NOW)
    assert got["ok"] is False and got["detail"] == "기록 없음"


def test_hour_based_freshness_for_the_bot_log():
    item = next(i for i in op.FRESHNESS if i["kind"] == "mtime_h")
    assert op.judge_freshness(item, "2026-10-08T10:30:00", NOW)["ok"] is True
    got = op.judge_freshness(item, "2026-10-08T03:00:00", NOW)
    assert got["ok"] is False and got["lag"] == 8.0


# ─── 포트·launchd 파서와 판정 ────────────────────────


LSOF = """COMMAND   PID        USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME
java    59871 byunhyunjun   63u  IPv6 0xb9b66c0db96df612      0t0  TCP *:http-alt (LISTEN)
Python  89961 byunhyunjun    3u  IPv4 0xa915cc1a6f4f1a06      0t0  TCP *:http-alt (LISTEN)
Python  89979 byunhyunjun    3u  IPv4 0x1bfe0d60eeca2924      0t0  TCP localhost:http-alt (LISTEN)
"""


def test_the_three_listener_overlap_of_0909_is_flagged():
    rows = op.parse_lsof(LSOF)
    assert [r["pid"] for r in rows] == ["59871", "89961", "89979"]
    got = op.judge_port(op.SERVICES[0], rows)
    assert got["ok"] is False and "3개 겹침" in got["detail"] and "java(59871)" in got["detail"]


def test_one_listener_is_fine_and_none_is_dead():
    one = op.parse_lsof("\n".join(LSOF.splitlines()[:1] + LSOF.splitlines()[3:]))
    assert op.judge_port(op.SERVICES[0], one)["ok"] is True
    assert op.judge_port(op.SERVICES[0], [])["detail"] == "듣는 프로세스 없음"


LAUNCHCTL = """PID\tStatus\tLabel
-\t0\tcom.hyunjun.ai-agent.signal-review
-\t1\tcom.hyunjun.ai-agent.weekly-kium-scan
89979\t0\tcom.hyunjun.ai-agent.paper
-\t0\tcom.apple.something
"""


def test_launchctl_rows_are_filtered_to_the_project_and_nonzero_exit_fails():
    rows = op.parse_launchctl(LAUNCHCTL)
    assert [r["label"] for r in rows] == ["signal-review", "weekly-kium-scan", "paper"]
    verdicts = {v["name"]: v for v in op.judge_launchd(rows)}
    assert verdicts["launchd weekly-kium-scan"]["ok"] is False
    assert "종료 코드 1" in verdicts["launchd weekly-kium-scan"]["detail"]
    assert verdicts["launchd signal-review"]["ok"] is True
    assert verdicts["launchd paper"]["detail"] == "PID 89979"


# ─── 전이: 바뀔 때만 말한다 ──────────────────────────


def _rec(*failing, at="2026-10-08T11:00:00"):
    names = ["A", "B", "C"]
    return {"at": at, "n": 3, "failing": sorted(failing),
            "checks": [{"name": n, "ok": n not in failing, "detail": f"{n} 상세"} for n in names]}


def test_first_run_with_failures_alerts_and_a_clean_first_run_does_not():
    assert op.transition(None, _rec("A"))["changed"] is True
    assert op.transition(None, _rec())["changed"] is False


def test_the_same_failure_twice_is_silent():
    """음성 대조: 15분마다 같은 말을 하지 않는다."""
    tr = op.transition(_rec("A"), _rec("A"))
    assert tr == {"broke": [], "fixed": [], "changed": False}


def test_a_recovery_is_reported_once():
    tr = op.transition(_rec("A"), _rec())
    assert tr["fixed"] == ["A"] and tr["changed"] is True
    assert op.transition(_rec(), _rec())["changed"] is False


def test_the_alert_names_what_broke_what_recovered_and_what_remains():
    text = op.format_alert(op.transition(_rec("A", "B"), _rec("B", "C")), _rec("B", "C"))
    assert "새로 깨짐" in text and "C: C 상세" in text
    assert "다시 정상" in text and "· A" in text
    assert "계속 깨져 있음 1건: B" in text
    assert "고치지 않습니다" in text


# ─── 파일에서 '마지막 기록' 읽기 ─────────────────────


def _roots(tmp_path: Path) -> dict:
    r = {"shareable": tmp_path / "s", "state": tmp_path / "st", "logs": tmp_path / "l"}
    for p in r.values():
        p.mkdir()
    return r


def test_last_of_reads_series_jsonl_and_mtime(tmp_path):
    roots = _roots(tmp_path)
    d = roots["shareable"] / "cache" / "indices"
    d.mkdir(parents=True)
    (d / "market_index_KOSPI_20261008.json").write_text(json.dumps(
        {"series": {"date": ["20260916", "20260917"], "close": [1, 2]}}), encoding="utf-8")
    assert op.last_of(op.FRESHNESS[0], roots) == "20260917"
    (roots["state"] / "proxy_indicators.jsonl").write_text(
        '{"day":"20261006"}\n{"day":"20261007","x":1}\n', encoding="utf-8")
    item = next(i for i in op.FRESHNESS if i["name"] == "대리 지표 기록")
    assert op.last_of(item, roots) == "20261007"
    (roots["logs"] / "market_data_collector.out.log").write_text("x", encoding="utf-8")
    item = next(i for i in op.FRESHNESS if i["name"] == "수집기 로그")
    assert op.last_of(item, roots) == datetime.now().strftime("%Y%m%d")


def test_last_of_is_none_for_missing_or_broken_files(tmp_path):
    roots = _roots(tmp_path)
    assert op.last_of(op.FRESHNESS[0], roots) is None
    (roots["state"] / "proxy_indicators.jsonl").write_text("not json\n", encoding="utf-8")
    item = next(i for i in op.FRESHNESS if i["name"] == "대리 지표 기록")
    assert op.last_of(item, roots) is None


# ─── 한 바퀴: 재고·기록·알림 ─────────────────────────


def _fake_probe_kw(roots, listeners_by_port=None):
    return dict(
        now=NOW, roots=roots,
        health=lambda svc: {"name": f"{svc['name']} health", "ok": True, "detail": "HTTP 200"},
        listeners=lambda port: (listeners_by_port or {}).get(port, [{"command": "Python", "pid": "1", "name": "x"}]),
        launchctl=lambda: "PID\tStatus\tLabel\n-\t0\tcom.hyunjun.ai-agent.paper\n",
    )


def test_run_once_records_and_alerts_only_on_change(tmp_path):
    roots = _roots(tmp_path)
    sent = []
    cur, tr = op.run_once(state_dir=roots["state"], sender=lambda t: sent.append(t) or 1,
                          **_fake_probe_kw(roots))
    # 신선도 항목은 파일이 없으니 전부 "기록 없음" — 첫 실행에 깨진 것이 있어 알린다
    assert tr["changed"] is True and len(sent) == 1 and "기록 없음" in sent[0]
    assert op.ledger_path(roots["state"]).exists() and op.latest_path(roots["state"]).exists()
    # 같은 상태로 한 번 더 — 조용하다
    cur2, tr2 = op.run_once(state_dir=roots["state"], sender=lambda t: sent.append(t) or 1,
                            **_fake_probe_kw(roots))
    assert tr2["changed"] is False and len(sent) == 1
    rows = op.ledger_path(roots["state"]).read_text(encoding="utf-8").splitlines()
    assert len(rows) == 2 and json.loads(rows[-1])["failing"] == cur2["failing"]


def test_the_ledger_row_is_compact_and_the_latest_file_is_full(tmp_path):
    roots = _roots(tmp_path)
    cur, _ = op.run_once(state_dir=roots["state"], sender=lambda t: 1, alert=False,
                         **_fake_probe_kw(roots))
    row = json.loads(op.ledger_path(roots["state"]).read_text(encoding="utf-8").splitlines()[0])
    assert set(row) == {"at", "n", "failing", "checks"} and "detail" not in row["checks"][0]
    latest = json.loads(op.latest_path(roots["state"]).read_text(encoding="utf-8"))
    assert latest["checks"][0]["detail"]


def test_no_lsof_means_the_port_check_is_skipped_not_failed(tmp_path):
    roots = _roots(tmp_path)
    kw = _fake_probe_kw(roots)
    kw["listeners"] = lambda port: None
    cur = op.probe(**kw)
    assert not any("포트" in c["name"] for c in cur["checks"])


def test_probe_does_not_write_anything(tmp_path):
    roots = _roots(tmp_path)
    op.probe(**_fake_probe_kw(roots))
    assert list(roots["state"].iterdir()) == []
