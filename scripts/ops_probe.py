"""ops_probe.py — 운영 상태를 **재는** 틀 (AIOps: 신선도·생존·소유자·종료코드)

왜 있나. 2026-09-09에 "8080 살아 있나, 수집은 되나"를 알려고 로그 8개를 손으로
뒤졌고 포트는 그래도 확정 못 했다. 2026-10-08에는 KOSPI 지수 캐시가 **3주째
09-17에 멈춰 있는데** 수집기는 매일 "성공"을 찍고 있었다 — 행 수가 362→350으로
하루 1씩 줄고 있었지만 읽는 코드가 없었다. 외국인 순매수 이력도 09-01에 멈춰
있었다. 측정 틀(nowcast_eval·weight_rule_backtest·signal_review)은 있는데
**운영 상태를 재는 틀이 없었다.**

교재(모델 서빙 & AIOps, 2026-09)에서 가져온 원리 세 가지 — 도구가 아니라 원리:
  ① 서비스는 자기 상태를 노출하고 누군가 주기적으로 긁는다(scrape).
  ② 알림은 규칙으로 판정하고 **상태가 바뀔 때만** 보낸다. 교재가 AIOps 1.0의
     실패 원인으로 꼽은 것이 알림 노이즈다 — 15분마다 같은 말을 하면 아무도 안 읽는다.
  ③ 무엇을 볼지는 **미리 정해 둔다**(FRESHNESS·SERVICES·LAUNCHD_PREFIX). 돌릴
     때마다 보는 것이 달라지면 "안 보였다"와 "안 봤다"를 가를 수 없다.

무엇을 재나:
  - 신선도: 캐시·원장·로그 각각의 "마지막 기록"이 오늘보다 몇 **평일** 뒤처졌나.
    휴장일을 평일로 세므로 연휴 뒤엔 한두 날 과장된다 → 문턱은 항목마다 그만큼 여유.
  - 생존: 8080·8090·8091 health를 127.0.0.1로 호출(2초).
  - 소유자: `lsof`로 그 포트를 듣는 프로세스 수 — 2026-09-09에 리스너 3개가 2주간
    겹쳐 있었다(java·정체불명 python·paper_ui).
  - 종료코드: `launchctl list`에서 이 프로젝트 라벨의 마지막 종료 코드 ≠ 0.

무엇을 하지 않나: 고치지 않는다. 재시작하지 않는다. 판정만 남기고, 바뀌면 말한다.
원장(`ops_probe.jsonl`)은 덧붙이기만 한다. 최신 상세는 `ops_probe_latest.json`에
따로 두어 화면이 읽는다.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Optional

log = logging.getLogger("ops_probe")

LEDGER_NAME = "ops_probe.jsonl"
LATEST_NAME = "ops_probe_latest.json"
LAUNCHD_PREFIX = "com.hyunjun.ai-agent."
HEALTH_TIMEOUT = 2.0

# ─── 사전 등록: 무엇을 보는가 ───────────────────────
#
# kind:
#   series    — JSON의 series.date 마지막 값 (지수·이력 캐시). glob의 마지막 파일.
#   jsonl_day — JSONL 마지막 줄의 날짜 필드(YYYY-MM-DD 또는 YYYYMMDD 앞 10자).
#   mtime     — 파일 수정 시각의 날짜.
#   mtime_h   — 파일 수정 시각, 시간 단위(장중 5분마다 적는 로그용).
# max_lag: 평일 수(또는 시간). 이 **이상**이면 실패.
FRESHNESS = (
    {"name": "KOSPI 지수 캐시", "kind": "series", "root": "shareable",
     "glob": "cache/indices/market_index_KOSPI_*.json", "max_lag": 3,
     "why": "FDR KS11이 2026-09-17에 멈춘 것을 3주간 몰랐다"},
    {"name": "VKOSPI 지수 캐시", "kind": "series", "root": "shareable",
     "glob": "cache/indices/market_index_VKOSPI_*.json", "max_lag": 4,
     "why": "KRX가 하루 늦게 준다 — 여유 하나 더"},
    {"name": "외국인 순매수 이력", "kind": "series", "root": "shareable",
     "glob": "cache/flow/foreign_net_*.json", "max_lag": 3,
     "why": "2026-09-01에 멈춘 채 나우캐스팅 예측 수가 늘지 않았다"},
    {"name": "대리 지표 기록", "kind": "jsonl_day", "root": "state",
     "path": "proxy_indicators.jsonl", "field": "day", "max_lag": 3},
    {"name": "신호 기록", "kind": "mtime", "root": "state",
     "path": "signal_log.jsonl", "max_lag": 3},
    {"name": "신호 결과 평가", "kind": "mtime", "root": "state",
     "path": "signal_outcomes.jsonl", "max_lag": 3},
    {"name": "키움 주간 선정/대조", "kind": "jsonl_day", "root": "state",
     "path": "bot_signals.jsonl", "field": "at", "max_lag": 9,
     "why": "매주 월요일. 09-21 DNS 장애·10-05 휴일로 두 주가 비었다"},
    {"name": "수집기 로그", "kind": "mtime", "root": "logs",
     "path": "market_data_collector.out.log", "max_lag": 3},
    {"name": "Private 백업 로그", "kind": "mtime", "root": "logs",
     "path": "private_backup.out.log", "max_lag": 3},
    {"name": "텔레그램 봇 로그", "kind": "mtime_h", "root": "logs",
     "path": "telegram.err.log", "max_lag": 6,
     "why": "장중 5분마다 적는다. 6시간 조용하면 죽었거나 Mac이 잠들었다"},
)

SERVICES = (
    {"name": "paper_ui", "port": 8080, "path": "/api/health"},
    {"name": "data_api", "port": 8090, "path": "/health"},
    {"name": "private_data_api", "port": 8091, "path": "/health"},
)


# ─── 순수 계산 ───────────────────────────────────────


def weekdays_between(last: str, today: str) -> int:
    """`last` 다음 날부터 `today`까지 평일 수(순수). 형식이 틀리면 -1."""
    try:
        a = datetime.strptime(last[:10].replace("-", ""), "%Y%m%d")
        b = datetime.strptime(today[:10].replace("-", ""), "%Y%m%d")
    except ValueError:
        return -1
    n = 0
    d = a + timedelta(days=1)
    while d <= b:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def judge_freshness(item: dict, last: Optional[str], now: datetime) -> dict:
    """항목 하나의 판정(순수). last는 날짜(YYYYMMDD·YYYY-MM-DD) 또는 ISO 시각."""
    out = {"name": item["name"], "kind": item["kind"], "last": last,
           "max_lag": item["max_lag"]}
    if not last:
        out.update(ok=False, lag=None, detail="기록 없음")
        return out
    if item["kind"] == "mtime_h":
        try:
            t = datetime.fromisoformat(last)
        except ValueError:
            out.update(ok=False, lag=None, detail="시각 형식 오류")
            return out
        hours = (now - t).total_seconds() / 3600
        ok = hours < item["max_lag"]
        out.update(ok=ok, lag=round(hours, 1),
                   detail=f"{hours:.1f}시간 전" + ("" if ok else f" ≥ {item['max_lag']}시간"))
        return out
    lag = weekdays_between(last, now.strftime("%Y%m%d"))
    if lag < 0:
        out.update(ok=False, lag=None, detail="날짜 형식 오류")
        return out
    ok = lag < item["max_lag"]
    out.update(ok=ok, lag=lag,
               detail=f"마지막 {last[:10]} · {lag}평일 전" + ("" if ok else f" ≥ {item['max_lag']}"))
    return out


def parse_lsof(text: str) -> list[dict]:
    """`lsof -nP -iTCP:PORT -sTCP:LISTEN` 출력 → [{command, pid, name}] (순수)."""
    rows = []
    for line in (text or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 9:
            continue
        rows.append({"command": parts[0], "pid": parts[1], "name": parts[-2]})
    return rows


def judge_port(service: dict, listeners: list[dict]) -> dict:
    """포트 소유자 판정(순수). 리스너가 0이면 죽은 것, 2 이상이면 겹친 것."""
    n = len(listeners)
    who = ", ".join(f"{r['command']}({r['pid']}) {r['name']}" for r in listeners)
    if n == 0:
        return {"name": f"{service['name']} 포트 {service['port']}", "ok": False,
                "listeners": listeners, "detail": "듣는 프로세스 없음"}
    if n > 1:
        return {"name": f"{service['name']} 포트 {service['port']}", "ok": False,
                "listeners": listeners,
                "detail": f"리스너 {n}개 겹침 — {who}"}
    return {"name": f"{service['name']} 포트 {service['port']}", "ok": True,
            "listeners": listeners, "detail": who}


def parse_launchctl(text: str, prefix: str = LAUNCHD_PREFIX) -> list[dict]:
    """`launchctl list` 출력 → 이 프로젝트 라벨만 [{label, pid, status}] (순수)."""
    rows = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) != 3 or not parts[2].startswith(prefix):
            continue
        pid, status, label = parts
        try:
            code = int(status)
        except ValueError:
            code = None
        rows.append({"label": label[len(prefix):], "pid": None if pid == "-" else pid,
                     "status": code})
    return rows


def judge_launchd(rows: list[dict]) -> list[dict]:
    """마지막 종료 코드가 0이 아닌(그리고 지금 안 도는) 잡만 실패(순수)."""
    out = []
    for r in rows:
        bad = r["pid"] is None and r["status"] not in (0, None)
        out.append({"name": f"launchd {r['label']}", "ok": not bad,
                    "detail": (f"마지막 종료 코드 {r['status']}" if bad else
                               (f"PID {r['pid']}" if r["pid"] else "대기 중"))})
    return out


def failing_names(record: dict) -> list[str]:
    return sorted(c["name"] for c in record.get("checks", []) if not c.get("ok"))


def transition(prev: Optional[dict], cur: dict) -> dict:
    """직전 기록과 견줘 **새로 깨진 것 / 다시 살아난 것**(순수).

    첫 실행(prev 없음)은 깨진 것이 있을 때만 알린다 — 정상 상태를 15분마다
    말하는 것이 교재가 꼽은 알림 노이즈다.
    """
    now_f = set(failing_names(cur))
    before = set(failing_names(prev)) if prev else set()
    broke = sorted(now_f - before)
    fixed = sorted(before - now_f)
    return {"broke": broke, "fixed": fixed, "changed": bool(broke or fixed)}


def format_alert(tr: dict, cur: dict) -> str:
    lines = [f"🩺 운영 감시 — {cur.get('at', '')[:16]}"]
    if tr["broke"]:
        lines.append("❌ 새로 깨짐")
        for name in tr["broke"]:
            c = next((x for x in cur["checks"] if x["name"] == name), {})
            lines.append(f"  · {name}: {c.get('detail', '')}")
    if tr["fixed"]:
        lines.append("✅ 다시 정상")
        for name in tr["fixed"]:
            lines.append(f"  · {name}")
    still = [n for n in failing_names(cur) if n not in tr["broke"]]
    if still:
        lines.append(f"⚠️ 계속 깨져 있음 {len(still)}건: " + ", ".join(still))
    lines.append("_고치지 않습니다. 재시작하지 않습니다. 판정만 합니다._")
    return "\n".join(lines)


def format_report(cur: dict) -> str:
    """사람이 읽는 전체 상태(순수)."""
    checks = cur.get("checks", [])
    bad = [c for c in checks if not c.get("ok")]
    lines = [f"🩺 운영 상태 {cur.get('at', '')[:16]} — {len(checks) - len(bad)}/{len(checks)} 정상"]
    for c in checks:
        lines.append(f"  {'✅' if c.get('ok') else '❌'} {c['name']}: {c.get('detail', '')}")
    return "\n".join(lines)


# ─── 경계 (I-O) ──────────────────────────────────────


def _roots() -> dict[str, Path]:
    try:
        from storage_paths import PATHS
        return {"shareable": Path(PATHS.shareable_root),
                "state": Path(PATHS.private_state_dir),
                "logs": Path(PATHS.logs_dir)}
    except Exception:  # noqa: BLE001
        base = Path(__file__).resolve().parents[1] / "data"
        return {"shareable": base / "shareable", "state": base / "private" / "state",
                "logs": base / "private" / "logs"}


def last_of(item: dict, roots: dict[str, Path]) -> Optional[str]:
    """항목의 '마지막 기록'을 파일에서 읽는다. 없거나 깨지면 None."""
    root = roots[item["root"]]
    try:
        if item["kind"] == "series":
            files = sorted(root.glob(item["glob"]))
            if not files:
                return None
            series = json.loads(files[-1].read_text(encoding="utf-8")).get("series") or {}
            dates = series.get("date") or []
            return str(dates[-1]) if dates else None
        p = root / item["path"]
        if not p.exists():
            return None
        if item["kind"] == "jsonl_day":
            last_line = ""
            with p.open("rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 65536))
                for line in f.read().decode("utf-8", "replace").splitlines():
                    if line.strip():
                        last_line = line
            if not last_line:
                return None
            value = json.loads(last_line).get(item["field"])
            return str(value)[:10] if value else None
        mtime = datetime.fromtimestamp(p.stat().st_mtime)
        return mtime.isoformat(timespec="seconds") if item["kind"] == "mtime_h" else mtime.strftime("%Y%m%d")
    except Exception as exc:  # noqa: BLE001
        log.warning("%s 읽기 실패: %s", item["name"], exc)
        return None


def http_health(service: dict, timeout: float = HEALTH_TIMEOUT) -> dict:
    url = f"http://127.0.0.1:{service['port']}{service['path']}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read(500).decode("utf-8", "replace")
            ok = resp.status == 200
            return {"name": f"{service['name']} health", "ok": ok,
                    "detail": f"HTTP {resp.status}" + ("" if ok else f" {body[:80]}")}
    except Exception as exc:  # noqa: BLE001
        return {"name": f"{service['name']} health", "ok": False,
                "detail": f"{type(exc).__name__}: {str(exc)[:80]}"}


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
    except Exception as exc:  # noqa: BLE001
        log.debug("%s 실패: %s", cmd[0], exc)
        return ""


def lsof_listeners(port: int) -> Optional[list[dict]]:
    """lsof가 없으면 None(검사 생략) — 없는 것을 '0개'로 말하지 않는다."""
    text = _run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"])
    if not text and subprocess.run(["which", "lsof"], capture_output=True).returncode != 0:
        return None
    return parse_lsof(text)


def probe(*, now: Optional[datetime] = None, roots: Optional[dict] = None,
          health: Callable[[dict], dict] = http_health,
          listeners: Callable[[int], Optional[list[dict]]] = lsof_listeners,
          launchctl: Callable[[], str] = lambda: _run(["launchctl", "list"])) -> dict:
    """한 번 재서 기록 한 건(dict)을 만든다. 쓰지 않는다."""
    now = now or datetime.now()
    roots = roots or _roots()
    checks: list[dict] = []
    for item in FRESHNESS:
        checks.append(judge_freshness(item, last_of(item, roots), now))
    for svc in SERVICES:
        checks.append(health(svc))
        ls = listeners(svc["port"])
        if ls is not None:
            checks.append(judge_port(svc, ls))
    checks.extend(judge_launchd(parse_launchctl(launchctl())))
    return {"at": now.isoformat(timespec="seconds"), "checks": checks,
            "n": len(checks), "failing": sorted(c["name"] for c in checks if not c["ok"])}


def ledger_path(state_dir: Optional[Path] = None) -> Path:
    return (state_dir or _roots()["state"]) / LEDGER_NAME


def latest_path(state_dir: Optional[Path] = None) -> Path:
    return (state_dir or _roots()["state"]) / LATEST_NAME


def load_previous(path: Path) -> Optional[dict]:
    """원장 마지막 줄(직전 상태). 없거나 깨지면 None."""
    try:
        last = ""
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                last = line
        return json.loads(last) if last else None
    except (OSError, ValueError):
        return None


def record(cur: dict, *, state_dir: Optional[Path] = None) -> None:
    """원장에는 요약 한 줄만 덧붙이고(길이 보호), 최신 상세는 통째로 바꿔 쓴다."""
    state_dir = state_dir or _roots()["state"]
    state_dir.mkdir(parents=True, exist_ok=True)
    row = {"at": cur["at"], "n": cur["n"], "failing": cur["failing"],
           "checks": [{"name": c["name"], "ok": c["ok"]} for c in cur["checks"]]}
    with ledger_path(state_dir).open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp = latest_path(state_dir).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(latest_path(state_dir))


def send_telegram(text: str) -> int:
    """등록된 사용자에게 보낸다. 환경이 없으면 0 — 기록은 이미 남았다."""
    try:
        import env_config
        env_config.ensure_env(("TELEGRAM_BOT_TOKEN", "ALLOWED_TELEGRAM_USER_ID"))
    except Exception:  # noqa: BLE001
        pass
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    ids = [x.strip() for x in os.environ.get("ALLOWED_TELEGRAM_USER_ID", "").split(",") if x.strip()]
    if not token or not ids:
        log.warning("텔레그램 환경 없음 — 알림 생략")
        return 0
    sent = 0
    for cid in ids:
        payload = json.dumps({"chat_id": cid, "text": text[:4000],
                              "disable_web_page_preview": True}).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                if json.loads(resp.read().decode()).get("ok"):
                    sent += 1
        except Exception as exc:  # noqa: BLE001
            log.error("텔레그램 전송 실패: %s", exc)
    return sent


def run_once(*, alert: bool = True, state_dir: Optional[Path] = None,
             sender: Callable[[str], int] = send_telegram, **probe_kw) -> tuple[dict, dict]:
    """재고 → 직전과 비교 → 기록 → 바뀌었으면 알림. (기록, 전이)"""
    state_dir = state_dir or _roots()["state"]
    prev = load_previous(ledger_path(state_dir))
    cur = probe(**probe_kw)
    tr = transition(prev, cur)
    record(cur, state_dir=state_dir)
    if alert and tr["changed"]:
        sender(format_alert(tr, cur))
    return cur, tr


def _cli() -> int:
    ap = argparse.ArgumentParser(description="운영 상태 감시 — 판정만, 고치지 않는다")
    ap.add_argument("--no-alert", action="store_true", help="텔레그램을 보내지 않는다")
    ap.add_argument("--dry", action="store_true", help="기록도 알림도 없이 결과만 출력")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    if args.dry:
        cur = probe()
        tr = transition(load_previous(ledger_path()), cur)
    else:
        cur, tr = run_once(alert=not args.no_alert)
    if args.json:
        print(json.dumps({"record": cur, "transition": tr}, ensure_ascii=False, indent=1))
    else:
        print(format_report(cur))
        if tr["changed"]:
            print()
            print(format_alert(tr, cur))
    # 종료 코드는 늘 0 — 깨진 것이 있다고 1로 끝내면 launchd가 이 잡을
    # "마지막 종료 코드 1"로 적고, 다음 실행이 **자기 자신**을 실패로 센다.
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
