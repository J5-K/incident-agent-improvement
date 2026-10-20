# 운영 인시던트 대응 에이전트 — 현재 구현 안내

제공된 `all_total_vue_student`를 바탕으로 조사 대상 정확도·근거 검증·부분 실패 대응·RAG 검색을 개선한 프로젝트다. 이 문서는 **2026-10-21 현재 코드**의 실행·구조·검증 상태를 설명한다. 상세 설계와 버전별 증거는 [프로젝트 보고서](../docs/프로젝트_보고서.md)를 참고한다.

## 주요 기능과 직접 개선한 내용

1. 미등록·미지정 시스템 확인 및 기본 서비스 임의 대체 방지.
2. 일상적인 장애 표현 분류 보완과 복수 시스템 조사 계획 누락 보완.
3. 로컬 임베딩 의미 검색으로 관련 Runbook 여러 개 수집.
4. 시스템·출처·매뉴얼 단계·수치 조건 검증과 최대 3회 재작성.
5. 일부 조회·검색 실패 시 성공 자료 보존 및 실패 대상·분석 한계 표시.
6. 확인된 사실·원인 추정·조건부 조치·출처를 구분한 진단 보고서.
7. Graph 노드 및 공통 로직 모듈화, 자동 테스트와 고정 데이터 전후 비교.

FastAPI·LangGraph·Vue 3·SSE·SQLite·MCP Tool/Resource/Prompt·의존성 주입·LangSmith·백엔드 교체는 제공 코드의 기반 기능이다. v2에 개선을 적용했으며 v1은 비교용 기존 흐름이다. 전문 Multi-Agent 서브그래프·HITL 승인·대화 요약은 구현하지 않았다.

## 실행

처음 사용하는 환경:

```bash
bash sh/setup_venv.sh
cp .env.example .env
# .env에 본인의 환경 설정 입력
bash sh/setup_ollama.sh
ollama pull embeddinggemma
bash sh/run_agent_ollma.sh
```

다른 터미널에서 `bash sh/run_frontend.sh` 실행 후 http://localhost:8600/ 접속한다. 백엔드는 :8100, Ollama는 :11434다. 설치된 환경은 준비 단계를 생략한다. 서버 코드 변경 후 백엔드를 재시작하고 브라우저도 새로고침한다.

기존 기동 스크립트는 포트 프로세스를 종료하고 session.db를 초기화한다. DB 보존이 필요하면 실행 전에 확인한다. SQLite 저장 자체는 대화를 유지하지만 새로고침 시 Vue가 새 thread ID를 만들므로 이전 대화가 자동 복원되는 화면은 아니다. `.env`·DB·가상환경·벡터 캐시는 Git에 올리지 않는다.

## 처리 흐름

```text
Vue → FastAPI → 대상 확인 → 질문 분류 → 계획 보완
→ MCP 모니터링 → 시스템별 의미 검색 → MCP Runbook 원문 수집
→ AI 제안 → 출처·조건 검증 → 보고서
                 └ 반려: 최대 3회 재작성
```

미등록 대상은 확인 질문으로 종료한다. 자료 수집 실패 또는 검색 무결과 시 미검증 원인·권고 생성은 생략하고 확보된 사실과 한계를 제공한다. 3회 검증 실패도 강제 통과시키지 않는다. 실제 운영 조치는 실행하지 않는다.

## 코드 구조

| 위치 | 역할 |
|---|---|
| `agent/graph.py`, `agent/state.py` | 그래프 연결·조건 분기, 공유 상태 |
| `agent/nodes/` | 대상·분류·계획·조사·분석·작성/검증 노드 |
| `agent/targets.py`, `planning.py`, `routing.py` | 대상 확인·계획 대조·일상 표현 보정 |
| `agent/runbook_search.py` | 임베딩·벡터 캐시·시스템별 의미 검색 |
| `agent/evidence.py`, `reporting.py` | 출처·조건 계약 검사와 보고서 생성 |
| `data/action_policies.json`, `data/runbooks/` | 검토된 조치 조건과 원본 매뉴얼 6개 |
| `server.py`, `web_chat.html` | API·SSE 및 채팅 화면 |
| `evaluation/`, `tests/` | 고정 데이터 평가·단위 및 그래프 검사 |

## RAG·모델 설정

기존 매뉴얼 6개는 변경하지 않았다. 짧은 문서 전체를 로컬 `embeddinggemma:latest`로 임베딩하고 코사인 유사도로 검색한다. 인덱스는 `data/.cache/`에 저장하며 문서 해시·모델 digest를 확인해 갱신한다. 검색 인덱스와 MCP 원문의 해시가 다르면 해당 자료를 제외한다.

