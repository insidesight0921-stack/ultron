# Private Data API 계약

> 상태: 2026-08-26 watchlist·일정 읽기/쓰기 운영 전환 및 실제 Telegram E2E 완료
> 목표: Shareable API와 프로세스·포트·토큰·라우터를 공유하지 않고 개인 데이터를
> 로컬 소비자에게만 제공한다.

## 1. 고정 경계

| 항목 | 계약 |
|---|---|
| 프로세스 | `private_data_api.py` 독립 프로세스 |
| 기본 주소 | `127.0.0.1:8091` |
| 네트워크 | IPv4/IPv6 loopback과 `localhost`만 허용 |
| 인증 | `Authorization: Bearer <AI_AGENT_PRIVATE_API_TOKEN>` |
| 토큰 | 32자 이상, 공백 금지, Telegram 토큰 재사용 금지 |
| 데이터 루트 | `storage_paths.py`가 선택한 `data/private`만 사용 |
| 문서 표면 | OpenAPI·Swagger·ReDoc 비활성화 |
| 응답 캐시 | 모든 응답 `Cache-Control: no-store` |
| 접근 로그 | query string 비밀값 유출 방지를 위해 Uvicorn access log 비활성화 |

8080(Paper), 8082(Tapnow), 8090(Shareable API), 11434(Ollama)는 Private API
포트로 사용할 수 없다. 비루프백 바인딩과 기존 서비스 포트 지정은 실행 전에 거부한다.

## 2. 현재 API 표면

| 메서드·경로 | 인증 | 응답 |
|---|---|---|
| `GET /health` | 불필요 | 서비스·버전·`private-auth-required` 경계만 반환 |
| `GET /v1/private/status` | Bearer 필수 | read capabilities·watchlist/schedule write capability·`writes_enabled=true` |
| `GET /v1/private/watchlist` | Bearer 필수 | ticker·name·created_at allowlist와 count |
| `GET /v1/private/schedule/events` | Bearer + chat scope 필수 | 해당 chat의 미완료 전체 일정, 최대 100건 |
| `GET /v1/private/schedule/events/upcoming` | Bearer + chat scope 필수 | 해당 chat의 현재 이후 미완료 일정 |
| `GET /v1/private/paper/portfolios` | Bearer 필수 | id·name·seed_capital·created_at, 최대 10건 |
| `GET /v1/private/paper/positions` | Bearer 필수 | position·slot 식별자와 ticker·보유·시각 allowlist, 최대 1,000건 |
| `GET /v1/private/paper/slots` | Bearer 필수 | slot·자본 allowlist와 position/trade count, 최대 20건 |
| `GET /v1/private/paper/trades` | Bearer 필수 | 체결·slot allowlist를 최신순으로 최대 1,000건 |
| `GET /v1/private/paper/ipo/records` | Bearer 필수 | IPO paper 기록 allowlist를 최신순으로 최대 500건 |
| `GET /v1/private/paper/ipo/stats` | Bearer 필수 | 청약·상장 완료 건의 등급별 집계, 최대 20건 |
| `GET /v1/private/paper/performance` | Bearer 필수 | 슬롯별 FIFO 성과 계산 결과만 반환 |
| `GET /v1/private/paper/myquant-tags` | Bearer 필수 | 태그별 n·pnl·win_rate와 표시 문자열만 반환 |

운영 v2 atomic bundle이 검증된 경우에만 watchlist와 일정의 쓰기 handler가 등록된다.
현재 8091은 `writes_enabled=true`이며 아래 watchlist 5개와 동일 구조의 schedule 5개 경로를
운영한다. `PUT`·`PATCH`·HTTP `DELETE`는 없고 실제 mutation은 승인된 POST apply에서만 수행한다.

| 운영 write 경로 | 기능 |
|---|---|
| `POST /v1/private/watchlist/write/preflight` | 기존 intent 또는 신규 resource version 확인 |
| `POST /v1/private/watchlist/write/intents` | 요청 봉투 검증 후 `pending` 저장 |
| `POST /v1/private/watchlist/write/approvals/approve` | `X-Approval-ID` 승인 |
| `POST /v1/private/watchlist/write/approvals/reject` | `X-Approval-ID` 거부 |
| `POST /v1/private/watchlist/write/approvals/apply` | 승인·버전 확인 뒤 1회 적용 |

일정은 `/v1/private/schedule/write/...` 아래 동일한
`preflight/intents/approvals/{approve,reject,apply}` 다섯 경로를 사용하고 chat scope header를
추가로 강제한다. Paper `buy/sell`과 Paper IPO 쓰기는 아직 운영 경로가 없다.

watchlist 조회는 `private_read_store.py`가 고정한 `PATHS.watchlist_db`만 SQLite `mode=ro`와
`query_only`로 읽는다. HTTP에서 경로·테이블·SQL을 받지 않고 최대 500건으로 제한하며
`source` 등 계약 밖 필드는 반환하지 않는다.

