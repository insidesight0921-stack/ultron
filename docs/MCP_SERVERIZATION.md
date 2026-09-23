# Phase 3 Shareable MCP 서버화

> 최종 실측 갱신: 2026-08-27 (Asia/Seoul)
> 현재 상태: stdio 최소 서버·운영 Shareable API·공식 MCP Inspector E2E·Codex host 등록 및
> 새 Codex 작업 실제 도구 호출·읽기 전용 승인 정책 이중 고정·로컬 MVP 완료 감사 통과

## 1. 현재 경계

```text
[MCP host] -- stdio --> scripts/shareable_mcp.py
                         |
                         +-- loopback HTTP --> 127.0.0.1:8090 Shareable Data API
```

- MCP subprocess는 새 포트를 열지 않는다.
- 데이터 파일·SQLite를 직접 읽지 않고 `ShareableDataClient`의 고정 8090 API만 사용한다.
- 도구 자동 탐색을 쓰지 않고 코드의 명시 allowlist와 실제 등록 집합이 다르면 시작을 거부한다.
- 8090이 내려가거나 응답 검증이 실패하면 도구는 fail-closed 오류를 반환한다.
- stdout은 MCP 프로토콜 전용이며 일반 출력·로그를 쓰지 않는다.
- 6개 도구 모두 MCP `readOnlyHint=true`, `openWorldHint=false`를 명시한다. 이는 host 판단용
  힌트이며 단독 보안 경계로 신뢰하지 않고 Codex `enabled_tools` allowlist와 함께 사용한다.

## 2. 노출 도구

| 도구 | 입력 | 공개 출력 |
|---|---|---|
| `get_instrument` | 6자리 `ticker` | ticker/name/corp_code/as_of |
| `search_instruments` | `query`, 1~50 `limit` | ticker/name/as_of 목록 |
| `get_latest_ohlcv` | 6자리 `ticker` | date/open/high/low/close/volume |
| `get_latest_market_index` | allowlisted `index_name` | date/close |
| `get_latest_universe` | allowlisted `market` | ticker/name 목록 |
| `get_latest_fundamentals` | `KOSPI/KOSDAQ market`, 6자리 `ticker` | 해당 종목 BPS/PER/PBR/EPS/DPS/DIV·시가총액 |

항상 비노출: Private watchlist·일정·포트폴리오·포지션·거래·수익률, vault/RAG,
파일·경로·SQL, `.env`, 로그, 추천·국면 판단·발송 이력.

MCP는 8090의 검증된 응답도 그대로 통과시키지 않는다. 각 도구가 위 표의 공개 필드만 다시
선별하므로 Shareable API 응답에 예상하지 못한 필드가 추가돼도 MCP 출력에는 포함되지 않는다.

## 3. 실측 증거

- 공식 Python SDK `mcp 2.1.1`, protocol `2026-07-28`.
- 실제 stdio child process server name: `ultron-shareable`.
- 실제 tool list: 위 6개와 정확히 일치, Private/file/path/SQL tool 0개.
- 실제 운영 8090을 통한 `005930` 조회: 삼성전자·공개 corp_code/as_of 정상.
- 공식 MCP Inspector CLI `tools/list` 5개 일치, `tools/call get_instrument ticker=005930`
  structuredContent 일치·`isError=false`.
- 운영 Shareable 응답 형태: OHLCV 291행, KOSPI index 363행, KOSPI200 universe 199종목.
- MCP/Data API·완료 감사 관련 검증과 shared worktree 전체 **1,732 tests** 통과.
- 설치 후 `pip check`: broken requirement 0.
- Paper/Private DB는 MCP 테스트 전후 논리값·integrity 불변.
- Codex 전역 설정에 `ultron-shareable`을 STDIO·enabled로 등록했다. `codex mcp get/list`에서
  Python·server script 절대 경로, 환경변수 없음, 인증 없음이 확인됐다.
- 등록 직후 운영 `http://127.0.0.1:8090/health`는 `status=ok`, `boundary=shareable-only`다.
- 새 일회성 Codex CLI 작업이 실제 `ultron-shareable/get_instrument(005930)`을 호출해
  삼성전자와 `ticker/name/corp_code/as_of` 4개 필드만 반환했다.
