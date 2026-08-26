# Phase 2 데이터 API 서버화

> 상태: 2026-08-26 Shareable API + Private 조회 + watchlist API 쓰기 운영 중
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
| Private Data API | 8091 | 별도 Bearer 인증, watchlist·일정·Paper 허용 조회 |

8090·8091 서버는 실제 저장소의 `agent_services.sh`가 서로 다른 launchd 서비스로 관리하며
로그인 시 자동 시작한다. 둘 다 `127.0.0.1`에만 바인딩한다. worktree에서는 운영 프로세스를 별도로
띄우지 않고 TestClient 또는 소유권을 확인하는 스모크로 계약만 검증한다.

## 3. API 표면 1차

| 메서드·경로 | 역할 | 데이터 |
|---|---|---|
| `GET /health` | readiness와 경계 버전 | 민감 경로·행 수 미포함 |
| `GET /v1/shareable/instruments?query=...` | 공개 종목명 검색 | 최신 ticker map |
| `GET /v1/shareable/instruments/{ticker}` | 공개 종목·법인 메타데이터 | ticker map, DART corp code |
| `GET /v1/shareable/universes/{market}/latest` | 공개 지수 구성종목 최신 캐시 | KOSPI200/KOSDAQ150 종목군 |
| `GET /v1/shareable/market-indices/{index}/latest` | 공개 시장지수 최신 캐시 | KOSPI, 검증된 캐시가 있을 때 VKOSPI |
| `GET /v1/shareable/ohlcv/{ticker}/latest` | 최신 공개 OHLCV 캐시 | 허용된 시계열 필드만 |
| `GET /v1/shareable/factors/{market}/latest` | 최신 공개 팩터 스냅샷 | KOSPI/KOSDAQ fundamental·시가총액 allowlist |

Shareable API에는 Private watchlist·일정·paper·RAG·로그 라우트가 의도적으로 없다. ticker는 6자리 숫자만
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

- Shareable API에는 `/v1/private/*`가 없고, Private API에는 allowlist 밖 범용 파일/SQL
  라우트가 없다.
- HTTP 입력으로 파일명·절대경로·테이블명·SQL을 받을 수 없다.
- symlink·루트 이탈 파일은 읽지 않는다.
- Shareable 응답에 `chat_id`, 포트폴리오, 거래, 토큰, 개인 상태 필드가 없다.
- 기본 바인딩은 `127.0.0.1:8090`이며 비루프백 주소를 거부한다.
- 테스트는 임시 Shareable 저장소만 사용하고 실제 운영 데이터를 수정하지 않는다.

## 6. 현재 완료 범위

- `shareable_store.py`: 고정 Shareable 루트, 6자리 ticker 검증, 최신 캐시 선택,
  symlink/루트 이탈 방지, 반환 필드 allowlist.
- `data_api.py`: Shareable 전용 FastAPI 앱과 루프백 전용 실행 진입점.
- `data_api_client.py`: loopback URL만 허용하는 읽기 클라이언트. 오류 유형을 구분하고
  응답 ticker/name을 재검증한다.
- `invest_bot.py`: `AI_AGENT_DATA_API_ENABLED=1`일 때 종목명 해석을 API로 우선 조회한다.
  API 미기동·오류·캐시 미스 시 기존 경로로 안전하게 폴백한다.
- 실제 8090 HTTP 통합에서 health·삼성전자 검색/직접 조회·Private 404와 `invest_bot`
  이름/티커 해석을 확인한 뒤 임시 서버를 종료했다.
- `quant_bot.py`: 같은 플래그가 켜졌을 때 OHLCV 캐시를 API로 우선 읽는다. 요청 end_date와
  API `as_of`가 같을 때만 채택하며 불일치·장애 시 기존 디스크 캐시/pykrx로 폴백한다.
- 클라이언트는 첫 연결 장애 뒤 30초 cooldown으로 회로를 열어 200종목 스캔이 종목마다
  timeout을 반복하지 않게 한다.
- 실제 HTTP에서 SK하이닉스 최신 OHLCV 280행을 API로 읽었고, 서버 종료 후 같은 요청이
  디스크 캐시 280행으로 폴백하는 것을 확인했다.
- launchd `com.hyunjun.ai-agent.data-api`를 등록하고 Data API readiness 이후 Telegram과
  Paper를 시작하도록 순서를 고정했다. 두 소비 서비스에는
  `AI_AGENT_DATA_API_ENABLED=1`을 적용했다.