일정 조회도 고정 `PATHS.schedule_db`를 같은 read-only 방식으로 읽는다. Telegram chat 범위는
URL이 아닌 숫자형 `X-AI-Agent-Chat-ID` 헤더로 필수 전달하며, API 응답에서는 `chat_id`,
알림 발송 상태, 생성 시각을 제외한다. 일정 제목·시각·메모·반복·사전알림 allowlist만 최대
100건 반환한다. Paper 조회는 고정 `PATHS.paper_db`에서 portfolio 4개 필드와 position
9개 필드만 읽는다. API는 slot 쿼리를 받지 않고 Paper 클라이언트가 응답을 로컬 필터링한다.
slots/trades도 같은 고정 DB에서 읽으며 API query는 받지 않는다. 소비자의 slot/limit 필터는
검증된 전체 응답에 로컬 적용한다. IPO records의 factors는 JSON 객체만 허용한다.
성과와 마이퀀트 태그는 원시 전체 거래를 응답에 넣지 않고 서버 내부 순수 계산 결과만
반환한다. RAG·상태·로그 라우트는 아직 없다. 토큰은
쿼리 문자열이나 응답에 넣지 않는다.

## 3. 인증 실패 계약

- 누락·형식 오류·불일치 토큰은 모두 HTTP 401로 동일 처리한다.
- `WWW-Authenticate: Bearer`를 반환하되 실패 이유나 토큰 일부를 노출하지 않는다.
- 비교는 `hmac.compare_digest`를 사용한다.
- 쿼리 토큰은 거부하며 요청 URL도 운영 접근 로그에 기록하지 않는다.
- 시작 시 토큰이 없거나 짧거나 placeholder이거나 Telegram 토큰과 같으면 프로세스를
  시작하지 않는다.

## 4. 향후 도메인 전환 순서

1. ~~운영 전용 토큰 생성 및 8091 launchd 등록, readiness·권한·로그 검증.~~ 완료
2. ~~watchlist 읽기 계약부터 연결하고 기존 직접 DB 경로를 폴백으로 유지.~~ 완료
3. ~~일정 list/upcoming 읽기 계약을 연결하고 기존 직접 DB 폴백을 유지.~~ 완료
4. ~~Paper 포트폴리오·포지션 읽기를 별도 단위로 연결.~~ 완료
5. ~~Paper slots/trades 읽기를 전수 확인하고 read-only 단위로 전환.~~ 완료
6. ~~IPO records/stats 런타임 읽기를 전환하고 오프라인 예외를 분류.~~ 완료
7. ~~성과·마이퀀트 태그 계산 계약을 원시 거래 API와 분리.~~ 완료
8. ~~멱등키·승인 상태·감사 로그·낙관적 충돌 방지 쓰기 계약 고정.~~ 완료
9. ~~watchlist add/remove 저장 계층을 임시 DB에서 계약 검증.~~ 완료
10. ~~Private API에 기본 비활성 write handler를 연결해 임시 DB HTTP 계약 검증.~~ 완료
11. ~~기본 비활성 Private client 쓰기 어댑터와 응답 검증 추가.~~ 완료
12. ~~Telegram consumer에 caller-supplied 요청 식별자를 전달하는 기본 비활성 통합 계약 설계.~~ 완료
13. ~~재시도에 필요한 watchlist resource version/intent 상태 preflight를 테스트 주입 전용으로 추가.~~ 완료
14. ~~identity+preflight+client를 묶는 기본 비활성 watchlist consumer executor 검증.~~ 완료
15. ~~운영 활성화 전 readiness gate와 단일 writer 소유권·백업·롤백 조건을 계약으로 고정.~~ 완료
16. ~~readiness 통과 보고서를 API/client/executor의 공통 activation permit으로 결합.~~ 완료
17. ~~운영 cutover bundle의 세 플래그·permit 발급·서비스 순서를 dry-run으로 검증.~~ 완료
18. ~~운영 DB fresh online backup과 격리 rollback rehearsal로 남은 readiness 조건 해소.~~ 완료
19. ~~API/Telegram의 atomic runtime activation bundle을 코드에 기본 비활성으로 연결.~~ 완료
20. ~~운영 activation candidate bundle 생성기와 사전 검증 runbook을 비설치 상태로 검증.~~ 완료
21. ~~명시 승인 후 전환 직전 backup/rehearsal 반복과 운영 watchlist write cutover.~~ 완료
22. ~~일정 격리 저장소부터 실제 Telegram add/delete 운영 E2E까지 전환.~~ 완료
23. ~~Paper `buy/sell` slot-scoped 기본 비활성 계약·저장소 검증.~~ 완료
24. ~~writer를 명시 주입한 기본 비활성 Paper `buy/sell` HTTP 계약 검증.~~ 완료
25. ~~별도 Paper permit을 요구하는 기본 비활성 Private client `buy/sell` 어댑터 검증.~~ 완료
26. ~~Paper UI·Telegram 직접 writer 소유권과 결정론적·비민감 요청 identity 계약 검증.~~ 완료
27. identity+Paper client를 묶는 기본 비활성 consumer executor 검증.

쓰기 계약에는 다음을 모두 포함해야 한다.

- `Idempotency-Key`: 재시도 중복 쓰기 방지.
- 승인 상태: 사용자 승인 전 `pending`, 승인 뒤 한 번만 적용.
- 감사 로그: 요청 ID·도메인·행위·결과·시각을 기록하되 토큰과 민감 본문은 기록 금지.
- 낙관적 충돌 방지: 기대 버전 또는 현재 상태를 함께 검증.
- 도메인별 allowlist: 임의 테이블명·SQL·DB 경로 입력 금지.

## 5. 고정된 쓰기 계약

