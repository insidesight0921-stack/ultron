"""action_scheduler 단위 테스트 — CRUD·영속·is_due 멱등 (tmp store)."""
from __future__ import annotations
from datetime import datetime
import pytest

import action_scheduler as sch


@pytest.fixture
def store(tmp_path):
    return tmp_path / "sched.json"


# ─── CRUD ───────────────────────────────────────────


def test_add_and_list(store):
    s = sch.add_schedule("news", "daily", "08:00", until="2026-06-11", path=store)
    assert s.id == 1 and s.action == "news"
    items = sch.load_schedules(store)
    assert len(items) == 1 and items[0].time == "08:00"


def test_add_increments_id(store):
    sch.add_schedule("news", "daily", "08:00", path=store)
    s2 = sch.add_schedule("signal", "daily", "15:40", path=store)
    assert s2.id == 2


def test_add_invalid_action(store):
    with pytest.raises(ValueError):
        sch.add_schedule("없는것", "daily", "08:00", path=store)


def test_delete(store):
    sch.add_schedule("news", "daily", "08:00", path=store)
    assert sch.delete_schedule(1, path=store) is True
    assert sch.load_schedules(store) == []


def test_delete_missing(store):
    assert sch.delete_schedule(99, path=store) is False


def test_set_enabled(store):
    sch.add_schedule("news", "daily", "08:00", path=store)
    assert sch.set_enabled(1, False, path=store) is True
    assert sch.load_schedules(store)[0].enabled is False


def test_format_list_empty(store):
    assert "없습니다" in sch.format_list(store)


def test_describe_weekly(store):
    s = sch.add_schedule("ipo", "weekly", "09:10", weekday=0, path=store)
    assert "매주 월 09:10" in s.describe()


def test_describe_daily_until(store):
    s = sch.add_schedule("news", "daily", "08:00", until="2026-06-11", path=store)
    d = s.describe()
    assert "매일 08:00" in d and "~2026-06-11" in d


# ─── is_due ─────────────────────────────────────────


def _sched(**kw):
    base = dict(id=1, action="news", freq="daily", time="08:00")
    base.update(kw)
    return sch.ActionSchedule(**base)


def test_is_due_daily_match():
    now = datetime(2026, 6, 9, 8, 0)
    assert sch.is_due(_sched(), now) is True


def test_is_due_wrong_time():
    now = datetime(2026, 6, 9, 8, 1)
    assert sch.is_due(_sched(), now) is False


def test_is_due_disabled():
    now = datetime(2026, 6, 9, 8, 0)
    assert sch.is_due(_sched(enabled=False), now) is False


def test_is_due_past_until():
    now = datetime(2026, 6, 12, 8, 0)
    assert sch.is_due(_sched(until="2026-06-11"), now) is False


def test_is_due_until_inclusive():
    now = datetime(2026, 6, 11, 8, 0)
    assert sch.is_due(_sched(until="2026-06-11"), now) is True


def test_is_due_already_fired_today():
    now = datetime(2026, 6, 9, 8, 0)
    assert sch.is_due(_sched(last_fired="2026-06-09"), now) is False


def test_is_due_weekly_weekday_match():
    # 2026-06-08은 월요일(weekday 0)
    now = datetime(2026, 6, 8, 9, 10)
    assert sch.is_due(_sched(freq="weekly", time="09:10", weekday=0), now) is True


def test_is_due_weekly_wrong_weekday():
    now = datetime(2026, 6, 9, 9, 10)  # 화요일
    assert sch.is_due(_sched(freq="weekly", time="09:10", weekday=0), now) is False


# ─── due_now / mark_fired 통합 ──────────────────────


def test_due_now_and_mark_fired(store):
    sch.add_schedule("news", "daily", "08:00", path=store)
    now = datetime(2026, 6, 9, 8, 0)
    due = sch.due_now(now, path=store)
    assert len(due) == 1
    sch.mark_fired(due[0].id, now, path=store)
    # 같은 날 재검사 → 멱등으로 0건
    assert sch.due_now(now, path=store) == []


# ─── 월간 리밸런싱 catch-up (콴텍 정상화, v3.46) ──────
from datetime import datetime as _dt
import action_scheduler as _a


def test_monthly_due_skips_when_already_pushed():
    assert _a.monthly_rebalance_due(_dt(2026, 7, 1, 10, 0), True, True, False) is False


def test_monthly_due_first_biz_before_time():
    assert _a.monthly_rebalance_due(_dt(2026, 7, 1, 9, 0), False, True, False) is False


def test_monthly_due_first_biz_at_time():
    assert _a.monthly_rebalance_due(_dt(2026, 7, 1, 9, 30), False, True, False) is True


def test_monthly_due_catch_up_after_first_biz():
    # 첫 영업일 당일이 아니어도 이미 지났으면 봇 켜진 즉시 발화(catch-up)
    assert _a.monthly_rebalance_due(_dt(2026, 7, 6, 7, 0), False, False, True) is True


def test_monthly_due_before_first_biz():
    assert _a.monthly_rebalance_due(_dt(2026, 7, 1, 8, 0), False, False, False) is False
