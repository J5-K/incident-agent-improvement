# Incident Agent Improvement

AI 서비스 Mini-Project — 운영 장애 에이전트의 조사 대상 정확도와 수치·조치 조건의 근거 일치 개선.

## 직접 구현한 개선

1. **조사 대상 확인**: 미등록 이름은 조회 없이 되묻고, incident로 분류된 대상 미지정 요청도 확인한다.
2. **복수 시스템 누락 방지**: 명시된 모든 시스템을 계획에 보완하고 시스템별 지표·매뉴얼을 연결한다.
3. **질문 분류 보완**: 등록된 시스템과 일상적 증상 표현이 있는 chat 오분류를 incident로 보정한다.
4. **수치·조건 검증**: AI가 수치를 자유 생성하지 않고 출처·단계·조건 코드를 선택한다. Python이 CPU `>90` / 메모리 `>=90`을 계산한다. CPU 지속 여부 등 미수집 조건은 보류한다.
5. **근거 보고서**: 사실 / 추정 / 조건부 조치 / 출처를 구분한다. 3회 반려 후 강제 통과 대신 사실만 제공한다.
6. **재현 가능한 자동 평가**: 고정 질문·지표·매뉴얼 15개 시나리오로 Git 버전별 그래프를 비교한다.

## 실행

기존 환경이 준비됐다면 백엔드·프론트엔드만 실행한다. 처음 설치할 때는 아래 순서를 따른다.

```bash
bash sh/setup_venv.sh
cp .env.example .env
# .env는 본인 환경에 맞춰 설정
bash sh/setup_ollama.sh
bash sh/run_agent_ollma.sh
```

다른 터미널: `bash sh/run_frontend.sh`, 화면: http://localhost:8600/.
기존 실행 스크립트는 이전 포트 프로세스를 종료하고 session.db를 초기화한다. 상세 제공 코드 설명은 [원본 매뉴얼](doc/README.md)을 참고한다.

Vue(:8600) → FastAPI(:8100) → LangGraph v2 → MCP Tool/Resource/Prompt. 기본 골격·SSE·SQLite·백엔드 교체는 제공 코드의 기능이다. v1은 교육용 기존 흐름이며 이번 검증 강화를 적용하지 않았다.

## 테스트와 자동 평가

```bash
PYTHONDONTWRITEBYTECODE=1 LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false .venv/bin/python3 -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false .venv/bin/python3 evaluation/run.py --refs 2c2973d f3836e2 HEAD
```

단위·그래프·보고서·평가 집계 테스트 **46개 통과**. 자동 평가는 실제 MCP/LLM 호출 없는 fake/mock 회귀 평가다. 실행 시간은 실제 모델 응답 성능이 아니다. `--refs WORKTREE`는 미커밋 코드 확인용이며 결과에 표시된다.

[자동 평가 결과](docs/evaluation/README.md) · [최신 검증 요약](docs/최종검증_결과요약.md) · [수정 내역](docs/개선내역_1-3.md).

## 증거와 문서

- `docs/before/` 및 `docs/baseline/`: 개선 전 원본 캡처 10개. 기준 커밋 `2c2973d`.
- `docs/after/`: `f3836e2`의 1차 Vue 검증 10개와 당시 발견한 오류를 보존.
- `docs/after/v2_grounded/`: 수치·조건 및 근거 보고서 개선 후 실모델 재검증 기록.
- `docs/evaluation/`: 동일 fixture의 버전별 JSON·CSV·요약.
- [대상 확인](docs/target-guard.md) · [복수 대상](docs/multi-target.md) · [분류](docs/routing.md) · [수치 조건](docs/condition-validation.md) · [근거 보고서](docs/grounded-report.md).

## 한계·절충

모니터링은 교육용 샘플이며 무작위 수치와 고정 경고가 다를 수 있다. 검토한 자동 수치 정책은 CPU 증설과 메모리 재시작 두 개다. 원문 해시가 달라지거나 수치가 없으면 보류한다. 나머지 단계는 조건·운영자 확인이 필요하며 실제 조치는 실행하지 않는다.

원인 가설을 제한된 코드로 선택하므로 자유 서술의 폭은 줄어든다. 대신 수치·출처·조건을 코드가 보존한다. 이 검사는 원인 가설의 진실성을 증명하지 않는다. RAG, LLM-as-a-Judge, 부분 실패 응답, 비동기 LLM 전환은 구현 범위에 없다. 일부 질문은 fake 라우터가 놓치며 도구 실패 시 전체 요청이 실패하는 한계도 평가에 기록했다.

미검증 JSON 초안 토큰은 화면에 흘리지 않는다. 조사·검증 이벤트와 검증 후 최종 보고서를 SSE로 표시하며 일반 대화 스트리밍은 유지한다. `.env`·세션 DB·가상환경·캐시는 Git에서 제외한다. `.env.example`에는 실제 키를 넣지 않는다.
