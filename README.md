# Incident Agent Improvement

AI 서비스 Mini-Project — 운영 장애 에이전트의 조사 대상·수치 조건·근거 보고서 개선.

**현재 구현, 직접 개선한 기능, 전후 비교, 검증 결과와 한계는 [프로젝트 보고서](docs/프로젝트_보고서.md) 한 곳에서 관리한다.** 캡처·JSON·CSV 원본은 `docs/before/`, `docs/after/`, `docs/evaluation/`에 보관한다.

## 실행

```bash
bash sh/setup_venv.sh
cp .env.example .env
# .env를 본인 환경에 맞게 설정
bash sh/setup_ollama.sh
ollama pull embeddinggemma
bash sh/run_agent_ollma.sh
```

준비된 환경에서는 설치·설정 단계를 생략한다. 다른 터미널에서 `bash sh/run_frontend.sh` 실행 후 http://localhost:8600/ 접속.

기존 실행 스크립트는 포트 프로세스를 종료하고 `session.db`를 초기화한다. 키·세션 DB·가상환경은 Git에서 제외하며 `.env.example`에는 실제 키를 넣지 않는다.

## 검증

```bash
PYTHONDONTWRITEBYTECODE=1 LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false .venv/bin/python3 -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false .venv/bin/python3 evaluation/run.py --refs 2c2973d f3836e2 HEAD
```

기존 버전 비교는 네트워크 없는 fake/mock 그래프 평가다. RAG 실제 임베딩 평가는 `.venv/bin/python3 evaluation/run_retrieval.py`로 실행한다. 서버 기본은 RAG이며 비교용 키워드 모드는 `RUNBOOK_SEARCH_MODE=keyword`다. 실제 모델 평가와 해석·재현 방법은 보고서를 참고한다. [제공 코드 원본 매뉴얼](doc/README.md)은 별도로 보존한다.


### 검색 범위·화면 보완 (2026-10-21)

증상별 RAG 검색·제외 조건·미지원 요청 처리를 보완하고 요약 보고서·접힌 상세 근거·진행 시간·오류 안내를 추가했습니다. 대표 실모델 결과와 한계는 [상세 설명서](doc/README.md)의 최신 절을 참고하세요.


### 최종 발표 자료

[G073 김주오 발표 자료와 최신 실행 캡처](docs/presentation/README.md)에 5분 발표용 8장 PPTX·PDF·대본 및 대표 4사례 캡처 5장을 정리했습니다.


## OpenAI 최종 검증 (2026-10-21)

기존 기준 질문 10개를 원문 그대로 OpenAI(gpt-4o-mini/gpt-4o)로 재실행하고 추가 2개와 구분했다. 1차 6·8번에서 발견한 공통 서술 분리 오류와 OpenAI JSON 모드 연결 누락을 수정했다. 최종 기존 10/10, 추가 2/2의 기대 동작을 확인하고 캡처·근거 텍스트·결과표를 저장했다. 자동 테스트 81개 통과. RAG 임베딩은 Ollama이며, 과거 Ollama 실행과 최신 OpenAI 결과를 구분한다. [최종 발표 자료·검증 목록](docs/presentation/openai_final/README.md) 참조. 전체 일반화 정확도·원인 확정·속도 개선율을 뜻하지 않는다.
