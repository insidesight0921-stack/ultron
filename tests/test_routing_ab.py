"""test_routing_ab.py — 마스터 라우터 A/B 채점기.

가장 중요한 검사: **픽스처가 정규식 단락에 걸리지 않는가.** 걸리면 LLM이
아니라 규칙을 재는 것이고, 두 모델이 그 항목에서 무조건 동점이 된다.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import router  # noqa: E402
import routing_ab as rab  # noqa: E402


def test_no_fixture_utterance_is_short_circuited():
    """**LLM을 재는 것이지 규칙을 재는 게 아니다.**"""
    caught = []
    for q, _, _ in rab.FIXTURE:
        hits = {
            "watchlist": router._detect_watchlist(q),
            "action_schedule": router._detect_action_schedule(q),
            "system_info": router._detect_system_info(q),
            "news": router._detect_news(q),
            "ipo": router._detect_ipo_analyze(q),
        }
        fired = [k for k, v in hits.items() if v]
        if fired:
            caught.append((q, fired))
    assert not caught, f"정규식 단락에 걸리는 발화: {caught}"


def test_every_expected_tool_is_a_known_tool():
    for _, tool, _ in rab.FIXTURE:
        assert tool in router.KNOWN_TOOLS, tool


def test_expected_actions_are_valid_for_their_tool():
    allowed = {"schedule_bot": router.SCHEDULE_ACTIONS,
               "finance_bot": router.FINANCE_ACTIONS}
    for _, tool, action in rab.FIXTURE:
        if action and tool in allowed:
            assert action in allowed[tool], (tool, action)


def test_the_fixture_covers_the_tools_the_llm_actually_routes():
    tools = {t for _, t, _ in rab.FIXTURE}
    for must in ("knowledge_bot", "schedule_bot", "finance_bot", "invest_bot",
                 "kium_bot", "quant_bot", "coding_bot", "respond_directly"):
        assert must in tools, must
    assert len(rab.FIXTURE) >= 30


def test_judge_distinguishes_tool_only_from_full_match():
    assert rab.judge({"tool": "finance_bot", "args": {"action": "latest"}},
                     "finance_bot", "latest")["action_ok"] is True
    got = rab.judge({"tool": "finance_bot", "args": {"action": "dashboard"}},
                    "finance_bot", "latest")
    assert got["tool_ok"] is True and got["action_ok"] is False
    assert rab.judge({"tool": "knowledge_bot", "args": {}}, "finance_bot", None)["tool_ok"] is False


def test_no_expected_action_means_tool_is_enough():
    assert rab.judge({"tool": "knowledge_bot", "args": {"query": "x"}},
                     "knowledge_bot", None)["action_ok"] is True


def test_score_and_verdict():
    rows = ([{"model": "A", "tool_ok": i < 25, "action_ok": i < 22, "json_fail": i > 27,
              "wall": 2.0} for i in range(30)]
            + [{"model": "B", "tool_ok": i < 23, "action_ok": i < 20, "json_fail": False,
                "wall": 1.0} for i in range(30)])
    s = rab.score(rows)
    assert s["A"]["tool"] == 25 and s["A"]["json_fail"] == 2 and s["B"]["tool"] == 23
    assert "구분되지 않는다" in rab.verdict(s)


def test_a_big_gap_is_named():
    rows = ([{"model": "A", "tool_ok": True, "action_ok": True, "json_fail": False, "wall": 1}] * 30
            + [{"model": "B", "tool_ok": i < 20, "action_ok": i < 20, "json_fail": False,
                "wall": 1} for i in range(30)])
    assert "A가 10건 더 맞힘" in rab.verdict(rab.score(rows))


def test_route_accepts_think_and_omits_it_by_default(monkeypatch):
    """`think`는 지정했을 때만 보낸다 — 비추론 모델은 이 필드를 거부할 수 있다."""
    seen = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return b'{"message":{"content":"{\\"tool\\":\\"respond_directly\\",\\"args\\":{\\"answer\\":\\"hi\\"}}"}}'

    def fake_urlopen(req, timeout=0):
        import json
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp()
    monkeypatch.setattr(router, "urlopen", fake_urlopen)
    router.route("고마워", model="m")
    assert "think" not in seen["body"]
    router.route("고마워", model="m", think=False)
    assert seen["body"]["think"] is False


def test_the_report_says_what_it_measures():
    rows = [{"model": "A", "query": "q", "expected_tool": "kium_bot", "expected_action": "scan",
             "tool_ok": True, "action_ok": True, "json_fail": False, "wall": 1.0,
             "got_tool": "kium_bot", "got_action": "scan"}]
    msg = rab.format_report(rows, rab.score(rows))
    assert "규칙을 재는 게 아니다" in msg