- `system_info.py`도 data-api/8090을 실제 상시 서비스로 안내한다.
- `kium_bot.py`: 당일 universe 캐시와 기준일이 일치하는 OHLCV를 API로 우선 읽는다.
  API 장애·404·오래된 universe·OHLCV 기준일 불일치 시 기존 디스크/pykrx 경로로
  폴백한다. 강제 갱신은 기존 외부 수집 경로를 유지한다.
- `market_data_collector.py`: 시장지수·universe·OHLCV·ticker-map·fundamental·시가총액
  외부 수집과 Shareable 캐시 원자적 쓰기의 단일 소유자. KOSPI와 OHLCV는
  FinanceDataReader 공개 소스를 사용하고, 전체 공개 데이터는 매일 16:20 launchd가 갱신한다.
  ticker-map과 팩터 원천은 KRX 응답을 공개 allowlist로 정제한다. 소비 봇은 파일 쓰기와
  직접 fundamental/시가총액 조회를 구현하지 않는다.
- factor 스냅샷은 비거래일 빈 시가총액 응답을 최대 7일 후퇴해 최신 거래일 기준으로
  저장한다. `quant_bot`은 API를 우선 사용하고 장애·미스일 때만 collector로 폴백한다.
- `invest_bot` OHLCV도 API 우선·collector 폴백으로 전환했다. collector는 FDR 응답에서
  date/open/high/low/close/volume만 보존하고 원자적으로 저장한다.
- VKOSPI는 기존 코드의 KRX `1003` 매핑을 제거했다. `1003`은 VKOSPI가 아니라 KOSPI
  중형주이며, 현재 검증된 무인증 VKOSPI 수집 소스가 없으므로 자동 수집하지 않는다.
  검증된 캐시가 없으면 API는 404, 소비자는 `None`으로 안전하게 축소 동작한다.
- 단위·API 보안 테스트: 실제 서버 기동 없이 임시 데이터로 검증.

공개 수집·읽기 경계, 독립 Private API 운영 골격, watchlist·일정과 Paper
포트폴리오·포지션·슬롯·거래·IPO·성과·마이퀀트 태그 조회 전환은 완료됐다. 멱등키·승인
상태·감사 로그·충돌 방지 쓰기 계약도 순수 검증 계층으로 고정했다. watchlist add/remove
저장 계층은 기본 잠금 상태와 명시적 임시 DB 경계에서 계약 검증을 마쳤다. writer를
명시 주입한 테스트 앱의 다섯 고정 POST handler로 HTTP 계약을 검증했다. 운영 main은 atomic
runtime bundle이 완전히 검증될 때만 같은 writer를 주입한다. 2026-08-26 승인된 전환에서
Private API를 단일 writer로 활성화했고 mutation route 5개와 `writes_enabled=true`를 운영한다. 기본
비활성 Private client 쓰기 어댑터와 엄격한 응답·오류 검증, Telegram caller identity
전달 계약과 동일 요청 재시도에 필요한 기존 intent 상태·최초 expected version preflight,
identity+preflight+client를 묶는 기본 비활성 consumer executor, 운영 활성화 전 readiness
gate, API/client/executor 공통 activation permit, 비실행 cutover/rollback dry-run, fresh
online backup과 격리 rollback rehearsal, 기본 비활성 atomic runtime bundle까지 완료했다.
activation candidate 생성기와 전환 runbook을 거쳐 Telegram add/remove는 API consumer만
사용하며 장애 시 direct SQLite 쓰기로 우회하지 않는다.

Private API 1차 계약은 [`PRIVATE_API_CONTRACT.md`](PRIVATE_API_CONTRACT.md)에 고정했다.
`private_data_api.py`는 8091·loopback·별도 Bearer token을 강제하고 문서 UI를 끄며,
인증된 status, watchlist, 일정 list/upcoming, Paper portfolios/positions/slots/trades/IPO/performance/myquant-tags 읽기 라우트를 운영한다. 64자 운영 토큰과 독립 launchd 전환은
완료했으며, token은 `.env`에만 보관하고 plist·로그에는 넣지 않는다. watchlist 저장소는
SQLite read-only 모드로 고정 경로만 읽고 HTTP 입력으로 경로·테이블·SQL을 받지 않는다.
일정 chat 범위는 숫자형 전용 헤더로 받고 응답에서는 제거한다.

## 7. 운영 스모크

`scripts/smoke_data_api.py`는 상시 등록 없이 다음을 한 번에 검증한다.

