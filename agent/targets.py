"""등록 시스템 확인. 명시된 이름만 사용하며 임의 기본값을 만들지 않는다."""
import json
import re
from pathlib import Path

SYSTEMS_PATH = Path(__file__).resolve().parent.parent / "data" / "monitoring.json"
# service 이름 또는 web-01 같은 호스트 이름. 별칭/대명사 해석은 지원하지 않는다.
SYSTEM_NAME = re.compile(r"(?<![a-z0-9_-])[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*[-_](?:service|[0-9]+)(?![a-z0-9_-])", re.I)


def load_known_systems() -> set[str]:
    # 설정 오류를 임의 시스템 목록으로 숨기지 않는다.
    return set(json.loads(SYSTEMS_PATH.read_text()))


def resolve_targets(query: str, known: set[str]) -> tuple[list[str], list[str]]:
    """등장 순서로 등록/미등록 이름을 분리한다. 대소문자는 정규화한다."""
    canonical = {name.lower(): name for name in known}
    # 등록 이름은 명명 패턴과 달라도 찾되 부분 문자열 매칭은 방지한다.
    alternatives = [re.escape(name) for name in sorted(known, key=len, reverse=True)]
    registered = (r"(?<![a-z0-9_-])(?:" + "|".join(alternatives) + r")(?![a-z0-9_-])") if alternatives else r"(?!)"
    pattern = re.compile(registered + "|" + SYSTEM_NAME.pattern, re.I)
    names = list(dict.fromkeys(m.group().lower() for m in pattern.finditer(query)))
    return ([canonical[n] for n in names if n in canonical],
            [n for n in names if n not in canonical])