| 설정 | 기본값 |
|---|---|
| `RUNBOOK_SEARCH_MODE` | `rag` (`keyword`는 명시적 비교용) |
| `RUNBOOK_EMBED_MODEL` | `embeddinggemma:latest` |
| `RUNBOOK_MIN_SCORE` | `0.43` |
| `RUNBOOK_SCORE_MARGIN` | `0.08` |
| `RUNBOOK_TOP_K` | `2` |

검색 입력에서 서비스 이름과 빈 연결 구문·일반 상태 문자열을 제외하고 대상별 증상·알림을 사용하도록 보완했다. 검색 무결과와 임베딩·검색 오류는 구분하며, 어느 경우도 기본 매뉴얼로 자동 대체하지 않는다. 모든 검색 문서가 제안에 포함되는지 검사한다.

Ollama의 Qwen3 호출은 `think=false`를 명시해 불필요한 장시간 thinking 출력을 제어한다. 이것만으로 전체 응답 시간이나 timeout 해결을 보장하지 않는다. 설정 근거는 [Ollama Chat API](https://docs.ollama.com/api/chat)를 참고한다. 기본 분류 모델은 qwen3:0.6b, 작성 모델은 qwen3:8b다. OpenAI 백엔드의 실제 API 사용은 과금되며 이번 실제 모델 검증 범위와 구분한다.

## 검증 및 결과

```bash
PYTHONDONTWRITEBYTECODE=1 LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false .venv/bin/python3 -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python3 evaluation/run_retrieval.py
PYTHONDONTWRITEBYTECODE=1 LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false .venv/bin/python3 evaluation/run.py --refs 2c2973d f3836e2 HEAD
```

- 현재 자동 테스트 **70개 통과**. 실제 의미 이해와 별개인 mock·합성 벡터·그래프 검사다.
- 2026-10-20 검색 평가: 별도 질문 9개 중 6개 문서 집합 일치, 무관련 질문 2/2 거절. 당시 설정은 최대 3개였으며 최신 최대 2개·검색 입력 보완의 최종 성능으로 소급하지 않는다.
- 당시 실제 MCP/임베딩 연결 검사에서 복수 시스템 payments 검색 무결과와 불필요한 추가 문서 선택을 발견했다. 해당 관측을 보존하고 현재 입력 정리·문서 수 제한을 추가했다.
- 당시 8B 작성은 4분 이상 완료되지 않아 취소했다. 그 실행은 성공으로 기록하지 않는다.
- 새 대표 실모델 검사 결과는 `docs/evaluation/rag_live_20261021.json`에 기록한다. 최신 전체 질문 10개의 Vue 검증·최종 캡처와 구분한다.

원본 증거: `docs/before/`·`baseline/`은 개선 전, `docs/after/` 최상위는 1차 개선, `after/v2_grounded/`는 수치 검증 변경 후 04·06 재검증이다. `docs/evaluation/`의 각 JSON 내부 버전·설정·실행 모드를 기준으로 해석한다. 예전 캡처를 현재 RAG 최종 성공 증거로 사용하지 않는다.

## 현재 한계와 남은 최종 확인

- 수치 자동 정책은 CPU 증설 `>90`과 메모리 재시작 `>=90` 두 가지다. CPU 지속 여부 등 미수집 조건은 추가 확인하며 실행 승인이 아니다.
- 검색 유사도는 정답 확률이 아니며 누락·불필요 문서 선택 가능성이 있다. 고정 경고와 무작위 현재 측정값을 분리한다.
- 일부 자료가 빠지면 성공한 시스템의 AI 권고도 함께 보류하는 보수적 정책이다. 계획 Prompt·LLM 오류·재접속 복원까지 모두 처리한 것은 아니다.
- 별칭·대명사·후속 대화 대상 복원은 지원하지 않는다. 근본 원인 확정·LLM-as-a-Judge도 미구현이다.
- 최신 코드의 전체 실모델/Vue 질문 10개와 복합 장애·검색 무결과·부분 실패 최종 확인, 캡처·발표 자료 정리는 별도 최종 검증 단계다.

2026-10-21 실제 MCP·임베딩·qwen3:0.6b/8b 대표 검사: 복수 시스템·복합 증상 2개 모두 incident 및 계약 검증 통과, 각각 14.76초, 14.97초. 이는 전체 최신 Vue 질문 10개 검증을 대체하지 않는다. 원본: docs/evaluation/rag_live_20261021.json.

검색 품질은 계약 검증 통과와 구분한다. 2026-10-21 복수 시스템 대표 실행에서는 payments의 CPU·지연 매뉴얼과 cache의 메모리 매뉴얼이 선택됐고, 단일 시스템 복합 증상에서는 CPU만 선택돼 기대한 메모리 매뉴얼이 누락됐다. 따라서 복합 증상의 검색 누락·불필요 문서 선택 문제는 아직 완전히 해결되지 않았다.