### 요청 봉투

향후 mutation은 Bearer 인증에 더해 다음 값을 모두 요구한다.

| 위치 | 필드 | 계약 |
|---|---|---|
| Header | `Idempotency-Key` | 16~128자, 영숫자로 시작, 영숫자·`.`·`_`·`:`·`-`만 허용 |
| Header | `X-Request-ID` | UUID, 한 논리 요청의 추적 식별자. 승인 전이에도 동일 값 필수 |
| Header | `X-Approval-ID` | UUID, 승인 레코드와 실행을 결합하는 식별자 |
| Body | `operation` | 아래 고정 operation allowlist 중 하나 |
| Body | `expected_version` | 0 이상 정수, 적용 직전 현재 자원 버전과 일치해야 함 |
| Body | `payload` | operation별 필수·선택 필드만 허용, 최대 32 KiB JSON |

`payload`의 중첩 위치까지 token·secret·password·SQL·DB 경로·table 입력을 거부한다.
NaN/Infinity와 JSON으로 직렬화할 수 없는 값도 거부한다.

| 도메인 | 허용 operation |
|---|---|
| 관심종목 | `watchlist.add`, `watchlist.remove` |
| 일정 | `schedule.add`, `schedule.delete`, `schedule.complete` |
| Paper 매매 | `paper.buy`, `paper.sell` |
| Paper IPO | `paper_ipo.subscribe`, `paper_ipo.close` |

### 승인과 1회 적용

상태 전이는 `pending → approved → applied`만 정상 적용 경로다. `pending`은
`rejected`/`expired`로 끝날 수 있고 `approved`는 실행 전 만료될 수 있다. `applied`,
`rejected`, `expired`는 종결 상태라 재승인·재적용할 수 없다. 승인 전에는 mutation을
실행하지 않으며, 승인 주체와 요청 주체의 범위 검증은 도메인 구현 단계에서 추가한다.

### 멱등·충돌 처리

- 같은 `Idempotency-Key`와 같은 요청 fingerprint면 저장된 결과를 `replayed`로 반환한다.
- 같은 키를 다른 request/payload/version/approval에 재사용하면 `409 idempotency_conflict`다.
- `expected_version`이 현재 버전과 다르면 쓰기 전에 `409 version_conflict`로 중단한다.
- 승인 상태 확인, 버전 확인, mutation, 적용 상태와 결과 저장은 향후 하나의 트랜잭션으로
  묶는다. 부분 적용 뒤 재시도로 이중 체결되는 경로를 허용하지 않는다.

요청 fingerprint는 operation·request ID·approval ID·expected version·정규화한 payload의
SHA-256이다. 원문 Idempotency-Key는 감사 로그에 남기지 않는다.

### 감사 이벤트

감사 이벤트 allowlist는 request ID, approval ID, Idempotency-Key SHA-256, domain, action,
result, UTC 시각, 선택적 resource version/error code뿐이다. Bearer token, 원문 key,
payload, 종목·일정·메모 등 민감 본문은 기록하지 않는다. 결과는 `pending`, `approved`,
`applied`, `replayed`, `rejected`, `expired`, `conflict`, `failed`만 허용한다.

향후 HTTP 오류 계약은 잘못된 봉투/필드 400, 인증 실패 401, 과대 payload 413,
멱등·버전·승인 충돌 409, 도메인 값 검증 실패 422, 저장소 장애 503으로 고정한다.

### Paper buy/sell HTTP 계약 검증 상태

- 기본 앱과 운영 CLI에는 Paper writer를 만들거나 활성화하는 경로가 없다. 명시적으로
  `writes_enabled=True`인 writer와 그 `paper.db` 절대 경계에 결합된 별도 activation permit을
  함께 주입한 테스트 앱에만 5개 POST를 등록한다.
- intent는 `X-AI-Agent-Paper-Slot-ID`의 canonical 양의 정수와 payload `slot`의 일치를
  강제한다. preflight·approve·reject·apply도 같은 slot scope를 요구하며 다른 slot 접근은
  `scope_conflict`로 거부한다.
- Bearer, query 차단, UUID·멱등키, 중복 JSON, 요청 크기, operation·ticker·수량·가격 allowlist,
  승인 전 무변경, buy/sell 1회 적용·replay, 잔고·보유량 충돌의 비변경 409를 임시 DB에서
  검증했다.
- readiness 대상 이름은 `assistant.db`와 `paper.db`만 허용한다. 각각 private 권한,
  integrity·메모리 복구, fresh 검증 백업을 통과해야 하며 서로의 permit은 대체할 수 없다.
- 운영 8091 status에는 Paper write capability가 없고 경로는 404다. 운영 `paper.db`는
  integrity ok, scoped table 0개, portfolio 1/slots 4/positions 4/trades 169로 불변이다.

### Paper buy/sell 기본 비활성 client 검증 상태

- 일반 watchlist·일정 쓰기의 `writes_enabled`/permit과 별도로 `paper_writes_enabled`,
  `paper_write_activation_permit`, `paper_write_database_path`를 모두 요구한다. DB 이름은
  `paper.db`여야 하고 permit의 절대 경계가 일치해야 하며 일반 쓰기와 같은 permit 재사용도
  거부한다.