1. 기존 8090 API가 응답하면 소유권 불명으로 즉시 중단한다.
2. 자신이 시작한 `data_api.py`의 readiness를 최대 10초 기다린다.
3. health 경계, 종목 메타, `invest_bot`, `quant_bot`, `kium_bot`, Private 404를 확인한다.
4. 자신이 시작한 자식 프로세스만 SIGINT로 정상 종료한다.
5. 종료 후 `quant_bot`이 동일 OHLCV를 디스크 캐시에서 읽는지 확인한다.

SIGINT가 5초 안에 끝나지 않을 때만 terminate, 추가 3초 뒤에도 남을 때만 해당 자식
프로세스를 kill한다. 기존 프로세스나 포트를 PID로 추정해 종료하지 않는다.

2026-08-22 실제 실행 결과 health 정상, 삼성전자 메타, SK하이닉스 OHLCV API 280행,
Private 경로 404, 서버 종료 후 디스크 폴백 280행, `server_stopped=true`를 확인했다.
전체 회귀 테스트는 1,082개 통과했다.

## 8. 운영 전환 결과

2026-08-22 승인 후 다음을 실제 로컬 환경에 적용했다.

- Data API: `com.hyunjun.ai-agent.data-api`, `127.0.0.1:8090`, health HTTP 200.
- 소비자: Telegram과 Paper plist에 `AI_AGENT_DATA_API_ENABLED=1` 적용 후 재시작.
- 시작 순서: Data API 로드 → readiness 확인 → watch/Telegram/Paper 로드.
- 보안 경계: `storage_layout=private-v1`, `boundary=shareable-only`, Private 경로 HTTP 404.
- 실제 소비 검증: 디스크 폴백 경로를 막고 삼성전자 메타와 SK하이닉스 OHLCV 280행을
  API에서 조회했다.
- kium 전환 검증: 최신 universe API가 보유 기준일을 명시하고, `kium_bot`은 당일
  캐시만 채택한다. pykrx 폴백을 막은 실운영 검사에서 SK하이닉스 OHLCV 280행을 API로
  읽었다.
- 시장지수 전환: KOSPI 363행을 수집해 실제 최신 거래일 `20260821`을 보존했고,
  collector 폴백을 막은 운영 검사에서 `kium_bot`이 8090 API만 사용했다. 매일 16:20
  `com.hyunjun.ai-agent.market-data-collector`가 갱신한다.
- VKOSPI 안전 교정: 잘못된 `1003` 매핑을 제거했으며 검증된 소스가 생길 때까지
  자동 수집하지 않는다. 운영 API는 거짓 대체값 대신 HTTP 404를 반환한다.
- 수집 경계 확장: universe/OHLCV의 외부 조회와 원자적 Shareable 쓰기를
  `market_data_collector.py`로 이동했다. 실제 SK하이닉스 14거래일을 격리된 임시 경로에
  저장했고, KRX 갱신 실패 시 운영 API의 마지막 universe 199종목을 유지하는 것을 확인했다.
- ticker/factor 경계 확장: ticker-map 조회·검증·원자적 쓰기와 fundamental/시가총액
  조회·필드 정제를 collector로 이동했다. pykrx 외부 출력에서 계정 식별자가 로그에
  노출되지 않도록 차단하고 빈 ticker-map 캐시도 금지했다.
- KRX 인증 복구: 비밀번호 갱신 후 ticker-map 2,687종목, KOSPI fundamental 890/
  시가총액 917종목, KOSDAQ fundamental 1,750/시가총액 1,770종목을 실제 수집했다.
  factor 기준일은 최신 거래일 `20260821`로 자동 후퇴했다.
- factor API 운영 전환: collector를 막은 검사에서 `quant_bot`이 8090 API만으로 KOSPI
  fundamental 890·시가총액 917종목을 읽었다.
- invest OHLCV 전환: 실제 삼성전자 14거래일 full OHLCV 수집과, collector를 막은 상태에서
  운영 API의 기존 280행으로 지표 계산 성공을 확인했다.
- universe 잔여 제약: KRX 구성종목 엔드포인트는 인증 복구 후에도 빈 응답이므로 새 캐시를
  쓰지 않고 운영 API의 마지막 KOSPI200 199종목 스냅샷을 계속 사용한다.
- 장애 검증: 사용할 수 없는 API 주소를 주입했을 때 같은 두 소비가 기존 캐시로 정상
  폴백했다.
- 공존 확인: Paper 8080, Tapnow backend 8082, Data API 8090, Private API 8091,
  Ollama 11434 유지.
