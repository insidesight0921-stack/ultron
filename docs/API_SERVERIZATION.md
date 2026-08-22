# Phase 2 데이터 API 서버화

> 상태: 2026-08-22 Phase 1 물리 분리 완료 후 1차 Shareable API 골격 착수
> 목표: 직접 파일·DB 접근을 명시적 API 계약으로 점진 전환하고, Private와 MCP용
> Shareable 표면을 코드 수준에서 분리한다.

## 1. 원칙

1. 운영 데이터 경로나 SQL을 요청 인자로 받지 않는다.
2. Shareable API는 `data/shareable`만 고정 참조한다.
3. Private 데이터는 Shareable API에 라우트를 만들지 않는다.
4. 서버는 기본 `127.0.0.1` 바인딩만 허용한다.
5. 기존 봇은 도메인 단위로 전환하며, 한 단계마다 직접 접근 폴백과 회귀 테스트를
   유지한다.
6. MCP는 Phase 3에서 Shareable API의 명시적 allowlist만 사용한다.

## 2. 포트와 프로세스 경계

| 서비스 | 포트 | 데이터 경계 |
|---|---:|---|
| Paper UI | 8080 | Private paper 데이터와 로컬 UI |
| Tapnow | 8082 | 별도 프로젝트 |
| Ollama | 11434 | 로컬 모델 |
| Shareable Data API | 8090 | 공개 캐시·일반화 가능한 조회만 |

8090 서버는 개발 골격 단계에서 자동 실행 서비스로 등록하지 않는다. worktree에서도
운영 서버를 띄우지 않고 TestClient로 계약을 검증한다. 실제 상시 기동은 API 소비자가
준비된 뒤 별도 운영 변경으로 수행한다.

## 3. API 표면 1차

| 메서드·경로 | 역할 | 데이터 |
|---|---|---|
| `GET /health` | readiness와 경계 버전 | 민감 경로·행 수 미포함 |
| `GET /v1/shareable/instruments/{ticker}` | 공개 종목·법인 메타데이터 | ticker map, DART corp code |
| `GET /v1/shareable/ohlcv/{ticker}/latest` | 최신 공개 OHLCV 캐시 | 허용된 시계열 필드만 |

Private watchlist·일정·paper·RAG·로그 라우트는 의도적으로 없다. ticker는 6자리 숫자만
허용하고, 반환 필드는 allowlist로 필터링한다. 캐시 파일에 예상 밖 필드가 들어가도 API가
그 값을 외부 표면으로 전달하지 않는다.

## 4. 점진 전환 순서

1. **Shareable 읽기 계약**: 종목 메타데이터와 OHLCV 캐시 조회를 먼저 고정한다.
2. **Shareable 클라이언트 어댑터**: `invest_bot`, `quant_bot`, `kium_bot`의 공개 캐시
   읽기를 API 클라이언트로 전환한다. 외부 원천 수집·캐시 쓰기는 서버 쪽으로 모은다.
3. **Private API 별도 표면**: loopback + 별도 인증을 전제로 assistant/paper 도메인을
   분리한다. Shareable 앱과 라우터·토큰·프로세스를 공유하지 않는다.
4. **Private 클라이언트 전환**: Telegram·Paper UI의 직접 SQLite 접근을 도메인별로
   제거한다. 거래 쓰기는 멱등키, 승인 상태, 감사 로그를 계약에 포함한다.
5. **직접 접근 차단**: 애플리케이션 모듈의 SQLite/경로 접근을 정적 검사로 금지하고,
   저장소 구현과 마이그레이션·백업 도구만 예외 allowlist로 둔다.
6. **Phase 2 완료 검증**: 모든 런타임 데이터 접근이 API 경유, 기존 기능·성능·백업·
   롤백 테스트 통과. 이후에만 Shareable API를 MCP 도구로 감싼다.

## 5. 보안 테스트 기준

- `/v1/private/*`와 범용 파일/SQL 라우트가 존재하지 않는다.
- ticker 이외의 경로 조각·파일명·절대경로를 받을 수 없다.
- symlink·루트 이탈 파일은 읽지 않는다.
- Shareable 응답에 `chat_id`, 포트폴리오, 거래, 토큰, 개인 상태 필드가 없다.
- 기본 바인딩은 `127.0.0.1:8090`이며 비루프백 주소를 거부한다.
- 테스트는 임시 Shareable 저장소만 사용하고 실제 운영 데이터를 수정하지 않는다.

## 6. 현재 완료 범위

- `shareable_store.py`: 고정 Shareable 루트, 6자리 ticker 검증, 최신 캐시 선택,
  symlink/루트 이탈 방지, 반환 필드 allowlist.
- `data_api.py`: Shareable 전용 FastAPI 앱과 루프백 전용 실행 진입점.
- 단위·API 보안 테스트: 실제 서버 기동 없이 임시 데이터로 검증.

다음 구현 단위는 `invest_bot`의 ticker map 읽기를 Shareable API 클라이언트 어댑터로
전환하는 것이다. 운영 서버 등록과 기존 직접 접근 제거는 해당 어댑터 검증 후 진행한다.
