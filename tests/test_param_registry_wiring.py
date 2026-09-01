"""test_param_registry_wiring.py — 등록된 값이 실제로 돌고, 못 잴 값은 안 들어간다.

**등록 조건은 하나다: 그 값을 판정할 계열이 있어야 한다.** 계열이 없으면
검증이 통과 도장이 되고, 그러면 이 창구는 근거 없는 숫자를 넣는 통로가 된다.
"""
from __future__ import annotations

import ast
import io
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import param_registry as pr  # noqa: E402
import param_store as ps  # noqa: E402


def _code_only(src: str) -> str:
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return " ".join(out)


def _func(path: Path, name: str) -> str:
    src = path.read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"{name} 없음")


def test_every_registered_param_points_at_a_real_constant():
    """모듈·속성 이름이 틀리면 조용히 폴백만 돌고 아무도 모른다."""
    for key, param in pr.PARAMS.items():
        value = pr.code_default(param)
        assert isinstance(value, float), key


def test_every_registered_param_has_a_sample_function():
    for key, param in pr.PARAMS.items():
        assert callable(param.sample), key
        assert isinstance(param.sample(), list), key


def test_every_registered_param_carries_its_provenance():
    """숫자만 있고 유래가 없으면 다음 사람이 또 상식으로 바꾼다."""
    for key, param in pr.PARAMS.items():
        assert param.note and len(param.note) > 10, key


def test_the_slope_threshold_is_not_registered():
    """계열은 있으나 **부호가 안 바뀌어** 어떤 값도 검증을 통과할 수 없다."""
    assert not any("slope" in k or "기울기" in p.label for k, p in pr.PARAMS.items())


def test_the_vkospi_weight_thresholds_are_not_registered_here():
    """자기 원장이 따로 있다 — 두 곳에서 고치면 한쪽만 바뀌는 날이 온다."""
    keys = set(pr.PARAMS)
    assert "vkospi.high" not in keys and "vkospi.low" not in keys


def test_taste_parameters_are_not_registered():
    """알림 주기·슬롯 비중은 판정할 분포가 없다 — 넣으면 규율이 장식이 된다."""
    bad = [k for k in pr.PARAMS if any(w in k for w in ("interval", "slot", "count"))]
    assert not bad, bad


# **아는 위반은 목록에 적어둔다.** 통과시켜 버리면 새 위반도 같이 묻히고,
# 테스트를 지우면 다음 사람이 이 사실을 모른다. 여기 있는 값은 "고쳐야 하는데
# 아직 승인을 안 받은 값"이지 "괜찮은 값"이 아니다.
#
# vix.calm / vix.stress — 2026-09-01 실측(494일): risk_on 58.1% · risk_off 3.4%.
#   교과서 값(≤18/≥28)이고 **죽은 가지는 아니지만**(양쪽 다 걸린다) 한쪽이
#   과반이라 사실상 기본 상태다. 분포 기준은 15.7 / 20.6(각 21.1% · 20.9%).
#   VIX는 분기 중앙값 15.8~20.8로 정상성이 있어 백분위가 무너지지 않는다
#   (VKOSPI는 19.8→79.6으로 4배 올라 레벨 임계가 무너졌다 — 다른 경우다).
#   **돈이 걸린 값이 아니라 자동으로 바꾸지 않고 `/파라미터` 승인에 맡긴다.**
# **비어 있다 — 지금 도는 값은 전부 자기 검증을 통과한다.**
#
# 2026-09-01 이력:
#   vix.calm   18.0 → 15.7  (58.1% → 21.1%)
#   vix.stress 28.0 → 20.6  ( 3.4% → 20.9%)
# 둘 다 `/params` 승인 경로로 바뀌었고 원장에 표본·발동률과 함께 남았다.
# 여기에 뭔가 다시 들어온다면 그건 "고쳐야 하는데 아직 승인을 안 받은 값"이지
# "괜찮은 값"이 아니다.
KNOWN_OUT_OF_BAND: set = set()


def _violations(getter) -> dict:
    out = {}
    for key, param in pr.PARAMS.items():
        sample = param.sample()
        if len(sample) < ps.MIN_SAMPLE:
            continue                      # 캐시 없는 환경(CI) — 건너뛴다
        check = ps.validate(param, getter(key, param), sample)
        if not check["ok"]:
            out[key] = check.get("reason")
    return out


def test_the_active_values_all_pass_their_own_validation():
    """**지금 실제로 도는 값**이 자기 검증을 통과 못 하면 그 값부터 틀린 것이다.

    코드 상수가 아니라 원장의 유효값을 본다 — 승인이 일어나면 둘이 갈린다.
    """
    failures = _violations(lambda key, _p: pr.active(key)[0])
    unexpected = {k: v for k, v in failures.items() if k not in KNOWN_OUT_OF_BAND}
    assert not unexpected, f"새로 한도를 벗어난 값: {unexpected}"


def test_the_code_fallbacks_are_known_even_when_out_of_band():
    """원장이 사라지면 코드 상수로 되돌아간다 — 그때 무엇이 되는지 알아야 한다.

    코드 상수는 역사적 출발점이라 한도 밖일 수 있다(vix.calm 18.0 = 58.1%).
    그 사실을 여기 적어두고, **새로 늘어나면** 실패시킨다.
    """
    known_fallback = {"vix.calm", "vix.stress"}
    failures = _violations(lambda _k, p: pr.code_default(p))
    unexpected = {k: v for k, v in failures.items() if k not in known_fallback}
    assert not unexpected, f"코드 상수가 새로 한도를 벗어났다: {unexpected}"


def test_the_known_violations_are_still_violations():
    """고쳐지면 목록에서 빼야 한다 — 안 빼면 목록이 낡아 아무것도 안 지킨다.

    2026-09-01에 이 테스트가 두 번 실패시켜 목록을 비우게 만들었다.
    """
    for key in KNOWN_OUT_OF_BAND:
        param = pr.PARAMS[key]
        sample = param.sample()
        if len(sample) < ps.MIN_SAMPLE:
            continue
        out = ps.validate(param, pr.active(key)[0], sample)
        assert not out["ok"], (
            f"{key}가 이제 통과한다 — KNOWN_OUT_OF_BAND에서 빼라")


# ─── 소비자가 원장을 읽는가 ─────────────────────────


def test_the_emergency_module_reads_the_ledger():
    body = _code_only(_func(SCRIPTS / "emergency_response.py", "assess"))
    assert "_threshold" in body


def test_the_indicator_states_read_the_ledger():
    for name in ("vix_state", "fx_state"):
        body = _code_only(_func(SCRIPTS / "proxy_indicators.py", name))
        assert "_param" in body, name


def test_explicit_arguments_still_win_over_the_ledger():
    """테스트·모의가 원장에 의존하면 결과가 날마다 달라진다."""
    import emergency_response as er

    a = er.assess(50.0, [100.0] * 10, vkospi_high=40.0, drop_pct=-99.0)
    assert a["level"] == er.ALERT


def test_the_telegram_command_is_registered():
    bot = (SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8")
    names = set()
    for node in ast.walk(ast.parse(bot)):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "CommandHandler"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Name)):
            names.add(node.args[1].id)
    assert "cmd_params" in names


def test_the_approval_callback_is_registered():
    bot = (SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8")
    assert "handle_param_callback" in _code_only(bot)
    assert "param:" in bot


def test_approval_goes_through_validation_not_straight_to_the_ledger():
    body = _code_only(_func(SCRIPTS / "telegram_bot.py", "handle_param_callback"))
    assert "_ps . approve" in body
    assert "ValueError" in body