- Private API 골격: 8091 loopback·별도 64자 Bearer token·기존 서비스 포트 거부·문서 UI
  비활성화·no-store를 구현하고 `com.hyunjun.ai-agent.private-data-api`로 운영 등록했다.
  health 200, 무인증·쿼리 토큰 401, 올바른 Bearer 200을 실제 HTTP로 확인했다.
  plist에는 token 이름과 값이 없으며 `.env`는 Git 비추적·권한 600이다.
- Private watchlist 읽기: `GET /v1/private/watchlist`는 ticker·name·created_at만 반환한다.
  Telegram list는 `AI_AGENT_PRIVATE_API_ENABLED=1`에서 API를 우선 사용하고 장애 시 기존
  로컬 DB로 폴백한다. add/remove는 아직 직접 경로다.
- 운영 DB 0건 조회 전후 행 수와 파일 해시가 동일했고, 로컬 DB 폴백을 막은 검사에서도
  Telegram 실행 경로가 8091 API만으로 응답했다. `writes_enabled=false`를 유지한다.
- Private 일정 조회: 전체·다가오는 일정 경로를 분리하고 숫자형 chat header를 필수화했다.
  운영 전체 5건/다가오는 일정 0건, 무인증 401, chat 누락 400, 응답 `chat_id` 제외,
  DB 행 수·파일 해시 무변경을 확인했다.
- Telegram 일정 list/upcoming은 API 우선·직접 DB 폴백이다. add/delete/complete와 알림
  스케줄러의 읽기·상태 쓰기는 이번 전환에서 제외했다.
- Private Paper 읽기: `GET /v1/private/paper/portfolios`와 `/positions`는 고정
  `paper.db`를 `mode=ro`+`query_only`로 읽고 기존 UI 응답의 4개/9개 필드만 반환한다.
  HTTP 쿼리는 받지 않으며 slot 필터는 클라이언트가 로컬에서 적용한다.
- Paper UI의 portfolios/positions/slots/trades는 `AI_AGENT_PRIVATE_API_ENABLED=1`에서
  API 우선, 장애 시 기존 DB 폴백이다. 매수·매도·IPO 쓰기는 아직 직접 경로다.
- 운영 스모크에서 Private API와 Paper UI가 각각 포트폴리오 1건·포지션 9건을 반환했고,
  무인증은 401이었다. 안정화 뒤 반복 조회 전후 DB 파일 해시와 논리 스냅샷이 동일했다.
  서비스 재기동 자체는 기존 `ensure_seed`/`ensure_slot` 초기화 때문에 SQLite 파일 해시를
  한 번 바꿨지만 행 수는 portfolios 1 / slots 4 / positions 9 / trades 164로 유지됐다.
- 최신 포트 확인에서 Paper 8080, Shareable 8090, Private 8091, Ollama 11434는 정상이다.
  Tapnow 8082는 현재 리슨하지 않아 별도 프로젝트 운영 상태 확인이 필요하다.
- Private slots/trades 읽기는 슬롯 dashboard의 8개 필드와 거래 11개 필드만 반환한다.
  API는 query를 받지 않으며 slot/limit은 클라이언트가 로컬 적용한다. 최대 슬롯 20·거래
  1,000건으로 제한한다.
- 운영 스모크에서 Private API slots 4 / trades 164, Paper UI slots 4 / 최근 trades 100,
  무인증 401을 확인했다. 조회 전후 DB 파일 해시와 논리 스냅샷은 동일했다.
- IPO records/stats는 최신 기록 500건과 등급 통계 20건의 고정 allowlist로 전환했다.
  factors는 JSON 객체인지 서버·클라이언트가 재검증하며 subscribe/close 쓰기는 직접 경로다.
- 운영 DB의 IPO 기록·통계가 현재 0건인 빈 목록 계약, 무인증 401, Paper UI 동일 응답과
  조회 전후 DB 파일 해시·논리 스냅샷 무변경을 확인했다.
- `paper_metrics.py` 순수 계산 계층을 추가해 기존 `paper_db.performance_stats()`와 Private
  read-only 저장소가 동일한 FIFO 성과 산식을 공유한다. myquant-tags도 내부에서 전체
  거래를 계산하지만 API에는 태그별 n/pnl/win_rate와 표시 문자열만 반환한다.
- 실제 운영에서 기존 직접 계산과 새 read-only 계산이 완전히 같았다. API/Paper UI 모두
  performance 4행·태그 0건이며 원시 거래 비노출, 무인증 401, 조회 전후 DB 불변을 확인했다.
