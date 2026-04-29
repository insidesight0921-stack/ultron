#!/usr/bin/env bash
# ===============================================================
# 1단계 모델 실측 스크립트
# - Gemma 4 26B MoE + 31B Dense 동시 로드 시 실제 메모리 측정
# - 응답 속도 (tok/s) 측정
# - 컨텍스트 윈도우별 KV 캐시 영향 확인
#
# 사용법: bash benchmark_models.sh
# 출력: ./benchmark_result.md
# ===============================================================
set -uo pipefail

OUT="./benchmark_result.md"
TS=$(date "+%Y-%m-%d %H:%M:%S")

write() { echo "$@" >> "$OUT"; }
section() { echo "" >> "$OUT"; echo "## $*" >> "$OUT"; echo "" >> "$OUT"; }

# 초기화
: > "$OUT"
write "# Gemma 4 모델 실측 결과"
write ""
write "- 측정 시각: $TS"
write "- 호스트: $(uname -srm)"
write "- Ollama 버전: $(ollama --version 2>&1 | head -1)"
write "- 가용 메모리: $(sysctl -n hw.memsize 2>/dev/null | awk '{printf "%.1f GB\n", $1/1024/1024/1024}')"

# ---------------------------------------------------------------
section "1. 모델 다운로드 확인"
write '```'
ollama list >> "$OUT" 2>&1
write '```'

# ---------------------------------------------------------------
section "2. 26B MoE 단독 로드 (warm-up)"
write "라우팅 응답 속도 측정 (10토큰 짧은 답변)"
write ""
write '```'
echo "--- gemma4:26b ---" >> "$OUT"
ollama run gemma4:26b --verbose "Reply in one short sentence: hello" >> "$OUT" 2>&1
write '```'
write ""
write '```'
echo "ollama ps after gemma4:26b load:" >> "$OUT"
ollama ps >> "$OUT" 2>&1
write '```'

# ---------------------------------------------------------------
section "3. 31B Dense 추가 로드 (동시 상주)"
write "두 모델 동시 로드 시 메모리 합산 — 36GB 이론치 대비 실측 비교"
write ""
write '```'
echo "--- 31b dense ---" >> "$OUT"
ollama run gemma4:31b --verbose "Reply in one short sentence: ready" >> "$OUT" 2>&1
write '```'
write ""
write '```'
echo "ollama ps with both loaded:" >> "$OUT"
ollama ps >> "$OUT" 2>&1
write '```'

# ---------------------------------------------------------------
section "4. 시스템 메모리 사용 (vm_stat)"
write '```'
vm_stat >> "$OUT" 2>&1
write '```'

# ---------------------------------------------------------------
section "5. GPU/CPU Power (powermetrics 5초 샘플)"
write "⚠️ sudo 권한 필요 — 비밀번호 입력 요청 가능"
write ""
write '```'
sudo powermetrics --samplers gpu_power,cpu_power -i 1000 -n 5 2>&1 | grep -E "(GPU|CPU|Power|MHz)" | head -40 >> "$OUT" || echo "(powermetrics 권한 필요 — 건너뜀)" >> "$OUT"
write '```'

# ---------------------------------------------------------------
section "6. 응답 속도 벤치마크 (긴 답변)"
write "31B Dense에 100토큰 응답 요청 — tok/s 측정"
write ""
write '```'
ollama run gemma4:31b --verbose "100단어 정도로 한국 주식시장의 모멘텀 팩터에 대해 설명해줘." >> "$OUT" 2>&1
write '```'

# ---------------------------------------------------------------
section "7. 컨텍스트 윈도우 영향 (선택)"
write "기본 컨텍스트 vs 32K 제한 비교가 필요하면 아래 수동 실행:"
write ""
write '```bash'
write "# 32K 제한 모델 변형 만들기"
write "cat > Modelfile <<EOF"
write "FROM gemma4:31b"
write "PARAMETER num_ctx 32768"
write "EOF"
write "ollama create gemma4:31b-32k -f Modelfile"
write "ollama ps  # 메모리 변화 비교"
write '```'

# ---------------------------------------------------------------
section "결과 해석 가이드"
write "- ollama ps SIZE 컬럼이 실제 메모리 점유"
write "- 두 모델 합 + KV 캐시 + 시스템 ≤ 40GB이면 안전"
write "- 40GB 초과 시 컨텍스트 32K로 제한 (위 7번 참조)"
write "- tok/s가 31B Dense에서 10 미만이면 Q4 양자화(gemma4:31b-q4_K_M) 검토"
write ""
write "끝. 이 파일을 현준이 Claude에 보내주면 다음 단계 결정."

echo ""
echo "==========================================="
echo "측정 완료. 결과: $OUT"
echo "==========================================="
cat "$OUT" | tail -20