- 비대화형 첫 호출은 사용자 승인 입력을 받을 수 없어 중단됐고, 재검증 명령 한 번에만
  `default_tools_approval_mode="approve"` override를 사용했다. 전역 자동승인 설정은 남기지 않았다.
- 보강 후 실제 stdio `tools/list`에서 6개 모두 `readOnlyHint=true`, `openWorldHint=false`를
  반환했다. Codex 설정은 같은 6개 `enabled_tools`와 `default_tools_approval_mode="writes"`를
  사용하며, 별도 override 없는 새 Codex 작업의 삼성전자 조회가 성공했다.
- Shareable API의 유일한 미노출 공개 endpoint인 시장 전체 factor snapshot은 KOSPI 기준
  fundamentals 890·market cap 917종목으로 과대하다. 대신 `get_latest_fundamentals`가 요청한
  한 ticker만 반환하며 새 Codex 작업에서 삼성전자 공개 지표 6개·시가총액을 확인했다.

## 4. 실행·검증

직접 stdio 실행은 host가 child process로 시작할 때 사용한다.

```bash
$PROJECT_PATH/.venv/bin/python \
  $PROJECT_PATH/scripts/shareable_mcp.py
```

터미널에서 실행하면 아무 출력 없이 입력을 기다리는 것이 정상이다. 재현 가능한 실측은 다음
smoke로 수행한다.

```bash
cd $PROJECT_PATH
PYTHONPATH=scripts .venv/bin/python scripts/smoke_shareable_mcp.py
```

## 5. Codex host 등록

2026-08-27 사용자 승인에 따라 Codex 전역 설정에 한 곳만 등록했다. ChatGPT desktop app,
Codex CLI, IDE extension은 같은 Codex host 설정을 공유한다.

```bash
codex mcp add ultron-shareable -- \
  $PROJECT_PATH/.venv/bin/python \
  $PROJECT_PATH/scripts/shareable_mcp.py
```

실측 결과는 `enabled=true`, `transport=stdio`, `env=-`, 위 두 절대 경로 일치다. Codex 쪽에도
`enabled_tools`를 같은 6개로 고정하고 `default_tools_approval_mode="writes"`를 적용했다. 서버가
각 도구를 읽기 전용으로 명시하므로 공개 조회는 반복 승인 없이 실행되며, allowlist 밖의 도구는
host에서 노출되지 않는다. 파일 쓰기 금지·ephemeral 새 Codex CLI 작업에서 별도 승인 override
없이 실제 `get_instrument(005930)` 호출이 성공했다. 현재 실행
중인 Codex 작업의 도구 목록은 동적으로 바뀌지 않으므로 이 작업 자체에는 도구가 새로 생기지
않지만, 다음 desktop 새 작업부터 같은 설정을 읽는다.

설정만 되돌리는 비파괴 rollback 명령은 아래와 같다. **승인 없이 실행하지 않는다.**

```bash
codex mcp remove ultron-shareable
```

## 6. 로컬 MVP 완료 감사

아래 감사는 파일·DB를 수정하지 않고 Codex 설정과 실제 STDIO server를 함께 검사한다.

```bash
cd $PROJECT_PATH
PYTHONPATH=scripts .venv/bin/python scripts/audit_shareable_mcp.py
```

2026-08-27 실측은 `ready=true`다. command/script는 macOS 한글 NFC/NFD 문자열이 아니라 실제
파일 identity로 일치했고, enabled tools 6개·writes 승인 정책·env 미전달·stdio-only,
protocol `2026-07-28`, 6개 read-only/closed-world annotation, 삼성전자 instrument/fundamentals
공개 응답이 모두 통과했다.

추가 도구 판정은 다음과 같다.

- 채택: 단일 종목 `get_latest_fundamentals` 1개.
- 보류: 시장 전체 factor snapshot(과대 응답), RAG resource/prompt(현재 공개 use case 없음),
  Streamable HTTP(인증·원격 위협모델 선행 필요).
- 영구 제외: Private/watchlist/schedule/portfolio/trade/RAG 원문/file/path/SQL.

따라서 **Phase 3 로컬 STDIO MVP는 완료**다. 원격 Streamable HTTP는 완료 조건이 아니라 별도
확장 과제로 관리한다.

원격 Streamable HTTP, 인증, 외부 공개는 현재 범위가 아니다. 필요해도 stdio 로컬 검증과
Private 비노출 검증을 완료한 뒤 별도 설계한다.