- Paper UI의 일반 런타임 읽기는 API 우선 전환을 마쳤다. 남은 직접 접근은 매수·매도·IPO
  같은 쓰기, Telegram 승인 전제 조회/쓰기, 일정 알림 스케줄러, 내부·오프라인 분석 예외다.
- Private 쓰기 사전 계약: 9개 고정 operation만 허용하고 UUID request/approval ID,
  16~128자 Idempotency-Key, expected version, `pending→approved→applied` 1회 상태 전이,
  요청 fingerprint, payload를 제외한 감사 이벤트를 순수 코드로 검증한다. 임의 SQL·DB
  경로·table·token 필드는 중첩 payload에서도 거부한다. API mutation 라우트는 0개다.
- watchlist 격리 쓰기 저장소: DB 경로를 반드시 주입하고 `writes_enabled=True`를 명시한
  경우만 연결한다. 임시 DB에서 pending/approved/applied, add/remove, 동일 요청 replay,
  idempotency/version/approval 충돌, payload 없는 감사 이벤트를 검증했다. 운영 DB와 8091
  서비스에는 연결하지 않았다.
- watchlist 비활성 HTTP 계약: 테스트 앱에만 intent/approve/reject/apply 네 고정 POST를
  주입했다. Bearer·no-query, 요청 크기, 중복 JSON key, UUID/Idempotency 헤더, operation
  allowlist, HTTP 400/401/409/413/422/503 매핑을 검증했다. 기본 앱과 운영 CLI에는 write
  route/활성화 옵션이 없다.
- watchlist 비활성 client: 생성자에서 명시 활성화하지 않으면 쓰기 전에 차단한다. 임시
  HTTP 앱에서 request/approval UUID 결합, submit/approve/reject/apply, replay와 401/409/
  413/422 매핑, 8개 응답 필드 allowlist를 검증했다. 환경변수와 소비자 연결은 없다.
- Telegram write identity: update/chat/user/message/action 좌표를 결정론적 request UUID,
  approval UUID, Idempotency-Key로 변환해 add/remove dispatch에 전달한다. 원문·종목·Telegram
  숫자 ID는 identity에 남기지 않으며 현재 `watchlist_bot`은 형식만 검증하고 기존 직접
  쓰기를 유지한다. client/환경변수/plist 연결은 없다.
- watchlist preflight: 테스트 주입 store/API/client에서 body 없이 operation·멱등키·두 UUID를
  받아 신규 current version 또는 기존 최초 expected version/state/result를 반환한다. 한
  SQLite snapshot으로 읽고 payload·원문 key·감사 변경은 없다. 기본 앱에는 경로가 없다.
- watchlist 기본 비활성 consumer executor: executor와 client의 이중 활성화 및 호출별 명시
  승인을 요구한다. preflight 뒤 신규·pending·approved·applied를 이어서 처리하고 rejected/
  expired를 중단한다. 부분 완료 재시도와 payload drift를 검증했으며 API 실패 시 직접 DB로
  우회하지 않는다. Telegram 운영 경로에는 executor를 주입하지 않았다.
- Private 쓰기 readiness gate: 세 활성화 조건, 단일 `private-data-api` writer, 허용된 private
  `assistant.db`/`paper.db` 무결성, 최근 24시간 이내 복구 검증 백업의 권한·해시·schema·행 수,
  호출별 승인, direct DB 무폴백, rollback 검증을 고정 code로 판정한다. 손상·위조·부분
  활성화는 fail-closed이며 환경변수·plist·서비스·운영 DB를 바꾸지 않는다.
- 공통 activation permit: readiness 전체 통과 뒤에만 DB/backup fingerprint-bound permit을
  발급하고 write API 앱, client, executor가 모두 요구한다. permit 누락·직접 생성·DB scope·
  client/executor fingerprint 불일치를 생성 단계에서 거부한다. 운영 CLI에는 발급·주입
  경로가 없다.
- cutover/rollback dry-run: current write stack 비활성·단일 legacy writer와 target 단일 API
  writer를 구분하고 cutover 6단계·rollback 5단계 순서를 고정한다. 자동 DB 복원과 순서
  drift를 거부하며 결과에는 permit 객체나 실행 기능이 없다. 운영 read-only 결과는 stale
  backup과 미검증 rollback 때문에 ready=false이고 DB는 불변이었다.
