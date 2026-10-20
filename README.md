# Incident Agent Improvement

AI 서비스 Mini-Project — 운영 장애 에이전트의 조사 대상·수치 조건·근거 보고서 개선.

**현재 구현, 직접 개선한 기능, 전후 비교, 검증 결과와 한계는 [프로젝트 보고서](docs/프로젝트_보고서.md) 한 곳에서 관리한다.** 캡처·JSON·CSV 원본은 `docs/before/`, `docs/after/`, `docs/evaluation/`에 보관한다.

## 실행

```bash
bash sh/setup_venv.sh
cp .env.example .env
# .env를 본인 환경에 맞게 설정
bash sh/setup_ollama.sh
bash sh/run_agent_ollma.sh
```

준비된 환경에서는 설치·설정 단계를 생략한다. 다른 터미널에서 `bash sh/run_frontend.sh` 실행 후 http://localhost:8600/ 접속.

기존 실행 스크립트는 포트 프로세스를 종료하고 `session.db`를 초기화한다. 키·세션 DB·가상환경은 Git에서 제외하며 `.env.example`에는 실제 키를 넣지 않는다.

## 검증

```bash
PYTHONDONTWRITEBYTECODE=1 LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false .venv/bin/python3 -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false .venv/bin/python3 evaluation/run.py --refs 2c2973d f3836e2 HEAD
```

자동 평가는 네트워크 없는 fake/mock 그래프 평가다. 실제 모델 평가와 해석·재현 방법은 보고서를 참고한다. [제공 코드 원본 매뉴얼](doc/README.md)은 별도로 보존한다.