- 비활성 client는 slot·payload 검증이나 네트워크 호출보다 먼저 실패한다. 활성 client는
  submit/preflight/approve/reject/apply 모두에 canonical slot header를 넣고 query나 URL에
  token·scope를 넣지 않는다.
- 응답은 기존 8개 write 필드만 허용하고 request/approval/operation/version을 대조한다.
  applied 결과의 slot·side·ticker·수량·가격·수수료를 검증하며 buy `total_cost`와 sell
  `proceeds`도 수량·가격·수수료에서 다시 계산한다.
- 임시 HTTP/SQLite에서 승인 전 무변경, buy/sell 적용, applied preflight·submit replay,
  cross-slot·reject·인증 실패·잔고 부족·응답 drift를 검증했다. 운영 runtime, Paper UI,
  Telegram에는 client나 permit을 주입하지 않았다.

### Paper direct writer 소유권과 요청 identity

- production AST 전수 대조에서 direct caller는 Paper UI `api_buy/api_sell`, Telegram
  `intraday_monitor_job`, `handle_kium_paper_callback`, `handle_quant_paper_callback`, operator
  CLI `_cli`의 6개 경계다. 새 direct caller가 생기면 inventory test가 실패한다.
- Paper UI·키움·콴텍은 사용자 요청/승인 계열, intraday monitor는 `sell-only` 자동 위험정책,
  CLI는 정상 운영에서 퇴역하고 수동 rollback 전용으로 보존한다. target mutation owner는
  정상 운영 경계 모두 `private-data-api` 하나다.
- identity 입력은 caller, operation, slot scope, actor coordinate, source event, batch item
  ordinal이다. actor/event/item은 즉시 domain-separated SHA-256으로 바꾸고 객체에는 원문을
  보존하지 않는다. request/approval UUID와 Idempotency-Key는 UUID5/hash로 재현 가능하다.
- ticker·종목명·수량·가격·수수료·notes는 identity 입력이 아니다. 같은 논리 이벤트의 payload
  변경은 새 identity로 우회되지 않고 write intent fingerprint의 `idempotency_conflict`가 된다.
- 아직 Paper UI·Telegram·runtime에 identity를 생성하거나 전달하는 코드는 연결하지 않았다.
  운영 DB와 route/capability는 불변이다.

### watchlist 저장 계층 검증 상태

`private_watchlist_write_store.py`는 DB 경로를 반드시 명시해야 하며 생성 시 기본값은
`writes_enabled=false`다. 잠금 상태에서는 DB 파일조차 만들지 않는다. 현재 Private API와
운영 `assistant.db`에는 연결하지 않았고, 테스트가 만든 임시 SQLite에서만 다음을 검증했다.

- `submit`은 `pending` intent와 비민감 감사 이벤트만 저장하고 관심종목은 바꾸지 않는다.
- `approve`도 상태만 `approved`로 전환하며 실제 add/remove는 `apply` 한 번만 수행한다.
- 같은 Idempotency-Key/fingerprint 재시도는 저장된 결과만 반환하고 버전을 올리지 않는다.
- 같은 키의 다른 요청은 `idempotency_conflict`, 오래된 expected version은
  `version_conflict`로 기록하고 관심종목을 바꾸지 않는다.
- SQLite `BEGIN IMMEDIATE` 안에서 승인 확인·버전 확인·mutation·결과/감사 저장을 묶는다.
- 감사 테이블은 payload와 원문 Idempotency-Key 컬럼을 갖지 않으며 키 SHA-256만 저장한다.
- add/remove가 실제 상태를 바꾼 경우에만 watchlist resource version을 1 증가시킨다.

### watchlist HTTP 계약 검증 상태

- 기존 Bearer 인증과 query 전면 거부를 네 handler에도 동일 적용한다.
- intent는 `application/json`, 전체 요청 최대 36 KiB, 정확히 `operation`,
  `expected_version`, `payload` 세 필드만 허용한다.
- `Idempotency-Key`, `X-Request-ID`, `X-Approval-ID`를 필수화하고 중복 JSON key,
  임의 operation·추가 필드·DB 경로를 400으로 거부한다.
- 승인 전이는 body 없이 UUID `X-Approval-ID`와 원래 `X-Request-ID`를 함께 받으며 저장된
  intent의 두 식별자와 모두 일치해야 한다. 동적 DB·table·SQL 경로는 없다.
- 잘못된 봉투 400, 미인증 401, 충돌 409, 도메인 값 422, 과대 요청 413, 저장 장애 503을
  비민감 오류로 매핑한다.
- 기본 앱 mutation route 0개, writer 없이 활성화 요청 시 앱 생성 실패, 운영 CLI 활성화
  옵션 부재를 테스트로 고정했다.

### 기본 비활성 client 검증 상태

`PrivateDataClient`의 쓰기 메서드는 생성자에서 `writes_enabled=True`를 명시한 경우에만
동작한다. 환경변수 기반 자동 활성화는 추가하지 않았으므로 기존 Telegram/Paper의 기본
클라이언트는 네트워크 쓰기 요청을 만들 수 없다.

- submit은 로컬에서도 operation·payload·UUID·Idempotency-Key를 먼저 검증한다.
- approve/reject/apply는 원래 request/approval UUID를 모두 전송하고 응답의 두 값이
  요청과 같은지 재검증한다.