- fresh backup/rollback rehearsal: 운영 `paper.db`, `assistant.db`를 온라인 백업·메모리 복구
  검증했고 원본은 불변이었다. fresh `assistant.db` clone에서 API write→legacy writer 재개와
  별도 emergency restore 일치를 확인했다. 산출물은 보존하고 자동 복원·서비스 변경은 하지
  않았다. 재평가한 cutover dry-run은 ready=true다.
- atomic runtime bundle: 단일 600 private JSON과 bundle ID 없이는 API writer/client/executor
  어느 것도 만들지 않는다. 고정 DB·승인 backup root·세 플래그·무폴백·rollback·자동 복원
  금지·permit fingerprint를 양 프로세스에서 재검증한다. 운영 bundle은 600 파일과 `.env`의
  단일 경로 키로 활성화하며 개별 쓰기 플래그는 두지 않는다.
- activation candidate/runbook: 운영 root와 다른 빈 700 staging에만 600 candidate를 exclusive
  생성하고 두 runtime loader가 같은 ID/fingerprint를 확인한다. 전환에 사용한 candidate는
  `/private/tmp/ultron-private-write-candidate-7jrmas7o/`에 보존했다. runbook은
  승인 전 재백업부터 cutover·service rollback·수동 emergency restore 순서를 고정한다.
- 운영 watchlist write cutover: fresh snapshot `20260826T005056+0900`과 격리 rollback rehearsal
  성공 뒤 bundle `528fcb275432…`를 설치했다. 8091은 인증 상태에서 쓰기 활성·5개 mutation
  route를 제공하고 Telegram은 같은 bundle의 executor로 정상 기동했다. 존재하지 않는
  `000000` 제거를 pending→approved→applied 후 동일 identity로 replay해 `removed=false`,
  watchlist 0건·resource version 0 불변, intent 1건·audit 3건을 확인했다. 실제 사용자 종목은
  추가하거나 삭제하지 않았다. macOS 한글 경로 NFC/NFD 차이는 동일 고정 경로로 정규화해
  fail-closed 검증을 유지한다.
- 실제 Telegram E2E: 사용자가 `삼성전자(005930)` 추가와 제거 메시지를 차례로 보냈고 두
  API intent가 각각 pending→approved→applied로 전이됐다. add는 `created=true`와 version 1,
  remove는 `removed=true`와 version 2를 만들었으며 최종 watchlist는 다시 0건이다.
  Telegram/Private API는 정상 PID를 유지해 합성 identity가 아닌 실제 Telegram update의
  양방향 mutation까지 검증했다.
- 일정 쓰기 순수 계약: `schedule.add/delete/complete`를 실제 라우터의 `rrule_freq/byday/until`
  및 다중 사전 알림 형태와 맞췄다. chat ID는 payload/audit에 두지 않고 scope hash를 intent
  fingerprint에 결합해 다른 chat의 동일 요청을 분리한다. DB·HTTP·runtime에는 연결하지 않아
  운영 일정 5건과 기존 writer 소유권은 그대로다.
- 일정 격리 쓰기 저장소: 공통 scope-bound 승인 상태 머신과 schedule 적용부를 분리했다.
  임시 SQLite에서 chat별 version, pending→approved→applied, submit/apply replay, stale intent
  만료, reject 무변경, 다른 chat의 preflight/승인/삭제·완료 차단을 검증했다. audit에는 scope
  hash만 남기고 chat ID·제목·payload·원문 멱등키를 제외한다. 운영 DB에는 scoped table이
  0개이므로 아직 schema·route·writer 변화가 없다.
- 일정 기본 비활성 HTTP 계약: schedule writer와 동일 DB scope의 activation permit을 명시
  주입한 테스트 앱에만 intent/preflight/approve/reject/apply 5개 POST를 등록한다. 모든 경로는
  Bearer·no-query·`X-AI-Agent-Chat-ID`·UUID·멱등키·요청 크기·중복 JSON 경계를 공유한다.
  schedule-only 앱은 정확히 5개, watchlist와 함께 주입할 때는 10개 mutation route다.
  운영 8091은 schedule write capability가 없고 해당 경로가 404이며 DB scoped table도 0개다.
- 일정 기본 비활성 Private client: 명시 활성화와 동일 activation permit을 가진 테스트 객체만
  schedule submit/preflight/approve/reject/apply를 호출한다. chat scope는 body/query가 아닌
  전용 헤더로 매 요청에 결합하고, 응답은 operation별 고정 필드만 허용한다. 임시 HTTP/SQLite에서
  승인 전 무변경, add/complete/delete, replay, cross-chat 충돌, 인증 실패를 검증했으며 운영
  Telegram·8091·DB에는 연결하지 않았다.
