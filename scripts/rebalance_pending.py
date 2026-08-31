"""rebalance_pending.py — 월간 리밸런싱 승인 대기의 영속화·재알림 (순수 코어 + 얇은 I/O)

**왜 필요한가.** 2026-07 콴텍 리밸런싱이 통째로 건너뛰어졌다. 로그로 확인한 사실:

    2026-06-18 21:05:37  푸시 → 21:05:50 실행 완료(8건)
    2026-07-05 21:05:29  푸시 → **실행 로그 없음. 거부 로그도 없음**
    2026-08-03 12:55:41  푸시 → 15:27:08 실행 완료(8건)

7월은 추천만 나가고 아무 일도 일어나지 않았다. 그런데 플래그에는 `"2026-07"`이
찍혀 있어 **다시 알리지 않았다.** 한 달치 진입이 사라졌고, 그동안 자동청산은
계속 돌아 콴텍 슬롯은 현금만 남았다(2026-08-31 실측 투입률 11.5%).

원인 두 가지.

1. **플래그가 "푸시했다"만 기록한다.** "실행했다"를 구분하지 않으니, 사용자가
   버튼을 누르지 않은 달과 정상 처리된 달이 같아 보인다.
2. **승인 대기 목록이 메모리에만 있었다**(`_PENDING_REBALANCE`). 봇이 재시작되면
   추천이 사라지고, 나중에 버튼을 눌러도 "저장된 추천 종목이 없습니다"가 뜬다.
   7월 5일 푸시 뒤 다음 재시작은 7월 8일이었다.

그래서 **승인 대기를 디스크에 두고, 실행 여부를 따로 기록하고, 실행되지 않은
채로 며칠이 지나면 다시 알린다.** 다만 매일 조르지는 않는다(간격·횟수 제한).
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger("rebalance_pending")

# 실행되지 않은 채 이만큼 지나면 다시 알린다.
RENOTIFY_AFTER_DAYS = 3
# 한 달에 재알림은 이 횟수까지. 더 조르면 알림이 소음이 되고, 소음이 되면 안 본다.
MAX_RENOTIFY = 3


# ─── 플래그 (순수) ───────────────────────────────────


def normalize_flag(raw: dict) -> dict:
    """옛 형식 `{"YYYY-MM": [uid, ...]}` → 새 형식으로 올린다.

    옛 기록을 버리지 않는다. 다만 **옛 항목은 "푸시됨"까지만 아는 것**이므로
    실행 여부는 비워 둔다 — 모르는 것을 안다고 적지 않는다.
    """
    out: dict = {}
    for month, value in (raw or {}).items():
        if isinstance(value, list):
            out[month] = {"pushed": [int(u) for u in value],
                          "executed": [], "pushes": {}}
        elif isinstance(value, dict):
            out[month] = {
                "pushed": [int(u) for u in value.get("pushed", [])],
                "executed": [int(u) for u in value.get("executed", [])],
                "pushes": {str(k): v for k, v in (value.get("pushes") or {}).items()},
            }
    return out


def _month_entry(flag: dict, month_key: str) -> dict:
    return flag.setdefault(month_key,
                           {"pushed": [], "executed": [], "pushes": {}})


def mark_pushed(flag: dict, month_key: str, uid: int, when: date) -> dict:
    """푸시 기록(순수). 같은 달에 여러 번 보낸 것도 센다."""
    entry = _month_entry(flag, month_key)
    if int(uid) not in entry["pushed"]:
        entry["pushed"].append(int(uid))
    hist = entry["pushes"].setdefault(str(uid), [])
    hist.append(when.isoformat())
    return flag


def mark_executed(flag: dict, month_key: str, uid: int) -> dict:
    """실행(또는 사용자가 명시적으로 건너뜀) 기록(순수).

    건너뜀도 실행으로 센다 — **사람이 판단을 내렸다는 사실이 중요하다.**
    다시 조를 이유가 없다.
    """
    entry = _month_entry(flag, month_key)
    if int(uid) not in entry["executed"]:
        entry["executed"].append(int(uid))
    return flag


def push_count(flag: dict, month_key: str, uid: int) -> int:
    return len((flag.get(month_key, {}).get("pushes", {}) or {}).get(str(uid), []))


def last_push(flag: dict, month_key: str, uid: int) -> Optional[date]:
    hist = (flag.get(month_key, {}).get("pushes", {}) or {}).get(str(uid), [])
    for raw in reversed(hist):
        try:
            return date.fromisoformat(str(raw)[:10])
        except ValueError:
            continue
    return None


def needs_push(flag: dict, month_key: str, uid: int, today: date,
               *, renotify_after_days: int = RENOTIFY_AFTER_DAYS,
               max_renotify: int = MAX_RENOTIFY) -> bool:
    """이 사용자에게 (다시) 알려야 하는가(순수).

    - 실행(또는 건너뜀)했으면 끝. 다시 알리지 않는다.
    - 아직 한 번도 안 보냈으면 보낸다.
    - 보냈는데 실행되지 않았고 `renotify_after_days`가 지났으면 **다시 보낸다.**
      이것이 없어서 2026-07이 통째로 사라졌다.
    - 다만 `max_renotify`까지만. 더 조르면 소음이 되고, 소음은 안 보게 된다.
    """
    entry = flag.get(month_key) or {}
    if int(uid) in [int(u) for u in entry.get("executed", [])]:
        return False
    sent = push_count(flag, month_key, uid)
    if sent == 0:
        return True
    if sent > max_renotify:
        return False
    last = last_push(flag, month_key, uid)
    if last is None:
        return True
    return (today - last).days >= renotify_after_days


def unexecuted_months(flag: dict, uid: int) -> list[str]:
    """푸시만 되고 실행되지 않은 달 목록(순수). 화면·로그에 쓴다."""
    out = []
    for month, entry in sorted((flag or {}).items()):
        pushed = [int(u) for u in entry.get("pushed", [])]
        done = [int(u) for u in entry.get("executed", [])]
        if int(uid) in pushed and int(uid) not in done:
            out.append(month)
    return out


# ─── 승인 대기 목록 (I/O) ────────────────────────────


def _rec_to_dict(rec) -> dict:
    """추천 1건 → 저장 가능한 dict. 객체/딕셔너리 모두 받는다."""
    if isinstance(rec, dict):
        return dict(rec)
    keys = ("ticker", "name", "composite_score", "raw_factors",
            "z_factors", "current_price")
    return {k: getattr(rec, k, None) for k in keys if hasattr(rec, k)}


def save_pending(path: Path, uid: int, month_key: str, recs: list,
                 when: Optional[datetime] = None) -> bool:
    """승인 대기 추천을 디스크에 저장한다.

    **메모리에만 두면 재시작으로 사라진다.** 사용자는 버튼을 눌렀는데
    "저장된 추천 종목이 없습니다"를 보게 되고, 그 달은 그대로 넘어간다.
    """
    payload = {"uid": int(uid), "month": month_key,
               "saved_at": (when or datetime.now()).isoformat(timespec="seconds"),
               "recs": [_rec_to_dict(r) for r in recs]}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f".tmp_{path.name}"
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError as e:
        log.warning("승인 대기 저장 실패: %s", e)
        return False


def load_pending(path: Path, uid: int, month_key: str) -> list[dict]:
    """저장된 승인 대기 추천. 사용자·달이 다르면 빈 목록(남의 달을 실행하지 않는다)."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.debug("승인 대기 로드 실패: %s", e)
        return []
    if int(payload.get("uid", -1)) != int(uid):
        return []
    if str(payload.get("month")) != str(month_key):
        return []
    recs = payload.get("recs")
    return [r for r in recs if isinstance(r, dict)] if isinstance(recs, list) else []


def clear_pending(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