- 응답은 8개 고정 필드와 operation별 result allowlist만 허용한다. 추가 private 필드,
  잘못된 UUID/state/version/ticker/name은 fail-closed다.
- 무인증 401, idempotency/version/identifier/approval 충돌 409, 과대 요청 413,
  도메인 검증 422를 별도 예외로 매핑하되 서버 본문·token은 오류 문자열에 넣지 않는다.
- urllib 요청을 임시 FastAPI TestClient에 연결한 격리 브리지에서 submit→approve→apply,
  reject, replay, stale version, 잘못된 ticker를 실제 HTTP 계약으로 검증했다.
- 도메인 검증 실패는 intent를 `expired`로 끝내고 payload 없는 `failed` 감사 이벤트를 남긴다.

### Telegram consumer identity 계약

`telegram_write_identity.py`는 Telegram 원문이나 token을 사용하지 않고 update/chat/user/
message 숫자 좌표와 watchlist action으로 결정론적 식별자를 만든다.

- 같은 Telegram update와 action은 재처리·프로세스 재시작 뒤에도 동일한 request UUID,
  approval UUID, Idempotency-Key를 만든다.
- update/chat/user/message/action 중 하나라도 달라지면 세 식별자가 모두 달라진다.
- request/approval ID는 고정 namespace UUID5, Idempotency-Key는 정규화한 source tuple의
  SHA-256 앞 48자리로 생성한다. 객체에는 원문 메시지·종목명·Telegram 숫자 ID를 보관하지
  않는다.
- `telegram_bot.py`는 watchlist add/remove에만 식별자를 생성해 `watchlist_bot.run()`에
  전달한다. list에는 생성하지 않는다.
- `watchlist_bot.py`는 action과 identity operation의 일치·UUID·멱등키 형식을 검증한다.
  executor를 명시 주입하지 않은 현재 운영 경로는 기존 직접 DB 쓰기를 그대로 실행한다.
- Telegram plist·환경변수·운영 8091 writer에는 연결하지 않았고 서비스도 재시작하지 않았다.

현재 운영 consumer는 아직 Private 쓰기 client를 호출하지 않는다. 테스트에서만 executor를
명시 주입해 아래 preflight와 상태별 승인 흐름을 검증했다.

### idempotent preflight 계약

위 제약을 해소하기 위해 테스트 주입 writer/API/client에 payload 없는 preflight를 추가했다.
운영 기본 앱에는 이 경로가 없다.

- 요청은 body 없이 `X-Write-Operation`, `Idempotency-Key`, `X-Request-ID`,
  `X-Approval-ID` 네 header만 받는다.
- 신규 key는 `exists=false`와 현재 watchlist version을 `expected_version` 및
  `current_version`으로 반환한다.
- 기존 key는 저장된 최초 expected version, 현재 intent state, 현재 resource version,
  적용 완료 시 저장된 allowlist result를 반환한다. payload와 원문 key는 반환하지 않는다.
- 같은 key에 request/approval ID가 다르면 `identifier_conflict`, operation이 다르면
  `idempotency_conflict` 409로 거부한다.
- resource version과 intent 조회는 한 SQLite SELECT snapshot으로 읽어 경합 중 서로 다른
  시점의 값을 조합하지 않는다.
- preflight는 intent·watchlist·version·감사 이벤트를 변경하지 않는다. 테스트에서는
  감사 이벤트가 0개인 신규 조회와, 다른 쓰기 뒤에도 최초 version을 복원하는 재시도를 확인했다.
- client는 8개 preflight 응답 필드를 엄격히 검증하고 신규/기존 상태 조합이 모순되면
  fail-closed한다.

### 기본 비활성 watchlist consumer executor

`private_watchlist_write_consumer.py`는 Telegram identity, preflight, submit, approve,
apply를 한 흐름으로 결합하지만 생성자 기본값은 비활성이다.

- executor와 Private client를 각각 명시 활성화해야 하며, 호출마다
  `user_approved=True`가 없으면 HTTP 조회·종목 해석·DB 접근 전에 거부한다.
- 신규 요청은 preflight의 현재 version으로 intent를 제출하고, pending은 승인 후 적용한다.
  approved는 재승인 없이 적용하며 applied는 저장된 result만 반환한다.
- rejected/expired는 terminal 상태로 중단하고 관심종목을 변경하지 않는다.
- 부분 완료 재시도는 최초 expected version을 보존하며, 같은 멱등키의 payload 변경은
  `idempotency_conflict`로 닫힌다. 적용 완료 재시도는 version을 다시 올리지 않는다.
- executor 경로에서 remove 조회도 Private client만 사용한다. API 오류 시 직접 SQLite로
  우회하지 않으며 실패를 호출자에게 전달한다.
- `watchlist_bot.run()`에 executor를 명시 주입한 테스트에서만 이 경로를 사용한다. 현재
  `telegram_bot.py`는 identity만 전달하며 executor나 승인 플래그를 주입하지 않는다.

### 운영 활성화 전 readiness gate

`private_write_readiness.py`는 실행 환경이나 설정을 직접 읽거나 바꾸지 않고, 명시적으로
주입한 증거만 검사하는 순수 fail-closed gate다. 경로나 DB 내용은 보고서와 오류에 넣지 않고
고정 check code만 반환한다.

- API writer, Private client, Telegram executor 세 활성화 조건을 각각 확인한다. 하나라도
  꺼진 부분 활성화 상태는 ready가 아니다.
