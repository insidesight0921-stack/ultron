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
- [ ] 1단계: 모델 실측 + Python 환경 + Obsidian/GitHub
- [ ] 2단계: 지식봇 + RAG (LanceDB)
- [ ] 3단계: 텔레그램 봇 + Tailscale
- [ ] 4단계: 마스터 에이전트 + 전체 봇
- [ ] 5단계: 투자봇 고도화 + 모의투자 사이트
- [ ] 6단계: 외부 API + 팩터 리서치봇

## 보안

- vault repo와 분리 — 키가 노트와 같은 repo에 있으면 노출 위험
- 모든 시크릿은 .env에서만 로드 (`python-dotenv`)
- 텔레그램 user_id 화이트리스트 필수
