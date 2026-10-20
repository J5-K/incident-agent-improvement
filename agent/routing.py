"""Conservative fallback for named systems described with everyday symptoms."""
import re

_SYMPTOM = re.compile(
    r"프로세서.{0,15}바쁘|처리.{0,10}밀[려리림]|응답.{0,10}느[려리림]|"
    r"메모리.{0,20}(?:먹|늘|증가)|접속.{0,8}안\s*돼|로그인.{0,8}안\s*돼|"
    r"디스크.{0,10}(?:부족|가득)|타임아웃|시간\s*초과|"
    r"(?:상태|모니터링).{0,10}(?:확인|조회)|"
    r"\b(?:cpu|memory|disk)\b.{0,15}(?:급증|부족|높|가득)", re.IGNORECASE)
_CONCEPT = re.compile(r"무슨\s*뜻|무엇|뭐야|뜻이|개념|정의|예시|번역")


def should_promote_incident(query: str, targets: list[str]) -> bool:
    # A named system alone is insufficient. Concept questions keep model routing.
    return bool(targets and _SYMPTOM.search(query) and not _CONCEPT.search(query))