- active writer owner는 정확히 하나이며 `private-data-api`여야 한다. Telegram이나 다른
  프로세스가 함께 writer로 선언되면 거부한다.
- 대상은 절대 경로의 일반 `assistant.db` 또는 `paper.db` 파일이어야 하고 symlink가 아니며 group/other
  권한이 없어야 한다. SQLite integrity와 메모리 복구를 읽기 전용으로 검증한다.
- `manifest.json`과 백업 DB도 private 권한, 최근 24시간 이내 시각, 복구 검증 표시,
  SHA-256, schema signature, table count, 실제 SQLite 복구 결과가 모두 일치해야 한다.
  백업 파일명 경로 이탈도 거부한다.
- 호출별 사용자 승인 강제, executor 실패 시 direct DB fallback 금지, 검증된 rollback을
  독립 조건으로 요구한다.
- 검사는 환경변수·plist·서비스·운영 DB를 변경하지 않는다. 현재 운영 활성화에는 아직
  연결하지 않았으므로 통과 보고서 자체가 쓰기를 켜지 않는다.

### 공통 activation permit

readiness 전체 통과 뒤에만 `PrivateWriteActivationPermit`을 발급한다. 일반 readiness
보고서나 호출자가 직접 만든 객체로는 대체할 수 없다.

- permit은 검증한 DB 절대 경계와 백업 manifest fingerprint에 결합된다. 경로는
  repr·오류에 노출하지 않고 64자리 opaque fingerprint만 비교에 사용한다.
- writer를 주입해 write handler를 켜는 `create_app()`, `PrivateDataClient(writes_enabled=True)`,
  `WatchlistWriteExecutor(enabled=True)`가 모두 유효한 permit을 요구한다.
- API permit의 DB 경계가 writer DB와 다르면 앱 생성 전에 거부한다. executor permit은
  client permit과 fingerprint가 다르면 생성 전에 거부한다.
- permit을 넘기면서 해당 계층을 비활성으로 두는 모순된 설정도 거부한다. 개별 CLI 활성화
  인자는 없으며 이후 추가된 atomic runtime bundle만 세 계층을 함께 구성할 수 있다.
- 임시 DB와 복구 검증 백업으로 permit 결합을 테스트했고, 운영에서는 bundle 환경키·파일이
  없으므로 launchd 8091 앱과 Telegram에 permit이 주입되지 않는다.

### cutover/rollback dry-run bundle

`private_write_cutover.py`는 실행 기능 없이 고정 순서와 현재·목표 상태만 검사한다.

- 현재 API writer/client/executor가 모두 비활성이고 current writer가 없거나 단일
  `telegram-direct`일 때만 계획을 만든다. 이미 일부가 활성인 상태는 거부한다.
- cutover 순서는 fresh backup 확인 → Telegram 직접 writer 중지 → Private API writer 시작 →
  API writer 검증 → Telegram API consumer 시작 → direct DB 무폴백 확인으로 고정한다.
- rollback 순서는 Telegram API consumer 중지 → Private API writer 중지 → read-only API 시작 →
  Telegram 직접 writer 시작 → watchlist 정합성 확인으로 고정한다.
- DB 백업 자동 복원은 계획에 넣을 수 없다. 복원은 별도 사람 승인과 장애 판정이 필요한
  수동 비상 절차로 남긴다.
- 내부적으로 permit 발급 가능성을 검증하지만 dry-run 결과에는 permit 객체를 넣지 않고
  fingerprint만 남긴다. 산출물에는 `execute`/`apply` 기능이 없다.
- 운영 read-only dry-run 결과는 `ready=false`이며 미충족 조건은 `backup_fresh`,
  `rollback_verified` 두 개다. 검사 전후 운영 DB 해시·수정 시각은 동일했다.

### fresh backup과 격리 rollback rehearsal

2026-08-25 22:44 KST에 실행 중인 운영 SQLite를 온라인 방식으로 새 스냅샷
`20260825T224422+0900`에 백업했다.

- `paper.db`, `assistant.db` 2개를 메모리 DB로 복구하고 integrity·schema·table count·해시를
  검증했다. 백업 전후 운영 원본의 해시·수정 시각·권한은 동일했다.
- `private_write_rollback.py`는 fresh manifest의 `assistant.db`만 별도 private workspace로
  다시 복제한다. 운영 DB 경로나 서비스 제어 기능은 없고 cleanup도 하지 않는다.
- active clone에서 승인된 API add를 1회 적용한 뒤 legacy 직접 writer가 그 결과를 읽고
  별도 add를 수행하는 것을 확인했다. 이는 API writer를 중지한 뒤 legacy writer로 돌아갈
  때 적용 완료 데이터가 보존됨을 검증한다.
- 같은 backup에서 별도의 emergency restore clone을 만들고 baseline signature와 완전히
  일치함을 확인했다. 자동 복원은 수행하지 않았고 복원 산출물도 격리 상태로 보존했다.
- rehearsal workspace는 `/private/tmp/ultron-private-rollback-so8mv5vr`이며 삭제하지 않았다.
  backup 원본과 운영 `assistant.db`는 rehearsal 전후 불변이다.
