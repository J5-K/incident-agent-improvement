# 고정 데이터 그래프 평가

실행: `.venv/bin/python3 evaluation/run.py --refs 2c2973d f3836e2 HEAD`

fixtures.json에 질문·정답 대상·시스템별 매뉴얼·고정 모니터링·매뉴얼 원문을 묶었다. current working tree를 확인하려면 `--refs WORKTREE`를 쓴다. 버전은 git archive를 임시 폴더에 풀어 별도 Python 프로세스로 실행한다. checkout이나 개발 중 파일은 수정하지 않는다.

실제 모델/API 키/운영 MCP를 쓰지 않는다. 버전별 fake 작성기와 AsyncMock MCP를 이용해 그래프 및 검증 계약을 비교한다. 이 결과를 LLM의 실제 의미 정확도나 처리량으로 해석하면 안 된다.

출력: docs/evaluation/{ref}.json, comparison.json, comparison.csv, README.md. 버전 전체 commit과 fixture SHA-256을 기록한다. 개별 예외도 결과에 저장하며 오류 초안을 일부러 주입하는 경우를 구분한다. 전체 그래프 실패도 원본 결과에 남긴다.
