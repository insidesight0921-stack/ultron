# AI Agent — 로컬 비서 + 자동화 시스템

> 인터넷 없이 작동하는 나만의 전문 지식 기반 AI 비서
> Gemma 4 26B MoE (마스터) + 31B Dense (하위 에이전트) on Apple Silicon

## 구조

```
ai-agent/
├── .env                  ← API 키 (gitignore)
├── .env.example          ← 키 이름 템플릿
├── .gitignore
├── README.md
├── scripts/              ← 1회성/스케줄러 스크립트
│   ├── benchmark_models.py    ← 1단계: 모델 메모리/속도 측정
│   ├── index_wiki.py          ← 2단계: LanceDB 인덱싱
│   ├── dart_demand_parser.py  ← 0단계: DART 수요예측 파싱
│   └── dart_finance_parser.py ← 0단계: DART 재무 파싱
├── tests/                ← 검증용 샘플 + 테스트
└── (이후 단계에서 추가)
    ├── master_agent/     ← 4단계: Function Calling 라우팅
    ├── bots/             ← 4단계: 지식봇/투자봇/코딩봇 등
    ├── mock_trading/     ← 5단계: FastAPI 모의투자 사이트
    └── telegram_bot/     ← 3단계: 텔레그램 봇 서버
```

## 실행 환경

- macOS (Apple Silicon, 48GB)
- Python 3.11+
- Ollama (MLX 백엔드)
- 의존 vault: `~/울트론/obsidian-vault/` (별도 git repo)

## 단계별 진행 상황

- [x] 모델 다운로드 (gemma4:26b-moe, gemma4:31b)
- [x] 1단계: 모델 실측 + Python 환경 + Obsidian/GitHub
- [x] 2단계: 지식봇 + RAG (LanceDB)
- [x] 3단계: 텔레그램 봇 + Tailscale
- [x] 4단계: 마스터 라우팅 + 개인 관심종목 자연어 관리
- [ ] 5단계: 투자봇 고도화 + 모의투자 사이트 (진행 중)
- [ ] 6단계: 외부 API + 팩터 리서치봇

목표 아키텍처 진행 상태:

- [x] Phase 1: Private/Shareable 분류·보호·물리 분리 (`private-v1`)
- [ ] Phase 2: 데이터 API 서버화 (Private 조회 + watchlist 쓰기 운영 전환 완료, 일정 비설치 activation candidate까지 검증)
- [ ] Phase 3: Shareable 도구만 MCP 서버로 노출

목표 아키텍처의 Phase 1 데이터 경계는 [`docs/DATA_CLASSIFICATION.md`](docs/DATA_CLASSIFICATION.md)에서 관리합니다. 실제 전환 결과와 보존·롤백 절차는 [`docs/STORAGE_MIGRATION.md`](docs/STORAGE_MIGRATION.md), Phase 2 API 경계와 점진 전환 순서는 [`docs/API_SERVERIZATION.md`](docs/API_SERVERIZATION.md), 분리된 Private 인증 계약은 [`docs/PRIVATE_API_CONTRACT.md`](docs/PRIVATE_API_CONTRACT.md)를 따릅니다. 일정 사용자 mutation과 알림 스케줄러의 분리 소유권은 [`docs/SCHEDULE_WRITE_CUTOVER_DRY_RUN.md`](docs/SCHEDULE_WRITE_CUTOVER_DRY_RUN.md), 실제 전환·롤백 순서는 [`docs/SCHEDULE_WRITE_CUTOVER_RUNBOOK.md`](docs/SCHEDULE_WRITE_CUTOVER_RUNBOOK.md)에 고정합니다.

관심종목은 `storage_paths.py`가 선택한 Private DB에만 저장되며 Git/MCP에 노출하지 않습니다.

## 보안

- vault repo와 분리 — 키가 노트와 같은 repo에 있으면 노출 위험
- 모든 시크릿은 .env에서만 로드 (`python-dotenv`)
- 텔레그램 user_id 화이트리스트 필수