- `rollback_verified=true`로 재평가한 cutover dry-run은 통과했다. 이 결과는 시간 제한이 있는
  readiness 증거일 뿐 운영 활성화 승인이 아니며, 실제 전환 직전 fresh backup을 다시 요구한다.

### 기본 비활성 atomic runtime bundle

`private_write_runtime.py`는 API writer/client/Telegram executor를 하나의 private JSON
bundle로만 구성한다.

- 유일한 런타임 입력은 `AI_AGENT_PRIVATE_WRITE_BUNDLE_PATH`이며, 값이 없으면 파일을 전혀
  읽지 않고 `None`을 반환한다. 개별 활성화 플래그나 DB 경로 환경변수는 없다.
- 파일은 고정 Private 경로 `data/private/private-write-activation.json`, 일반 파일, 600,
  최대 16KiB, 중복 JSON key 없음, 정확한 key allowlist를 모두 만족해야 한다.
- API writer/client/executor 세 플래그, 사용자 승인, direct DB 무폴백, rollback은 모두
  true여야 하고 자동 backup restore는 false여야 한다. 하나만 다른 부분 활성화는 전체 거부다.
- DB는 `storage_paths.py`의 고정 `assistant.db`만 사용한다. manifest는 승인된 private backup
  root 바로 아래 snapshot이어야 하며 임의 경로·token·DB override는 JSON에 넣을 수 없다.
- loader가 readiness, cutover 순서, bundle ID, permit fingerprint를 매 프로세스에서 다시
  검증한다. API는 permit-bound writer, Telegram은 같은 fingerprint의 client/executor를 만든다.
- Telegram add/remove를 요청한 동일 사용자 메시지만 `user_approved=True`로 전달한다.
  executor 경로는 API 실패 시 direct DB로 폴백하지 않는다.
- 현재 `.env`, 프로세스 환경, 두 launchd plist에 bundle key가 없고 activation 파일도 없다.
  따라서 8091 기본 앱은 mutation route 0개, 운영 DB의 write table도 0개다.

### 비설치 activation candidate와 runbook

`private_write_candidate.py`는 운영 Private root와 다른 빈 700 staging 디렉터리에만 candidate를
exclusive 생성한다.

- readiness·cutover·rollback을 다시 통과한 뒤 정확한 runtime JSON을 600으로 기록하고,
  API/Telegram loader를 각각 실행해 bundle ID와 permit fingerprint 일치를 확인한다.
- staging이 운영 DB 디렉터리이거나 비어 있지 않거나 기존 파일이 있으면 덮어쓰거나 삭제하지
  않고 거부한다. stale backup·미검증 rollback은 파일 생성 전에 거부한다.
- candidate에는 token·DB override가 없고 install/execute/service/env 편집 기능도 없다.
- 운영 fresh 증거로 생성한 candidate는
  `/private/tmp/ultron-private-write-candidate-p46sn34t/private-write-activation.json`에 보존했다.
  mode 600, bundle ID `37a4047a6d63575285a95249dc7c7512be2f465387e68ef122f9fe52ce83d828`,
  API/Telegram fingerprint 일치, `installed=false`다.
- 운영 고정 activation 파일·`.env`/프로세스/launchd bundle key는 여전히 없다. 운영 DB는
  생성·검증 전후 불변이며 candidate의 backup은 시간이 지나면 freshness를 잃는다.
- 실제 순서와 승인 경계는 `docs/PRIVATE_WRITE_CUTOVER_RUNBOOK.md`에 고정했다. 전환 직전 fresh
  backup/rehearsal/candidate를 다시 만들고 명시 승인을 받은 뒤에만 서비스 중지부터 시작한다.

## 6. 운영 적용 상태

- `.env`에 64자 운영 전용 `AI_AGENT_PRIVATE_API_TOKEN`을 생성했다. 값은 Git·plist·로그에
  기록하지 않고 파일 권한 600으로 보관한다.
- `com.hyunjun.ai-agent.private-data-api`를 launchd에 등록했다. 8091 health 200과
  `Cache-Control: no-store`를 확인했다.
- 무인증·쿼리 토큰 요청은 401, 올바른 Bearer 요청만 200임을 실제 HTTP로 검증했다.
- 최초 스모크 중 query string이 기본 접근 로그에 남은 것을 발견해 해당 토큰을 즉시
  교체·무효화했고, 이후 Uvicorn access log를 비활성화했다. 현재 토큰은 plist·로그에 없다.
- Paper 8080, Tapnow 8082, Shareable API 8090, Ollama 11434와 동시에 실행된다.
- 실제 운영 watchlist 0건을 API로 읽었고 조회 전후 DB 행 수와 파일 해시가 동일했다.
- Telegram plist에는 `AI_AGENT_PRIVATE_API_ENABLED=1`만 넣었다. `list`는 API를 우선
  사용하고 장애 시 기존 DB로 폴백하며, add/remove는 기존 직접 쓰기를 유지한다.
- 일정 전체 5건·다가오는 일정 0건을 chat 범위별 API로 읽었다. 무인증 401, chat 범위
  누락 400, `chat_id` 응답 미포함, DB 행 수·파일 해시 무변경을 확인했다.
- Telegram 일정 `list/upcoming`은 API 우선·직접 DB 폴백이다. add/delete/complete와
  알림 조회·발송 상태 갱신은 기존 직접 경로를 유지한다.
