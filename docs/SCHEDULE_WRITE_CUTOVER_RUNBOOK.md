# 일정 쓰기 운영 전환 runbook

> **운영 전환 완료 (2026-08-26)**: 승인된 순서대로 v2 bundle을 설치하고
> Telegram→Private API 순서로 재시작했다. 이 문서의 cutover/rollback 순서는 계속 유효하다.

## 현재 기준선

- 운영 bundle: `data/private/private-write-activation.json`
- 운영 bundle ID: `f0c651775aa4…` (v2, schedule 활성)
- 운영 backup: `20260826T193342+0900` (paper/assistant 온라인 백업·메모리 복구 검증)
- 운영 상태: watchlist·schedule write 활성, 8091 `schedule:write-contract` 확인
- 운영 DB: 일정 5건, 사전 알림 4건, watchlist 0건/version 2
- 검증 scope: `private_scoped_resource_versions` 1행,
  `private_scoped_write_intents` 1행, `private_scoped_write_audit` 3행
- 운영 writer:
  - watchlist 사용자 mutation: `private-data-api`
  - 일정 사용자 mutation: `private-data-api`
  - 일정 알림 내부 mutation: `telegram-schedule-notifier`

## 설치·롤백 산출물

- 설치 후보 보존: `/private/tmp/ultron-schedule-candidate-operational-BKWKGk/private-write-activation.json`
- 이전 v1·설치 v2 보존: `/private/tmp/ultron-schedule-cutover-operational-PWTtTX/`
- 이전 v1 bundle: `private-write-activation.v1.json` (600)
- 설치 v2 bundle: `private-write-activation.v2.json` (600)
- v2 bundle ID: `f0c651775aa455499fd88dc4eef7cc06b0605b99c158b52f419a67711e4175bb`
- activation fingerprint: `5515f11433bed4f788c011b1ba4a7c5f7e332d9b70d185a4128327fa533e483f`
- 설치 bundle SHA-256: `60dd84d6d38ccb4507788f34ef1613e494ef35c907585372a8fef516daf6c67d`
- 영구 backup manifest:
  `$HOME/울트론/private-backups/ai-agent/20260826T193342+0900/manifest.json`

초기 `/private/tmp/ultron-schedule-candidate-922Se5/` 후보는 리허설 backup root에 묶인
비설치 증거로만 보존한다. 운영에는 승인된 영구 backup root로 다시 생성·검증한 후보를 설치했다.

## 운영 검증 결과

- Telegram PID `5265`, Private API PID `5792`
- Telegram 일정 알림 scheduler 재등록 및 프로세스 유지
- Private API 인증 status: `writes_enabled=true`, `schedule:write-contract`
- 격리된 검증 scope에서 존재하지 않는 event 삭제가 `applied/deleted=false`로 완료
- 같은 identity 재실행은 `replayed=true`; 실제 일정 행은 변경되지 않음
- 실제 Telegram 테스트 일정 add는 `pending→approved→applied`, chat version `0→1`
- 실제 Telegram 일정 `#8` delete는 `pending→approved→applied`, chat version `1→2`
- SQLite integrity/메모리 복구 검증 성공, 실제 일정 5건·사전 알림 4건 유지
- 전체 회귀 테스트 `1,484 passed`
- Paper 8080, Shareable API 8090은 기존 PID로 유지
- 삭제·DB 복원·커밋·푸시는 수행하지 않음

## 전환 전 필수 확인

1. 사용자의 실제 운영 전환 승인
2. 운영 DB 온라인 backup·메모리 복구 검증
3. fresh clone rollback rehearsal 재실행
4. 새 v2 candidate의 bundle/fingerprint와 600 권한 확인
5. 기존 v1 bundle을 별도 600 파일로 보존하고 해시 기록
6. Telegram/Private API PID와 일정·watchlist·version·notifier 기준선 기록
7. 자동 DB restore가 어떤 단계에도 포함되지 않았는지 확인

## 고정 cutover 순서

1. fresh `assistant.db` backup을 최종 확인한다.
2. 검증된 v2 candidate를 고정 bundle 경로에 원자적으로 설치한다.
   실행 중 프로세스는 아직 기존 메모리 상태라 동작이 바뀌지 않는다.
3. **Telegram을 먼저 재시작**한다.
   - 일정 사용자 mutation은 API executor로 전환된다.
   - Private API는 아직 schedule route가 없으므로 이 짧은 구간의 일정 변경은 fail-closed다.
   - 직접 SQLite fallback은 없어야 한다.
   - notifier가 다시 등록되고 기존 내부 writer 소유권을 유지하는지 확인한다.
4. Telegram 직접 일정 writer가 더 이상 호출되지 않는지 확인한다.
5. **Private API를 다음으로 재시작**한다.
   - watchlist writer는 계속 유지한다.
   - schedule write capability와 5개 일정 mutation route가 추가돼 총 10개가 된다.
6. 인증 status, schedule preflight, chat scope, no-store를 확인한다.
7. 사용자 승인 아래 Telegram 일정 add/delete 또는 complete E2E를 실행한다.
8. intent/audit, chat별 version, 일정 행, notifier 상태, watchlist version 2를 확인한다.

Private API를 먼저 재시작하면 기존 Telegram 직접 일정 writer와 API writer가 겹칠 수 있으므로
금지한다.

## 고정 rollback 순서

1. 보존한 기존 watchlist-only v1 bundle을 고정 경로에 원자적으로 복원한다.
2. **Private API를 먼저 재시작**한다.
   - schedule writer가 내려가고 schedule route는 다시 `404`가 된다.
   - watchlist writer는 계속 유지한다.
   - 이 구간의 Telegram 일정 API 요청은 fail-closed다.
3. schedule writer 비활성·watchlist 정상 상태를 확인한다.
4. **Telegram을 다음으로 재시작**한다.
   - 일정 사용자 mutation은 기존 직접 writer로 돌아간다.
   - notifier가 다시 등록되고 내부 mutation을 계속 소유한다.
5. 일정·알림·watchlist 정합성과 direct user writer 단일성을 확인한다.

Telegram을 먼저 legacy writer로 되돌리면 아직 살아 있는 API schedule writer와 겹칠 수 있으므로
금지한다.

## 비상 DB 복원

서비스 rollback만으로 정합성을 회복할 수 없을 때만 검토한다. 자동 복원은 금지하며, 복원 대상
snapshot·영향받는 일정 변경·서비스 중지 범위를 다시 제시하고 별도 승인을 받은 뒤 수동으로
실행한다.