- Telegram 일정 write identity: update/chat/user/message/action을 일정 도메인 전용 UUIDv5와
  해시 멱등키로 결정론적으로 변환한다. identity 표현에는 원본 Telegram 숫자 ID와 일정 payload가
  없으며 watchlist identity와 충돌하지 않는다. add/delete/complete만 생성하고 `schedule_bot`은
  action 일치를 DB 실행 전에 검증한다. 아직 일정 client/executor에는 연결하지 않았다.
- 일정 기본 비활성 consumer executor: executor/client 이중 활성화와 동일 activation permit,
  호출별 명시 승인을 통과해야만 preflight→submit→approve→apply를 수행한다. 임시 HTTP/SQLite에서
  pending/approved 재개, applied replay, reject·payload drift·cross-chat 차단과 add/complete/delete를
  검증했다. `schedule_bot`에 executor가 주입되면 직접 SQLite 분기를 사용하지 않고 API 오류에도
  폴백하지 않는다. 운영 Telegram runtime에는 주입하지 않았다.
- 일정 cutover readiness/dry-run: 사용자 `add/delete/complete`는 Private API, 알림 발송 상태와
  반복 일정 진전은 Telegram notifier가 소유하도록 operation 집합을 분리했다. 전환·롤백은
  watchlist writer를 중단하지 않고 일정 writer만 바꾸며 notifier를 계속 유지한다. fresh backup,
  permit, 명시 승인, 무폴백, writer 소유권, 고정 순서와 자동 복원 금지를 부작용 없는 보고서로
  검증한다. 상세 순서는 `docs/SCHEDULE_WRITE_CUTOVER_DRY_RUN.md`에 고정했다.
- 일정 rollback rehearsal: 운영 `assistant.db`의 새 온라인 백업을 만든 뒤 보존되는 격리 workspace
  사본에서만 API 일정 추가, notifier의 발송 상태 기록·반복 진전, legacy 사용자 writer 재개,
  API 결과 보존과 별도 emergency restore의 baseline 일치를 확인했다. 운영 DB의 논리 서명과
  일정 5건·watchlist 0건·scoped table 0개는 불변이었다.
- 일정 atomic runtime bundle: 기존 운영 v1 bundle은 schedule=false로 하위 호환하고, v2는
  `schedule:{enabled:false}`를 기본으로 한다. enabled=true일 때만 일정 API/client/executor와
  notifier owner·operation·동일 DB·무폴백 증거를 combined bundle fingerprint로 검증해 API와
  Telegram stack을 함께 만든다. 부분 활성화·notifier drift·fingerprint 불일치는 시작 전에
  거부한다. 운영 v1 bundle은 실제 재검증에서도 schedule=false였다.
- 비설치 일정 activation candidate: fresh rehearsal manifest에 묶인 v2 combined bundle을 운영
  Private root와 다른 700 staging에 600 파일로 생성하고 API/Telegram runtime loader 양쪽 조건을
  재검증했다. 후보는 installed=false다. writer 중첩 방지를 위해 cutover는 Telegram API-only
  consumer를 먼저 올린 뒤 Private API 일정 writer를 올리고, rollback은 반대 순서로 고정했다.
  상세 절차는 `docs/SCHEDULE_WRITE_CUTOVER_RUNBOOK.md`를 따른다.
- 운영 일정 write cutover·실제 E2E: 영구 backup `20260826T193342+0900`에 묶인 v2 bundle
  `f0c651775aa4…`를 설치하고 Telegram→Private API 순서로 재시작했다. 사용자 일정 mutation은
  API-only, notifier 내부 mutation은 Telegram 소유로 유지한다. 격리 무데이터 replay와 실제
  Telegram 테스트 일정 add→`#8` delete를 모두 승인 상태 머신으로 적용했고 chat version은
  `0→1→2`, 최종 일정/사전 알림은 5/4건으로 복원됐다.
- Paper buy/sell 기본 비활성 계약·저장소: API scope는 이름 별칭 대신 양의 정수 `slot_id`로
  고정하고 ticker·수량·가격·수수료·본문을 도메인별 정규화한다. 명시 활성화된 임시 DB에서만
  pending→approved→applied, 슬롯별 version/replay/stale/cross-slot 차단과 buy 가중평균·자본
  차감, partial/full sell·자본 복원, 실패 원자성을 검증했다. 운영 Paper DB에는 scoped table이
  없고 8091에도 Paper write capability가 없다.