- Paper portfolios/positions를 인증 라우트로 추가하고 Paper UI에 API 우선·DB 폴백을
  적용했다. Paper plist에는 enable 플래그만 있고 token은 없다.
- 운영 데이터 포트폴리오 1건·포지션 9건을 양쪽 경로로 확인했다. 무인증은 401이며,
  안정화 뒤 반복 조회 전후 DB 파일 해시·논리 스냅샷은 동일했다. 전체 테스트 1,173개 통과.
- slots/trades도 Paper UI API 우선·DB 폴백으로 전환했다. 운영 slots 4 / trades 164,
  UI 최근 trades 100, 무인증 401, 조회 전후 DB 파일 해시·논리 스냅샷 무변경을 확인했다.
  전체 테스트 1,182개 통과.
- IPO records/stats를 Paper UI API 우선·DB 폴백으로 전환했다. 운영 DB는 현재 양쪽 모두
  0건이며 빈 목록, 무인증 401, 조회 전후 DB 불변을 확인했다. 전체 테스트 1,192개 통과.
- 성과 계산을 순수 `paper_metrics.py`로 분리하고 performance/myquant-tags를 Paper UI
  API 우선·DB 폴백으로 전환했다. 운영 performance 4행·태그 0건, 직접 계산과 결과 동일,
  원시 거래 비노출, 무인증 401, 조회 전후 DB 불변을 확인했다. 전체 테스트 1,199개 통과.
- `private_write_contract.py`에 9개 operation allowlist, UUID 요청/승인 식별자,
  Idempotency-Key, expected version, 승인 상태 머신, 요청 fingerprint, 비민감 감사 이벤트
  검증을 순수 계층으로 고정했다. HTTP mutation 라우트와 저장 계층은 추가하지 않았다.
- `private_watchlist_write_store.py`에 기본 잠금·명시 DB 경로·승인 상태·멱등 결과·낙관적
  버전·감사 트랜잭션을 구현했다. 임시 DB 테스트 외에는 연결하지 않았다.
- `private_data_api.py`에 writer 주입 시에만 생성되는 다섯 고정 handler를 추가했다. 현재
  운영 bundle이 없어 main은 기본 앱을 만들며 mutation route 0개다.
- `private_data_api_client.py`에 생성자 기본값이 비활성인 watchlist submit/approve/reject/
  apply 어댑터와 엄격한 응답·오류 검증을 추가했다. Telegram은 atomic bundle이 검증된
  경우에만 이를 생성하며 현재는 연결되지 않는다.
- `telegram_write_identity.py`의 개인 ID 비노출 결정론적 식별자를 watchlist Telegram dispatch에
  전달했다. Private client 호출·운영 설정 변경은 없다.
- 테스트 주입 store/API/client에 preflight를 추가해 신규 version, pending 최초 version 복원,
  applied 결과 재사용, 식별자·operation 충돌을 검증했다.
- 기본 비활성 executor에서 신규·pending·approved·applied·rejected 흐름, 명시 승인,
  payload drift, direct DB fallback 금지를 임시 HTTP/SQLite로 검증했다.
- readiness gate에서 세 활성화 조건, 단일 writer, DB/백업 무결성·신선도·해시, 승인,
  무폴백, rollback을 검사하고 부분 활성화·복수 writer·손상 DB·위조/경로 이탈 백업을
  fail-closed로 검증했다.
- readiness 통과 뒤에만 발급되는 DB-bound activation permit을 API/client/executor 생성
  경계에 강제했다. 누락·직접 생성·일반 보고서 대체·DB 범위·peer permit 불일치를 모두
  네트워크·DB 접근 전에 거부한다.
- current/target writer, 고정 cutover/rollback 순서, 자동 복원 금지를 비실행 bundle로
  검증했다. 부분 활성화·복수 writer·순서 drift·stale backup은 계획 생성 전에 거부하고
  dry-run 결과에는 permit 객체를 남기지 않는다.
- verified backup clone에서 API write, legacy writer 재개, API 적용 데이터 보존, 별도 emergency
  restore, backup 불변을 rehearsal했다. workspace가 비어 있지 않거나 manifest가 stale·위조·
  경로 이탈이면 어떤 사본도 만들기 전에 거부한다.
- 단일 private JSON runtime bundle을 API/Telegram 코드에 연결했다. 키 부재 기본 비활성,
  불완전 플래그·schema/중복 key·권한/경로·backup root·bundle fingerprint 오류 전체 거부,
  두 프로세스 permit fingerprint 일치를 검증했다.
- 운영 root 차단·빈 private staging·exclusive write·stale/rollback 선검증을 갖춘 candidate
  생성기를 추가했다. fresh 운영 증거 candidate를 양 loader에서 검증했지만 설치하지 않았다.
- activation candidate/runbook 테스트를 포함한 전체 회귀 테스트 1,352개가 통과했다.

Telegram과 Paper는 `.env`에서 활성 token을 직접 로드하되 plist·로그에는 token을 넣지 않는다.
다음 단위는 운영 watchlist write cutover다. 서비스 중지·고정 파일 설치·`.env` 변경·서비스
재시작과 운영 DB mutation schema 생성이 포함되므로, runbook의 정확한 변경·rollback을 다시
제시하고 명시 승인을 받은 뒤에만 시작한다. 승인 뒤에도 전환 직전 fresh backup부터 반복한다.
