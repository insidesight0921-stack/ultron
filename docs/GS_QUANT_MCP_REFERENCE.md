# GS Quant 검토 메모 — Phase 3 MCP 서버화 참고용

> 작성일: 2026-08-26
> 결론: **의존성으로 추가하지 않는다.** `gs_quant/mcp/` 구조만 Phase 3 설계 참고로 사용한다.
> 검증 방식: 격리 환경에 `gs-quant 2.1.4` 실제 설치 후 인증 없이 기능별 실행

---

## 1. 무엇인가

- 골드만삭스가 공개한 파이썬 퀀트 라이브러리. GitHub `goldmansachs/gs-quant`, License **Apache-2.0**
- 최신 2.x대(검증 시점 설치본 2.1.4), Python 3.9+
- 신규 공개가 아니라 수년 전부터 공개돼 있던 라이브러리 (PyPI에 0.5.x대 배포 이력 존재)

## 2. 도입하지 않는 이유

**홍보에서 강조되는 기능(파생상품 프라이싱, 백테스트, 리스크)은 전부 Marquee API 인증(client id/secret)이 필요하고,
해당 자격증명은 골드만삭스 기관 고객에게만 발급된다.** 개인은 발급받을 수 없다.

### 실측 결과 (인증 없이 실행)

| 기능 | 결과 |
|---|---|
| moving_average / bollinger_bands / RSI / EMA | ✅ 정상 계산 |
| volatility / max_drawdown / beta / correlation | ✅ 정상 계산 |
| sharpe_ratio | ❌ `MqUninitialisedError: GsSession is not initialised` (무위험수익률을 GS 서버 조회) |
| IRSwaption.price() | ❌ 동일 오류 |
| Dataset('TREOD').get_data() | ❌ 동일 오류 |
| backtests 모듈 | ⚠️ import만 가능, 가격계산이 API 의존이라 실행 불가 |

### 판단 근거

1. 인증 없이 쓸 수 있는 범위 = 기술적 지표 계산 → **울트론 `quant_bot`/`invest_bot`에 이미 있음**
2. 한국 시장 데이터 미지원 → pykrx / FinanceDataReader를 대체하지 못함
3. 신규 의존성 56개(scipy, statsmodels, lmfit, opentelemetry 등) 유입 → 얻는 것 대비 과중

### 호환성 (참고 — 붙일 경우에도 충돌은 없음)

- Python 3.12 ✅ / pandas 2.3.3 ✅ / numpy 1.26.4 ✅ (요구: `numpy<2.4.0`)
- httpx 요구 `>=0.28.1`, 울트론 설치본 0.28.1 → 충돌 없음
- 즉 **막혀서 안 쓰는 게 아니라, 실익이 없어서 안 쓰는 것**

---

## 3. 유일하게 건질 것 — `gs_quant/mcp/` 구조

gs-quant 2.x에는 **FastMCP 기반 MCP 서버 구현체**가 포함돼 있다.
도구 자체는 GS 인증이 필요해 쓸 수 없지만, **실제 금융기관의 MCP 서버 계층 구조 공개 사례**로서 Phase 3 참고 가치가 있다.

```
gs_quant/mcp/
├── run.py             ← FastMCP + uvicorn 기동 (포트/호스트/SSL 주입)
├── config.py          ← McpServiceConfig (base_path, port, host, env, ssl_config) pydantic 모델
├── middleware.py      ← LocalUserAuthMiddleware / RemoteUserAuthMiddleware
├── session_utils.py   ← 쿠키·헤더에서 토큰 추출, AuthType 판별, 세션 구성 (cachetools 캐싱)
├── dependencies.py    ← 도구가 세션을 주입받는 지점 (Depends), 실패 시 ToolError
└── tools/
    ├── registry.py    ← @mcp_tool 데코레이터 등록 + discover_tools(package) 자동 탐색
    ├── data/tools.py
    ├── marketview/tools.py
    └── users/tools.py
```

### 울트론 Phase 3에 시사하는 점

| 관찰 | 울트론 적용 |
|---|---|
| **인증을 도구가 아니라 middleware 계층에서 처리** (`on_request`에서 세션을 컨텍스트 state에 주입) | 도구마다 인증 로직을 넣으면 하나만 빠뜨려도 구멍 — 계획서의 "Gateway = 단일 검문소" 원칙과 동일한 구조 |
| **단일 사용자용 / 다중 사용자용 미들웨어를 클래스로 분리** (Local vs Remote) | 울트론은 1인 시스템이므로 Local 상당 구조로 시작하고, 확장 시 교체 지점을 미리 분리해 두는 설계가 타당함을 뒷받침 |
| 세션 객체를 `serializable=False`로 요청 범위에만 유지 | 개인 자격증명이 요청 밖으로 새지 않게 하는 처리 — Private 계층 설계와 같은 방향 |
| `@mcp_tool` 데코레이터 + 패키지 자동 탐색으로 도구 등록 | 도구가 늘어날 때 등록 누락을 막는 방식. 다만 **자동 탐색은 노출 범위를 넓히기 쉬움** → 울트론은 Shareable만 노출해야 하므로 **자동 탐색보다 명시적 allowlist 등록이 안전** |
| 도메인별 tools 하위 패키지 분리 (data / marketview / users) | 울트론도 shareable 도메인 단위(price / factor / indicator)로 분리 |

### 주의 (그대로 따라가면 안 되는 것)

- gs-quant의 도구 자동 탐색(`discover_tools`)은 **"등록된 것은 모두 노출"** 전제다.
  울트론은 데이터 이원화가 전제이므로 **기본 비노출 + 명시 등록(fail-closed)** 을 유지할 것.
- `config.py` 기본 host가 `0.0.0.0` — 울트론은 loopback 고정 원칙을 지킬 것.

---

## 4. 결론

1. `requirements.txt`에 **추가하지 않는다**
2. Phase 3 착수 시 `gs_quant/mcp/`를 **읽기 참고 자료로만** 사용 (별도 위치에 클론, `.venv` 설치 금지)
3. 차용할 것: middleware 계층 인증, 요청 범위 세션, 도메인별 도구 분리
4. 차용하지 않을 것: 도구 자동 탐색 노출, `0.0.0.0` 기본 바인딩