- Paper buy/sell 기본 비활성 HTTP 계약: 별도 `paper.db` readiness permit과 writer를 명시
  주입한 테스트 앱에만 intent/preflight/approve/reject/apply 5개 POST를 등록했다. 인증,
  no-query, canonical slot header와 payload slot 일치, UUID/멱등키, 중복 JSON·크기 제한,
  cross-slot과 잔고 충돌을 검증했다. readiness가 허용하는 DB 이름은 `assistant.db`와
  `paper.db`뿐이며 각각 자기 절대 경로와 검증 백업에 permit이 결합된다. 운영 8091은 Paper
  write capability 없음·경로 404이고 운영 DB는 integrity ok·scoped table 0개·기존 행 수를
  유지한다.
- Paper buy/sell 기본 비활성 Private client: 일반 쓰기 스위치와 분리된 Paper 활성화 플래그,
  `paper.db` 절대 경계에 결합된 별도 permit, 명시 DB 경로를 모두 요구한다. 모든 요청에
  canonical slot header를 강제하고 payload/응답 slot, 응답 필드 allowlist, buy total cost와
  sell proceeds 계산을 검증한다. 임시 HTTP/SQLite에서 전체 승인 흐름·replay·reject·cross-slot·
  인증·잔고 충돌을 검증했으며 운영 runtime·Paper UI·Telegram에는 주입하지 않았다.
- Paper direct writer 소유권·identity: AST 전수 대조로 Paper UI buy/sell, Telegram 장중
  sell-only, 키움·콴텍 승인 batch, operator CLI의 6개 direct 경계를 고정했다. 정상 운영
  target DB writer는 Private API 하나이며 CLI direct는 rollback-only 퇴역 대상이다. UI와
  Telegram 4개 caller identity는 actor/event/batch ordinal을 hash해 결정론적 UUID·멱등키를
  만들고 원문 좌표·slot·ticker·수량·가격·본문을 보존하지 않는다. payload drift는 같은
  identity의 fingerprint conflict로 처리하도록 payload를 identity 입력에서 제외했다.
- Paper 기본 비활성 consumer executor: 전용 Paper permit이 일치하는 client/executor에서만
  preflight→submit→approve→apply를 수행한다. Paper UI·키움·콴텍은 명시적 사용자 승인,
  Telegram intraday는 sell-only 정책 승인을 요구하며 두 승인 모드를 혼용하면 fail-closed다.
  임시 HTTP/SQLite에서 buy/sell, pending/approved 재개, applied replay, reject, payload drift,
  cross-slot 사전 차단, 잔고 실패 원자성, API 장애 시 direct DB fallback 0을 신규 11개 테스트로
  확인했다. 운영 runtime에는 import·주입하지 않아 8091 Paper write route는 404이고 운영
  `paper.db`는 1/4/4/169, scoped table 0, integrity ok를 유지한다. 검증 중 외부 서비스 재시작과
  함께 raw SHA-256이 `7a94a9ddd021…→adcb98c80723…`로 바뀌어 논리 불변만 확인했고
  byte-for-byte 불변은 주장하지 않는다.
- Private API는 query string이 기본 접근 로그에 기록되는 경로를 차단하기 위해 Uvicorn
  access log를 끈다. 발견 당시 token은 즉시 교체·무효화했고 현재 token은 plist·로그에 없다.
- 전체 회귀 테스트: 1,606개 통과. (Paper consumer 신규 11개와 병행 변경 테스트 포함)

현재 전환은 코드·설정 변경 상태이며 아직 커밋하지 않았다. Shareable 긴급 롤백은
Telegram/Paper plist의 `AI_AGENT_DATA_API_ENABLED`, Private 조회 롤백은 Telegram/Paper의
`AI_AGENT_PRIVATE_API_ENABLED`를 제거하거나 `0`으로 바꾸고 해당 소비 서비스를 재시작하면
된다. Private watchlist 쓰기는 API 장애 시 direct DB 폴백이 없으며,
`PRIVATE_WRITE_CUTOVER_RUNBOOK.md`의 writer 소유권 rollback 순서를 따른다. 일정 쓰기는
`SCHEDULE_WRITE_CUTOVER_RUNBOOK.md`를 따른다. 다음 서버화 쓰기 단위는 Paper UI·Telegram·
intraday writer 소유권을 비중첩 operation으로 고정하는 readiness/rollback 경계다.
